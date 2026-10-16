import json

import pytest

from imcam import call, extract_offer

SDP = (
    "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "a=ice-ufrag:abcd\r\na=fingerprint:sha-256 AA:BB\r\na=setup:actpass\r\na=mid:0\r\n"
)
WANT = SDP.replace("\r\n", "\n")


def _msg():
    return json.dumps(
        {
            "messageType": "callRequest",
            "message": json.dumps({"type": "offer", "sdp": SDP}),
        }
    )


@pytest.mark.parametrize(
    "text",
    [
        "D/SLog: send " + _msg() + "\n",  # SDP escaped once inside the message string
        "D/SLog: " + json.dumps(_msg()) + "\n",  # escaped twice
        "".join(
            "10-15 19:00:01.123  100  200 D SLog : %s\n" % ln
            for ln in SDP.split("\r\n")
        ),  # multi-line logcat
    ],
)
def test_find_offers(text):
    offers = extract_offer.find_offers("noise\n" + text + "more noise\n")
    assert offers and offers[-1] == WANT


def test_extract_writes_last_offer(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("x " + _msg() + "\n" + "y " + _msg().replace("abcd", "wxyz") + "\n")
    out = tmp_path / "o.sdp"
    path, n = extract_offer.extract(str(log), str(out))
    assert n == 2 and "wxyz" in out.read_text()


def test_extract_without_offer_exits(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("nothing here\n")
    with pytest.raises(SystemExit, match="no WebRTC offer"):
        extract_offer.extract(str(log), str(tmp_path / "o.sdp"))


def test_missing_offer_raises_helpful_error(tmp_path, monkeypatch):
    monkeypatch.delenv("IMCAM_APP_OFFER", raising=False)
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path))
    with pytest.raises(call.OfferTemplateMissing, match="imcam extract-offer") as e:
        call.offer_template_path()
    assert str(tmp_path) in str(e.value)
    (tmp_path / "app_offer.sdp").write_text(SDP)
    assert call.offer_template_path() == str(tmp_path / "app_offer.sdp")
