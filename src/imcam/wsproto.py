"""RFC 6455 WebSocket client pieces: URL parsing, handshake, frame encode/decode (blocking sockets, no deps)."""
import base64
import os
import socket
import ssl
import struct


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
