#!/usr/bin/env python3
"""ImCam (com.cylan.chatcam) Linux/Windows client — stdlib-only WebSocket client.

Wire protocol:
  endpoint   wss://fast.jfgou.com:443/fast   (/im is down; /fast verified working)
  request    {"headers":{"callee":"","caller":"","id":<msgid>,"req_id":<n>,
              "time":<unix_sec>},"body":{...}}
  response   {"headers":{...},"ret":0,"msg":"","body":{...}}   matched by req_id
  heartbeat  every 5 s: {"headers":{"id":"pub_heartbeat","req_id":"1","time":1}}

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
Session persists in ~/.imcam/session.json (account, sessid, url, udid).
"""
import asyncio
import base64
import hashlib
import json
import os
import random
import re
import socket
import ssl
import struct
import sys
import threading
import time
import uuid

from . import config, debug, extract_keys, extract_offer, paths, relay, tracker  # tracker is stdlib-only at import; numpy/cv2 load lazily inside it
from fractions import Fraction

DEFAULT_URL = "wss://fast.jfgou.com:443/fast"
SESSION_FILE = paths.path("session.json")
BUNDLE_ID = "com.cylan.chatcam"
APP_VERSION = "2.35.8"
VID = os.environ.get("IMCAM_VID", "0001")
OEM_VID = os.environ.get("IMCAM_OEM_VID", "0001")


class RspException(Exception):
    def __init__(self, ret, msg):
        self.ret, self.msg = ret, msg
        super().__init__("ret=%s msg=%s" % (ret, msg))


def parse_url(u):
    if not u.startswith(("ws://", "wss://")):
        raise ValueError("need ws:// or wss:// URL")
    secure = u.startswith("wss://")
    rest = u[6:] if secure else u[5:]
    host, _, path = rest.partition("/")
    if ":" in host:
        host, port = host.split(":", 1)
        port = int(port)
    else:
        port = 443 if secure else 80
    path = "/" + path
    # the server expects an http(s)-style Origin: the URL with its ws scheme replaced by http
    origin = "http" + u[u.index(":"):]
    return host, port, path, secure, origin


def ws_handshake(url, timeout=10):
    """RFC 6455 handshake; returns (ssl-sock, leftover bytes)."""
    host, port, path, secure, origin = parse_url(url)
    sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\nOrigin: %s\r\n\r\n"
           % (path, host, port, key, origin))
    sock.sendall(req.encode())
    buf = b""
    while not buf.endswith(b"\r\n\r\n"):
        c = sock.recv(4096)
        if not c:
            raise OSError("server closed during handshake")
        buf += c
    head, _, extra = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].split(b" ", 2)[1].decode()
    if status != "101":
        raise OSError("handshake failed: %s" % status)
    return sock, extra


def frame_encode(opcode, payload):
    """Client frames must be masked (RFC 6455)."""
    head = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    head += mask
    head += bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return head


def parse_frame(buf, sock):
    """Decode one complete frame from buf, refilling from sock if needed."""
    def more(n):
        nonlocal buf
        while len(buf) < n:
            c = sock.recv(4096)
            if not c:
                raise ConnectionError("closed mid-frame")
            buf += c
    if len(buf) < 2:
        more(2)
    opcode = buf[0] & 0x0F
    masked = buf[1] & 0x80
    ln = buf[1] & 0x7F
    extra = 0
    if ln == 126:
        more(4)
        ln = struct.unpack(">H", buf[2:4])[0]
        extra = 2
    elif ln == 127:
        more(10)
        ln = struct.unpack(">Q", buf[2:10])[0]
        extra = 8
    more(2 + extra + (4 if masked else 0) + ln)
    off = 2 + extra
    if masked:
        mask = buf[off:off + 4]
        off += 4
        payload = buf[off:off + ln]
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    else:
        payload = buf[off:off + ln]
    return opcode, payload, buf[off + ln:]


