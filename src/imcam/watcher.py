"""Runs the separate `babywatch` program on the relay's tracking stream and turns its events into alerts.

babywatch reads video only through PyAV from files or stream URLs, and it cannot open the relay's RTP .sdp. A
bridge ffmpeg therefore reads TRACK_SDP and copies the H.265 into MPEG-TS on a local UDP port (BRIDGE_URL), which
`babywatch live --rtsp` reads. The same ffmpeg copies the camera's audio (Opus) into MPEG-TS on AUDIO_URL, the
audio stream for external consumers, and writes the --record segments (video and audio). UDP output needs no
reader: with nobody on AUDIO_URL the packets are dropped.

babywatch prints a JSON line whenever its notification state changes; "notify": true means the baby is upright
in the crib with no adult in view. Alerts turns that into an "awake" alert, at most once per
IMCAM_NOTIFY_COOLDOWN seconds. Health raises "stream lost" when the relay has had no video for
IMCAM_STREAM_LOST_S seconds and "stream restored" when it comes back. Delivery is notify.py.

IMCAM_AUDIO_ANALYZER, when set, is a command line run with AUDIO_URL appended. It prints JSON lines
{"event": name, "message": text, "push": bool}; each one is delivered as is (the analyzer owns any cooldown).

Every babywatch line, analyzer line and imcam alert, plus session_start / session_stop / restart markers, is also
written to the event log (eventlog.py, ~/.imcam/events.db) through `record_event`, the seam tests replace.

A supervisor thread restarts the bridge, babywatch or the analyzer when one exits and checks the stream's health.
`launch` is the seam tests replace (it returns a Popen-like object)."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

from . import children, config, eventlog, notify, paths, relay

TOOL = "babywatch"
BRIDGE_PORT = 47308
BRIDGE_URL = "udp://127.0.0.1:%d" % BRIDGE_PORT
AUDIO_PORT = 47314
AUDIO_URL = "udp://127.0.0.1:%d" % AUDIO_PORT
CHECK_S = 2.0  # supervisor period


def bridge_command(record=None):
    """ffmpeg argv: TRACK_SDP video -> MPEG-TS on BRIDGE_PORT, audio -> MPEG-TS on AUDIO_PORT, plus 10-minute
    .mkv segments (video and audio) in `record`."""
    cmd = [
        paths.require_tool("ffmpeg"),
        "-loglevel",
        "error",
        "-fflags",
        "+nobuffer+genpts",
        "-protocol_whitelist",
        "file,udp,rtp",
        "-i",
        relay.write_sdp(relay.TRACK_SDP, relay.TRACK_PORT, relay.AUDIO),
        "-map",
        "0:v",
        "-c",
        "copy",
        "-f",
        "mpegts",
        BRIDGE_URL + "?pkt_size=1316",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-f",
        "mpegts",
        AUDIO_URL + "?pkt_size=1316",
    ]
    if record:
        os.makedirs(record, exist_ok=True)
        cmd += [
            "-map",
            "0:v",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "600",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            os.path.join(record, "%Y%m%d-%H%M%S.mkv"),
        ]
    return cmd


def babywatch_command():
    """babywatch argv reading the bridge; exits with install help if it is missing."""
    exe = shutil.which(TOOL)
    if not exe:
        raise SystemExit(
            "%s not found on PATH; install it (see its docs/architecture-and-usage.md) "
            "and check with `imcam doctor`" % TOOL
        )
    return [exe, "live", "--rtsp", BRIDGE_URL + "?overrun_nonfatal=1"]


def analyzer_command():
    """IMCAM_AUDIO_ANALYZER argv with AUDIO_URL appended, or None when it is not set."""
    argv = shlex.split(config.optional("IMCAM_AUDIO_ANALYZER"), posix=os.name != "nt")
    return argv + [AUDIO_URL] if argv else None


def launch(cmd, stdout=None):
    return children.popen(cmd, stdout=stdout, text=True)


class Alerts:
    """babywatch events -> deliver(name, msg, push) calls, with a cooldown between pushed "awake" alerts."""

    def __init__(self, deliver, cooldown_s, mono=time.monotonic):
        self.deliver, self.cooldown_s, self.mono = deliver, cooldown_s, mono
        self.last_alert, self.up = None, False

    def line(self, line):
        """Act on one babywatch stdout line; returns the parsed event or None for non-JSON lines."""
        return self.event(parse(line))

    def event(self, ev):
        """Act on one parsed babywatch line (a dict, or None); returns it, or None when it has no "notify"."""
        if not isinstance(ev, dict) or "notify" not in ev:
            return None
        if ev["notify"] and not self.up:
            now = self.mono()
            push = self.last_alert is None or now - self.last_alert >= self.cooldown_s
            if push:
                self.last_alert = now
            msg = "baby is up (sitting/standing, p=%s)" % ev.get("p")
            self.deliver("awake", msg if push else msg + ", within cooldown", push)
        elif not ev["notify"] and self.up:
            why = "adult in view" if ev.get("adult") else "lying down or out of view"
            self.deliver("down", "baby no longer up (%s)" % why, False)
        self.up = bool(ev["notify"])
        return ev


def parse(line):
    """The JSON object on one stdout line, or None."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def analyzer_event(line):
    """(name, message, push, raw dict) from one analyzer stdout line, or None for anything else."""
    ev = parse(line)
    if ev is None or not isinstance(ev.get("event"), str):
        return None
    return (
        ev["event"],
        str(ev.get("message") or ev["event"]),
        ev.get("push") is True,
        ev,
    )


