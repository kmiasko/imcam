"""Runs the separate `babywatch` program on the relay's tracking stream and turns its events into alerts.

babywatch reads video only through PyAV from files or stream URLs, and it cannot open the relay's RTP .sdp. A
bridge ffmpeg therefore reads TRACK_SDP and copies the H.265 into MPEG-TS on a local UDP port (BRIDGE_URL), which
`babywatch live --rtsp` reads. The same ffmpeg also writes the --record segments.

babywatch prints a JSON line whenever its notification state changes; "notify": true means the baby is upright
in the crib with no adult in view. Alerts turns that into an "awake" alert, at most once per
IMCAM_NOTIFY_COOLDOWN seconds. Health raises "stream lost" when the relay has had no video for
IMCAM_STREAM_LOST_S seconds and "stream restored" when it comes back. Delivery is notify.py.

A supervisor thread restarts the bridge or babywatch when either exits and checks the stream's health.
`launch` is the seam tests replace (it returns a Popen-like object)."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

from . import children, config, notify, paths, relay

TOOL = "babywatch"
BRIDGE_PORT = 47308
BRIDGE_URL = "udp://127.0.0.1:%d" % BRIDGE_PORT
CHECK_S = 2.0  # supervisor period


def bridge_command(record=None):
    """ffmpeg argv: TRACK_SDP -> MPEG-TS on BRIDGE_PORT, plus 10-minute .mkv segments in `record`."""
    cmd = [
        paths.require_tool("ffmpeg"),
        "-loglevel",
        "error",
        "-fflags",
        "+nobuffer+genpts",
        "-protocol_whitelist",
        "file,udp,rtp",
        "-i",
        relay.write_sdp(relay.TRACK_SDP, relay.TRACK_PORT),
        "-map",
        "0:v",
        "-c",
        "copy",
        "-f",
        "mpegts",
        BRIDGE_URL + "?pkt_size=1316",
    ]
    if record:
        os.makedirs(record, exist_ok=True)
        cmd += [
            "-map",
            "0:v",
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


def launch(cmd, stdout=None):
    return children.popen(cmd, stdout=stdout, text=True)


class Alerts:
    """babywatch events -> deliver(name, msg, push) calls, with a cooldown between pushed "awake" alerts."""

    def __init__(self, deliver, cooldown_s, mono=time.monotonic):
        self.deliver, self.cooldown_s, self.mono = deliver, cooldown_s, mono
        self.last_alert, self.up = None, False

    def line(self, line):
        """Act on one babywatch stdout line; returns the parsed event or None for non-JSON lines."""
        try:
            ev = json.loads(line)
        except ValueError:
            return None
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
    """The bridge ffmpeg and babywatch plus the supervisor thread; stop() ends all of them.

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
    ):
        self.record, self.rx, self.mono = record, rx, mono
        self.launcher = launcher or launch
        self.deliver = deliver or notify.make_notifier(
            config.optional("IMCAM_NOTIFY_HOOK")
        )
        self.alerts = Alerts(
            self.deliver, float(config.optional("IMCAM_NOTIFY_COOLDOWN")), mono
        )
        self.health = Health(float(config.optional("IMCAM_STREAM_LOST_S")))
        self._lock, self._stopping = threading.Lock(), threading.Event()
        babywatch_command()  # fail on a missing babywatch before the bridge is started
        self.bridge = self.launcher(bridge_command(record))
        self.watch = self._start_babywatch()
        self._thread = None
        if supervise:
            # children restarted by this thread are tied to it (children.py: PDEATHSIG); it lives until stop()
            self._thread = threading.Thread(target=self._supervise, daemon=True)
            self._thread.start()

    def _start_babywatch(self):
        proc = self.launcher(babywatch_command(), stdout=subprocess.PIPE)
        if proc.stdout is not None:
            threading.Thread(
                target=self._read, args=(proc.stdout,), daemon=True
            ).start()
        return proc

    def _read(self, stream):
        for line in stream:
            self.alerts.line(line)

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
            self.deliver(*ev)

    def _video_flowing(self):
        """ffmpeg's RTP input exits ~10 s after the video stops, and a bridge started with no video fails at
        once (the recording can't learn the frame size). So restart it only while video arrives."""
        if self.rx is None:
            return True
        last = self.rx()
        return last is not None and self.mono() - last < 2 * CHECK_S

    def ensure_running(self):
        """Start babywatch again if it exited, and the bridge too once video flows; True when it did."""
        with self._lock:
            if self._stopping.is_set():
                return False
            restarted = False
            code = self.bridge.poll()
            if code is not None and self._video_flowing():
                print("bridge ffmpeg exited (%s), restarting" % code, file=sys.stderr)
                self.bridge = self.launcher(bridge_command(self.record))
                restarted = True
            code = self.watch.poll()
            if code is not None:
                print("babywatch exited (%s), restarting" % code, file=sys.stderr)
                self.watch = self._start_babywatch()
                restarted = True
            return restarted

    def stop(self, timeout=10):
        """End the supervisor, babywatch, then the bridge (ffmpeg finishes the recording's trailer)."""
        with self._lock:
            self._stopping.set()
        if self._thread:
            self._thread.join(timeout)
        for proc in (self.watch, self.bridge):
            children.stop(proc, timeout)
