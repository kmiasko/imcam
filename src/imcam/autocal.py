"""Self-calibrating awake detection: learn the crib ROI and the baby's lying appearance from the stream.

AutoDetector replaces the hand-drawn roi/lying_line of tracker.py:

  1. discover: for `auto_cal_s` seconds a whole-frame BgSubDetector runs while a heatmap of where the
     foreground (the baby moving about) keeps showing up is accumulated. Global events (foreground over
     `auto_global_frac` of the frame: an adult, lights) are skipped. Every blob's size also seeds a
     Baseline of the baby's lying appearance (assumed mostly lying while we watch).
  2. run: a BgSubDetector inside the discovered ROI judges "upright" relative to the Baseline (blob height
     and rise above the lying top, in units of the lying blob's width), and refreshes the Baseline from
     its own "lying" observations.
  3. drift: a periodic ORB-feature check of the scene outside the ROI against a reference; a camera shift above `drift_px`
     drops the calibration and goes back to 1. The scene outside the ROI is the reference, so the
     check only runs once an ROI exists.

A hand-pinned roi (roi.json without `calibrated: auto`) is kept; only the Baseline is learned then.
numpy/cv2 are imported lazily, like tracker.py.
"""

import collections
import statistics

from . import debug, tracker
from .tracker import Obs


class Baseline:
    """Robust (rolling median) stats of the baby's lying blob, in frame pixels at cfg frame size."""

    OUTLIER = 3.0  # once ready, samples whose area is off the median by this factor are ignored

    def __init__(self, min_samples=20, maxlen=300):
        self.min_samples = min_samples
        self.samples = collections.deque(maxlen=maxlen)  # (top, width, height, area)

    @property
    def ready(self):
        return len(self.samples) >= self.min_samples

    def add(self, top, width, height, area, seed=False):
        if self.ready and not seed:
            med = self.area
            if not med / self.OUTLIER <= area <= med * self.OUTLIER:
                return
        self.samples.append((top, width, height, area))

    def _med(self, i):
        return statistics.median(s[i] for s in self.samples)

    top = property(lambda self: self._med(0))
    width = property(lambda self: self._med(1))
    height = property(lambda self: self._med(2))
    area = property(lambda self: self._med(3))


class Discovery:
    """Accumulates the foreground heatmap and seeds a Baseline; roi() turns the heat into a polygon."""

    def __init__(self, cfg, np, cv2):
        self.cfg, self.np, self.cv2 = cfg, np, cv2
        self.w, self.h = cfg["frame_w"], cfg["frame_h"]
        self.heat = np.zeros((self.h, self.w), np.float32)
        self.baseline = Baseline(cfg["auto_min_samples"])
        self.n = self.useful = 0

    def feed(self, fg, blob):
        self.n += 1
        if fg is None:
            return
        cv2 = self.cv2
        if cv2.countNonZero(fg) > self.cfg["auto_global_frac"] * self.w * self.h:
            return  # adult / lights: not the baby's footprint
        self.useful += 1
        self.heat += fg > 0
        if blob and blob[4] >= 0.002 * self.w * self.h:
            x, y, w, h, area = blob
            self.baseline.add(y, w, h, area, seed=True)

    def ready(self):
        need = self.cfg["auto_cal_s"] * self.cfg["fps"]
        return self.n >= need and self.baseline.ready and self.roi() is not None

    def roi(self):
        """Normalized polygon: hull of the persistent foreground, grown sideways/downward by
        `auto_roi_margin` and upward by one lying-blob width (an upright baby must stay inside)."""
        cv2, np = self.cv2, self.np
        peak = float(self.heat.max())
        if peak < 2:
            return None
        m = (self.heat >= max(2.0, 0.2 * peak)).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        cnts = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
        cnts = [c for c in cnts if cv2.contourArea(c) >= 0.002 * self.w * self.h]
        if not cnts:
            return None
        pts = np.vstack(cnts)[:, 0, :].astype(np.float32)
        cx, cy = pts.mean(axis=0)
        grow = 1 + self.cfg["auto_roi_margin"]
        pts = np.stack([cx + (pts[:, 0] - cx) * grow, cy + (pts[:, 1] - cy) * grow], 1)
        up = self.baseline.width if self.baseline.ready else 0.3 * self.h
        top = pts[:, 1].min() - up
        lo, hi = pts[:, 0].min(), pts[:, 0].max()
        pts = np.vstack([pts, [[lo, top], [hi, top]]])
        pts = np.clip(pts, 0, [self.w - 1, self.h - 1])
        hull = cv2.convexHull(pts.astype(np.float32))[:, 0, :]
        if cv2.contourArea(hull) < 0.01 * self.w * self.h:
            return None
        return [[float(x) / self.w, float(y) / self.h] for x, y in hull]


