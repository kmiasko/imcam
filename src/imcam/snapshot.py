"""`imcam snapshot`: save one frame of the live stream as a PNG (for `baby-tracker calibrate --image`).

Runs in a separate process from `watch`/`monitor`: it touches SNAP_FLAG so the relay also feeds SNAP_PORT, then
ffmpeg grabs one key frame from it."""

import subprocess

from . import paths, relay


def grab(out_path, timeout=40):
    """Write one frame of the running stream to out_path; exits if none arrives."""
    cmd = [
        paths.require_tool("ffmpeg"),
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,udp,rtp",
        "-skip_frame",
        "nokey",
        "-i",
        relay.write_sdp(relay.SNAP_SDP, relay.SNAP_PORT),
        "-frames:v",
        "1",
        out_path,
    ]
    try:
        with relay.SnapshotFlag().request():
            code = subprocess.run(cmd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        code = 1
    if code:
        raise SystemExit(
            "no frame from the live stream; run `imcam watch` or `monitor` first"
        )
    return out_path
