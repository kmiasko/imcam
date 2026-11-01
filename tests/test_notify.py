from imcam import config, notify


def test_backend_is_known():
    assert notify.backend() in ("notify-send", "osascript", "windows", None)


class Recorder:
    def __init__(self, hook_error=None):
        self.logs, self.toasts, self.hooks, self.hook_error = [], [], [], hook_error

    def notifier(self, hook=""):
        return notify.make_notifier(
            hook,
            desktop=lambda t, m: self.toasts.append((t, m)),
            run_hook=self._hook,
            log=self.logs.append,
        )

    def _hook(self, argv, env):
        if self.hook_error:
            raise self.hook_error
        self.hooks.append((argv, env["IMCAM_EVENT"], env["IMCAM_MESSAGE"]))


def test_push_event_logs_toasts_and_runs_hook():
    r = Recorder()
    r.notifier("say hi")("awake", "baby is up")
    assert len(r.logs) == 1 and r.logs[0].endswith("[awake] baby is up")
    assert r.toasts == [("ImCam", "baby is up")]
    assert r.hooks == [(["say", "hi"], "awake", "baby is up")]


def test_non_push_event_is_only_logged():
    r = Recorder()
    r.notifier("say hi")("down", "baby no longer up", False)
    assert len(r.logs) == 1 and r.toasts == [] and r.hooks == []


def test_no_hook_configured_means_no_hook_call():
    r = Recorder()
    r.notifier("")("stream_lost", "stream lost")
    assert r.toasts and r.hooks == []


def test_hook_failure_is_logged_not_raised():
    r = Recorder(hook_error=OSError("no such command"))
    r.notifier("nope")("awake", "x")
    assert r.toasts == [("ImCam", "x")]
    assert r.logs[-1] == "hook failed: no such command"


def test_default_desktop_adapter_is_send(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg: sent.append((title, msg)))
    notify.make_notifier("", log=lambda line: None)("awake", "x")
    assert sent == [("ImCam", "x")]


def test_optional_settings_have_defaults_and_read_config_env(monkeypatch, tmp_path):
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMCAM_NOTIFY_COOLDOWN", raising=False)
    assert config.optional("IMCAM_NOTIFY_COOLDOWN") == "600"
    (tmp_path / "config.env").write_text("IMCAM_NOTIFY_COOLDOWN=30\n")
    assert config.optional("IMCAM_NOTIFY_COOLDOWN") == "30"
    monkeypatch.setenv("IMCAM_NOTIFY_COOLDOWN", "5")
    assert config.optional("IMCAM_NOTIFY_COOLDOWN") == "5"
