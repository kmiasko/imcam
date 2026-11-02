import os
import struct

from imcam import relay


def rtp(pt=relay.H265_PT, ssrc=0xAABBCCDD, size=40):
    return bytes([0x80, pt]) + bytes(6) + struct.pack("!I", ssrc) + bytes(size - 12)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSnapshot:
    def __init__(self, wanted=False):
        self._wanted = wanted

    def wanted(self):
        return self._wanted


def make_relay(ports=(relay.RTP_PORT,), snapshot=None, clock=None):
    sent, logs = [], []
    r = relay.Relay(
        ports,
        send=lambda data, port: sent.append((port, data)),
        snapshot=snapshot or FakeSnapshot(),
        mono=clock or Clock(),
        log=logs.append,
    )
    return r, sent, logs


def test_h265_packet_is_copied_to_every_consumer_port():
    r, sent, _ = make_relay(ports=(relay.RTP_PORT, relay.TRACK_PORT))
    pkt = rtp()
    r.on_rtp(pkt)
    assert sent == [(relay.RTP_PORT, pkt), (relay.TRACK_PORT, pkt)]


ANSWER = (
    "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 125\r\na=rtpmap:125 H265/90000\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF %s\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:111 opus/48000/2\r\n"
)


def test_audio_codec_is_read_from_the_answer():
    assert relay.audio_codec(ANSWER % "111 0") == (111, "opus/48000/2")
    assert relay.audio_codec(ANSWER % "0") == (0, "PCMU/8000")
    assert relay.audio_codec(ANSWER.split("m=audio")[0]) is None
    assert relay.audio_codec(ANSWER.replace("m=audio 9", "m=audio 0") % "111") is None


def test_negotiated_audio_goes_to_the_audio_ports_and_not_to_aiortc():
    clock = Clock()
    r, sent, _ = make_relay(ports=(relay.RTP_PORT, relay.TRACK_PORT), clock=clock)
    pkt = rtp(pt=111)
    assert not r.consumes(pkt)  # before the answer: unknown, left to aiortc
    r.on_answer(ANSWER % "111 0")
    assert r.consumes(pkt) and r.on_rtp(pkt) is None
    assert sent == [
        (relay.RTP_PORT + relay.AUDIO_OFFSET, pkt),
        (relay.TRACK_PORT + relay.AUDIO_OFFSET, pkt),
    ]
    assert r.last_audio_rx == clock.t and r.last_rx is None


def test_audio_in_another_codec_than_the_readers_expect_is_dropped():
    r, sent, logs = make_relay()
    r.on_answer(ANSWER % "0")
    assert r.consumes(rtp(pt=0))
    r.on_rtp(rtp(pt=0))
    assert sent == [] and "not relayed" in logs[0]


def test_reader_ports_do_not_overlap():
    """Each reader binds its port and port+1 (RTCP), for video and audio."""
    bound = []
    for port in (relay.RTP_PORT, relay.TRACK_PORT):
        bound += [
            port,
            port + 1,
            port + relay.AUDIO_OFFSET,
            port + relay.AUDIO_OFFSET + 1,
        ]
    bound += [relay.SNAP_PORT, relay.SNAP_PORT + 1]
    assert len(set(bound)) == len(bound)
    assert not {47308, 47314} & set(bound)  # watcher's MPEG-TS outputs


def test_other_payload_types_are_not_forwarded():
    r, sent, _ = make_relay()
    assert r.on_rtp(rtp(pt=96)) is None
    assert sent == [] and not r.is_h265(rtp(pt=96))


def test_snapshot_port_is_fed_only_while_a_snapshot_is_wanted():
    snap = FakeSnapshot()
    r, sent, _ = make_relay(snapshot=snap)
    r.on_rtp(rtp())
    snap._wanted = True
    r.on_rtp(rtp())
    assert [port for port, _ in sent] == [
        relay.RTP_PORT,
        relay.RTP_PORT,
        relay.SNAP_PORT,
    ]


def test_keyframe_request_is_due_first_then_every_five_seconds():
    clock = Clock()
    r, _, _ = make_relay(clock=clock)
    assert r.on_rtp(rtp(ssrc=7)) == 7  # first packet asks right away
    clock.t += 4.9
    assert r.on_rtp(rtp(ssrc=7)) is None
    clock.t += 0.2
    assert r.on_rtp(rtp(ssrc=7)) == 7


def test_progress_is_logged_every_500_packets():
    r, _, logs = make_relay()
    for _ in range(501):
        r.on_rtp(rtp())
    assert [line.split("(")[1] for line in logs] == [
        "0 packets so far)",
        "500 packets so far)",
    ]


def test_snapshot_flag_request_creates_and_removes_the_flag(tmp_path):
    flag = relay.SnapshotFlag(str(tmp_path / "snap.want"))
    assert not flag.wanted()
    with flag.request():
        assert os.path.exists(flag.path)
    assert not os.path.exists(flag.path)


def test_snapshot_flag_is_seen_by_the_relay_side_and_polled_at_most_once_a_second(
    tmp_path,
):
    path = str(tmp_path / "snap.want")
    mono, wall = Clock(0.0), Clock(os.path.getmtime(str(tmp_path)))
    relay_side = relay.SnapshotFlag(path, mono=mono, wall=wall)
    assert not relay_side.wanted()
    with relay.SnapshotFlag(path).request():
        assert not relay_side.wanted()  # cached for SNAP_POLL_S
        mono.t += 1.5
        wall.t = os.path.getmtime(path)
        assert relay_side.wanted()


def test_stale_snapshot_flag_is_ignored(tmp_path):
    path = str(tmp_path / "snap.want")
    open(path, "w").close()
    wall = Clock(os.path.getmtime(path) + relay.SNAP_FLAG_TTL_S + 1)
    assert not relay.SnapshotFlag(path, mono=Clock(), wall=wall).wanted()


def test_write_sdp_describes_the_forwarded_stream(tmp_path):
    path = relay.write_sdp(str(tmp_path / "sub" / "x.sdp"), 47304)
    sdp = open(path).read()
    assert "m=video 47304 RTP/AVP %d" % relay.H265_PT in sdp
    assert "a=rtpmap:%d H265/90000" % relay.H265_PT in sdp


def test_write_sdp_adds_audio_two_ports_up(tmp_path):
    path = relay.write_sdp(str(tmp_path / "x.sdp"), 47304, relay.AUDIO)
    sdp = open(path).read()
    assert sdp.index("m=video 47304") < sdp.index("m=audio 47306 RTP/AVP 111")
    assert "a=rtpmap:111 opus/48000/2" in sdp
