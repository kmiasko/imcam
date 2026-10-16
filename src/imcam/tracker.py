"""Baby "awake" tracking for ImCam: background subtraction inside a crib ROI.

Awake = the foreground blob's top edge rises above a calibrated "head level when lying"
line, or its height/width ratio passes a threshold, for `hold_s` seconds in a row.

Needs numpy + OpenCV (installed with the package); imported lazily by cli.py. Config lives in
~/.imcam/roi.json (hand-editable, coordinates normalized to 0..1 of the frame).
"""
import collections
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time

from . import notify, paths

CONFIG_FILE = paths.path("roi.json")
H265_PT = 125
# cli.py forwards each RTP packet to the ports below, one reader each. ffmpeg's rtp input also binds
# port+1 for RTCP, so ports must be at least 2 apart (ffplay in cli.py: 47301 + 47302)
TRACK_PORT = 47304  # tracker's ffmpeg (pipe frames + recording)
SNAP_PORT = 47306   # calibrate's one-frame grab
TRACK_SDP = paths.path("track.sdp")
SNAP_SDP = paths.path("snap.sdp")
SNAP_FLAG = paths.path("snap.want")  # touched by calibrate; forwarder then also feeds SNAP_PORT


def write_sdp(path, port, pt=H265_PT):
    sdp = ("v=0\no=- 0 0 IN IP4 127.0.0.1\ns=ImCam\nc=IN IP4 127.0.0.1\nt=0 0\n"
           "m=video %d RTP/AVP %d\na=rtpmap:%d H265/90000\n" % (port, pt, pt))
    return write_raw_sdp(path, sdp)


def write_raw_sdp(path, sdp):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(sdp)
    return path

DEFAULTS = {
    "roi": None,                # [[x, y], ...] crib polygon, normalized
    "lying_line": None,         # head level when lying: [[x1, y1], [x2, y2]] (tilted ok) or a single y; blob top above it = upright
    "lying_margin": 0.0,        # extra normalized distance above the line required
    "aspect_thresh": 1.5,       # blob height/width above this = upright
    "min_blob_frac": 0.02,      # blob area (fraction of ROI area) below this is ignored
    "outside_frac": 0.02,       # foreground outside ROI (fraction of frame) pauses detection
    "brightness_jump": 25.0,    # global mean-gray / chroma jump that resets the background model
    "warmup_frames": 15,        # frames after a reset before detecting again
    "learning_rate": 0.001,     # MOG2 learning rate (slow: a sitting baby must not be absorbed)
    "hold_s": 5.0,              # upright must persist this long to fire
    "cooldown_s": 600.0,        # minimum time between awake notifications
    "stream_lost_s": 60.0,      # no frames for this long -> "stream lost"
    "frame_w": 640,
    "frame_h": 360,
    "fps": 3,
    "hwaccel": "auto",          # ffmpeg -hwaccel value ("" disables)
    "hook": "",                 # command run on notifications; env IMCAM_EVENT / IMCAM_MESSAGE
}


def load_config(path=None):
    path = path or CONFIG_FILE
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        raise SystemExit("no %s; run: imcam calibrate" % path)
    except ValueError as e:
        raise SystemExit("%s: invalid JSON: %s" % (path, e))
    unknown = set(user) - set(DEFAULTS)
    if unknown:
        raise SystemExit("%s: unknown keys %s" % (path, sorted(unknown)))
    return validate_config(dict(DEFAULTS, **user))


def validate_config(cfg):
    roi = cfg["roi"]
    if (not isinstance(roi, list) or len(roi) < 3
            or not all(isinstance(p, (list, tuple)) and len(p) == 2
                       and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in p) for p in roi)):
        raise SystemExit("roi must be >= 3 [x, y] points, each in 0..1")
    cfg["lying_line"] = norm_line(cfg["lying_line"])
    for k in ("aspect_thresh", "min_blob_frac", "hold_s", "cooldown_s", "stream_lost_s",
              "frame_w", "frame_h", "fps", "brightness_jump", "learning_rate"):
        if not isinstance(cfg[k], (int, float)) or cfg[k] <= 0:
            raise SystemExit("%s must be a positive number" % k)
    return cfg


def norm_line(ll):
    """lying_line as ((x1, y1), (x2, y2)); a bare number y means the horizontal line at y."""
    if isinstance(ll, (int, float)) and not isinstance(ll, bool) and 0 < ll < 1:
        return ((0.0, float(ll)), (1.0, float(ll)))
    if (isinstance(ll, (list, tuple)) and len(ll) == 2
            and all(isinstance(p, (list, tuple)) and len(p) == 2
                    and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in p) for p in ll)
            and ll[0][0] != ll[1][0]):
        return tuple(tuple(map(float, p)) for p in ll)
    raise SystemExit("lying_line must be a y in (0, 1) or two [x, y] points with different x")