class ImCam:
    def __init__(self, url=None):
        self.url = url or (load_session() or {}).get("url") or DEFAULT_URL
        self.req_id = 0
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.pending = {}
        self.alive = False
        self.push_cb = None
        self.sock = None

    def close(self):
        self.alive = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def connect(self, url=None, retries=3, delay=2):
        if url:
            self.url = url
        self.close()
        for i in range(retries):
            try:
                sock, extra = ws_handshake(self.url)
                sock.settimeout(None)  # the reader blocks indefinitely; heartbeat sends detect a dead link
                self.sock = sock
                self.alive = True
                threading.Thread(target=self._reader, args=(sock, extra), daemon=True).start()
                threading.Thread(target=self._heartbeat, args=(sock,), daemon=True).start()
                print("connected to %s" % self.url)
                return
            except Exception as e:
                print("connect %d/%d failed: %s" % (i + 1, retries, e), file=sys.stderr)
                if i + 1 < retries:
                    time.sleep(delay)
        raise SystemExit("cannot connect to %s" % self.url)

    def _reader(self, sock, extra=b""):
        buf = extra
        try:
            while True:
                if not buf:
                    c = sock.recv(4096)
                    if not c:
                        raise ConnectionError("closed")
                    buf += c
                op, payload, buf = parse_frame(buf, sock)
                self._on_frame(op, payload, sock)
        except Exception:
            # a reader left over from before a reconnect must not mark the new connection dead
            if sock is self.sock:
                self.alive = False
            with self.cond:
                self.cond.notify_all()

    def _on_frame(self, op, payload, sock=None):
        if op == 0x8:
            raise ConnectionError("server closed connection")
        if op == 0x9:
            self.send_raw(0xA, b"", sock)
            return
        if op != 0x1:
            return
        data = json.loads(payload.decode("utf-8", "replace"))
        headers = data.get("headers", {})
        if headers.get("id") == "pub_heartbeat_rsp":
            return
        if "ret" in data:
            rid = str(headers.get("req_id", ""))
            with self.cond:
                if rid in self.pending:
                    self.pending[rid] = data
                    self.cond.notify()
        elif self.push_cb:
            self.push_cb(data)

    def send_raw(self, op, payload, sock=None):
        with self.lock:
            (sock or self.sock).sendall(frame_encode(op, payload))

    def send(self, msg_id, body=None, timeout=10):
        """Send request, wait for matching response, return its body.
        Raises RspException on ret != 0, TimeoutError on no reply."""
        with self.lock:
            self.req_id += 1
            rid = str(self.req_id)
            msg = {"headers": {"callee": "", "caller": "", "id": msg_id,
                              "req_id": rid, "time": int(time.time())},
                   "body": body or {}}
            self.pending[rid] = None
        self.send_raw(0x1, json.dumps(msg).encode())
        with self.cond:
            deadline = time.time() + timeout
            while self.pending[rid] is None:
                left = deadline - time.time()
                if left <= 0:
                    del self.pending[rid]
                    raise TimeoutError("no response to %s within %ss" % (msg_id, timeout))
                self.cond.wait(left)
            data = self.pending.pop(rid)
        if data.get("ret", 0) != 0:
            raise RspException(data.get("ret"), data.get("msg", ""))
        return data.get("body")

    def _heartbeat(self, sock):
        while self.alive and sock is self.sock:
            time.sleep(5)
            if not (self.alive and sock is self.sock):
                return
            try:
                self.send_raw(0x1, json.dumps(
                    {"headers": {"id": "pub_heartbeat", "req_id": "1", "time": 1}}).encode(), sock)
            except Exception:
                if sock is self.sock:
                    self.alive = False
                return


def load_session():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_session(d):
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    # the file holds the sessid credential: create it owner-only
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # POSIX only; also tightens a file created earlier with looser permissions
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def udid():
    p = paths.path("udid")
    try:
        return open(p).read().strip()
    except Exception:
        v = uuid.uuid4().hex
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(v)
        return v


