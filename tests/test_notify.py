from imcam import notify


def test_backend_is_known():
    assert notify.backend() in ("notify-send", "osascript", "windows", None)


class Recorder:
    def __init__(self, hook_error=None):
        self.logs, self.toasts, self.hooks, self.hook_error = [], [], [], hook_error

    def notifier(self, hook=""):
        return notify.make_notifier({"hook": hook}, desktop=lambda t, m: self.toasts.append((t, m)),
                                    run_hook=self._hook, log=self.logs.append)

    def _hook(self, argv, env):
        if self.hook_error:
            raise self.hook_error
        self.hooks.append((argv, env["IMCAM_EVENT"], env["IMCAM_MESSAGE"]))


def test_push_event_logs_toasts_and_runs_hook():
    r = Recorder()
    r.notifier("say hi")("awake", "baby is awake")
    assert len(r.logs) == 1 and r.logs[0].endswith("[awake] baby is awake")
    assert r.toasts == [("ImCam", "baby is awake")]
    assert r.hooks == [(["say", "hi"], "awake", "baby is awake")]


def test_non_push_event_is_only_logged():
    r = Recorder()
    r.notifier("say hi")("lying_again", "baby is lying down again", False)
    assert len(r.logs) == 1 and r.toasts == [] and r.hooks == []


def test_no_hook_configured_means_no_hook_call():
    r = Recorder()
    r.notifier("")("stream_lost", "stream lost")
    assert r.toasts and r.hooks == []


def test_hook_failure_is_reported_not_raised(capsys):
    r = Recorder(hook_error=OSError("no such command"))
    r.notifier("nope")("awake", "x")
    assert r.toasts == [("ImCam", "x")]
    assert "hook failed: no such command" in capsys.readouterr().err


def test_default_desktop_adapter_is_notify_send(monkeypatch):
    # regression: the closure used to shadow the notify module, so every push event raised AttributeError
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg: sent.append((title, msg)))
    notify.make_notifier({}, log=lambda line: None)("awake", "x")
    assert sent == [("ImCam", "x")]
