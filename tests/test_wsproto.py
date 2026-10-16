import json
import socket
import struct
import threading

import pytest

from imcam import wsproto


def server_frame(opcode, payload):
    """Server-to-client frames are not masked."""
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, n])
    elif n < 65536:
        head = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
    return head + payload


def test_frame_round_trip_across_length_encodings():
    heartbeat = json.dumps(
        {"headers": {"id": "pub_heartbeat", "req_id": "1", "time": 1}}
    ).encode()
    for payload in (
        heartbeat,
        b"x" * 300,
        b"y" * 70000,
    ):  # 7-bit, 16-bit and 64-bit lengths
        op, got, rest = wsproto.parse_frame(wsproto.frame_encode(0x1, payload), None)
        assert op == 0x1 and got == payload and not rest


def test_client_frames_are_masked_and_server_frames_are_not():
    assert wsproto.frame_encode(0x1, b"hi")[1] & 0x80
    op, payload, rest = wsproto.parse_frame(server_frame(0x1, b"hi"), None)
    assert (op, payload, rest) == (0x1, b"hi", b"")


def test_parse_frame_returns_the_bytes_after_the_frame():
    op, payload, rest = wsproto.parse_frame(
        server_frame(0x1, b"one") + server_frame(0x1, b"two"), None
    )
    assert (payload, rest) == (b"one", server_frame(0x1, b"two"))


def test_parse_frame_refills_from_the_socket_mid_frame():
    a, b = socket.socketpair()
    frame = server_frame(0x1, b"z" * 300)
    a.sendall(frame[5:])
    assert wsproto.parse_frame(frame[:5], b) == (0x1, b"z" * 300, b"")


def test_parse_frame_reports_a_connection_closed_mid_frame():
    a, b = socket.socketpair()
    a.close()
    with pytest.raises(ConnectionError):
        wsproto.parse_frame(server_frame(0x1, b"abc")[:3], b)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "wss://fast.jfgou.com:443/fast",
            ("fast.jfgou.com", 443, "/fast", True, "http://fast.jfgou.com:443/fast"),
        ),
        ("ws://host/x", ("host", 80, "/x", False, "http://host/x")),
        ("wss://host", ("host", 443, "/", True, "http://host")),
    ],
)
def test_parse_url(url, expected):  # the server expects an http:// Origin even for wss
    assert wsproto.parse_url(url) == expected


def test_parse_url_rejects_other_schemes():
    with pytest.raises(ValueError):
        wsproto.parse_url("http://host/")


def _serve_once(listener, response):
    conn, _ = listener.accept()
    with conn:
        buf = b""
        while not buf.endswith(b"\r\n\r\n"):
            buf += conn.recv(4096)
        conn.sendall(response)
        _serve_once.request = buf


def _handshake_against(response):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    t = threading.Thread(target=_serve_once, args=(listener, response), daemon=True)
    t.start()
    try:
        return wsproto.ws_handshake(
            "ws://127.0.0.1:%d/fast" % listener.getsockname()[1], timeout=5
        )
    finally:
        t.join(5)
        listener.close()


def test_handshake_sends_an_upgrade_request_and_keeps_early_bytes():
    sock, extra = _handshake_against(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
    sock.close()
    request = _serve_once.request.decode()
    assert request.startswith("GET /fast HTTP/1.1") and "Upgrade: websocket" in request
    assert "Origin: http://127.0.0.1:" in request and extra == b""


def test_handshake_refused_status_raises():
    with pytest.raises(OSError, match="handshake failed: 403"):
        _handshake_against(b"HTTP/1.1 403 Forbidden\r\n\r\n")
