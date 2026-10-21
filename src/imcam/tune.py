"""Offline fit of the self-calibrating detector's few scale-free knobs on labelled recordings.

`imcam tune --clip FILE:T1,T2,... [--clip ...]` replays every clip with automatic calibration (no roi.json
is read or written) over a small grid, with the alert cooldown off so one early alert cannot hide a later
wake. Objective: most matched wakes, then fewest false alerts per hour. Each clip is also scored with
parameters fitted on the *other* clips (leave-one-clip-out), which is the honest estimate; the winners on
all clips are printed as roi.json keys.
"""

import itertools
import json

from . import tracker

DETECTOR_GRID = {  # change the observations: each combination is one pass over every clip
    "rise_mult": [0.25, 0.4, 0.6],
    "lying_len_mult": [0.5, 0.6, 0.8],
    "min_area_ratio": [0.25, 0.35, 0.5],
}
TRACKER_GRID = {  # only the debounce: replayed on the cached observations
    "hold_s": [3.0, 5.0, 8.0],
    "gap_s": [1.0, 2.0, 3.0],
}


def auto_cfg(cfg):
    """cfg forced into self-calibrating mode with the alert cooldown off."""
    return tracker.validate_config(
        dict(cfg, roi=None, lying_line=None, calibrated="auto", cooldown_s=1.0)
    )


def combos(grid):
    keys = sorted(grid)
    return [dict(zip(keys, v)) for v in itertools.product(*(grid[k] for k in keys))]


def observe(frames, cfg):
    """[(ts, Obs)] of one pass of the detector over (ts, frame) pairs."""
    det = tracker.make_detector(cfg)
    return [(ts, det.analyze(f)) for ts, f in frames]


def alerts(observations, cfg):
    trk = tracker.Tracker(cfg)
    return [
        ts
        for ts, obs in observations
        for name, _, _ in trk.update(obs, ts)
        if name == "awake"
    ]


def objective(results):
    """Sort key (smaller is better) over per-clip score dicts with a `hours` entry added."""
    matched = sum(r["matched"] for r in results)
    hours = sum(r["hours"] for r in results) or 1.0
    false = sum(r["false_alerts"] for r in results)
    delays = [abs(d) for r in results for d in r["delays"]]
    return (-matched, false / hours, sum(delays) / len(delays) if delays else 0.0)


def fit(clips, base_cfg, tol_s=30.0, detector_grid=None, tracker_grid=None):
    """clips: [(frames_factory, wakes)], frames_factory() -> (ts, frame) pairs.
    Returns (best params on all clips, [(held-out clip index, params, score)])."""
    dgrid, tgrid = detector_grid or DETECTOR_GRID, tracker_grid or TRACKER_GRID
    cfg0 = auto_cfg(base_cfg)
    table = {}  # (clip, detector combo index, tracker combo index) -> score dict
    dcombos, tcombos = combos(dgrid), combos(tgrid)
    for di, dp in enumerate(dcombos):
        dcfg = dict(cfg0, **dp)
        for ci, (frames, wakes) in enumerate(clips):
            obs = observe(frames(), dcfg)
            hours = (obs[-1][0] if obs else 0.0) / 3600.0
            for ti, tp in enumerate(tcombos):
                r = tracker.score(alerts(obs, dict(dcfg, **tp)), wakes, tol_s)
                table[ci, di, ti] = dict(r, hours=hours)

    def best(use):
        return min(
            ((di, ti) for di in range(len(dcombos)) for ti in range(len(tcombos))),
            key=lambda k: objective([table[ci, k[0], k[1]] for ci in use]),
        )

    def params(k):
        return dict(dcombos[k[0]], **tcombos[k[1]])

    everyone = range(len(clips))
    held = []
    if len(clips) > 1:
        for out in everyone:
            k = best([c for c in everyone if c != out])
            held.append((out, params(k), table[out, k[0], k[1]]))
    return params(best(everyone)), held


def run(clip_specs, tol_s=30.0, out=print):
    """clip_specs: ["FILE:T1,T2,...", ...] as given to --clip."""
    base = tracker.load_config()
    clips = []
    for spec in clip_specs:
        path, _, w = spec.rpartition(":")
        if not path:
            raise SystemExit("--clip needs FILE:T1,T2,... (wake times in seconds)")
        wakes = [float(t) for t in w.split(",") if t]
        clips.append((lambda p=path: tracker.file_frames(p, base), wakes))
    best, held = fit(clips, base, tol_s)
    for i, p, r in held:
        out("held out %s: %s -> %s" % (clip_specs[i], p, r))
    out("best on all clips (put in roi.json):")
    out(json.dumps(best, indent=2))
    return best
