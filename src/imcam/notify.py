"""Desktop notifications: notify-send (Linux), osascript (macOS), winotify toasts (Windows).
Never raises; returns False when no backend could show the message."""
import shutil
import subprocess
import sys

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
            subprocess.Popen(["notify-send", "-u", "critical", title, message], **_DEVNULL)
        elif b == "osascript":  # message goes in as argv, never spliced into the script
            subprocess.Popen(["osascript", "-e", "on run argv",
                              "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                              "-e", "end run", title, message], **_DEVNULL)
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
