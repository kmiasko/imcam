import io
import json

import pytest

from imcam import cli, paths, relay, watcher


class FakeProc:
    def __init__(self, cmd, lines=()):
        self.cmd, self.code, self.terminated = cmd, None, False
        self.stdout = io.StringIO("".join(lines))

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


@pytest.fixture
def launched(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(paths, "require_tool", lambda name: "/bin/" + name)
    monkeypatch.setattr(relay, "write_sdp", lambda path, port, audio=None: path)
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path))  # no user config.env / .env
    monkeypatch.chdir(tmp_path)
    return []


def launcher(launched, lines=(), analyzer_lines=()):
    def launch(cmd, stdout=None):
        out = ()
        if cmd[0].endswith("babywatch"):
            out = lines
        elif cmd[-1] == watcher.AUDIO_URL:
            out = analyzer_lines
        launched.append(FakeProc(cmd, out))
        return launched[-1]

    return launch


def outputs(cmd):
    """{output url/path: [map specs]} of an ffmpeg argv with -map before each output."""
    out, maps = {}, []
    i = cmd.index("-i") + 2
    while i < len(cmd):
        if cmd[i] == "-map":
            maps.append(cmd[i + 1])
            i += 2
        elif cmd[i].startswith("-"):
            i += 2
        else:
            out[cmd[i]], maps = maps, []
            i += 1
    return out


def test_bridge_reads_the_track_sdp_and_feeds_babywatch(launched, tmp_path):
    cmd = watcher.bridge_command()
    assert cmd[0] == "/bin/ffmpeg"
    assert cmd[cmd.index("-i") + 1] == relay.TRACK_SDP
    assert outputs(cmd)[watcher.BRIDGE_URL + "?pkt_size=1316"] == ["0:v"]
    assert "segment" not in cmd
    rec = watcher.bridge_command(record=str(tmp_path / "rec"))
    assert rec[-1].startswith(str(tmp_path / "rec"))
    assert (tmp_path / "rec").is_dir()
    assert watcher.babywatch_command()[:3] == ["/bin/babywatch", "live", "--rtsp"]
    assert watcher.babywatch_command()[3].startswith(watcher.BRIDGE_URL)


def test_bridge_exposes_audio_and_records_it_when_present(launched, tmp_path):
    outs = outputs(watcher.bridge_command(record=str(tmp_path / "rec")))
    assert outs[watcher.BRIDGE_URL + "?pkt_size=1316"] == ["0:v"]
    assert outs[watcher.AUDIO_URL + "?pkt_size=1316"] == ["0:a?"]
    seg = [k for k in outs if k.endswith(".mkv")]
    assert outs[seg[0]] == ["0:v", "0:a?"]


def test_analyzer_is_off_by_default(launched):
    assert watcher.analyzer_command() is None
    w = watcher.WatchProcess(
        launcher=launcher(launched), deliver=lambda *e: None, supervise=False
    )
    assert w.analyzer is None and len(launched) == 2
    w.stop()


def test_analyzer_events_are_delivered_and_bad_lines_ignored(launched, monkeypatch):
    monkeypatch.setenv("IMCAM_AUDIO_ANALYZER", "babylisten live --db -40")
    lines = [
        "starting\n",
        "[1]\n",
        json.dumps({"message": "no event"}) + "\n",
        json.dumps({"event": "noise", "message": "loud for 5 s", "push": True}) + "\n",
        json.dumps({"event": "breathing_normal"}) + "\n",
    ]
    got = []
    w = watcher.WatchProcess(
        launcher=launcher(launched, analyzer_lines=lines),
        deliver=lambda *e: got.append(e),
        supervise=False,
    )
    assert launched[2].cmd == [
        "babylisten",
        "live",
        "--db",
        "-40",
        watcher.AUDIO_URL,
    ]
    w._read_analyzer(launched[2].stdout)
    assert got == [
        ("noise", "loud for 5 s", True),
        ("breathing_normal", "breathing_normal", False),
    ]
    launched[2].code = 1
    assert w.ensure_running() and launched[3].cmd == launched[2].cmd
    w.stop()
    assert launched[3].terminated


def test_missing_babywatch_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(watcher.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="babywatch not found"):
        watcher.babywatch_command()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ev(notify, adult=False, p=0.99):
    return json.dumps(
        {"ts": "t", "notify": notify, "state": "upright", "adult": adult, "p": p}
    )


def test_alerts_push_once_per_cooldown_and_log_the_rest():
    got, clock = [], Clock()
    a = watcher.Alerts(lambda *e: got.append(e), cooldown_s=600, mono=clock)
    assert a.line("not json") is None and a.line("[1]") is None
    a.line(ev(True))
    a.line(ev(True))  # no change: babywatch repeats only with --verbose
    a.line(ev(False, adult=True))
    clock.t += 60
    a.line(ev(True))
    clock.t += 600
    a.line(ev(False))
    a.line(ev(True))
    assert [(n, push) for n, _, push in got] == [
        ("awake", True),
        ("down", False),
        ("awake", False),
        ("down", False),
        ("awake", True),
    ]
    assert "adult in view" in got[1][1] and "within cooldown" in got[2][1]


