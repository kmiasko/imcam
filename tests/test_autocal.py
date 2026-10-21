import numpy as np
import pytest

from imcam import autocal, tracker
from imcam.tracker import Tracker

CFG = tracker.validate_config(dict(tracker.DEFAULTS, fps=2, auto_cal_s=30.0))
W, H = CFG["frame_w"], CFG["frame_h"]
RNG = np.random.default_rng(2)
# static scene structure (room, bars) so a camera shift is visible to the drift check
TEX = np.zeros((H, W), np.int16)
for _ in range(150):
    x, y = RNG.integers(0, W - 40), RNG.integers(0, H - 40)
    TEX[y : y + RNG.integers(8, 40), x : x + RNG.integers(8, 40)] = RNG.integers(
        -60, 60
    )


def bg(level=100):
    f = np.full((H, W, 3), level, np.int16) + TEX[:, :, None]
    return np.clip(f + RNG.integers(-2, 3, f.shape), 0, 255).astype(np.uint8)


def blob(f, x0, y0, w, h):
    f[y0 : y0 + h, x0 : x0 + w] = 220
    return f


class Sim:
    def __init__(self, cfg=CFG, on_roi=None):
        self.cfg, self.t, self.n, self.events = cfg, 0.0, 0, []
        self.det, self.trk = tracker.make_detector(cfg, on_roi), Tracker(cfg)

    def feed(self, mk, seconds):
        for _ in range(int(seconds * self.cfg["fps"])):
            obs = self.det.analyze(mk(self.n))
            self.events += [e[0] for e in self.trk.update(obs, self.t)]
            self.t += 1.0 / self.cfg["fps"]
            self.n += 1


def empty(i):
    return bg()


def wriggling(i):  # lying baby shifting about: the blob jumps between two spots
    return blob(bg(), 150 + 25 * (i // 4 % 2), 250, 300, 50)


def lying(i):
    return blob(bg(), 162, 250, 300, 50)


def upright(i):
    return blob(bg(), 280, 110, 70, 190)


@pytest.fixture
def calibrated():
    found = []
    s = Sim(on_roi=found.append)
    s.feed(empty, 10)
    s.feed(wriggling, 40)
    s.found = found
    return s


def test_discovers_roi_around_the_moving_baby(calibrated):
    assert len(calibrated.found) == 1
    xs, ys = zip(*calibrated.found[0])
    assert min(xs) * W < 150 and max(xs) * W > 475  # covers the footprint
    assert min(ys) * H < 250 - 100  # room above for sitting up
    assert max(ys) < 1 and min(xs) > 0.05  # not the whole frame
    assert calibrated.events == []


def test_baseline_learns_the_lying_blob(calibrated):
    bl = calibrated.det.run.baseline
    assert bl.ready and 280 < bl.width < 360 and 240 < bl.top < 252


def test_no_alert_while_lying_or_rolling(calibrated):
    calibrated.feed(lying, 30)
    calibrated.feed(wriggling, 30)
    assert calibrated.events == []


def test_upright_alerts_without_any_hand_set_line(calibrated):
    calibrated.feed(lying, 10)
    calibrated.feed(upright, 8)
    assert calibrated.events == ["awake"]


def test_wide_low_blob_is_not_upright(calibrated):
    calibrated.feed(
        lambda i: blob(bg(), 200, 215, 200, 90), 10
    )  # taller, but still wide/low
    assert calibrated.events == []


def test_calibrating_phase_never_alerts():
    s = Sim()
    s.feed(empty, 10)
    s.feed(upright, 8)  # < auto_cal_s: still calibrating
    assert s.events == []


def test_global_foreground_does_not_poison_the_heatmap():
    def adult(
        i,
    ):  # a big dim shape over most of the frame every few seconds (no brightness reset)
        if i % 8 < 2:
            f = bg()
            f[:300, :450] = 135
            return f
        return wriggling(i)

    s = Sim()
    s.feed(empty, 10)
    s.feed(adult, 50)
    assert s.det.run is not None
    assert s.det.run.roi_mask[5, 5] == 0  # the adult's area did not become the crib


def test_camera_shift_drops_the_calibration(calibrated):
    assert calibrated.det.run is not None
    for i in range(45):  # past two drift checks: the first takes the reference
        calibrated.det.analyze(wriggling(i))
    kinds = [
        calibrated.det.analyze(np.roll(wriggling(i), 17, axis=1)).kind
        for i in range(45)
    ]
    assert "reset" in kinds and kinds[-1] == "calibrating"


def test_steady_camera_is_never_flagged_as_moved(calibrated):
    kinds = {calibrated.det.analyze(wriggling(i)).kind for i in range(80)}
    assert "reset" not in kinds and "calibrating" not in kinds


def test_hand_pinned_lying_line_bypasses_autocal():
    cfg = tracker.validate_config(dict(CFG, lying_line=0.6))
    assert isinstance(tracker.make_detector(cfg), tracker.BgSubDetector)


def test_auto_marked_config_still_self_calibrates():
    cfg = tracker.validate_config(dict(CFG, lying_line=0.6, calibrated="auto"))
    assert isinstance(tracker.make_detector(cfg), autocal.AutoDetector)


def test_baseline_ignores_area_outliers_once_ready():
    b = autocal.Baseline(min_samples=3)
    for _ in range(3):
        b.add(250, 300, 50, 15000)
    b.add(100, 400, 400, 100000)  # a parent leaning in
    assert b.area == 15000 and len(b.samples) == 3


def test_save_auto_roi_never_overwrites_a_hand_roi(tmp_path):
    p = str(tmp_path / "roi.json")
    assert tracker.save_auto_roi([(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)], p) == p
    tracker.save_config([(0, 0), (1, 0), (1, 1)], [(0, 0.5), (1, 0.5)], p)
    assert tracker.save_auto_roi([(0.2, 0.2), (0.8, 0.2), (0.5, 0.8)], p) is None


def test_tune_picks_params_that_catch_the_wake():
    from imcam import tune

    def frames():
        t = 0.0
        for i in range(100):  # 50 s of empty + wriggling at 2 fps
            yield t, empty(i) if i < 20 else wriggling(i)
            t += 0.5
        for i in range(100, 160):
            yield t, upright(i) if i >= 140 else lying(i)
            t += 0.5

    grid = {"rise_mult": [0.4], "lying_len_mult": [0.6]}
    best, held = tune.fit(
        [(frames, [70.0]), (frames, [70.0])],
        CFG,
        detector_grid=grid,
        tracker_grid={"hold_s": [5.0]},
    )
    assert best == {"lying_len_mult": 0.6, "rise_mult": 0.4, "hold_s": 5.0}
    assert [r["matched"] for _, _, r in held] == [1, 1]