class DriftCheck:
    """Camera-move detector: ORB features of the scene *outside* the crib ROI (so the baby moving about
    cannot look like a camera shift) matched against a reference; the median displacement is the shift."""

    MIN_MATCHES = 12  # fewer matches: no verdict (featureless / covered scene)

    def __init__(self, cv2, np, limit_px):
        self.cv2, self.np, self.limit = cv2, np, limit_px
        self.orb = cv2.ORB_create(500)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.ref = None  # (keypoint xy array, descriptors)

    def reset(self):
        self.ref = None

    def _features(self, frame, roi_mask):
        g = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        kps, des = self.orb.detectAndCompute(g, self.cv2.bitwise_not(roi_mask))
        return self.np.array([k.pt for k in kps], self.np.float32), des

    def moved(self, frame, roi_mask):
        pts, des = self._features(frame, roi_mask)
        if self.ref is None:
            self.ref = (pts, des)
            return False
        rpts, rdes = self.ref
        if des is None or rdes is None:
            return False
        m = self.matcher.match(rdes, des)
        if len(m) < self.MIN_MATCHES:
            return False
        d = self.np.array([pts[x.trainIdx] - rpts[x.queryIdx] for x in m])
        return float(self.np.hypot(*self.np.median(d, axis=0))) > self.limit


class AutoDetector(tracker.Detector):
    """Detector that calibrates itself; reports Obs("calibrating") while discovering.
    on_roi(roi) is told each discovered ROI (to persist it); not called for a pinned ROI."""

    def __init__(self, cfg, on_roi=None):
        import cv2
        import numpy as np

        self.cv2, self.np, self.on_roi = cv2, np, on_roi
        # relative mode needs lying_line off even if roi.json has a stale auto one
        self.cfg = dict(cfg, lying_line=None)
        self.pinned = cfg["roi"] if cfg["calibrated"] != "auto" else None
        self.drift = DriftCheck(cv2, np, cfg["drift_px"])
        self.drift_every = max(1, int(cfg["drift_check_s"] * cfg["fps"]))
        self.frames = 0
        self._discover()

    def _discover(self):
        cfg = self.cfg
        self.run = None
        self.inner = tracker.BgSubDetector(cfg)  # whole frame
        self.disc = Discovery(cfg, self.np, self.cv2)
        self.drift.reset()
        debug.autocal("discovering")

    def _settle(self):
        self.drift.reset()
        roi = self.pinned or self.disc.roi()
        base = self.disc.baseline
        self.run = tracker.BgSubDetector(self.cfg, baseline=base, roi=roi)
        debug.autocal("roi=%s baseline top=%.0f w=%.0f" % (roi, base.top, base.width))
        if self.on_roi and not self.pinned:
            self.on_roi(roi)

    def analyze(self, frame):
        cv2, cfg = self.cv2, self.cfg
        if frame.shape[:2] != (cfg["frame_h"], cfg["frame_w"]):
            frame = cv2.resize(frame, (cfg["frame_w"], cfg["frame_h"]))
        self.frames += 1
        if (
            self.run is not None
            and self.frames % self.drift_every == 0
            and self.drift.moved(
                frame, self.run.roi_mask
            )  # first call sets the reference
        ):
            debug.autocal("camera moved, recalibrating")
            self._discover()
            return Obs("reset")
        if self.run is not None:
            obs = self.run.analyze(frame)
            if obs.kind == "reset":
                self.drift.reset()
            return obs
        obs = self.inner.analyze(frame)
        if obs.kind == "reset":
            self.drift.reset()
        if obs.kind not in ("warmup", "reset"):
            self.disc.feed(self.inner.last_fg, self.inner.last_blob)
            if self.disc.ready():
                self._settle()
        return Obs("calibrating")