def line_y(line, x):
    """y of the (extended) line at normalized x."""
    (x1, y1), (x2, y2) = line
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


def save_config(roi, lying_line, path=None):
    path = path or CONFIG_FILE
    try:
        with open(path) as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    cur["roi"] = [[round(x, 4), round(y, 4)] for x, y in roi]
    cur["lying_line"] = [[round(x, 4), round(y, 4)] for x, y in lying_line]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cur, f, indent=2)
    return path


# --- detection -------------------------------------------------------------------------

Obs = collections.namedtuple("Obs", "kind top ratio area")
# kind: warmup | reset | paused | empty | lying | upright
Obs.__new__.__defaults__ = (None, None, None)


class Detector:
    """Interface: analyze(frame_bgr) -> Obs. A pose model can replace BgSubDetector later."""
    def analyze(self, frame):
        raise NotImplementedError


class BgSubDetector(Detector):
    def __init__(self, cfg):
        import cv2
        import numpy as np
        self.cv2, self.np, self.cfg = cv2, np, cfg
        self.w, self.h = cfg["frame_w"], cfg["frame_h"]
        poly = np.array([[x * self.w, y * self.h] for x, y in cfg["roi"]], np.int32)
        self.roi_mask = np.zeros((self.h, self.w), np.uint8)
        cv2.fillPoly(self.roi_mask, [poly], 255)
        self.roi_area = max(1, int(cv2.countNonZero(self.roi_mask)))
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.prev = None
        self._new_model()

    def _new_model(self):
        self.mog = self.cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=True)
        self.warm = self.cfg["warmup_frames"]

    def _stats(self, frame):
        f = frame.astype(self.np.int16)
        return float(f.mean()), float(self.np.abs(f[:, :, 0] - f[:, :, 2]).mean())  # brightness, chroma

    def analyze(self, frame):
        cv2, np, cfg = self.cv2, self.np, self.cfg
        if frame.shape[:2] != (self.h, self.w):
            frame = cv2.resize(frame, (self.w, self.h))
        stats = self._stats(frame)
        jumped = self.prev is not None and any(abs(a - b) > cfg["brightness_jump"] for a, b in zip(stats, self.prev))
        self.prev = stats
        if jumped:  # IR <-> colour switch or lights: the model is meaningless now
            self._new_model()
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        # while warming up, learn fast; afterwards slowly so a sitting baby is not absorbed
        fg = self.mog.apply(gray, learningRate=-1 if self.warm > 0 else cfg["learning_rate"])
        if jumped:
            return Obs("reset")
        if self.warm > 0:
            self.warm -= 1
            return Obs("warmup")
        fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)[1]  # drop shadows (127)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.kernel)
        outside = cv2.countNonZero(cv2.bitwise_and(fg, cv2.bitwise_not(self.roi_mask)))
        if outside > cfg["outside_frac"] * self.w * self.h:
            return Obs("paused")
        inside = cv2.dilate(cv2.bitwise_and(fg, self.roi_mask), self.kernel, iterations=2)
        cnts = cv2.findContours(inside, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
        if not cnts:
            return Obs("empty")
        c = max(cnts, key=cv2.contourArea)
        area = cv2.contourArea(c) / self.roi_area
        if area < cfg["min_blob_frac"]:
            return Obs("empty")
        x, y, w, h = cv2.boundingRect(c)
        top, ratio = y / self.h, h / max(1, w)
        tx = c[c[:, 0, 1].argmin(), 0, 0] / self.w  # x of the blob's highest point, compared to the line there
        upright = top < line_y(cfg["lying_line"], tx) - cfg["lying_margin"] or ratio > cfg["aspect_thresh"]
        return Obs("upright" if upright else "lying", top, ratio, area)


class Tracker:
    """Debounce / cooldown state machine over observations. update() returns events
    [(name, message, notify)]; times are in the caller's clock (frame timestamps)."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.upright_since = None
        self.fired = False        # alert already sent (or attempted) for this upright episode
        self.alerted = False      # an awake alert is outstanding, so "lying again" is worth logging
        self.last_alert = None

    def update(self, obs, ts):
        k, ev = obs.kind, []
        if k in ("reset", "warmup", "paused", "empty"):
            self.upright_since, self.fired = None, False
        elif k == "lying":
            self.upright_since, self.fired = None, False
            if self.alerted:
                self.alerted = False
                ev.append(("lying_again", "baby is lying down again", False))
        elif k == "upright":
            if self.upright_since is None:
                self.upright_since = ts
            if (not self.fired and ts - self.upright_since >= self.cfg["hold_s"]
                    and (self.last_alert is None or ts - self.last_alert >= self.cfg["cooldown_s"])):
                self.fired = self.alerted = True
                self.last_alert = ts
                ev.append(("awake", "baby is awake (sitting/standing up)", True))
        return ev


class Health:
    """Stream-lost / restored events from frame arrival times (wall clock)."""
    def __init__(self, lost_s):
        self.lost_s, self.last, self.lost = lost_s, None, False

    def frame(self, now):
        self.last = now
        if self.lost:
            self.lost = False
            return [("stream_restored", "stream restored", True)]
        return []

    def check(self, now):
        if self.last is not None and not self.lost and now - self.last >= self.lost_s:
            self.lost = True
            return [("stream_lost", "stream lost (no frames for %ds)" % self.lost_s, True)]
        return []


def make_notifier(cfg):
    hook = shlex.split(cfg.get("hook") or "", posix=os.name != "nt")

    def notify(name, msg, push=True):
        print("%s [%s] %s" % (time.strftime("%H:%M:%S"), name, msg), flush=True)
        if not push:
            return
        notify.send("ImCam", msg)
        if hook:
            try:
                subprocess.Popen(hook, env=dict(os.environ, IMCAM_EVENT=name, IMCAM_MESSAGE=msg))
            except OSError as e:
                print("hook failed: %s" % e, file=sys.stderr)
    return notify


# --- frame sources ---------------------------------------------------------------------

def file_frames(path, cfg):
    """(ts, frame) from a video (ts = media time, sampled to cfg fps) or a single image."""
    import cv2
    w, h = cfg["frame_w"], cfg["frame_h"]
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit("cannot open %s" % path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0
    if src_fps <= 0 or src_fps > 1000:  # image / unknown
        ok, f = cap.read()
        if ok:
            yield 0.0, cv2.resize(f, (w, h))
        return
    step, i, nxt = 1.0 / cfg["fps"], 0, 0.0
    while True:
        ok, f = cap.read()
        if not ok:
            return
        ts = i / src_fps
        i += 1
        if ts + 1e-9 >= nxt:
            nxt += step
            yield ts, cv2.resize(f, (w, h))


def ffmpeg_cmd(cfg, record_dir=None, pipe=True):
    cmd = [paths.require_tool("ffmpeg"), "-loglevel", "error", "-fflags", "+nobuffer+genpts",
           "-protocol_whitelist", "file,udp,rtp"]
    if cfg.get("hwaccel"):
        cmd += ["-hwaccel", cfg["hwaccel"]]
    cmd += ["-i", write_sdp(TRACK_SDP, TRACK_PORT)]
    if record_dir:
        os.makedirs(record_dir, exist_ok=True)
        cmd += ["-map", "0:v", "-c", "copy", "-f", "segment", "-segment_time", "600",
                "-reset_timestamps", "1", "-strftime", "1",
                os.path.join(record_dir, "%Y%m%d-%H%M%S.mkv")]
    if pipe:
        cmd += ["-map", "0:v", "-vf", "fps=%s,scale=%d:%d" % (cfg["fps"], cfg["frame_w"], cfg["frame_h"]),
                "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    return cmd


def pipe_frames(cfg, record_dir, stop):
    """(monotonic ts, frame) from one ffmpeg reading the RTP forward; restarted if it exits."""
    import numpy as np
    w, h = cfg["frame_w"], cfg["frame_h"]
    n = w * h * 3
    while not stop.is_set():
        proc = subprocess.Popen(ffmpeg_cmd(cfg, record_dir), stdout=subprocess.PIPE)
        try:
            while not stop.is_set():
                buf = proc.stdout.read(n)
                if len(buf) < n:
                    break
                yield time.monotonic(), np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        finally:
            proc.kill()
            proc.wait()
        if not stop.wait(2):
            print("ffmpeg exited, restarting", file=sys.stderr)


def run(frames, cfg, notify, live):
    """Feed (ts, frame) pairs through detector + tracker until exhausted (or forever if live)."""
    det, trk, health = BgSubDetector(cfg), Tracker(cfg), Health(cfg["stream_lost_s"])
    q = queue.Queue(maxsize=4)

    def pump():
        try:
            for item in frames:
                if live:
                    try:
                        q.put_nowait(item)
                    except queue.Full:
                        pass
                else:
                    q.put(item)
        except Exception as e:
            print("frame source failed: %r" % e, file=sys.stderr)
        q.put(None)
    threading.Thread(target=pump, daemon=True).start()

    last = None
    while True:
        try:
            item = q.get(timeout=1)
        except queue.Empty:
            if live:
                for ev in health.check(time.monotonic()):
                    notify(*ev)
            continue
        if item is None:
            return
        ts, frame = item
        if live:
            for ev in health.frame(time.monotonic()):
                notify(*ev)
        obs = det.analyze(frame)
        if os.environ.get("IMCAM_DEBUG") and obs.kind != last:
            print("%.1fs obs: %s" % (ts, obs))
        last = obs.kind
        for ev in trk.update(obs, ts):
            if not live:
                ev = (ev[0], "[t=%.1fs] %s" % (ts, ev[1]), False)
            notify(*ev)


class LiveTracker:
    """Background tracking (and/or recording) of the RTP forward; stop() ends it."""
    def __init__(self, record_dir=None, track=True):
        self.stop_ev = threading.Event()
        self.proc = None
        cfg = load_config() if track else dict(DEFAULTS)
        if track:
            self.thread = threading.Thread(
                target=run, args=(pipe_frames(cfg, record_dir, self.stop_ev), cfg, make_notifier(cfg), True),
                daemon=True)
            self.thread.start()
        else:  # recording only
            self.proc = subprocess.Popen(ffmpeg_cmd(cfg, record_dir, pipe=False))

    def stop(self):
        self.stop_ev.set()
        if self.proc:
            self.proc.terminate()


def run_file(path):
    cfg = load_config()
    run(file_frames(path, cfg), cfg, make_notifier(cfg), live=False)


# --- calibration -----------------------------------------------------------------------

def snapshot_live():
    import cv2
    import numpy as np
    cmd = [paths.require_tool("ffmpeg"), "-loglevel", "error", "-protocol_whitelist", "file,udp,rtp",
           "-skip_frame", "nokey", "-i", write_sdp(SNAP_SDP, SNAP_PORT),
           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    open(SNAP_FLAG, "w").close()
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=40).stdout
    except subprocess.TimeoutExpired:
        out = b""
    finally:
        try:
            os.unlink(SNAP_FLAG)
        except OSError:
            pass
    img = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR) if out else None
    if img is None:
        raise SystemExit("no frame from the live stream; run `imcam watch` or `monitor` first")
    return img


def calibrate(input_file=None):
    # opencv-python's bundled Qt only ships the xcb plugin; on Wayland it needs XWayland
    if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    import cv2
    if input_file:
        cap = cv2.VideoCapture(input_file)
        ok, img = cap.read()
        if not ok:
            raise SystemExit("cannot read %s" % input_file)
    else:
        img = snapshot_live()
    h, w = img.shape[:2]
    pts, line, phase = [], [], ["roi"]

    def redraw():
        v = img.copy()
        if len(pts) > 1:
            cv2.polylines(v, [__import__("numpy").array(pts, "int32")], phase[0] != "roi", (0, 255, 0), 2)
        for p in pts:
            cv2.circle(v, tuple(p), 4, (0, 255, 0), -1)
        for p in line:
            cv2.circle(v, tuple(p), 4, (0, 0, 255), -1)
        if len(line) == 2 and line[0][0] != line[1][0]:
            ext = [(x, int(line_y([[a / w, b / h] for a, b in line], x / w) * h)) for x in (0, w)]
            cv2.line(v, ext[0], ext[1], (0, 0, 255), 2)
        msg = {"roi": "click crib corners, Enter when done (u=undo)",
               "line": "click 2 points of the head level when lying (can be tilted), Enter to save (u=undo)"}[phase[0]]
        cv2.putText(v, msg, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.imshow("calibrate", v)

    def on_mouse(ev, x, y, *_):
        if ev == cv2.EVENT_LBUTTONDOWN:
            if phase[0] == "roi":
                pts.append([x, y])
            else:
                if len(line) >= 2:
                    line.clear()
                line.append([x, y])
            redraw()

    cv2.namedWindow("calibrate")
    cv2.setMouseCallback("calibrate", on_mouse)
    redraw()
    while True:
        k = cv2.waitKey(50) & 0xFF
        if k == 27:
            cv2.destroyAllWindows()
            raise SystemExit("cancelled")
        if k == ord("u"):
            if phase[0] == "line":
                if line:
                    line.pop()
                else:
                    phase[0] = "roi"
            elif pts:
                pts.pop()
            redraw()
        elif k in (13, 10):
            if phase[0] == "roi" and len(pts) >= 3:
                phase[0] = "line"
                redraw()
            elif phase[0] == "line" and len(line) == 2 and line[0][0] != line[1][0]:
                break
    cv2.destroyAllWindows()
    path = save_config([(x / w, y / h) for x, y in pts], [(x / w, y / h) for x, y in line])
    print("saved %s" % path)


# --- self test -------------------------------------------------------------------------

def selftest():
    import numpy as np
    cfg = dict(DEFAULTS, roi=[[0.1, 0.3], [0.9, 0.3], [0.9, 0.95], [0.1, 0.95]], lying_line=0.6)
    validate_config(cfg)
    W, H = cfg["frame_w"], cfg["frame_h"]
    rng = np.random.default_rng(1)

    def bg(level=100, color=False):
        f = np.full((H, W, 3), level, np.int16)
        if color:
            f[:, :, 2] += 60
        return np.clip(f + rng.integers(-2, 3, f.shape), 0, 255).astype(np.uint8)

    def blob(f, x0, y0, w, h):
        f[y0:y0 + h, x0:x0 + w] = 220
        return f

    lying = lambda: blob(bg(), 150, 250, 300, 50)       # top 0.69 > line 0.6, ratio 0.17
    upright = lambda: blob(bg(), 280, 110, 70, 190)     # top 0.31 < line

    class Sim:
        def __init__(self, **kw):
            self.cfg = dict(cfg, **kw)
            self.det, self.trk, self.t, self.events = BgSubDetector(self.cfg), Tracker(self.cfg), 0.0, []

        def feed(self, mk, seconds, fps=2):
            for _ in range(int(seconds * fps)):
                for ev in self.trk.update(self.det.analyze(mk()), self.t):
                    self.events.append(ev[0])
                self.t += 1.0 / fps

        def names(self):
            return self.events

    s = Sim()
    s.feed(bg, 15)
    s.feed(lying, 30)
    assert s.names() == [], s.names()                   # lying blob: no alert
    s.feed(upright, 3)
    assert s.names() == [], s.names()                   # < hold_s: no alert
    s.feed(lying, 5)
    s.feed(upright, 8)
    assert s.names() == ["awake"], s.names()            # persists >= 5 s: alert
    s.feed(lying, 5)
    assert s.names() == ["awake", "lying_again"], s.names()
    s.feed(upright, 8)
    assert s.names() == ["awake", "lying_again"], s.names()  # cooldown holds
    s2 = Sim(cooldown_s=1.0)
    s2.feed(bg, 15)
    s2.feed(lying, 20)
    s2.feed(upright, 8)
    s2.feed(lying, 5)
    s2.feed(upright, 8)
    assert s2.names() == ["awake", "lying_again", "awake"], s2.names()
    s3 = Sim()                                           # foreground outside ROI pauses detection
    s3.feed(bg, 15)
    s3.feed(lying, 20)
    s3.feed(lambda: blob(upright(), 5, 5, 80, 60), 10)
    assert s3.names() == [], s3.names()
    assert s3.det.analyze(blob(upright(), 5, 5, 80, 60)).kind == "paused"
    s4 = Sim()                                           # brightness/colour jump resets, no alert
    s4.feed(bg, 15)
    s4.feed(lying, 20)
    kinds = []
    for _ in range(4):
        kinds.append(s4.det.analyze(blob(bg(220, True), 150, 250, 300, 50)).kind)
    assert kinds[0] == "reset" and kinds[1:] == ["warmup"] * 3, kinds
    s4.feed(lambda: blob(bg(220, True), 150, 250, 300, 50), 20)
    assert s4.names() == [], s4.names()
    # tilted line: left end y=0.8, right end y=0.4; blob top 0.5 is above it on the left, below it on the right
    tilt = [[0.0, 0.8], [1.0, 0.4]]
    s5 = Sim(lying_line=tilt)
    s5.feed(bg, 15)
    left = lambda: blob(bg(), 100, 180, 80, 40)   # ratio 0.5, top 0.5 < line_y(0.28)=0.69
    right = lambda: blob(bg(), 480, 220, 80, 40)  # top 0.61 > line_y(0.8)=0.48
    assert s5.det.analyze(left()).kind == "upright"
    s5.feed(bg, 10)
    assert s5.det.analyze(right()).kind == "lying"
    assert line_y(norm_line(0.6), 0.3) == 0.6
    h = Health(60)                                       # stream health
    assert h.frame(0) == [] and h.check(59) == []
    assert [e[0] for e in h.check(61)] == ["stream_lost"] and h.check(70) == []
    assert [e[0] for e in h.frame(80)] == ["stream_restored"] and h.frame(81) == []
    print("tracker selftest ok")


if __name__ == "__main__":
    selftest()
