from imcam import stock_offer

SDP = (
    "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "a=ice-ufrag:abcd\r\na=fingerprint:sha-256 AA:BB\r\na=setup:actpass\r\na=mid:0\r\n"
)


def test_stock_offer_keeps_our_credentials():
    ours = SDP + "a=ice-pwd:secretpwd\r\n"
    out = stock_offer.build(ours)
    assert "a=ice-ufrag:abcd" in out and "a=ice-pwd:secretpwd" in out
    assert out.count("a=fingerprint:sha-256 AA:BB") == 2
    assert "H265/90000" in out and out.endswith("\r\n")
