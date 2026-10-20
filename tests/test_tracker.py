import numpy as np
import pytest

from imcam import tracker
from imcam.tracker import (
    BgSubDetector,
    Health,
    Obs,
    Tracker,
    line_y,
    norm_line,
    replay,
    score,
)

CFG = dict(
    tracker.DEFAULTS,
    roi=[[0.1, 0.3], [0.9, 0.3], [0.9, 0.95], [0.1, 0.95]],
    lying_line=0.6,
)
W, H = CFG["frame_w"], CFG["frame_h"]
RNG = np.random.default_rng(1)


def bg(level=100, color=False):
    f = np.full((H, W, 3), level, np.int16)
    if color:
        f[:, :, 2] += 60
    return np.clip(f + RNG.integers(-2, 3, f.shape), 0, 255).astype(np.uint8)


def blob(f, x0, y0, w, h):
    f[y0 : y0 + h, x0 : x0 + w] = 220
    return f


def lying():
    return blob(bg(), 150, 250, 300, 50)  # top 0.69 > line 0.6, ratio 0.17


def upright():
    return blob(bg(), 280, 110, 70, 190)  # top 0.31 < line


class Sim:
    def __init__(self, **kw):
        self.cfg = tracker.validate_config(dict(CFG, **kw))
        self.det, self.trk, self.t, self.events = (
            BgSubDetector(self.cfg),
            Tracker(self.cfg),
            0.0,
            [],
        )

    def feed(self, mk, seconds, fps=2):
        for _ in range(int(seconds * fps)):
            for ev in self.trk.update(self.det.analyze(mk()), self.t):
                self.events.append(ev[0])
            self.t += 1.0 / fps


@pytest.fixture
def settled():
    s = Sim()
    s.feed(bg, 15)
    s.feed(lying, 30)
    return s


def test_lying_blob_raises_no_alert(settled):
    assert settled.events == []


def test_upright_shorter_than_hold_raises_no_alert(settled):
    settled.feed(upright, 3)
    assert settled.events == []


def test_upright_persisting_past_hold_alerts_once_then_lying_again(settled):
    settled.feed(upright, 8)
    assert settled.events == ["awake"]
    settled.feed(lying, 5)
    assert settled.events == ["awake", "lying_again"]


def test_cooldown_suppresses_second_alert(settled):
    settled.feed(upright, 8)
    settled.feed(lying, 5)
    settled.feed(upright, 8)
    assert settled.events == ["awake", "lying_again"]


def test_alert_repeats_once_cooldown_has_passed():
    s = Sim(cooldown_s=1.0)
    s.feed(bg, 15)
    s.feed(lying, 20)
    s.feed(upright, 8)
    s.feed(lying, 5)
    s.feed(upright, 8)
    assert s.events == ["awake", "lying_again", "awake"]


def test_foreground_outside_roi_pauses_detection():
    s = Sim()
    s.feed(bg, 15)
    s.feed(lying, 20)
    s.feed(lambda: blob(upright(), 5, 5, 80, 60), 10)
    assert s.events == []
    assert s.det.analyze(blob(upright(), 5, 5, 80, 60)).kind == "paused"


def test_brightness_jump_resets_model_without_alert():
    s = Sim()
    s.feed(bg, 15)
    s.feed(lying, 20)
    kinds = [
        s.det.analyze(blob(bg(220, True), 150, 250, 300, 50)).kind for _ in range(4)
    ]
    assert kinds[0] == "reset" and kinds[1:] == ["warmup"] * 3
    s.feed(lambda: blob(bg(220, True), 150, 250, 300, 50), 20)
    assert s.events == []


def test_tilted_lying_line_is_compared_at_the_blob_x():
    # left end y=0.8, right end y=0.4; blob top 0.5 is above the line on the left, below it on the right
    s = Sim(lying_line=[[0.0, 0.8], [1.0, 0.4]])
    s.feed(bg, 15)
    assert s.det.analyze(blob(bg(), 100, 180, 80, 40)).kind == "upright"
    s.feed(bg, 10)
    assert s.det.analyze(blob(bg(), 480, 220, 80, 40)).kind == "lying"


def test_long_upright_is_not_absorbed_into_the_background(settled):
    settled.feed(upright, 900)  # 15 min at 2 fps, far past MOG2's slow-learning horizon
    assert settled.det.analyze(upright()).kind == "upright"


def test_background_still_adapts_while_lying():
    s = Sim()
    s.feed(bg, 15)
    s.feed(lying, 900)
    assert s.det.analyze(lying()).kind == "empty"  # settled baby became background


def test_single_flicker_frame_does_not_cancel_the_hold():
    trk = Tracker(CFG)
    seq = ["upright"] * 6 + ["lying"] + ["upright"] * 6  # 0.5 s steps, hold is 5 s
    events = [e[0] for i, k in enumerate(seq) for e in trk.update(Obs(k), i * 0.5)]
    assert events == ["awake"]


def test_gap_longer_than_gap_s_ends_the_episode():
    trk = Tracker(CFG)
    seq = ["upright"] * 6 + ["empty"] * 4 + ["upright"] * 6  # 2 s gap > gap_s
    events = [e[0] for i, k in enumerate(seq) for e in trk.update(Obs(k), i * 0.5)]
    assert events == []


def test_bare_number_lying_line_is_horizontal():
    assert line_y(norm_line(0.6), 0.3) == 0.6


def test_health_reports_stream_lost_and_restored():
    h = Health(60)
    assert h.frame(0) == [] and h.check(59) == []
    assert [e[0] for e in h.check(61)] == ["stream_lost"] and h.check(70) == []
    assert [e[0] for e in h.frame(80)] == ["stream_restored"] and h.frame(81) == []


def test_score_matches_alerts_to_labelled_wakes():
    r = score([100.0, 500.0, 900.0], [95.0, 2000.0], tol_s=30, duration_s=3600)
    assert r["matched"] == 1 and r["missed"] == 1 and r["false_alerts"] == 2
    assert r["delays"] == [5.0] and r["false_per_hour"] == 2.0


def test_replay_yields_observation_and_events_per_frame():
    cfg = tracker.validate_config(dict(CFG))
    frames = [
        (i * 0.5, f)
        for i, f in enumerate(
            [bg() for _ in range(20)]
            + [lying() for _ in range(40)]
            + [upright() for _ in range(16)]
        )
    ]
    out = list(replay(iter(frames), cfg))
    assert [e[0] for _, _, evs in out for e in evs] == ["awake"]
