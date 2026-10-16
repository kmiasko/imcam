import hashlib
import json
import os
import socket
import threading
import time

import pytest

from _aeshelper import MAGIC, PREFIX, make_cipher
from imcam import cloud, wsproto
from test_wsproto import server_frame


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Server:
    """The far end of an in-memory socket: records what the client sends, answers via handler(msg)."""

    def __init__(self, handler=None):
        self.client_sock, self.peer = socket.socketpair()
        self.handler, self.received, self.ops = handler, [], []
        threading.Thread(target=self._serve, daemon=True).start()

    def push(self, obj):
        self.peer.sendall(server_frame(0x1, json.dumps(obj).encode()))

    def _serve(self):
        buf = b""
        try:
            while True:
                if not buf:
                    chunk = self.peer.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                op, payload, buf = wsproto.parse_frame(buf, self.peer)
                self.ops.append(op)
                if op == 0x1:
                    msg = json.loads(payload)
                    self.received.append(msg)
                    reply = self.handler(msg) if self.handler else None
                    if reply is not None:
                        self.push(reply)
        except (OSError, ValueError):
            return


def ok(msg, body=None, **extra):
    return dict(
        {
            "headers": {"req_id": msg["headers"]["req_id"]},
            "ret": 0,
            "msg": "",
            "body": body or {},
        },
        **extra,
    )


class Net:
    """dial() hands out a fresh in-memory Server per connection and remembers the URLs asked for."""

    def __init__(self, handler=None):
        self.handler, self.urls, self.servers = handler, [], []

    def dial(self, url):
        self.urls.append(url)
        server = Server(self.handler)
        self.servers.append(server)
        return server.client_sock, b""


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("IMCAM_VKEY", "vkey")
    monkeypatch.setenv("IMCAM_AES_PREFIX", PREFIX)
    monkeypatch.chdir(tmp_path)


def client_for(handler=None, **kw):
    net = Net(handler)
    c = cloud.Client(
        url="wss://cloud.test/fast",
        dial=net.dial,
        heartbeat_s=kw.pop("heartbeat_s", 60),
        **kw,
    )
    c.connect(retries=1)
    return c, net


def test_send_returns_the_response_body():
    c, net = client_for(lambda m: ok(m, {"echo": m["body"]}))
    assert c.send("cli_dev_list", {"a": 1}) == {"echo": {"a": 1}}
    sent = net.servers[0].received[0]
    assert sent["headers"]["id"] == "cli_dev_list" and sent["headers"]["req_id"] == "1"
    c.close()


def test_responses_are_matched_by_req_id():
    c, _ = client_for(lambda m: ok(m, {"for": m["headers"]["id"]}))
    assert c.send("first") == {"for": "first"} and c.send("second") == {"for": "second"}
    c.close()


def test_nonzero_ret_raises_rsp_exception():
    c, _ = client_for(
        lambda m: {
            "headers": {"req_id": m["headers"]["req_id"]},
            "ret": 7,
            "msg": "denied",
        }
    )
    with pytest.raises(cloud.RspException, match="ret=7 msg=denied"):
        c.send("cli_login")
    c.close()


def test_unanswered_request_times_out():
    c, _ = client_for(lambda m: None)
    with pytest.raises(TimeoutError, match="no response to slow"):
        c.send("slow", timeout=0.2)
    assert c.pending == {}
    c.close()


def test_unsolicited_messages_go_to_the_push_callback():
    c, net = client_for()
    got = []
    c.push_cb = got.append
    net.servers[0].push({"headers": {"id": "dev_online"}, "body": {"sn": "x"}})
    assert wait_for(lambda: got) and got[0]["body"] == {"sn": "x"}
    c.close()


def test_ping_is_answered_with_pong():
    c, net = client_for()
    net.servers[0].peer.sendall(server_frame(0x9, b""))
    assert wait_for(lambda: 0xA in net.servers[0].ops)
    c.close()


def test_heartbeats_are_sent_periodically():
    c, net = client_for(heartbeat_s=0.05)
    assert wait_for(
        lambda: any(
            m["headers"]["id"] == "pub_heartbeat" for m in net.servers[0].received
        )
    )
    c.close()


def test_losing_the_link_marks_the_client_dead():
    c, net = client_for()
    assert c.alive
    net.servers[0].peer.shutdown(
        socket.SHUT_RDWR
    )  # close() alone would not wake the thread blocked in recv
    assert wait_for(lambda: not c.alive)


def test_connect_gives_up_with_system_exit():
    def dial(url):
        raise OSError("unreachable")

    c = cloud.Client(url="wss://cloud.test/fast", dial=dial)
    with pytest.raises(SystemExit, match="cannot connect"):
        c.connect(retries=1)


# --- login / saved session ---


def test_login_body_hashes_the_password_and_flags_simple_ones():
    strong = cloud.login_body("a@b.c", "Abcdef1!")
    assert strong["passwd"] == hashlib.md5(b"Abcdef1!").hexdigest()
    assert strong["simple_passwd"] is False and strong["vkey"] == "vkey"
    assert cloud.login_body("a@b.c", "abc")["simple_passwd"] is True
    assert "Abcdef1!" not in json.dumps(strong)


