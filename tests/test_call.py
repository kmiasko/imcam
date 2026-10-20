import asyncio
import json
import threading

import pytest

from imcam import call

LOCAL_SDP = (
    "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 125\r\na=mid:0\r\n"
    "a=candidate:1 1 udp 2130706431 192.168.1.2 5000 typ host\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:1\r\n"
    "a=candidate:2 1 udp 2130706431 192.168.1.2 5002 typ host\r\n"
)


class FakeSig:
    def __init__(self, fail_on=None):
        self.sent, self.stop, self.fail_on = [], threading.Event(), fail_on

    def send(self, obj):
        if self.fail_on and obj.get("messageType") == self.fail_on:
            raise OSError("socket closed")
        self.sent.append(obj)

    def types(self):
        return [m["messageType"] for m in self.sent]

    def of(self, message_type):
        return [m for m in self.sent if m["messageType"] == message_type]


class FakePeer:
    """Scripted stand-in for AiortcPeer: records calls, connects instantly."""

    def __init__(self):
        self.answer, self.candidates, self.closed, self.reported = (
            None,
            [],
            False,
            False,
        )
        self.video_started = False

    async def offer(self):
        return LOCAL_SDP

    async def accept_answer(self, sdp):
        self.answer = sdp

    async def add_remote_candidate(self, message):
        self.candidates.append(message)

    async def send_video(self, on_connected):
        self.video_started = True
        on_connected()
        await asyncio.sleep(3600)

    async def report(self):
        self.reported = True

    async def close(self):
        self.closed = True


def msg(message_type, **fields):
    return dict({"opcode": "message", "messageType": message_type}, **fields)


def answer(sdp="v=0 answer", **fields):
    return msg(
        "callResponse",
        message=json.dumps(dict({"type": "answer", "sdp": sdp}, **fields)),
    )


def candidate(n):
    return msg(
        "remoteCandidate",
        message=json.dumps(
            {"sdpMid": "0", "sdpMLineIndex": 0, "candidate": "candidate:%d" % n}
        ),
    )


def run(script, peer=None, sig=None, answer_timeout=5):
    """Feed `script` (messages, None = signaling closed) to run_call; returns (reason, sig, peer)."""
    peer, sig = peer or FakePeer(), sig or FakeSig()

    async def main():
        inbox = asyncio.Queue()

        async def feed():  # one message per loop turn, like a socket reader, so background tasks get to run
            for item in script:
                await asyncio.sleep(0.01)
                if callable(item):  # gate: wait until the condition holds
                    while not item():
                        await asyncio.sleep(0.01)
                    continue
                inbox.put_nowait(item)

        feeder = asyncio.create_task(feed())
        reason = await asyncio.wait_for(
            call.run_call(
                sig,
                inbox,
                peer,
                7,
                "SER",
                "PERMIT",
                munge=lambda s: "M:" + s,
                answer_timeout=answer_timeout,
            ),
            10,
        )
        await feeder
        return reason

    return asyncio.run(main()), sig, peer


def test_call_request_carries_the_munged_offer_then_each_local_candidate():
    _, sig, _ = run([None])
    first = sig.sent[0]
    assert (
        first["messageType"] == "callRequest"
        and first["sid"] == 7
        and first["toSerialCode"] == "SER"
    )
    assert (
        first["permitCode"] == "PERMIT"
        and first["toSid"] == -1
        and first["callType"] == 0
    )
    assert json.loads(first["message"]) == {"type": "offer", "sdp": "M:" + LOCAL_SDP}
    cands = [json.loads(m["message"]) for m in sig.of("remoteCandidate")]
    assert cands == [
        {
            "sdpMid": "0",
            "sdpMLineIndex": 0,
            "candidate": "candidate:1 1 udp 2130706431 192.168.1.2 5000 typ host",
        },
        {
            "sdpMid": "1",
            "sdpMLineIndex": 1,
            "candidate": "candidate:2 1 udp 2130706431 192.168.1.2 5002 typ host",
        },
    ]


def test_answer_is_accepted_then_audio_is_switched_and_video_requested_once_connected():
    peer = FakePeer()
    reason, sig, peer = run(
        [answer("v=0 theirs"), lambda: peer.video_started, msg("hangup")], peer=peer
    )
    assert reason == "hangup" and peer.answer == "v=0 theirs" and peer.video_started
    assert sig.types() == [
        "callRequest",
        "remoteCandidate",
        "remoteCandidate",
        "switchAudio",
        "switchVideo",
        "hangup",
    ]
    assert json.loads(sig.of("switchVideo")[0]["message"]) == {"type": 2, "act": 0}
    assert json.loads(sig.of("switchAudio")[0]["message"]) == {"type": 0, "act": 0}