# passwords matching this are "strong"; the login request carries the inverse as simple_passwd
PWD_RE = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z\d\W_]{6,64}$')

def login_body(account, passwd, sessid="", md5=True):
    # the password is sent as its lowercase MD5 hex digest, never raw
    # (md5=False is the session re-login: empty passwd plus the stored sessid)
    return {
        "account": account,
        "passwd": hashlib.md5(passwd.encode("utf-8")).hexdigest() if md5 else passwd,
        "sessid": sessid,
        "simple_passwd": (not bool(PWD_RE.fullmatch(passwd))) if md5 else False,
        "language_type": 0,
        "service_type": 0,
        "device_token": "",
        "sys_version": "12",
        "bundle_id": BUNDLE_ID,
        "model": "linux__imcam__64",
        "net": 1,
        "os": 2,
        "version": APP_VERSION,
        "vid": VID,
        "vkey": config.vkey(),
        "oem_vid": OEM_VID,
        "udid": udid(),
    }


def do_login(c, account, passwd):
    # a password login always starts with an empty sessid
    rsp = c.send("cli_login", login_body(account, passwd, ""))
    save_session({"account": account, "sessid": rsp.get("sessid", ""),
                 "url": c.url, "udid": udid()})
    if rsp.get("switch_region") and rsp.get("url"):
        new_url = rsp["url"][0]
        print("switched region to %s" % new_url)
        c.connect(new_url)
        save_session({"account": account, "sessid": rsp.get("sessid", ""),
                     "url": new_url, "udid": udid()})
    print("logged in as %s" % account)


def ensure_logged_in(c):
    """Re-authenticate from the saved session (empty passwd + stored sessid)."""
    s = load_session()
    if s.get("account") and s.get("sessid"):
        try:
            c.send("cli_login", login_body(s["account"], "", s["sessid"], md5=False))
            return
        except (RspException, TimeoutError) as e:
            print("session restore failed: %s" % e, file=sys.stderr)
    print("not logged in; run: imcam login <account> <passwd>", file=sys.stderr)
    raise SystemExit(1)


# --- RTC / /im signaling ---

def _gf_mul(a, b):
    p = 0
    for i in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = (a << 1) ^ (0x11b if a & 0x80 else 0)
    return p

def _gf_inv(x):
    if x == 0:
        return 0
    p, r, e = 1, x, 254
    while e:
        if e & 1:
            p = _gf_mul(p, r)
        e >>= 1
        r = _gf_mul(r, r)
    return p

def _rotl8(b, n):
    return ((b << n) | (b >> (8 - n))) & 0xff

# FIPS-197 5.1.1: affine transform uses bitwise rotations, not GF(2^8) multiplies
_SBOX = bytes((lambda b: b ^ _rotl8(b, 1) ^ _rotl8(b, 2) ^ _rotl8(b, 3) ^ _rotl8(b, 4) ^ 0x63)(_gf_inv(i))
              for i in range(256))
_INV_SBOX = bytes(_SBOX.index(i) for i in range(256))

