"""`imcam snapshot`: save one frame of the live stream as a PNG (e.g. as a babywatch reference frame: `babywatch set-roi --ref`).

Runs in a separate process from `watch`/`monitor`: it touches SNAP_FLAG so the relay also feeds SNAP_PORT, then
ffmpeg grabs one key frame from it."""

import subprocess

from . import children, paths, relay


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
    with relay.SnapshotFlag().request():
        proc = children.popen(cmd)
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            code = 1
        finally:
            children.stop(proc)
    if code:
        raise SystemExit(
            "no frame from the live stream; run `imcam watch` or `monitor` first"
        )
    return out_path
