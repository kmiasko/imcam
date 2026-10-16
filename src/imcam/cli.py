#!/usr/bin/env python3
"""ImCam (com.cylan.chatcam) Linux/Windows client: command dispatch, the WebRTC call and the monitor loop.
(Cloud protocol: cloud.py. Video relay: relay.py. Awake tracking: tracker.py.)

Commands:
  imcam selftest | doctor
  imcam extract-keys <app.apk|.xapk>        print the app's vkey / AES prefix as NAME=value lines (needs imcam[keys])
  imcam extract-offer <app-log> [-o FILE]   install the official app's offer SDP (required by watch/monitor)
  imcam login <account> <passwd>
  imcam code-get <account>
  imcam code-check <account> <code> [token]
  imcam register <account> <passwd> <code> [token] [country]
  imcam devlist | account | logout
  imcam call <msgid> [json-body]
  imcam watch <serial> <permit> [--track] [--record DIR]   (ffplay window)
  imcam monitor <serial> <permit> [--record DIR] [--input FILE]
                            headless awake-tracking with reconnect; --input replays a file
  imcam calibrate [--input FILE]   click crib ROI + lying line -> ~/.imcam/roi.json
  (needs ffmpeg/ffplay on PATH; `imcam doctor` checks the installation)
"""
import asyncio
import json
import os
import random
import sys
import threading
import time
import uuid

from . import cloud, config, debug, extract_keys, extract_offer, paths, relay, tracker, wsproto  # tracker is stdlib-only at import; numpy/cv2 load lazily inside it
from fractions import Fraction

# --- RTC / /im signaling ---

def _ice_servers(rsp):
    from aiortc import RTCIceServer
    servers = [RTCIceServer(urls=u) for u in rsp.get("stunAddr", [])]
    for u, user, pwd in zip(rsp.get("turnAddr", []), rsp.get("turnUser", []), rsp.get("turnPass", [])):
        servers.append(RTCIceServer(urls=u.split("?")[0], username=user, credential=pwd))
    return servers


def _call_msg(sid, serial, permit, msg_type, message=""):
    """Signaling message with the field set the server expects (toSid is always -1)."""
    msg = {"opcode": "message", "messageType": msg_type, "sid": sid, "serialCode": "",
           "toSerialCode": serial, "permitCode": permit, "message": message,
           "reqId": "a_sig_py_%s_%d" % (uuid.uuid4().hex[:6], time.time_ns() % 1000),
           "toSid": -1}
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
            mid = line[len("a=mid:"):]
        elif line.startswith("a=candidate:"):
            out.append({"sdpMid": mid, "sdpMLineIndex": idx, "candidate": line[2:]})
    return out


def _remote_candidate(d):
    from aiortc.sdp import candidate_from_sdp
    m = json.loads(d.get("message") or "{}")
    line = m.get("candidate", "")
    if line.startswith("candidate:"):
        line = line[len("candidate:"):]
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


def _split_annexb(data):
    """NAL units (without start codes) of an Annex-B byte stream."""
    nals, i, n = [], 0, len(data)
    starts = []
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for k, st in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while end > st and data[end - 1] == 0:  # trailing_zero / 4-byte start code
            end -= 1
        nals.append(data[st:end])
    return nals


def h265_packetize(nals, mtu=1200):
    """RFC 7798 payloads for one access unit: single NAL unit packets, and fragmentation
    units (type 49) for NALs larger than mtu. Returns [(payload, is_last_of_AU)]."""
    out = []
    for ni, nal in enumerate(nals):
        last_nal = ni == len(nals) - 1
        if len(nal) <= mtu:
            out.append((bytes(nal), last_nal))
            continue
        hdr = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        ntype, body = (nal[0] >> 1) & 0x3F, nal[2:]
        step = mtu - 3
        for off in range(0, len(body), step):
            chunk = body[off:off + step]
            end = off + step >= len(body)
            fu = (0x80 if off == 0 else 0) | (0x40 if end else 0) | ntype
            out.append((hdr + bytes([fu]) + chunk, last_nal and end))
    return out


