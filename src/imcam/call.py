"""The WebRTC call to the camera: signaling messages over /im, the stock-shaped offer (stock_offer.py), and the call state machine. The peer connection is the seam: AiortcPeer in production, a scripted
fake in tests."""

import asyncio
import json
import sys
import threading
import time
import uuid

from . import cloud, debug, stock_offer, testvideo, wsproto


def _ice_servers(rsp):
    from aiortc import RTCIceServer

    servers = [RTCIceServer(urls=u) for u in rsp.get("stunAddr", [])]
    for u, user, pwd in zip(
        rsp.get("turnAddr", []), rsp.get("turnUser", []), rsp.get("turnPass", [])
    ):
        servers.append(
            RTCIceServer(urls=u.split("?")[0], username=user, credential=pwd)
        )
    return servers


def _call_msg(sid, serial, permit, msg_type, message=""):
    """Signaling message with the field set the server expects (toSid is always -1)."""
    msg = {
        "opcode": "message",
        "messageType": msg_type,
        "sid": sid,
        "serialCode": "",
        "toSerialCode": serial,
        "permitCode": permit,
        "message": message,
        "reqId": "a_sig_py_%s_%d" % (uuid.uuid4().hex[:6], time.time_ns() % 1000),
        "toSid": -1,
    }
    if msg_type == "callRequest":
        msg.update(callType=0, videoCodecType=0)
    return msg


def _local_candidates(sdp):
    """(sdpMid, sdpMLineIndex, candidate) for each a=candidate line in an SDP, in the form the server expects."""
    out, mid, idx = [], None, -1
    for line in sdp.splitlines():
        if line.startswith("m="):
            idx += 1
        elif line.startswith("a=mid:"):
            mid = line[len("a=mid:") :]
        elif line.startswith("a=candidate:"):
            out.append({"sdpMid": mid, "sdpMLineIndex": idx, "candidate": line[2:]})
    return out


def _remote_candidate(d):
    from aiortc.sdp import candidate_from_sdp

    m = json.loads(d.get("message") or "{}")
    line = m.get("candidate", "")
    if line.startswith("candidate:"):
        line = line[len("candidate:") :]
    if not line:
        return None
    c = candidate_from_sdp(line)
    c.sdpMid, c.sdpMLineIndex = m.get("sdpMid"), m.get("sdpMLineIndex")
    return c


class _Signal:
    """Blocking /im socket: a reader thread feeds parsed messages to an asyncio queue,
    a heartbeat thread keeps the session alive. Writes are serialized with a lock."""

    def __init__(self, sock, buf, loop, inbox):
        self.sock, self.buf, self.loop, self.inbox = sock, buf, loop, inbox
        self.wlock = threading.Lock()
        self.stop = threading.Event()

    def send(self, obj=None, op=0x1, payload=None):
        data = payload if payload is not None else json.dumps(obj).encode()
        with self.wlock:
            self.sock.sendall(wsproto.frame_encode(op, data))

    def _heartbeat(self):
        while not self.stop.wait(5):
            self.send({"opcode": "heartbeat", "reqId": "a_sig_1"})

    def _read(self):
        try:
            while True:
                op, payload, self.buf = wsproto.parse_frame(self.buf, self.sock)
                if op == 0x9:
                    self.send(op=0xA, payload=payload)
                elif op == 0x8:
                    break
                elif op == 0x1:
                    d = json.loads(payload.decode("utf-8", "replace"))
                    if d.get("opcode") != "heartbeat":
                        self.loop.call_soon_threadsafe(self.inbox.put_nowait, d)
        except Exception as e:
            print("signaling socket: %r" % e, file=sys.stderr)
        self.loop.call_soon_threadsafe(self.inbox.put_nowait, None)

    def start(self):
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()


class AiortcPeer:
    """Peer-connection adapter over aiortc: an offer with video first and a silent audio track; the video we
    send is the H.265 test pattern (the camera's own H.265 is picked up by the relay, not by aiortc)."""

    def __init__(self, rsp):
        from aiortc import RTCConfiguration, RTCPeerConnection
        from aiortc.mediastreams import AudioStreamTrack

        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=_ice_servers(rsp)))
        # Video first (mid 0), audio second. No video track: we write the H.265 RTP ourselves.
        self.video = self.pc.addTransceiver("video", direction="sendrecv")
        # Audio is sendrecv; a silent placeholder track stands in for a microphone.
        self.pc.addTrack(AudioStreamTrack())

    async def offer(self):
        """Local SDP with ICE candidates already gathered."""
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return self.pc.localDescription.sdp

    async def accept_answer(self, sdp):
        from aiortc import RTCSessionDescription

        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type="answer")
        )

    async def add_remote_candidate(self, message):
        cand = _remote_candidate(message)
        if cand is not None:
            await self.pc.addIceCandidate(cand)

    async def send_video(self, on_connected):
        """Wait for the connection, call on_connected(), then send test video until cancelled."""
        while self.pc.connectionState != "connected":
            await asyncio.sleep(0.1)
        # type 2 = HD; switchVideo is sent only once ICE is connected
        on_connected()
        print("connected, sending H.265 test video")
        try:
            await testvideo.send_test_video(self.video.sender)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("video sender stopped: %r" % e, file=sys.stderr)

    async def report(self):
        await debug.dtls_stats(self.pc)

    async def close(self):
        await self.pc.close()


