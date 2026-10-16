from imcam import cli, notify, tracker


def test_cli_selftest():
    cli.selftest()


def test_tracker_selftest():
    tracker.selftest()


def test_notify_backend_is_known():
    assert notify.backend() in ("notify-send", "osascript", "windows", None)