async def _send_h265_test_video(sender, w=640, h=360, fps=15):
    """The device only streams after the viewer sends video of its own. Encode a moving
    test pattern with libx265 and write its RTP through the sender's DTLS/SRTP transport."""
    import av
    from aiortc.rtp import RtpPacket

    dtls = sender.transport
    enc = av.CodecContext.create("libx265", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.framerate = fps
    enc.time_base = Fraction(1, 90000)
    enc.gop_size = fps
    enc.max_b_frames = 0
    enc.options = {"preset": "ultrafast", "tune": "zerolatency",
                   "x265-params": "repeat-headers=1:annexb=1:keyint=%d:bframes=0:log-level=none" % fps}
    seq, ts0, i = random.randrange(1 << 16), random.randrange(1 << 32), 0
    nxt = time.monotonic()
    while True:
        frame = av.VideoFrame(w, h, "yuv420p")
        row = bytes((x + i * 8) % 256 for x in range(w))
        frame.planes[0].update(b"".join(row for _ in range(h)))
        frame.planes[1].update(bytes([(128 + i * 4) % 256]) * (w * h // 4))
        frame.planes[2].update(bytes([(128 - i * 4) % 256]) * (w * h // 4))
        frame.pts = i * (90000 // fps)
        i += 1
        for pkt in enc.encode(frame):
            for payload, marker in h265_packetize(_split_annexb(bytes(pkt))):
                rtp = RtpPacket(payload_type=relay.H265_PT, sequence_number=seq, timestamp=(ts0 + pkt.pts) & 0xFFFFFFFF,
                                ssrc=sender._ssrc, payload=payload)
                rtp.marker = int(marker)
                seq = (seq + 1) & 0xFFFF
                await dtls._send_rtp(rtp.serialize())
        nxt += 1.0 / fps
        await asyncio.sleep(max(0, nxt - time.monotonic()))


def _offer_from_template(sdp, path):
    """The offer template with our ICE credentials and fingerprint substituted in."""
    ours = {}
    for line in sdp.split("\r\n"):
        for key in ("a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:"):
            if line.startswith(key):
                ours.setdefault(key, line)
    out = []
    for line in open(path).read().splitlines():
        out.append(next((v for k, v in ours.items() if line.startswith(k)), line))
    return "\r\n".join(out) + "\r\n"


class OfferTemplateMissing(Exception):
    """The official app's offer SDP (needed for the camera to stream) is not installed."""


def offer_template_path():
    """Path of the app's offer SDP (IMCAM_APP_OFFER, else ~/.imcam/app_offer.sdp); raises if absent."""
    tpl = os.environ.get("IMCAM_APP_OFFER") or paths.path(extract_offer.OFFER_NAME)
    if not os.path.isfile(tpl):
        raise OfferTemplateMissing(
            "The official app's WebRTC offer was not found at %s.\n"
            "The camera only streams when our offer looks like the app's, so this file is required, and it is not "
            "shipped with imcam.\n"
            "Capture the app's signaling log while it opens a live view (e.g. `adb logcat -d > app.log`), then run:\n"
            "    imcam extract-offer app.log\n"
            "See the README section \"The app offer\"." % tpl)
    return tpl


def _munge_offer(sdp):
    # the camera only streams when the offer has the official app's shape, so the template is used verbatim and only
    # our ICE credentials and fingerprint go into it
    return _offer_from_template(sdp, offer_template_path())


async def _video_when_connected(pc, vt, on_connected):
    while pc.connectionState != "connected":
        await asyncio.sleep(0.1)
    # type 2 = HD; switchVideo is sent only once ICE is connected
    on_connected()
    print("connected, sending H.265 test video")
    try:
        await _send_h265_test_video(vt.sender)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("video sender stopped: %r" % e, file=sys.stderr)


async def _watch_async(sock, buf, sid, serial, permit, rsp):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack

    loop = asyncio.get_running_loop()
    inbox = asyncio.Queue()
    sig = _Signal(sock, buf, loop, inbox)
    sig.start()

    pc = RTCPeerConnection(RTCConfiguration(iceServers=_ice_servers(rsp)))
    # Video first (mid 0), audio second. No video track: we write the H.265 RTP ourselves.
    vt = pc.addTransceiver("video", direction="sendrecv")
    # Audio is sendrecv; a silent placeholder track stands in for a microphone.
    pc.addTrack(AudioStreamTrack())

    # ICE candidates are gathered into the SDP here, so the offer is complete
    await pc.setLocalDescription(await pc.createOffer())
    sig.send(_call_msg(sid, serial, permit, "callRequest",
                       json.dumps({"type": "offer", "sdp": _munge_offer(pc.localDescription.sdp)})))
    print("call request sent to %s, waiting for answer ..." % serial)
    # each local candidate is also sent to the peer as its own remoteCandidate message
    for cand in _local_candidates(pc.localDescription.sdp):
        sig.send(_call_msg(sid, serial, permit, "remoteCandidate", json.dumps(cand)))

    answered, early, video_task = False, [], None
    try:
        while True:
            try:
                d = await asyncio.wait_for(inbox.get(), None if answered else 15)
            except asyncio.TimeoutError:
                print("no answer from device within 15s", file=sys.stderr)
                break
            if d is None:
                print("signaling closed", file=sys.stderr)
                break
            if d.get("opcode") != "message":
                print("<<< %s" % json.dumps(d))
                continue
            mt = d.get("messageType")
            if mt == "callResponse":
                if d.get("code", 0) != 0:
                    print("call rejected: code=%s" % d["code"], file=sys.stderr)
                    break
                ans = json.loads(d.get("message") or "{}")
                if not ans.get("sdp") or ans.get("type") == "exceed":
                    print("device refused call: %s" % ans, file=sys.stderr)
                    break
                debug.offer_and_answer(pc.localDescription.sdp, ans["sdp"])
                await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type="answer"))
                answered = True
                print("answer received, connecting ...")
                video_task = loop.create_task(_video_when_connected(
                    pc, vt, lambda: sig.send(_call_msg(sid, serial, permit, "switchVideo", json.dumps({"type": 2, "act": 0})))))
                for cand in early:
                    await pc.addIceCandidate(cand)
                early = []
                # switchAudio (remote audio off) goes out right after the answer
                sig.send(_call_msg(sid, serial, permit, "switchAudio", json.dumps({"type": 0, "act": 0})))
            elif mt == "remoteCandidate":
                cand = _remote_candidate(d)
                if cand is None:
                    continue
                if answered:
                    await pc.addIceCandidate(cand)
                else:
                    early.append(cand)
            elif mt == "hangup":
                print("device hung up")
                break
            else:
                print("<<< %s" % json.dumps(d))
    finally:
        if video_task:
            video_task.cancel()
        await debug.dtls_stats(pc)
        try:
            sig.send(_call_msg(sid, serial, permit, "hangup"))
        except OSError:
            pass
        sig.stop.set()
        await pc.close()


def _call_once(c, serial, permit):
    """One signin + call; returns when the call ends (hangup, no answer, signaling closed)."""
    sock, buf, rsp = cloud.signin(c)
    try:
        asyncio.run(_watch_async(sock, buf, rsp["sid"], serial, permit, rsp))
    finally:
        sock.close()


def do_watch(c, serial, permit, track=False, record=None):
    """Sign in to the /im signaling server, send a WebRTC callRequest to the device
    and show the incoming video in ffplay until Ctrl-C. Needs aiortc (~/.imcam/venv).
    --track / --record also feed the stream to tracker.py's ffmpeg."""
    offer_template_path()  # fail before opening any window
    relay.install((relay.RTP_PORT,) + ((relay.TRACK_PORT,) if record or track else ()))
    player = relay.start_ffplay()
    live = _start_tracker(record, track)
    try:
        _call_once(c, serial, permit)
    except KeyboardInterrupt:
        pass
    finally:
        player.terminate()
        if live:
            live.stop()


def _start_tracker(record, track):
    if not (record or track):
        return None
    paths.require_tool("ffmpeg")
    return tracker.LiveTracker(record, track)


def do_monitor(c, serial, permit, record=None):
    """Headless: no ffplay, tracking on, reconnects forever with exponential backoff (cap 30 s)."""
    offer_template_path()  # fail before connecting
    relay.install((relay.TRACK_PORT,))
    live = _start_tracker(record, True)
    backoff = 1
    try:
        while True:
            started = time.monotonic()
            try:
                if not c.alive:
                    c.connect()
                cloud.ensure_logged_in(c)
                _call_once(c, serial, permit)
            except (OfferTemplateMissing, config.ConfigMissing):
                raise  # retrying cannot fix this
            except (Exception, SystemExit) as e:
                print("monitor: %s" % (e or type(e).__name__), file=sys.stderr)
            # a call that lived a while means the link was good: start backing off from scratch
            backoff = 1 if time.monotonic() - started > 60 else min(backoff * 2, 30)
            print("monitor: reconnecting in %ds" % backoff, file=sys.stderr)
            time.sleep(backoff)
    except KeyboardInterrupt:
        pass
    finally:
        live.stop()


def selftest():
    """Run the repo's pytest suite when pytest and tests/ are available (a source checkout)."""
    tests = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "tests")
    try:
        import pytest
    except ImportError:
        pytest = None
    if pytest is None or not os.path.isdir(tests):
        print("selftest skipped: needs pytest and the source checkout's tests/")
        return
    code = pytest.main(["-q", os.path.normpath(tests)])
    if code:
        raise SystemExit(code)
    print("selftest ok")


def doctor():
    """Report what is installed; returns the process exit code."""
    import importlib
    from . import notify
    ok = True
    for tool in ("ffmpeg", "ffplay"):
        found = __import__("shutil").which(tool)
        print("%-8s %s" % (tool, found or "MISSING (needed for %s)" % ("watch/monitor" if tool == "ffmpeg" else "watch")))
        ok &= bool(found) or tool == "ffplay"
    for mod in ("aiortc", "av", "numpy", "cv2"):
        try:
            importlib.import_module(mod)
            print("%-8s ok" % mod)
        except ImportError as e:
            print("%-8s MISSING (%s)" % (mod, e))
            ok = False
    for name in config.SETTINGS:
        try:
            config.get(name)
            print("%-8s %s set" % ("config", name))
        except config.ConfigMissing as e:
            print("%-8s %s MISSING\n%s" % ("config", name, e))
            ok = False
    try:
        print("offer    %s" % offer_template_path())
    except OfferTemplateMissing as e:
        print("offer    MISSING (run `imcam extract-offer`)\n%s" % e)
        ok = False
    print("notify   %s" % (notify.backend() or "none (messages are only logged)"))
    print("data dir %s" % paths.home())
    return 0 if ok else 1


def _opt(args, name, flag=False):
    """Remove `name` (and its value unless flag) from args in place and return it."""
    if name not in args:
        return False if flag else None
    i = args.index(name)
    if flag:
        del args[i]
        return True
    if i + 1 >= len(args):
        raise SystemExit("%s needs a value" % name)
    val = args[i + 1]
    del args[i:i + 2]
    return val


def main():
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd == "selftest":
        selftest()
        return
    record = _opt(args, "--record")
    infile = _opt(args, "--input")
    track = _opt(args, "--track", flag=True)
    if cmd == "doctor":
        sys.exit(doctor())
    if cmd == "extract-keys":
        extract_keys.main(args[1:])
        return
    if cmd == "extract-offer":
        extract_offer.main(args[1:])
        return
    if cmd in ("calibrate", "monitor") and infile:  # offline: no network
        tracker.calibrate(infile) if cmd == "calibrate" else tracker.run_file(infile)
        return
    if cmd == "calibrate":
        tracker.calibrate()
        return
    if cmd in ("watch", "monitor"):
        try:
            offer_template_path()  # before any network traffic
        except OfferTemplateMissing as e:
            sys.exit("error: %s" % e)
    try:
        config.require("IMCAM_VKEY", *(("IMCAM_AES_PREFIX",) if cmd in ("watch", "monitor") else ()))
    except config.ConfigMissing as e:
        sys.exit("error: %s" % e)
    url = args[1] if len(args) > 1 and args[1].startswith(("ws://", "wss://")) else None
    c = cloud.Client(url=url)
    c.connect()
    try:
        if cmd == "login":
            cloud.login(c, args[1], args[2])
        elif cmd == "code-get":
            cloud.ensure_logged_in(c)
            print(json.dumps(c.send("cli_code_get", {"account": args[1]})))
        elif cmd == "code-check":
            cloud.ensure_logged_in(c)
            print(json.dumps(cloud.code_check(c, args[1], args[2], args[3] if len(args) > 3 else "")))
        elif cmd == "register":
            cloud.ensure_logged_in(c)
            print(json.dumps(cloud.register(c, args[1], args[2], args[3] if len(args) > 3 else "",
                                            args[4] if len(args) > 4 else "")))
        elif cmd == "devlist":
            cloud.ensure_logged_in(c)
            print(json.dumps(c.send("cli_dev_list", {})))
        elif cmd == "account":
            cloud.ensure_logged_in(c)
            print(json.dumps(c.send("cli_account_info_get", {})))
        elif cmd == "call":
            cloud.ensure_logged_in(c)
            body = json.loads(args[2]) if len(args) > 2 else {}
            print(json.dumps(c.send(args[1], body)))
        elif cmd == "watch":
            if len(args) < 3:
                raise SystemExit("usage: imcam watch <device-serial> <permit-code>")
            cloud.ensure_logged_in(c)
            do_watch(c, args[1], args[2], track, record)
        elif cmd == "monitor":
            if len(args) < 3:
                raise SystemExit("usage: imcam monitor <device-serial> <permit-code> [--record DIR] [--input FILE]")
            cloud.ensure_logged_in(c)
            do_monitor(c, args[1], args[2], record)
        elif cmd == "logout":
            cloud.ensure_logged_in(c)
            c.send("cli_logout", {})
            print("logged out")
        else:
            print("unknown command: %s" % cmd, file=sys.stderr)
            sys.exit(1)
    except (cloud.RspException, TimeoutError, OfferTemplateMissing, config.ConfigMissing) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    finally:
        c.close()


if __name__ == "__main__":
    main()
