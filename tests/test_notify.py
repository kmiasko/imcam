from imcam import notify


def test_backend_is_known():
    assert notify.backend() in ("notify-send", "osascript", "windows", None)