class Health:
    """Stream-lost / restored events from the time of the latest video packet (monotonic clock)."""

    def __init__(self, lost_s):
        self.lost_s, self.lost, self.seen = lost_s, False, None

    def check(self, last_rx, now):
        if last_rx is not None and last_rx != self.seen:
            self.seen = last_rx
        if self.seen is None:  # nothing yet: the first call may still be connecting
            return []
        if self.lost and now - self.seen < self.lost_s:
            self.lost = False
            return [("stream_restored", "stream restored", True)]
        if not self.lost and now - self.seen >= self.lost_s:
            self.lost = True
            return [
                ("stream_lost", "stream lost (no video for %ds)" % self.lost_s, True)
            ]
        return []


class WatchProcess:
    """The bridge ffmpeg, babywatch, the optional audio analyzer and the supervisor thread; stop() ends all.

    rx() returns the monotonic time of the latest video packet (relay.Relay.last_rx) or None.
    supervise=False leaves out the thread (tests call ensure_running() / check_health() directly)."""

    def __init__(
        self,
        record=None,
        rx=None,
        launcher=None,
        deliver=None,
        mono=time.monotonic,
        supervise=True,
        record_event=None,
    ):
        self.record, self.rx, self.mono = record, rx, mono
        self.launcher = launcher or launch
        self.deliver = deliver or notify.make_notifier(
            config.optional("IMCAM_NOTIFY_HOOK")
        )
        self._log = None if record_event else eventlog.default()
        self.record_event = record_event or self._log.record
        self.alerts = Alerts(
            self._alert, float(config.optional("IMCAM_NOTIFY_COOLDOWN")), mono
        )
        self.health = Health(float(config.optional("IMCAM_STREAM_LOST_S")))
        self._lock, self._stopping = threading.Lock(), threading.Event()
        babywatch_command()  # fail on a missing babywatch before the bridge is started
        self.analyzer_cmd = analyzer_command()
        self.record_event("imcam", "session_start", "monitor started")
        self.bridge = self.launcher(bridge_command(record))
        self.watch = self._start_babywatch()
        self.analyzer = self._start_analyzer() if self.analyzer_cmd else None
        self._thread = None
        if supervise:
            # children restarted by this thread are tied to it (children.py: PDEATHSIG); it lives until stop()
            self._thread = threading.Thread(target=self._supervise, daemon=True)
            self._thread.start()

    def _alert(self, name, msg, push):
        self.record_event("imcam", name, msg, push)
        self.deliver(name, msg, push)

    def _restarted(self, msg):
        print(msg + ", restarting", file=sys.stderr)
        self.record_event("imcam", "restart", msg)

    def _start_reader(self, cmd, read):
        proc = self.launcher(cmd, stdout=subprocess.PIPE)
        if proc.stdout is not None:
            threading.Thread(target=read, args=(proc.stdout,), daemon=True).start()
        return proc

    def _start_babywatch(self):
        return self._start_reader(babywatch_command(), self._read)

    def _start_analyzer(self):
        return self._start_reader(self.analyzer_cmd, self._read_analyzer)

    def _read(self, stream):
        for line in stream:
            ev = parse(line)
            if ev is not None:
                self.record_event(
                    "babywatch", "state", str(ev.get("state", "")), False, ev
                )
            self.alerts.event(ev)

    def _read_analyzer(self, stream):
        for line in stream:
            ev = analyzer_event(line)
            if ev:
                name, msg, push, raw = ev
                source = os.path.basename(self.analyzer_cmd[0])
                self.record_event(source, name, msg, push, raw)
                self.deliver(name, msg, push)

    def _supervise(self):
        while not self._stopping.wait(CHECK_S):
            try:
                self.ensure_running()
                self.check_health()
            except (
                Exception
            ) as e:  # keep supervising; a failed restart is retried next tick
                print("watcher: %s" % e, file=sys.stderr)

    def check_health(self):
        if self.rx is None:
            return
        for ev in self.health.check(self.rx(), self.mono()):
            self._alert(*ev)

    def _video_flowing(self):
        """ffmpeg's RTP input exits ~10 s after the video stops, and a bridge started with no video fails at
        once (the recording can't learn the frame size). So restart it only while video arrives."""
        if self.rx is None:
            return True
        last = self.rx()
        return last is not None and self.mono() - last < 2 * CHECK_S

    def ensure_running(self):
        """Start babywatch or the analyzer again if it exited, and the bridge too once video flows; True when
        it did."""
        with self._lock:
            if self._stopping.is_set():
                return False
            restarted = False
            code = self.bridge.poll()
            if code is not None and self._video_flowing():
                self._restarted("bridge ffmpeg exited (%s)" % code)
                self.bridge = self.launcher(bridge_command(self.record))
                restarted = True
            code = self.watch.poll()
            if code is not None:
                self._restarted("babywatch exited (%s)" % code)
                self.watch = self._start_babywatch()
                restarted = True
            code = self.analyzer.poll() if self.analyzer else None
            if code is not None:
                self._restarted("audio analyzer exited (%s)" % code)
                self.analyzer = self._start_analyzer()
                restarted = True
            return restarted

    def stop(self, timeout=10):
        """End the supervisor, babywatch and the analyzer, then the bridge (ffmpeg finishes the recording's
        trailer)."""
        with self._lock:
            if self._stopping.is_set():
                return
            self._stopping.set()
        if self._thread:
            self._thread.join(timeout)
        for proc in (self.watch, self.analyzer, self.bridge):
            if proc:
                children.stop(proc, timeout)
        self.record_event("imcam", "session_stop", "monitor stopped")
        if self._log:
            self._log.close()
