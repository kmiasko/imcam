"""The offer we send to the camera: shaped like stock Android libwebrtc output and generated in code, so nothing
captured from the official app is needed. Only our own ICE credentials and fingerprint are taken from the
aiortc offer; everything else is fixed here."""

import random
import string

_EXTMAPS = (
    "urn:ietf:params:rtp-hdrext:toffset",
    "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
    "urn:3gpp:video-orientation",
    "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
    "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay",
    "http://www.webrtc.org/experiments/rtp-hdrext/video-content-type",
    "http://www.webrtc.org/experiments/rtp-hdrext/video-timing",
    "http://www.webrtc.org/experiments/rtp-hdrext/color-space",
    "urn:ietf:params:rtp-hdrext:sdes:mid",
    "urn:ietf:params:rtp-hdrext:sdes:rtp-stream-id",
    "urn:ietf:params:rtp-hdrext:sdes:repaired-rtp-stream-id",
)
_RTCP_FB = ("goog-remb", "transport-cc", "ccm fir", "nack", "nack pli")
# (payload type, codec line, fmtp or None, rtx payload type)
_VIDEO = (
    (96, "VP8/90000", None, 97),
    (
        100,
        "H264/90000",
        "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
        101,
    ),
    (125, "H265/90000", None, 124),
)
_AUDIO = (
    "a=rtpmap:111 opus/48000/2",
    "a=fmtp:111 minptime=10;useinbandfec=1",
    "a=rtpmap:0 PCMU/8000",
    "a=rtpmap:8 PCMA/8000",
    "a=rtpmap:13 CN/8000",
    "a=rtpmap:126 telephone-event/8000",
)


def _ours(sdp):
    """Our ICE credentials and fingerprint lines, keyed by attribute prefix."""
    ours = {}
    for line in sdp.split("\r\n"):
        for key in ("a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:"):
            if line.startswith(key):
                ours.setdefault(key, line)
    return ours


def build(sdp, rng=random):
    """A libwebrtc-shaped offer carrying the ICE credentials and fingerprint from our own offer `sdp`."""
    ours = _ours(sdp)
    transport = [ours[k] for k in ("a=ice-ufrag:", "a=ice-pwd:") if k in ours] + [
        "a=ice-options:trickle renomination"
    ]
    transport.append(ours.get("a=fingerprint:", ""))
    transport = [t for t in transport if t] + ["a=setup:actpass"]
    cname = "".join(rng.choice(string.ascii_letters) for _ in range(16))
    ssrc = [rng.randrange(1, 2**32) for _ in range(3)]
    pts = [pt for pt, _, _, _ in _VIDEO] + [rtx for _, _, _, rtx in _VIDEO]

    out = [
        "v=0",
        "o=- %d 2 IN IP4 127.0.0.1" % rng.randrange(1, 2**62),
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0 1",
        "a=extmap-allow-mixed",
        "a=msid-semantic: WMS ARDAMS",
        "m=video 9 UDP/TLS/RTP/SAVPF " + " ".join(str(p) for p in pts),
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
    ]
    out += transport + ["a=mid:0"]
    out += ["a=extmap:%d %s" % (i, u) for i, u in enumerate(_EXTMAPS, 1)]
    out += ["a=sendrecv", "a=msid:- local_video_track", "a=rtcp-mux", "a=rtcp-rsize"]
    for pt, codec, fmtp, rtx in _VIDEO:
        out.append("a=rtpmap:%d %s" % (pt, codec))
        out += ["a=rtcp-fb:%d %s" % (pt, fb) for fb in _RTCP_FB]
        if fmtp:
            out.append("a=fmtp:%d %s" % (pt, fmtp))
        out += ["a=rtpmap:%d rtx/90000" % rtx, "a=fmtp:%d apt=%d" % (rtx, pt)]
    out += ["a=ssrc-group:FID %d %d" % (ssrc[0], ssrc[1])]
    for s in ssrc[:2]:
        out += [
            "a=ssrc:%d cname:%s" % (s, cname),
            "a=ssrc:%d msid:- local_video_track" % s,
        ]
    out += [
        "m=audio 9 UDP/TLS/RTP/SAVPF 111 0 8 13 126",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
    ]
    out += transport + ["a=mid:1", "a=sendrecv", "a=msid:ARDAMS audio_track_id"]
    out += ["a=rtcp-mux"] + list(_AUDIO)
    out += [
        "a=ssrc:%d cname:%s" % (ssrc[2], cname),
        "a=ssrc:%d msid:ARDAMS audio_track_id" % ssrc[2],
    ]
    return "\r\n".join(out) + "\r\n"
