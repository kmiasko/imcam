"""Extract the official app's WebRTC offer SDP from a capture of its signaling log.

The offer is not stored in the APK: libwebrtc generates it when the app starts a call. The app does log its
signaling messages, so capture them (e.g. `adb logcat -d > app.log` right after starting a live view in the
official app) and run:

    imcam extract-offer app.log            # writes ~/.imcam/app_offer.sdp
    imcam extract-offer app.log -o FILE

Handles SDP embedded as a JSON string (\\r\\n escaped, possibly escaped twice) and multi-line logcat output.
"""

import re
import sys

from . import paths

OFFER_NAME = "app_offer.sdp"

_LOGCAT_PREFIX = re.compile(
    r"^(?:\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+[VDIWEF]\s+\S+\s*:\s?"  # threadtime
    r"|[VDIWEF]/\S+\(\s*\d+\):\s?)"
)  # brief
_ESCAPED = re.compile(r'v=0(?:\\+r)?\\+n[^"]*')
_MULTILINE = re.compile(r"(?m)^v=0\r?\n(?:[a-z]=[^\n]*\n?)+")


def _unescape(s):
    s = re.sub(r"\\+r\\+n|\\+n", "\n", s)
    s = re.sub(r"\\+/", "/", s)
    return s.rstrip("\\ \n") + "\n"


def _is_offer(sdp):
    return all(
        k in sdp
        for k in ("m=video", "a=ice-ufrag:", "a=fingerprint:", "a=setup:actpass")
    )


def find_offers(text):
    """All complete offer SDPs in `text`, in order of appearance (newline-normalized)."""
    found = []
    for m in _ESCAPED.finditer(text):
        found.append((m.start(), _unescape(m.group(0))))
    cleaned = "\n".join(
        _LOGCAT_PREFIX.sub("", ln.rstrip("\r")) for ln in text.split("\n")
    )
    for m in _MULTILINE.finditer(cleaned):
        found.append((m.start(), m.group(0).replace("\r", "")))
    return [sdp for _, sdp in sorted(found, key=lambda t: t[0]) if _is_offer(sdp)]


def extract(log_path, out_path=None):
    with open(log_path, encoding="utf-8", errors="replace") as f:
        offers = find_offers(f.read())
    if not offers:
        raise SystemExit(
            "no WebRTC offer (v=0 ... m=video ... a=fingerprint) found in %s.\n"
            "Capture the log while the official app opens a live view of the camera."
            % log_path
        )
    out_path = out_path or paths.path(OFFER_NAME)
    import os

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", newline="\n") as f:
        f.write(offers[-1])  # the most recent call
    return out_path, len(offers)


def main(argv):
    out, args = None, list(argv)
    if "-o" in args:
        i = args.index("-o")
        out = args[i + 1] if i + 1 < len(args) else sys.exit("-o needs a file")
        del args[i : i + 2]
    if len(args) != 1:
        raise SystemExit("usage: imcam extract-offer <log-file> [-o OUTPUT]")
    path, n = extract(args[0], out)
    print(
        "wrote %s (%d offer%s found, used the last)" % (path, n, "" if n == 1 else "s")
    )


if __name__ == "__main__":
    main(sys.argv[1:])