def test_session_relogin_body_sends_stored_sessid_and_empty_password():
    body = cloud.login_body("a@b.c", "", "SESS", md5=False)
    assert (
        body["passwd"] == ""
        and body["sessid"] == "SESS"
        and body["simple_passwd"] is False
    )


def test_udid_is_created_once_and_reused():
    first = cloud.udid()
    assert len(first) == 32 and cloud.udid() == first


def test_login_saves_the_session_owner_only():
    c, _ = client_for(lambda m: ok(m, {"sessid": "S1"}))
    cloud.login(c, "a@b.c", "Abcdef1!")
    saved = cloud.load_session()
    assert saved == {
        "account": "a@b.c",
        "sessid": "S1",
        "url": "wss://cloud.test/fast",
        "udid": cloud.udid(),
    }
    if os.name == "posix":
        assert os.stat(cloud.session_file()).st_mode & 0o777 == 0o600
    c.close()


def test_login_follows_a_region_switch():
    def handler(msg):
        return ok(
            msg, {"sessid": "S1", "switch_region": True, "url": ["wss://eu.test/fast"]}
        )

    c, net = client_for(handler)
    cloud.login(c, "a@b.c", "Abcdef1!")
    assert net.urls == ["wss://cloud.test/fast", "wss://eu.test/fast"]
    assert (
        c.url == "wss://eu.test/fast"
        and cloud.load_session()["url"] == "wss://eu.test/fast"
    )
    c.close()


def test_ensure_logged_in_restores_from_the_saved_session():
    cloud.save_session(
        {"account": "a@b.c", "sessid": "S9", "url": "wss://cloud.test/fast"}
    )
    c, net = client_for(lambda m: ok(m))
    cloud.ensure_logged_in(c)
    body = net.servers[0].received[0]["body"]
    assert (
        body["account"] == "a@b.c" and body["sessid"] == "S9" and body["passwd"] == ""
    )
    c.close()


def test_ensure_logged_in_exits_without_a_saved_session(capsys):
    c, _ = client_for()
    with pytest.raises(SystemExit) as e:
        cloud.ensure_logged_in(c)
    assert e.value.code == 1 and "imcam login" in capsys.readouterr().err
    c.close()


def test_ensure_logged_in_exits_when_the_server_rejects_the_session(capsys):
    cloud.save_session({"account": "a@b.c", "sessid": "old"})
    c, _ = client_for(
        lambda m: {
            "headers": {"req_id": m["headers"]["req_id"]},
            "ret": 1,
            "msg": "expired",
        }
    )
    with pytest.raises(SystemExit):
        cloud.ensure_logged_in(c)
    assert "session restore failed" in capsys.readouterr().err
    c.close()


def test_client_url_defaults_to_the_saved_session_then_the_default():
    assert cloud.Client().url == cloud.DEFAULT_URL
    cloud.save_session({"account": "a", "sessid": "s", "url": "wss://eu.test/fast"})
    assert cloud.Client().url == "wss://eu.test/fast"


def test_register_and_code_check_carry_vendor_constants():
    c, net = client_for(lambda m: ok(m))
    cloud.register(c, "a@b.c", "pw", "tok", "PL")
    cloud.code_check(c, "a@b.c", "1234")
    reg, chk = (m["body"] for m in net.servers[0].received)
    assert (
        reg["vkey"] == chk["vkey"] == "vkey"
        and reg["country_code"] == "PL"
        and len(reg["passwd"]) == 32
    )
    assert chk["code"] == "1234" and chk["token"] == ""
    c.close()


# --- signaling sign-in ---


def test_signin_derives_the_access_key_and_opens_the_im_socket():
    cloud.save_session({"account": "a", "sessid": "SESS"})
    cipher_b64, expected_key = make_cipher("SESS", b"plain-secret")
    rtc = {
        "magic_md5": MAGIC,
        "aes_key": cipher_b64,
        "wss": "wss://rtc.test/fast",
        "access_id": "AID",
    }
    im = {}

    def fast(msg):
        assert msg["headers"]["id"] == "pub_dp_get"
        return ok(msg, [{"id": 9, "value": json.dumps(rtc)}])

    def signin_reply(msg):
        im["signin"] = msg
        return {"opcode": "signin", "code": 0, "sid": 42}

    net = Net()
    fast_server = Server(fast)
    im_server = Server(signin_reply)

    def dial(url):
        net.urls.append(url)
        return (
            (
                fast_server
                if url.endswith("/fast") and len(net.urls) == 1
                else im_server
            ).client_sock,
            b"",
        )

    c = cloud.Client(url="wss://cloud.test/fast", dial=dial, heartbeat_s=60)
    c.connect(retries=1)
    sock, buf, rsp = cloud.signin(c)
    assert net.urls == ["wss://cloud.test/fast", "wss://rtc.test/im"]
    assert (
        rsp["sid"] == 42
        and im["signin"]["secret"] == expected_key
        and im["signin"]["key"] == "AID"
    )
    assert im["signin"]["serialCode"] == "SESS"
    sock.close()
    c.close()


def test_signin_exits_when_the_rtc_config_is_missing():
    c, _ = client_for(lambda m: ok(m, []))
    with pytest.raises(SystemExit, match="no RtcConfig"):
        cloud.signin(c)
    c.close()
