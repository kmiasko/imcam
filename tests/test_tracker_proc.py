import pytest

from imcam import cli, relay, tracker_proc


class FakeProc:
    def __init__(self, cmd):
        self.cmd, self.code, self.terminated = cmd, None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


@pytest.fixture
def launched(monkeypatch):
    monkeypatch.setattr(tracker_proc.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(relay, "write_sdp", lambda path, port: path)
    return []


def launcher(launched):
    def launch(cmd):
        launched.append(FakeProc(cmd))
        return launched[-1]

    return launch


def test_command_points_baby_tracker_at_the_track_sdp(launched):
    cmd = tracker_proc.command(record="/rec")
    assert cmd[:3] == ["/bin/baby-tracker", "monitor", "--input"]
    assert cmd[3] == relay.TRACK_SDP
    assert cmd[4:] == ["--record", "/rec"]
    assert "--record" not in tracker_proc.command()


def test_missing_baby_tracker_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(tracker_proc.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="baby-tracker not found"):
        tracker_proc.command()


def test_stop_terminates_and_ensure_running_restarts_a_dead_tracker(launched):
    live = tracker_proc.TrackerProcess(launcher=launcher(launched))
    assert not live.ensure_running()
    launched[0].code = 1
    assert live.ensure_running() and len(launched) == 2
    live.stop()
    assert launched[1].terminated


def test_start_tracker_only_when_tracking_or_recording(launched, monkeypatch):
    monkeypatch.setattr(tracker_proc, "launch", launcher(launched))
    assert cli._start_tracker(None, False) is None
    live = cli._start_tracker("/rec", False)
    assert launched[0].cmd[-2:] == ["--record", "/rec"]
    live.stop()
