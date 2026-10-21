"""Train the stage-2 classifier (classifier.py) on labelled recordings.

`imcam train --clip FILE:T1,T2,... [--clip ...]` replays every clip through the self-calibrating detector
(rules, no classifier) and records the per-frame features. A frame is labelled upright from a labelled wake to
`train_window_s` later and not-upright elsewhere; the baby may keep sitting after the window, so frames are
ignored while the rules still see an upright baby after it, and for MARGIN_S around the label edges.

Evaluation is leave-one-clip-out: each clip is scored with a model fitted on the others, the probability
threshold is picked on the pooled out-of-fold predictions, and the tracker (debounce unchanged, alert cooldown
off, like tune.py) runs on the re-judged observations. The weights are written to ~/.imcam/classifier.json
only if that is not worse than the rules on the same clips; otherwise the rules stay.
"""

from . import classifier, tracker, tune
from .tracker import Obs

MARGIN_S = 10.0  # frames this close to a label edge are not trained on
THRESHOLDS = [round(0.3 + 0.05 * i, 2) for i in range(14)]  # 0.30 .. 0.95


def collect(frames, cfg):
    """[(ts, Obs, features or None)] of one rules-only pass of the auto detector."""
    det = tracker.make_detector(cfg)
    rows = []
    for ts, f in frames:
        obs = det.analyze(f)
        rows.append((ts, obs, getattr(det, "last_features", None)))
    return rows


def label(rows, wakes, window_s, margin_s=MARGIN_S):
    """Per row: 1 / 0, or None when the frame must not be trained on (no features, or a doubtful label)."""
    out, tail = [], False
    wakes = sorted(wakes)
    for ts, obs, feat in rows:
        if feat is None:
            out.append(None)
            continue
        inside = any(w <= ts <= w + window_s for w in wakes)
        if inside:
            out.append(1)
            tail = True
            continue
        if tail:  # the window just ended
            if obs.kind == "upright":
                out.append(None)  # still sitting: not a "no"
                continue
            tail = False
        near = any(
            w - margin_s <= ts < w or w + window_s < ts <= w + window_s + margin_s
            for w in wakes
        )
        out.append(None if near else 0)
    return out


def rejudge(rows, probs, threshold):
    """Observations with the classifier's verdict on every frame that has one (others unchanged)."""
    out = []
    for (ts, obs, feat), p in zip(rows, probs):
        if feat is not None and obs.kind in ("upright", "lying"):
            obs = Obs(
                "upright" if p >= threshold else "lying", obs.top, obs.ratio, obs.area
            )
        out.append((ts, obs))
    return out


def _score(clip_obs, wakes_by_clip, cfg, tol_s):
    results = []
    for obs, wakes in zip(clip_obs, wakes_by_clip):
        hours = (obs[-1][0] if obs else 0.0) / 3600.0
        r = tracker.score(tune.alerts(obs, cfg), wakes, tol_s)
        results.append(dict(r, hours=hours))
    return results


def _xy(rows, labels):
    pairs = [(f, l) for (_, _, f), l in zip(rows, labels) if l is not None]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _fit(rows_list, labels_list):
    X, y = [], []
    for rows, labels in zip(rows_list, labels_list):
        a, b = _xy(rows, labels)
        X += a
        y += b
    try:
        return classifier.fit(X, y)
    except ValueError as e:
        raise SystemExit("cannot train: %s" % e)


def _probs(model, rows):
    return [model.predict(f) if f is not None else 0.0 for _, _, f in rows]


def train(clips, base_cfg, tol_s=30.0, out=print):
    """clips: [(frames_factory, wakes)]. Returns (Model or None, report). The model is None when the
    held-out results are worse than the rules'; nothing is written here."""
    if len(clips) < 2 or sum(len(w) for _, w in clips) < 2:
        raise SystemExit("train needs at least 2 clips and 2 labelled wakes in total")
    cfg = tune.auto_cfg(dict(base_cfg, classifier=""))
    window = cfg["train_window_s"]
    rows_list = [collect(frames(), cfg) for frames, _ in clips]
    wakes_list = [w for _, w in clips]
    labels_list = [label(r, w, window) for r, w in zip(rows_list, wakes_list)]
    for i, (rows, labels) in enumerate(zip(rows_list, labels_list)):
        out(
            "clip %d: %d frames, %d upright / %d not-upright labelled"
            % (i, len(rows), labels.count(1), labels.count(0))
        )
    n = len(clips)
    oof = []  # out-of-fold probabilities per clip
    for held in range(n):
        rest = [i for i in range(n) if i != held]
        model = _fit([rows_list[i] for i in rest], [labels_list[i] for i in rest])
        oof.append(_probs(model, rows_list[held]))

    rules = _score(
        [[(ts, o) for ts, o, _ in rows] for rows in rows_list], wakes_list, cfg, tol_s
    )
    best = None
    for thr in THRESHOLDS:
        res = _score(
            [rejudge(r, p, thr) for r, p in zip(rows_list, oof)], wakes_list, cfg, tol_s
        )
        if best is None or tune.objective(res) < tune.objective(best[1]):
            best = (thr, res)
    thr, held_res = best
    for i in range(n):
        out(
            "held out clip %d: rules %s | classifier %s"
            % (i, _brief(rules[i]), _brief(held_res[i]))
        )
    report = {"threshold": thr, "rules": rules, "classifier": held_res}
    out("rules      objective %s" % (tune.objective(rules),))
    out("classifier objective %s (threshold %.2f)" % (tune.objective(held_res), thr))
    if tune.objective(held_res) > tune.objective(rules):
        out("held-out results are worse than the rules: keeping the rules")
        return None, report
    model = _fit(rows_list, labels_list)
    model.threshold = thr
    return model, report


def _brief(r):
    return "%d matched, %d missed, %d false" % (
        r["matched"],
        r["missed"],
        r["false_alerts"],
    )


def run(clip_specs, tol_s=30.0, out=print):
    base = tracker.load_config()
    model, _ = train(tune.load_clips(clip_specs, base), base, tol_s, out)
    if model is not None:
        out("wrote %s" % classifier.save(model, base))
    return model
