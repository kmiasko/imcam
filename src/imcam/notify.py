"""Event delivery: a log line, a desktop notification (notify-send on Linux, osascript on macOS, winotify
toasts on Windows) and the user's hook command. send() never raises; it returns False when no backend
could show the message."""

import os
import shlex
import shutil
import subprocess
import sys
import time

_DEVNULL = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def backend():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "osascript" if shutil.which("osascript") else None
    return "notify-send" if shutil.which("notify-send") else None


def send(title, message):
    b = backend()
    try:
        if b == "notify-send":
            subprocess.Popen(
                ["notify-send", "-u", "critical", title, message], **_DEVNULL
            )
        elif b == "osascript":  # message goes in as argv, never spliced into the script
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    "on run argv",
                    "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e",
                    "end run",
                    title,
                    message,
                ],
                **_DEVNULL,
            )
        elif b == "windows":
            return _windows(title, message)
        else:
            return False
        return True
    except OSError:
        return False


def _windows(title, message):
    try:
        from winotify import Notification, audio

        t = Notification(app_id="ImCam", title=title, msg=message, duration="long")
        t.set_audio(audio.Default, loop=False)
        t.show()
        return True
    except Exception:  # winotify missing or toast refused: at least make a sound
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        return False


def _run_hook(argv, env):
    subprocess.Popen(argv, env=env)


def make_notifier(cfg, desktop=None, run_hook=None, log=print):
    """Callable (name, msg, push=True) delivering one event. Every event is logged; push events also go to
    the desktop and to cfg["hook"] with IMCAM_EVENT / IMCAM_MESSAGE in its environment. desktop(title, msg),
    run_hook(argv, env) and log(line) are the adapters; the defaults are send(), Popen and print."""
    hook = shlex.split(cfg.get("hook") or "", posix=os.name != "nt")

    def deliver(name, msg, push=True):
        log("%s [%s] %s" % (time.strftime("%H:%M:%S"), name, msg))
        if not push:
            return
        (desktop or send)("ImCam", msg)
        if hook:
            try:
                (run_hook or _run_hook)(
                    hook, dict(os.environ, IMCAM_EVENT=name, IMCAM_MESSAGE=msg)
                )
            except OSError as e:
                print("hook failed: %s" % e, file=sys.stderr)

    return deliver