def _aes_expand(key):
    nk, nr = len(key) // 4, len(key) // 4 + 6
    rcon = [1, 2, 4, 8, 16, 32, 64, 128, 27, 54]
    w = list(key)
    for i in range(nk, 4 * (nr + 1)):
        prev = w[(i - 1) * 4:(i - 1) * 4 + 4]
        cur = w[(i - nk) * 4:(i - nk) * 4 + 4]
        if i % nk == 0:
            prev = prev[1:] + prev[:1]
            prev = [_SBOX[b] for b in prev]
            prev[0] ^= rcon[i // nk - 1]
        w += [a ^ b for a, b in zip(cur, prev)]
    return [bytes(w[i * 16:(i + 1) * 16]) for i in range(len(w) // 16)]

def _inv_cipher_block(blk, rk):
    nr = len(rk) - 1
    s = list(blk)

    def add_rk(s, k):
        return [a ^ b for a, b in zip(s, k)]

    def inv_shift(s):
        out = [0] * 16
        for c in range(4):
            for rr in range(4):
                out[c * 4 + rr] = s[((c - rr) % 4) * 4 + rr]
        return out

    def inv_mix(s):
        mc = [0] * 16
        for c in range(4):
            col = s[c * 4:c * 4 + 4]
            mc[c * 4]     = _gf_mul(col[0], 14) ^ _gf_mul(col[1], 11) ^ _gf_mul(col[2], 13) ^ _gf_mul(col[3], 9)
            mc[c * 4 + 1] = _gf_mul(col[0], 9)  ^ _gf_mul(col[1], 14) ^ _gf_mul(col[2], 11) ^ _gf_mul(col[3], 13)
            mc[c * 4 + 2] = _gf_mul(col[0], 13) ^ _gf_mul(col[1], 9)  ^ _gf_mul(col[2], 14) ^ _gf_mul(col[3], 11)
            mc[c * 4 + 3] = _gf_mul(col[0], 11) ^ _gf_mul(col[1], 13) ^ _gf_mul(col[2], 9)  ^ _gf_mul(col[3], 14)
        return mc

    s = add_rk(s, rk[nr])
    for r in range(nr - 1, 0, -1):
        s = inv_shift(s)
        s = [_INV_SBOX[b] for b in s]
        s = add_rk(s, rk[r])
        s = inv_mix(s)
    s = inv_shift(s)
    s = [_INV_SBOX[b] for b in s]
    return bytes(add_rk(s, rk[0]))

def aes_ecb_decrypt(key, data):
    if len(key) not in (16, 24, 32) or len(data) % 16:
        raise ValueError("invalid AES key/data")
    rk = _aes_expand(key)
    out = [_inv_cipher_block(data[i:i + 16], rk) for i in range(0, len(data), 16)]
    dec = b"".join(out)
    pad = dec[-1]
    if 1 <= pad <= 16:
        dec = dec[:-pad]
    return dec

def derive_access_key(session_id, magic, cipher_b64):
    """Signaling access key: AES key = md5(prefix+sid).lower() hex chars selected by
    1-bits of magic's (padded) binary; AES/ECB/PKCS5 decrypt of cipher[16:];
    result = uppercase MD5 of the plaintext."""
    key_hex = hashlib.md5((config.aes_prefix() + session_id).encode("utf-8")).hexdigest().lower()
    sel = "".join(key_hex[i] for i, bit in enumerate(format(magic, "b").zfill(32)) if bit == "1")
    if len(sel) not in (16, 24, 32):
        raise ValueError("selected AES key length %d not in (16,24,32)" % len(sel))
    dec = aes_ecb_decrypt(sel.encode("utf-8"), base64.b64decode(cipher_b64)[16:])
    return hashlib.md5(dec).hexdigest().upper()

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
            self.sock.sendall(frame_encode(op, data))

    def _heartbeat(self):
        while not self.stop.wait(5):
            self.send({"opcode": "heartbeat", "reqId": "a_sig_1"})

    def _read(self):
        try:
            while True:
                op, payload, self.buf = parse_frame(self.buf, self.sock)
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


def _signin(c):
    """Fetch the RTC config, open /im and sign in. Returns (sock, buf, signin-response)."""
    s = load_session()
    sessid = s.get("sessid", "")
    body = c.send("pub_dp_get", {"oem_vid": OEM_VID, "sn": "ALL", "dpids": [9]})
    rtc = next((json.loads(i["value"]) for i in (body or []) if i.get("id") == 9), None)
    if not rtc:
        raise SystemExit("no RtcConfig (dpid=9) in pub_dp_get response")
    access_key = derive_access_key(sessid, int(rtc["magic_md5"]), rtc["aes_key"])
    url = rtc["wss"]
    if url.endswith("fast"):
        url = url[:-4] + "im"
    print("rtc signaling: %s (access_id=%s)" % (url, rtc["access_id"]))
    sock, extra = ws_handshake(url)
    sock.settimeout(None)
    buf = extra
    signin = {"opcode": "signin", "loginType": 0, "key": rtc["access_id"],
              "secret": access_key, "authType": 0, "authCode": "",
              "serialCode": sessid, "mac": "", "mustPermitCode": 0,
              "permitCode": "", "reUseSid": -1}
    sock.sendall(frame_encode(0x1, json.dumps(signin).encode()))
    rsp = None
    while rsp is None:
        op, payload, buf = parse_frame(buf, sock)
        if op == 0x9:
            sock.sendall(frame_encode(0xA, payload))
            continue
        if op == 0x8:
            raise SystemExit("rtc server closed during signin")
        if op != 0x1:
            continue
        d = json.loads(payload.decode("utf-8", "replace"))
        if d.get("opcode") == "signin":
            rsp = d
    if rsp.get("code", 0) != 0:
        raise SystemExit("signin failed: code=%s" % rsp.get("code"))
    print("signed in, sid=%s" % rsp.get("sid"))
    return sock, buf, rsp


def _call_once(c, serial, permit):
    """One signin + call; returns when the call ends (hangup, no answer, signaling closed)."""
    sock, buf, rsp = _signin(c)
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
                ensure_logged_in(c)
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
    c = ImCam(url=url)
    c.connect()
    try:
        if cmd == "login":
            do_login(c, args[1], args[2])
        elif cmd == "code-get":
            ensure_logged_in(c)
            print(json.dumps(c.send("cli_code_get", {"account": args[1]})))
        elif cmd == "code-check":
            ensure_logged_in(c)
            print(json.dumps(c.send("cli_code_check", {
                "account": args[1], "code": args[2],
                "token": args[3] if len(args) > 3 else "",
                "vid": VID, "vkey": config.vkey(), "oem_vid": OEM_VID})))
        elif cmd == "register":
            ensure_logged_in(c)
            print(json.dumps(c.send("cli_register", {
                "account": args[1], "passwd": hashlib.md5(args[2].encode("utf-8")).hexdigest(),
                "token": args[3] if len(args) > 3 else "",
                "country_code": args[4] if len(args) > 4 else "",
                "default_set": 1, "language_type": 0, "notify_type": 0,
                "register_type": 0, "os": 2,
                "vid": VID, "vkey": config.vkey(), "oem_vid": OEM_VID})))
        elif cmd == "devlist":
            ensure_logged_in(c)
            print(json.dumps(c.send("cli_dev_list", {})))
        elif cmd == "account":
            ensure_logged_in(c)
            print(json.dumps(c.send("cli_account_info_get", {})))
        elif cmd == "call":
            ensure_logged_in(c)
            body = json.loads(args[2]) if len(args) > 2 else {}
            print(json.dumps(c.send(args[1], body)))
        elif cmd == "watch":
            if len(args) < 3:
                raise SystemExit("usage: imcam watch <device-serial> <permit-code>")
            ensure_logged_in(c)
            do_watch(c, args[1], args[2], track, record)
        elif cmd == "monitor":
            if len(args) < 3:
                raise SystemExit("usage: imcam monitor <device-serial> <permit-code> [--record DIR] [--input FILE]")
            ensure_logged_in(c)
            do_monitor(c, args[1], args[2], record)
        elif cmd == "logout":
            ensure_logged_in(c)
            c.send("cli_logout", {})
            print("logged out")
        else:
            print("unknown command: %s" % cmd, file=sys.stderr)
            sys.exit(1)
    except (RspException, TimeoutError, OfferTemplateMissing, config.ConfigMissing) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    finally:
        c.close()


if __name__ == "__main__":
    main()