def test_health_reports_lost_and_restored_once():
    h = watcher.Health(lost_s=60)
    assert h.check(None, 500) == []  # nothing received yet: no alarm
    assert h.check(100, 150) == []
    assert [e[0] for e in h.check(100, 160)] == ["stream_lost"]
    assert h.check(100, 999) == []
    assert [e[0] for e in h.check(1000, 1001)] == ["stream_restored"]
    assert h.check(1000, 1002) == []


def test_babywatch_output_is_turned_into_alerts(launched):
    got = []
    w = watcher.WatchProcess(
        launcher=launcher(launched), deliver=lambda *e: got.append(e), supervise=False
    )
    w._read(io.StringIO(ev(True) + "\n"))
    assert got and got[0][0] == "awake" and got[0][2] is True
    w.stop()


def test_supervisor_restarts_dead_processes_and_reports_stream_health(launched):
    got, clock, rx = [], Clock(), [None]
    w = watcher.WatchProcess(
        launcher=launcher(launched),
        deliver=lambda *e: got.append(e),
        rx=lambda: rx[0],
        mono=clock,
        supervise=False,
    )
    assert not w.ensure_running()
    launched[0].code = 1  # bridge
    launched[1].code = 1  # babywatch
    assert w.ensure_running() and len(launched) == 3  # no video yet: only babywatch
    assert not w.ensure_running()
    rx[0] = clock.t
    assert w.ensure_running() and len(launched) == 4  # video flows: bridge too
    clock.t += 61
    w.check_health()
    assert [e[0] for e in got] == ["stream_lost"]
    w.stop()
    assert launched[2].terminated and launched[3].terminated
    assert not w.ensure_running()  # never restarts after stop()


def test_supervisor_thread_stops_with_the_watcher(launched, monkeypatch):
    monkeypatch.setattr(watcher, "CHECK_S", 0.01)
    w = watcher.WatchProcess(launcher=launcher(launched), deliver=lambda *e: None)
    w.stop()
    assert not w._thread.is_alive()


def test_start_tracker_only_when_tracking_or_recording(launched, monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "launch", launcher(launched))
    assert cli._start_tracker(None, False) is None
    live = cli._start_tracker(str(tmp_path), False)
    assert launched[0].cmd[-1].startswith(str(tmp_path))
    live.stop()


def recorded(launched, **kw):
    rec = []
    w = watcher.WatchProcess(
        launcher=launcher(launched, **kw),
        deliver=lambda *e: None,
        record_event=lambda *e: rec.append(e),
        supervise=False,
    )
    return w, rec


def test_babywatch_lines_and_alerts_are_recorded(launched):
    w, rec = recorded(launched)
    rec.clear()
    w._read(io.StringIO(ev(True) + "\n" + ev(True) + "\nnot json\n"))
    assert [(r[0], r[1]) for r in rec] == [
        ("babywatch", "state"),
        ("imcam", "awake"),
        ("babywatch", "state"),  # no notify change: still recorded
    ]
    assert rec[0][2] == "upright" and rec[0][4]["p"] == 0.99
    assert rec[1][3] is True
    w.stop()


def test_analyzer_lines_are_recorded_once_with_extra_fields(launched, monkeypatch):
    monkeypatch.setenv("IMCAM_AUDIO_ANALYZER", "/usr/bin/babylisten live")
    line = {"event": "cry", "message": "crying", "push": True, "db": -20, "f0_hz": 410}
    w, rec = recorded(launched)
    rec.clear()
    w._read_analyzer(io.StringIO(json.dumps(line) + "\n"))
    assert rec == [("babylisten", "cry", "crying", True, line)]
    w.stop()


def test_session_and_restart_markers_and_health_are_recorded(launched):
    clock, rx = Clock(), [None]
    rec = []
    w = watcher.WatchProcess(
        launcher=launcher(launched),
        deliver=lambda *e: None,
        record_event=lambda *e: rec.append(e),
        rx=lambda: rx[0],
        mono=clock,
        supervise=False,
    )
    launched[1].code = 3
    assert w.ensure_running()
    rx[0] = clock.t
    clock.t += 61
    w.check_health()
    w.stop()
    w.stop()
    assert [(r[0], r[1]) for r in rec] == [
        ("imcam", "session_start"),
        ("imcam", "restart"),
        ("imcam", "stream_lost"),
        ("imcam", "session_stop"),
    ]
    assert rec[1][2] == "babywatch exited (3)" and rec[2][3] is True


def test_default_event_log_is_in_imcam_home(launched, tmp_path):
    w = watcher.WatchProcess(
        launcher=launcher(launched), deliver=lambda *e: None, supervise=False
    )
    w.stop()
    assert (tmp_path / "events.db").exists()
