import os
import signal
import subprocess
import sys
import time

import pytest

from imcam import children

posix = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG")

SRC = os.path.join(os.path.dirname(__file__), os.pardir, "src")


class Stubborn:
    """Ignores SIGTERM: stop() must fall back to kill."""

    def __init__(self):
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired("x", timeout)
        return -9

    def kill(self):
        self.killed = True


def test_stop_kills_a_child_that_ignores_sigterm():
    proc = Stubborn()
    children.stop(proc, timeout=0.01)
    assert proc.killed


def test_stop_all_ends_every_live_child():
    proc = children.popen([sys.executable, "-c", "import time; time.sleep(60)"])
    children.stop_all()
    assert proc.poll() is not None and proc not in children._live


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is dead but still visible until reaped
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def _run_parent(body):
    """Start a Python parent that popen()s a long sleep and prints its pid; returns (parent, child_pid)."""
    code = (
        "import sys, time\nfrom imcam import children\nchildren.install()\n"
        "p = children.popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n" + body
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PYTHONPATH=SRC),
    )
    return parent, int(parent.stdout.readline())


def _gone(pid, within=5):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@linux
def test_child_dies_when_imcam_is_sigkilled():
    parent, child = _run_parent("time.sleep(60)")
    parent.kill()
    parent.wait()
    assert _gone(child)


@posix
def test_sigterm_runs_finally_and_stops_children():
    parent, child = _run_parent(
        "try:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n    print('cleaned', flush=True)\n"
    )
    parent.send_signal(signal.SIGTERM)
    assert parent.stdout.readline().strip() == "cleaned"
    parent.wait(5)
    assert _gone(child)
