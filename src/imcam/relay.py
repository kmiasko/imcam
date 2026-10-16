"""RTP relay: the camera's H.265 video arrives over WebRTC (aiortc has no H.265 codec), so each decrypted RTP
packet is copied to local UDP ports where ffplay and the tracker's ffmpeg read it through SDP files.

Each UDP port has one reader, and sending to a port nobody reads broke playback: only active readers get
packets. ffmpeg's rtp input also binds port+1 for RTCP, so ports must be at least 2 apart.
`imcam calibrate` runs in another process; it asks for a one-frame grab on SNAP_PORT by touching SNAP_FLAG,
and the relay (in `watch`/`monitor`) then also feeds that port while the flag is fresh.
"""

import contextlib
import os
import socket
import struct
import subprocess
import time

from . import debug, paths

H265_PT = 125  # payload type the device answers with
RTP_PORT = 47301  # ffplay window (watch)
TRACK_PORT = 47304  # tracker's ffmpeg: pipe frames + recording
SNAP_PORT = 47306  # calibrate's one-frame grab
LIVE_SDP = paths.path("live.sdp")
TRACK_SDP = paths.path("track.sdp")
SNAP_SDP = paths.path("snap.sdp")
SNAP_FLAG = paths.path("snap.want")
SNAP_FLAG_TTL_S = 120  # a flag older than this is stale (calibrate died)
SNAP_POLL_S = 1.0  # how often the relay re-checks the flag
PLI_EVERY_S = 5.0


def write_sdp(path, port):
    """SDP file describing the H.265 RTP forwarded to `port`; returns path."""
    sdp = (
        "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=ImCam\nc=IN IP4 127.0.0.1\nt=0 0\n"
        "m=video %d RTP/AVP %d\na=rtpmap:%d H265/90000\n" % (port, H265_PT, H265_PT)
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(sdp)
    return path


def start_ffplay():
    path = write_sdp(LIVE_SDP, RTP_PORT)
    return subprocess.Popen(
        [
            paths.require_tool("ffplay"),
            "-loglevel",
            "error",
            "-window_title",
            "ImCam",
            "-fflags",
            "nobuffer",
            "-protocol_whitelist",
            "file,udp,rtp",
            "-i",
            path,
        ]
    )


class SnapshotFlag:
    """Cross-process request for a snapshot feed: calibrate holds the flag, the relay polls it."""

    def __init__(self, path=None, mono=time.monotonic, wall=time.time):
        self.path, self.mono, self.wall = path or SNAP_FLAG, mono, wall
        self._checked, self._wanted = -SNAP_POLL_S - 1, False

    def wanted(self):
        now = self.mono()
        if now - self._checked > SNAP_POLL_S:
            self._checked = now
            try:
                self._wanted = (
                    self.wall() - os.path.getmtime(self.path) < SNAP_FLAG_TTL_S
                )
            except OSError:
                self._wanted = False
        return self._wanted

    @contextlib.contextmanager
    def request(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        open(self.path, "w").close()
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.unlink(self.path)


class Relay:
    """Copies H.265 RTP packets to the consumers' ports. on_rtp() returns the SSRC to send a keyframe
    request (PLI) for when one is due, else None; the caller sends it."""

    def __init__(self, ports, send=None, snapshot=None, mono=time.monotonic, log=print):
        self.ports = tuple(ports)
        self.snapshot = snapshot or SnapshotFlag()
        self.mono, self.log = mono, log
        self.seen, self._last_pli = 0, None
        if send is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send = lambda data, port: sock.sendto(data, ("127.0.0.1", port))
        self._send = send

    def targets(self):
        return self.ports + (SNAP_PORT,) if self.snapshot.wanted() else self.ports

    def is_h265(self, data):
        return len(data) > 1 and data[1] & 0x7F == H265_PT

    def on_rtp(self, data):
        debug.rtp_in(data)
        if not self.is_h265(data):
            return None
        if self.seen % 500 == 0:
            self.log("receiving H.265 video (%d packets so far)" % self.seen)
        self.seen += 1
        debug.nal_in(data)
        for port in self.targets():
            self._send(data, port)
        # aiortc can't decode H.265, so the receiver never asks the camera for a keyframe. Request one now
        # and every PLI_EVERY_S so ffplay can start decoding from an IDR.
        now = self.mono()
        if self._last_pli is None or now - self._last_pli > PLI_EVERY_S:
            self._last_pli = now
            debug.pli_sent()
            return struct.unpack("!I", data[8:12])[0]
        return None


def install(ports):
    """Advertise H.265 in aiortc's offer and route its incoming H.265 RTP through a Relay to `ports`."""
    from aiortc import RTCRtpCodecParameters
    from aiortc.codecs import CODECS
    from aiortc.rtcdtlstransport import RTCDtlsTransport
    from aiortc.rtp import RTCP_PSFB_PLI, RtcpPsfbPacket

    CODECS["video"].append(
        RTCRtpCodecParameters(
            mimeType="video/H265", clockRate=90000, payloadType=H265_PT
        )
    )
    relay = Relay(ports)
    orig = RTCDtlsTransport._handle_rtp_data

    async def handle(self, data, arrival_time_ms):
        ssrc = relay.on_rtp(data)
        if relay.is_h265(data):
            if ssrc is not None:
                await self._send_rtp(
                    bytes(
                        RtcpPsfbPacket(
                            fmt=RTCP_PSFB_PLI, ssrc=0, media_ssrc=ssrc, fci=b""
                        )
                    )
                )
            return
        await orig(self, data, arrival_time_ms)

    RTCDtlsTransport._handle_rtp_data = handle
    debug.install_transport_tracing()
    return relay
