import json

import numpy as np
import pytest

from imcam import classifier, tracker, train
from imcam.tracker import Obs
from test_autocal import CFG, Sim, bg, empty, lying, upright, wriggling


def hand_model(bias=-4.0):
    """Upright when the blob is taller than ~0.4 lying widths (feature 0 = height)."""
    w = [0.0] * len(classifier.FEATURES)
    w[0] = 10.0
    return classifier.Model([0.0] * len(w), [1.0] * len(w), w, bias, 0.5)


def test_fit_separates_a_synthetic_set_and_round_trips_json():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, len(classifier.FEATURES)))
    y = (X[:, 0] + 0.5 * X[:, 1] > 0.3).astype(int)
    m = classifier.fit(X, y)
    pred = np.array([m.upright(x) for x in X])
    assert (pred == y).mean() > 0.95
    m2 = classifier.Model.from_json(json.loads(json.dumps(m.to_json())))
    assert all(m.predict(x) == pytest.approx(m2.predict(x)) for x in X[:20])


def test_fit_handles_class_imbalance_and_refuses_one_class():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(500, len(classifier.FEATURES)))
    y = np.zeros(500, int)
    y[X[:, 0] > 1.6] = 1  # ~5% positives
    m = classifier.fit(X, y)
    assert (
        sum(m.upright(x) for x in X[y == 1]) / y.sum() > 0.8
    )  # positives not drowned out
    with pytest.raises(ValueError):
        classifier.fit(X, np.zeros(500))


def test_load_only_accepts_a_current_model(tmp_path):
    p = tmp_path / "c.json"
    cfg = dict(CFG, classifier=str(p))
    assert classifier.load(cfg) is None  # not trained
    classifier.save(hand_model(), cfg)
    assert classifier.load(cfg).predict([1] * 7) == pytest.approx(
        hand_model().predict([1] * 7)
    )
    assert classifier.load(dict(CFG, classifier="")) is None
    d = hand_model().to_json()
    d["features"] = d["features"][:-1] + ["something_new"]
    p.write_text(json.dumps(d))
    assert classifier.load(cfg) is None  # features changed: retrain


def run_scene(cfg):
    s = Sim(cfg)
    s.feed(empty, 10)
    s.feed(wriggling, 40)
    return s


def with_model(tmp_path, model):
    p = tmp_path / "c.json"
    classifier.save(model, dict(CFG, classifier=str(p)))
    return tracker.validate_config(dict(CFG, classifier=str(p)))


def test_classifier_driven_detector_alerts_on_upright_not_on_lying(tmp_path):
    s = run_scene(with_model(tmp_path, hand_model()))
    assert s.det.run.model is not None
    s.feed(lying, 20)
    s.feed(wriggling, 20)
    assert s.events == []
    s.feed(upright, 8)
    assert s.events == ["awake"]


def test_the_classifier_replaces_the_rule(tmp_path):
    s = run_scene(with_model(tmp_path, hand_model(bias=-50.0)))  # never upright
    s.feed(lying, 10)
    s.feed(upright, 10)
    assert s.events == []  # the rule alone would have fired


def test_manual_lying_line_ignores_the_classifier(tmp_path):
    cfg = dict(with_model(tmp_path, hand_model(bias=-50.0)), lying_line=0.6)
    det = tracker.make_detector(tracker.validate_config(cfg))
    assert isinstance(det, tracker.BgSubDetector) and det.model is None


def test_features_are_deterministic_and_scale_free():
    frames = scene()

    def replay():
        det = tracker.make_detector(CFG)
        out = []
        for f in frames:
            det.analyze(f)
            out.append(det.last_features)
        return out

    a, b = replay(), replay()
    assert a == b
    lie, up = a[-21], a[-1]
    h, rise = classifier.FEATURES.index("height"), classifier.FEATURES.index("rise")
    assert lie[h] < 0.3 and lie[rise] < 0.1
    assert up[h] > 0.5 and up[rise] > 0.3


# --- training ------------------------------------------------------------------------------


def lower(i):
    """The baby in a different state than when the detector absorbed its spot: the same footprint, now much
    darker, is foreground again (a perfectly still blob would be absorbed and never seen)."""
    f = bg()
    x = 150 + 25 * (i // 4 % 2)
    f[250:300, x : x + 300] = 40
    return f


def scene(n_lower=50):
    """empty, wriggling (calibration), shifting about lower down, then sitting up at 70 s (2 fps)."""
    return (
        [empty(i) for i in range(20)]
        + [wriggling(i) for i in range(70)]
        + [lower(i) for i in range(n_lower)]
        + [upright(i) for i in range(20)]
    )


def make_clip():
    frames = [(i * 0.5, f) for i, f in enumerate(scene())]
    return lambda: iter(frames), [70.0]


def test_train_fits_a_model_that_tells_upright_from_lying():
    out = []
    model, report = train.train([make_clip(), make_clip()], CFG, out=out.append)
    assert model is not None and report["classifier"][0]["matched"] == 1
    rows = train.collect(
        make_clip()[0](), tracker.validate_config(dict(CFG, classifier=""))
    )
    feats = [(o.kind, f) for _, o, f in rows if f is not None]
    assert all(model.upright(f) for k, f in feats if k == "upright")
    assert not any(model.upright(f) for k, f in feats if k == "lying")
    assert any("rules" in line for line in out)


def test_train_refuses_degenerate_input():
    clip = make_clip()
    with pytest.raises(SystemExit):
        train.train([clip], CFG, out=lambda *_: None)  # one clip
    with pytest.raises(SystemExit):
        train.train(
            [(clip[0], []), (clip[0], [])], CFG, out=lambda *_: None
        )  # no wakes


def test_label_marks_the_window_and_skips_doubtful_frames():
    def obs(ts):
        return Obs("upright" if 10 <= ts <= 17 else "lying")

    rows = [(float(ts), obs(ts), None if ts < 3 else (0.0,) * 7) for ts in range(30)]
    lab = train.label(rows, [10.0], window_s=5.0, margin_s=2.0)
    assert lab[:3] == [None] * 3  # no features
    assert lab[3:8] == [0] * 5
    assert lab[8:10] == [None] * 2  # margin before the wake
    assert lab[10:16] == [1] * 6  # wake .. wake + window
    assert lab[16:18] == [None] * 2  # still sitting after the window
    assert lab[18:] == [0] * 12


def test_rejudge_only_touches_judged_frames():
    rows = [
        (0.0, Obs("lying", 0.5, 1.0, 0.1), (0.0,) * 7),
        (1.0, Obs("paused"), None),
        (2.0, Obs("upright", 0.2, 3.0, 0.3), (1.0,) * 7),
    ]
    out = train.rejudge(rows, [0.9, 0.9, 0.1], 0.5)
    assert [o.kind for _, o in out] == ["upright", "paused", "lying"]
