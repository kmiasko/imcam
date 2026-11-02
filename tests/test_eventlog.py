import json
import sqlite3
import threading

from imcam import eventlog, paths


def rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT ts, source, name, message, push, data FROM events ORDER BY id"
        ).fetchall()


def test_first_write_creates_dir_file_and_schema(tmp_path):
    path = tmp_path / "a" / "b" / "events.db"
    log = eventlog.Log(str(path), clock=lambda: 12.5)
    assert not path.exists()
    log.record("imcam", "session_start", "monitor started")
    log.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert rows(path) == [(12.5, "imcam", "session_start", "monitor started", 0, None)]


def test_data_round_trips_as_json(tmp_path):
    path = str(tmp_path / "events.db")
    log = eventlog.Log(path)
    raw = {"event": "cry", "db": -21.5, "rate_bpm": None, "ts": "t"}
    log.record("babylisten", "cry", "crying", True, raw)
    log.close()
    ((_, source, name, msg, push, data),) = rows(path)
    assert (source, name, msg, push) == ("babylisten", "cry", "crying", 1)
    assert json.loads(data) == raw


def test_concurrent_writes_all_land(tmp_path):
    path = str(tmp_path / "events.db")
    log = eventlog.Log(path)

    def write(i):
        for j in range(50):
            log.record("t%d" % i, "n", str(j))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    assert len(rows(path)) == 200


def test_unwritable_path_never_raises_and_warns_once(tmp_path, capsys):
    (tmp_path / "file").write_text("x")
    log = eventlog.Log(str(tmp_path / "file" / "events.db"))
    log.record("imcam", "a")
    log.record("imcam", "b")
    log.close()
    err = capsys.readouterr().err
    assert err.count("eventlog:") == 1


def test_default_follows_imcam_home():
    assert eventlog.default().path == paths.path("events.db")
