"""Runs the separate `baby-tracker` program on the relay's tracking stream.

baby-tracker reads the RTP that relay.py forwards to TRACK_PORT through TRACK_SDP; this module only starts it and
stops it. `launch` is the seam tests replace (it returns a Popen-like object)."""

import shutil
import subprocess
import sys

from . import relay

TOOL = "baby-tracker"


def command(record=None):
    """baby-tracker argv for the relay's tracking stream; exits with install help if it is missing."""
    exe = shutil.which(TOOL)
    if not exe:
        raise SystemExit(
            "%s not found on PATH; install it (https://github.com/kmiasko/baby-tracker) "
            "and check with `%s doctor`" % (TOOL, TOOL)
        )
    cmd = [
        exe,
        "monitor",
        "--input",
        relay.write_sdp(relay.TRACK_SDP, relay.TRACK_PORT),
    ]
    if record:
        cmd += ["--record", record]
    return cmd


def launch(cmd):
    return subprocess.Popen(cmd)


class TrackerProcess:
    def __init__(self, record=None, launcher=None):
        self.record, self.launcher = record, launcher or launch
        self.proc = self.launcher(command(record))

    def ensure_running(self):
        """Start baby-tracker again if it exited; returns True when it had to."""
        code = self.proc.poll()
        if code is None:
            return False
        print("baby-tracker exited (%s), restarting" % code, file=sys.stderr)
        self.proc = self.launcher(command(self.record))
        return True

    def stop(self, timeout=10):
        """Ask baby-tracker to end (it stops its ffmpeg) and wait; kill it after `timeout` seconds."""
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
