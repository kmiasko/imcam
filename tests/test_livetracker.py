import os
import sys
import time

import pytest

from imcam import tracker

FRAMES = """
import sys, time
frame = bytes([100]) * (640 * 360 * 3)
while True:
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    time.sleep(0.05)
"""
STALLED = "import time\nwhile True: time.sleep(0.05)\n"
IGNORES_SIGTERM = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.05)\n"


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    cfg = tracker.validate_config(
        dict(
            tracker.DEFAULTS,
            roi=[[0.1, 0.3], [0.9, 0.3], [0.9, 0.95], [0.1, 0.95]],
            lying_line=0.6,
            fps=20,
        )
    )
    monkeypatch.setattr(tracker, "load_config", lambda path=None: cfg)

    def use(script):
        monkeypatch.setattr(
            tracker,
            "ffmpeg_cmd",
            lambda cfg, record_dir=None, pipe=True: [sys.executable, "-c", script],
        )

    return use


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_stop_returns_only_after_ffmpeg_and_the_tracking_thread_are_gone(
    fake_ffmpeg, capsys
):
    fake_ffmpeg(FRAMES)
    live = tracker.LiveTracker(track=True)
    assert wait_for(lambda: live.proc is not None)
    time.sleep(0.3)  # frames flowing
    started = time.monotonic()
    live.stop()
    assert time.monotonic() - started < 5
    assert live.proc.poll() is not None and not live.thread.is_alive()
    assert "restarting" not in capsys.readouterr().err


def test_stop_unblocks_a_tracker_waiting_on_a_silent_ffmpeg(fake_ffmpeg):
    fake_ffmpeg(
        STALLED
    )  # stream lost: ffmpeg is alive but writes nothing, so the reader is blocked
    live = tracker.LiveTracker(track=True)
    assert wait_for(lambda: live.proc is not None)
    started = time.monotonic()
    live.stop()
    assert time.monotonic() - started < 5
    assert live.proc.poll() is not None and not live.thread.is_alive()


def test_recording_only_stop_waits_for_ffmpeg(fake_ffmpeg):
    fake_ffmpeg(STALLED)
    live = tracker.LiveTracker(track=False)
    live.stop()
    assert live.proc.poll() is not None


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX signals")
def test_an_ffmpeg_that_ignores_sigterm_is_killed_after_the_grace_period():
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", IGNORES_SIGTERM], stdout=subprocess.PIPE
    )
    time.sleep(0.3)  # let it install the handler
    started = time.monotonic()
    tracker.stop_process(proc, grace=0.5)
    assert proc.poll() is not None and time.monotonic() - started < 5