async def run_call(
    sig, inbox, peer, sid, serial, permit, munge=None, answer_timeout=15
):
    """Offer, then handle the device's signaling messages until the call ends. Returns why it ended:
    no_answer | signaling_closed | rejected | refused | hangup. sig.send(obj) writes a signaling message,
    inbox yields parsed messages (None = signaling closed), peer is an AiortcPeer or a fake."""
    munge = munge or stock_offer.build
    loop = asyncio.get_running_loop()
    local = await peer.offer()
    sig.send(
        _call_msg(
            sid,
            serial,
            permit,
            "callRequest",
            json.dumps({"type": "offer", "sdp": munge(local)}),
        )
    )
    print("call request sent to %s, waiting for answer ..." % serial)
    # each local candidate is also sent to the peer as its own remoteCandidate message
    for cand in _local_candidates(local):
        sig.send(_call_msg(sid, serial, permit, "remoteCandidate", json.dumps(cand)))

    answered, early, video_task, reason = False, [], None, None
    try:
        while True:
            try:
                d = await asyncio.wait_for(
                    inbox.get(), None if answered else answer_timeout
                )
            except asyncio.TimeoutError:
                print(
                    "no answer from device within %ss" % answer_timeout, file=sys.stderr
                )
                reason = "no_answer"
                break
            if d is None:
                print("signaling closed", file=sys.stderr)
                reason = "signaling_closed"
                break
            if d.get("opcode") != "message":
                print("<<< %s" % json.dumps(d))
                continue
            mt = d.get("messageType")
            if mt == "callResponse":
                if d.get("code", 0) != 0:
                    print("call rejected: code=%s" % d["code"], file=sys.stderr)
                    reason = "rejected"
                    break
                ans = json.loads(d.get("message") or "{}")
                if not ans.get("sdp") or ans.get("type") == "exceed":
                    print("device refused call: %s" % ans, file=sys.stderr)
                    reason = "refused"
                    break
                debug.offer_and_answer(local, ans["sdp"])
                await peer.accept_answer(ans["sdp"])
                answered = True
                print("answer received, connecting ...")
                video_task = loop.create_task(
                    peer.send_video(
                        lambda: sig.send(
                            _call_msg(
                                sid,
                                serial,
                                permit,
                                "switchVideo",
                                json.dumps({"type": 2, "act": 0}),
                            )
                        )
                    )
                )
                for message in early:
                    await peer.add_remote_candidate(message)
                early = []
                # switchAudio (remote audio off) goes out right after the answer
                sig.send(
                    _call_msg(
                        sid,
                        serial,
                        permit,
                        "switchAudio",
                        json.dumps({"type": 0, "act": 0}),
                    )
                )
            elif mt == "remoteCandidate":
                if answered:
                    await peer.add_remote_candidate(d)
                else:
                    early.append(d)
            elif mt == "hangup":
                print("device hung up")
                reason = "hangup"
                break
            else:
                print("<<< %s" % json.dumps(d))
    finally:
        if video_task:
            video_task.cancel()
        await peer.report()
        try:
            sig.send(_call_msg(sid, serial, permit, "hangup"))
        except OSError:
            pass
        sig.stop.set()
        await peer.close()
    return reason


async def watch_async(sock, buf, sid, serial, permit, rsp):
    loop = asyncio.get_running_loop()
    inbox = asyncio.Queue()
    sig = _Signal(sock, buf, loop, inbox)
    sig.start()
    return await run_call(sig, inbox, AiortcPeer(rsp), sid, serial, permit)


def call_once(c, serial, permit):
    """One signin + call; returns when the call ends (hangup, no answer, signaling closed)."""
    sock, buf, rsp = cloud.signin(c)
    try:
        asyncio.run(watch_async(sock, buf, rsp["sid"], serial, permit, rsp))
    finally:
        sock.close()