def test_candidates_before_the_answer_are_held_then_added_in_order():
    first, second, late = candidate(1), candidate(2), candidate(3)
    reason, _, peer = run([first, second, answer(), late, msg("hangup")])
    assert peer.candidates == [first, second, late]


def test_candidates_are_not_added_without_an_answer():
    _, _, peer = run([candidate(1), msg("hangup")])
    assert peer.candidates == []


def test_non_zero_call_response_code_means_rejected():
    reason, _, peer = run([msg("callResponse", code=3)])
    assert reason == "rejected" and peer.answer is None


@pytest.mark.parametrize("fields", [{"type": "exceed", "sdp": "v=0"}, {"sdp": ""}])
def test_device_refusal_ends_the_call(fields):
    reason, _, peer = run([msg("callResponse", message=json.dumps(fields))])
    assert reason == "refused" and peer.answer is None


def test_no_answer_within_the_timeout():
    reason, _, _ = run([], answer_timeout=0.05)
    assert reason == "no_answer"


def test_closed_signaling_ends_the_call():
    reason, _, _ = run([None])
    assert reason == "signaling_closed"


def test_the_call_waits_without_a_timeout_once_answered():
    async def main():
        peer, sig, inbox = FakePeer(), FakeSig(), asyncio.Queue()
        inbox.put_nowait(answer())
        task = asyncio.create_task(
            call.run_call(sig, inbox, peer, 7, "S", "P", munge=str, answer_timeout=0.05)
        )
        await asyncio.sleep(0.3)  # well past answer_timeout
        assert not task.done()
        inbox.put_nowait(msg("hangup"))
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) == "hangup"


def test_other_signaling_is_ignored(capsys):
    reason, _, peer = run(
        [{"opcode": "notice", "x": 1}, msg("somethingNew"), answer(), msg("hangup")]
    )
    assert reason == "hangup" and peer.answer
    assert "somethingNew" in capsys.readouterr().out


def test_every_exit_hangs_up_stops_signaling_and_closes_the_peer():
    for script in (
        [None],
        [msg("callResponse", code=1)],
        [answer(), msg("hangup")],
        [],
    ):
        reason, sig, peer = run(script, answer_timeout=0.05)
        assert (
            sig.types()[-1] == "hangup"
            and sig.stop.is_set()
            and peer.closed
            and peer.reported
        )


def test_a_dead_signaling_socket_does_not_stop_cleanup():
    reason, sig, peer = run([msg("hangup")], sig=FakeSig(fail_on="hangup"))
    assert reason == "hangup" and sig.stop.is_set() and peer.closed


def test_exceptions_from_the_peer_still_clean_up():
    class Broken(FakePeer):
        async def accept_answer(self, sdp):
            raise RuntimeError("bad sdp")

    peer, sig = Broken(), FakeSig()
    with pytest.raises(RuntimeError, match="bad sdp"):
        run([answer()], peer=peer, sig=sig)
    assert sig.types()[-1] == "hangup" and peer.closed


# --- message and SDP helpers ---


def test_call_msg_has_the_field_set_the_server_expects():
    m = call._call_msg(5, "SER", "PERMIT", "switchAudio", "{}")
    assert (
        m["opcode"] == "message"
        and m["toSid"] == -1
        and m["serialCode"] == ""
        and m["message"] == "{}"
    )
    assert m["reqId"].startswith("a_sig_py_") and "callType" not in m
    assert call._call_msg(5, "S", "P", "callRequest")["videoCodecType"] == 0


def test_local_candidates_carry_their_media_section():
    out = call._local_candidates(LOCAL_SDP)
    assert [(c["sdpMid"], c["sdpMLineIndex"]) for c in out] == [("0", 0), ("1", 1)]
    assert out[0]["candidate"].startswith("candidate:1 ") and not out[0][
        "candidate"
    ].startswith("a=")


def test_remote_candidate_parses_the_servers_message_and_ignores_empty_ones():
    line = "candidate:9 1 udp 2130706431 10.0.0.5 6000 typ host"
    c = call._remote_candidate(
        {"message": json.dumps({"candidate": line, "sdpMid": "0", "sdpMLineIndex": 0})}
    )
    assert (c.ip, c.port, c.sdpMid, c.sdpMLineIndex) == ("10.0.0.5", 6000, "0", 0)
    assert call._remote_candidate({"message": json.dumps({"candidate": ""})}) is None
    assert call._remote_candidate({}) is None


def test_aiortc_peer_offers_video_first_then_audio_and_closes_cleanly():
    async def main():
        peer = call.AiortcPeer({})
        try:
            return await peer.offer()
        finally:
            await peer.close()

    sdp = asyncio.run(main())
    assert sdp.index("m=video") < sdp.index("m=audio")
    assert "a=ice-ufrag:" in sdp and "a=fingerprint:" in sdp
