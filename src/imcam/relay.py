"""RTP relay: the camera's H.265 video arrives over WebRTC (aiortc has no H.265 codec), so each decrypted RTP
packet is copied to local UDP ports where ffplay and watcher.py's bridge ffmpeg read it through SDP files.
The camera's audio is copied the same way to port+AUDIO_OFFSET of each video port and kept away from aiortc
(nothing reads its decoder there). The readers' SDP files are written before the call is answered, so they
declare AUDIO (the first audio codec of our offer); if the answer picks another codec, audio is not relayed.

Each UDP port has one reader, and sending to a port nobody reads broke playback: only active readers get
packets. ffmpeg's rtp input also binds port+1 for RTCP, so ports must be at least 2 apart (video and audio
ports of one reader included).
`imcam snapshot` runs in another process; it asks for a one-frame grab on SNAP_PORT by touching SNAP_FLAG,
and the relay (in `watch`/`monitor`) then also feeds that port while the flag is fresh.
"""

import contextlib
import os
import shlex
import socket
import struct
import time

from . import children, config, debug, paths

H265_PT = 125  # payload type the device answers with
AUDIO = (0, "PCMU/8000")  # (payload type, rtpmap codec) the readers' SDP declares
AUDIO_OFFSET = 2  # audio port = video port + AUDIO_OFFSET
RTP_PORT = 47300  # ffplay window (watch); audio on 47302
TRACK_PORT = (
    47304  # watcher's bridge ffmpeg: babywatch feed + recording; audio on 47306
)
SNAP_PORT = 47310  # snapshot's one-frame grab (video only)
LIVE_SDP = paths.path("live.sdp")
TRACK_SDP = paths.path("track.sdp")
SNAP_SDP = paths.path("snap.sdp")
SNAP_FLAG = paths.path("snap.want")
SNAP_FLAG_TTL_S = 120  # a flag older than this is stale (snapshot died)
SNAP_POLL_S = 1.0  # how often the relay re-checks the flag
PLI_EVERY_S = 5.0


def write_sdp(path, port, audio=None):
    """SDP file describing the H.265 RTP forwarded to `port`, plus audio (pt, codec) on port+AUDIO_OFFSET
    when given; returns path."""
    sdp = (
        "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=ImCam\nc=IN IP4 127.0.0.1\nt=0 0\n"
        "m=video %d RTP/AVP %d\na=rtpmap:%d H265/90000\n" % (port, H265_PT, H265_PT)
    )
    if audio:
        pt, codec = audio
        sdp += "m=audio %d RTP/AVP %d\na=rtpmap:%d %s\n" % (
            port + AUDIO_OFFSET,
            pt,
            pt,
            codec,
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(sdp)
    return path


def audio_codec(answer_sdp):
    """(payload type, rtpmap codec) of the answer's audio section, or None when it has no usable audio."""
    pt, in_audio = None, False
    for line in answer_sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            parts = line.split()
            in_audio = line.startswith("m=audio") and len(parts) > 3 and parts[1] != "0"
            if in_audio:
                pt = int(parts[3])
        elif in_audio and line.startswith("a=rtpmap:%d " % pt):
            return pt, line.split(" ", 1)[1].strip()
    return None


def player_command(path):
    """IMCAM_PLAYER argv with the SDP `path` appended, or the default ffplay one when it is not set."""
    argv = shlex.split(config.optional("IMCAM_PLAYER"), posix=os.name != "nt")
    if argv:
        return argv + [path]
    return [
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


def start_player():
    return children.popen(player_command(write_sdp(LIVE_SDP, RTP_PORT, AUDIO)))


class SnapshotFlag:
    """Cross-process request for a snapshot feed: snapshot holds the flag, the relay polls it."""

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
    """Copies H.265 RTP packets to the consumers' ports and audio RTP to port+AUDIO_OFFSET. on_rtp() returns
    the SSRC to send a keyframe request (PLI) for when one is due, else None; the caller sends it.
    on_answer(sdp) learns the negotiated audio payload type from each call's answer."""

    def __init__(self, ports, send=None, snapshot=None, mono=time.monotonic, log=print):
        self.ports = tuple(ports)
        self.snapshot = snapshot or SnapshotFlag()
        self.mono, self.log = mono, log
        self.seen, self._last_pli = 0, None
        self.last_rx = None  # mono() of the latest H.265 packet, for stream-lost alerts
        self.audio = None  # (pt, codec) negotiated for the current call
        self.audio_seen, self.last_audio_rx = 0, None
        if send is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send = lambda data, port: sock.sendto(data, ("127.0.0.1", port))
        self._send = send

    def targets(self):
        return self.ports + (SNAP_PORT,) if self.snapshot.wanted() else self.ports

    def is_h265(self, data):
        return len(data) > 1 and data[1] & 0x7F == H265_PT

    def is_audio(self, data):
        return (
            self.audio is not None and len(data) > 1 and data[1] & 0x7F == self.audio[0]
        )

    def consumes(self, data):
        """True for packets the relay handles; the rest go on to aiortc."""
        return self.is_h265(data) or self.is_audio(data)

    def on_answer(self, sdp):
        self.audio = audio_codec(sdp)
        if self.audio is None:
            self.log("no audio in the camera's answer")
        elif self.audio != AUDIO:
            self.log(
                "camera answered audio %d %s, readers expect %d %s: audio not relayed"
                % (self.audio + AUDIO)
            )
        else:
            self.log("camera audio: %d %s" % self.audio)

    def _on_audio(self, data):
        if self.audio_seen % 500 == 0:
            self.log("receiving audio (%d packets so far)" % self.audio_seen)
        self.audio_seen += 1
        self.last_audio_rx = self.mono()
        if self.audio == AUDIO:
            for port in self.ports:
                self._send(data, port + AUDIO_OFFSET)

    def on_rtp(self, data):
        debug.rtp_in(data)
        if self.is_audio(data):
            self._on_audio(data)
            return None
        if not self.is_h265(data):
            return None
        if self.seen % 500 == 0:
            self.log("receiving H.265 video (%d packets so far)" % self.seen)
        self.seen += 1
        debug.nal_in(data)
        self.last_rx = self.mono()
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
    """Advertise H.265 in aiortc's offer and route its incoming H.265 and audio RTP through a Relay to `ports`;
    the call's answer reaches the Relay through call.on_answer."""
    from aiortc import RTCRtpCodecParameters
    from aiortc.codecs import CODECS
    from aiortc.rtcdtlstransport import RTCDtlsTransport
    from aiortc.rtp import RTCP_PSFB_PLI, RtcpPsfbPacket

    CODECS["video"].append(
        RTCRtpCodecParameters(
            mimeType="video/H265", clockRate=90000, payloadType=H265_PT
        )
    )
    from . import call

    relay = Relay(ports)
    call.on_answer = relay.on_answer
    orig = RTCDtlsTransport._handle_rtp_data

    async def handle(self, data, arrival_time_ms):
        ssrc = relay.on_rtp(data)
        if relay.consumes(data):
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
