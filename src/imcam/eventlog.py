"""SQLite log of every event imcam sees, in ~/.imcam/events.db, for later viewing (e.g. one night of sleep).

One table, `events`: ts (unix time when imcam received it), source ('imcam', 'babywatch' or the analyzer program
name), name, message, push (0/1) and data (the producer's raw JSON object, or NULL). The DB is created on the first
write, in WAL mode so other readers (the sqlite3 CLI, a viewer) can read while imcam writes.

record() is called from several threads (the babywatch and analyzer readers, the supervisor) and, like notify.py,
never raises: a failed write prints one `eventlog:` line on stderr and imcam keeps running."""

import json
import os
import sqlite3
import sys
import threading
import time

from . import paths

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY,
  ts      REAL NOT NULL,
  source  TEXT NOT NULL,
  name    TEXT NOT NULL,
  message TEXT,
  push    INTEGER NOT NULL DEFAULT 0,
  data    TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class Log:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self._conn, self._lock, self._warned = None, threading.Lock(), False

    def _open(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, check_same_thread=False
        )
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def record(self, source, name, message="", push=False, data=None):
        try:
            blob = None if data is None else json.dumps(data)
            with self._lock:
                if self._conn is None:
                    self._conn = self._open()
                self._conn.execute(
                    "INSERT INTO events (ts, source, name, message, push, data)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (self.clock(), source, name, message, int(bool(push)), blob),
                )
        except (sqlite3.Error, OSError, TypeError, ValueError) as e:
            if not self._warned:
                self._warned = True
                print("eventlog: %s: %s" % (self.path, e), file=sys.stderr)

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None


def default():
    return Log(paths.path("events.db"))
