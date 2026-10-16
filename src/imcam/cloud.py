"""The vendor cloud as a module: WebSocket request/response client, saved session, login, account
requests and the /im signaling sign-in. Session data persists in ~/.imcam/session.json
(account, sessid, url, udid).

Wire protocol:
  endpoint   wss://fast.jfgou.com:443/fast   (/im is down; /fast verified working)
  request    {"headers":{"callee":"","caller":"","id":<msgid>,"req_id":<n>,
              "time":<unix_sec>},"body":{...}}
  response   {"headers":{...},"ret":0,"msg":"","body":{...}}   matched by req_id
  heartbeat  every 5 s: {"headers":{"id":"pub_heartbeat","req_id":"1","time":1}}
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid

from . import accesskey, config, paths, wsproto

DEFAULT_URL = "wss://fast.jfgou.com:443/fast"
BUNDLE_ID = "com.cylan.chatcam"
APP_VERSION = "2.35.8"
VID = os.environ.get("IMCAM_VID", "0001")
OEM_VID = os.environ.get("IMCAM_OEM_VID", "0001")


class RspException(Exception):
    def __init__(self, ret, msg):
        self.ret, self.msg = ret, msg
        super().__init__("ret=%s msg=%s" % (ret, msg))


class Client:
    """Request/response session with the cloud (/fast). dial(url) -> (socket, leftover bytes) is the seam:
    ws_handshake in production, a socketpair in tests."""
    def __init__(self, url=None, dial=wsproto.ws_handshake, heartbeat_s=5):
        self._dial, self.heartbeat_s = dial, heartbeat_s
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
                sock, extra = self._dial(self.url)
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
                op, payload, buf = wsproto.parse_frame(buf, sock)
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
            (sock or self.sock).sendall(wsproto.frame_encode(op, payload))

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
            time.sleep(self.heartbeat_s)
            if not (self.alive and sock is self.sock):
                return
            try:
                self.send_raw(0x1, json.dumps(
                    {"headers": {"id": "pub_heartbeat", "req_id": "1", "time": 1}}).encode(), sock)
            except Exception:
                if sock is self.sock:
                    self.alive = False
                return


def session_file():
    return paths.path("session.json")


def load_session():
    try:
        with open(session_file()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_session(d):
    os.makedirs(os.path.dirname(session_file()), exist_ok=True)
    # the file holds the sessid credential: create it owner-only
    fd = os.open(session_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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


def login(c, account, passwd):
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


def signin(c):
    """Fetch the RTC config, open /im and sign in. Returns (sock, buf, signin-response)."""
    s = load_session()
    sessid = s.get("sessid", "")
    body = c.send("pub_dp_get", {"oem_vid": OEM_VID, "sn": "ALL", "dpids": [9]})
    rtc = next((json.loads(i["value"]) for i in (body or []) if i.get("id") == 9), None)
    if not rtc:
        raise SystemExit("no RtcConfig (dpid=9) in pub_dp_get response")
    access_key = accesskey.derive_access_key(sessid, int(rtc["magic_md5"]), rtc["aes_key"])
    url = rtc["wss"]
    if url.endswith("fast"):
        url = url[:-4] + "im"
    print("rtc signaling: %s (access_id=%s)" % (url, rtc["access_id"]))
    sock, extra = c._dial(url)
    sock.settimeout(None)
    buf = extra
    signin = {"opcode": "signin", "loginType": 0, "key": rtc["access_id"],
              "secret": access_key, "authType": 0, "authCode": "",
              "serialCode": sessid, "mac": "", "mustPermitCode": 0,
              "permitCode": "", "reUseSid": -1}
    sock.sendall(wsproto.frame_encode(0x1, json.dumps(signin).encode()))
    rsp = None
    while rsp is None:
        op, payload, buf = wsproto.parse_frame(buf, sock)
        if op == 0x9:
            sock.sendall(wsproto.frame_encode(0xA, payload))
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


def code_check(c, account, code, token=""):
    return c.send("cli_code_check", {"account": account, "code": code, "token": token,
                                     "vid": VID, "vkey": config.vkey(), "oem_vid": OEM_VID})


def register(c, account, passwd, token="", country_code=""):
    return c.send("cli_register", {
        "account": account, "passwd": hashlib.md5(passwd.encode("utf-8")).hexdigest(),
        "token": token, "country_code": country_code,
        "default_set": 1, "language_type": 0, "notify_type": 0, "register_type": 0, "os": 2,
        "vid": VID, "vkey": config.vkey(), "oem_vid": OEM_VID})
