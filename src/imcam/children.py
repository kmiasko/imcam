"""Child processes (ffplay, ffmpeg, babywatch) that never outlive imcam, and clean exit on Ctrl+C or kill.

popen() starts each child in its own session, so the terminal's Ctrl+C reaches only imcam, which then stops
the children in order (ffmpeg gets SIGTERM and finishes its recording). On Linux the kernel also sends the
child SIGTERM when imcam dies, even by SIGKILL (PR_SET_PDEATHSIG). The signal follows the *thread* that called
popen(), so call it from the main thread only. install() turns SIGTERM/SIGHUP into KeyboardInterrupt, so the
same `finally` blocks run as for Ctrl+C (the call hangs up, the snapshot flag is removed), and stops any
child still running at exit."""

import atexit
import ctypes
import os
import signal
import subprocess
import sys

_live = set()
_PR_SET_PDEATHSIG = 1


def _die_with_parent(parent):
    def preexec():
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        except (OSError, AttributeError):
            return
        if os.getppid() != parent:  # imcam died before prctl took effect
            os._exit(1)

    return preexec


def popen(cmd, **kw):
    if os.name == "nt":
        kw.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw.setdefault("start_new_session", True)
        if sys.platform.startswith("linux"):
            kw.setdefault("preexec_fn", _die_with_parent(os.getpid()))
    proc = subprocess.Popen(cmd, **kw)
    _live.add(proc)
    return proc


def stop(proc, timeout=10):
    """SIGTERM, wait up to `timeout` s, then SIGKILL. Another Ctrl+C while waiting kills at once."""
    try:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            proc.wait()
    except OSError:
        pass
    finally:
        _live.discard(proc)


def stop_all(timeout=5):
    for proc in list(_live):
        stop(proc, timeout)


def _interrupt(signum, frame):
    raise KeyboardInterrupt("signal %d" % signum)


def install():
    """Call once from main(): signal handlers plus an atexit sweep of leftover children."""
    atexit.register(stop_all)
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _interrupt)
