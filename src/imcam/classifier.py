"""Stage-2 "upright" classifier: logistic regression over a few scale-free blob features.

`imcam train` (train.py) fits it on the user's own labelled clips and writes ~/.imcam/classifier.json
(`{features, mean, std, weights, bias, threshold}`, no pickle). With the file present (cfg `classifier: "auto"`)
BgSubDetector asks Model.upright(features) instead of applying the rise/length rule; without it nothing
changes. Inference is stdlib-only; fitting uses numpy (imported inside fit()).
"""

import json
import math
import os
import sys

from . import paths

FEATURES = (  # order of BgSubDetector.last_features; all relative to the baby's own lying appearance
    "height",  # blob height / lying width
    "rise",  # (lying top - blob top) / lying width
    "area",  # blob area / lying area
    "aspect",  # blob height / width
    "motion",  # frame-to-frame foreground change / blob area
    "persist",  # fraction of the last few seconds with the blob raised above the lying top
    "top",  # blob top within the ROI (0 = ROI top)
)
FILE = "classifier.json"


def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


class Model:
    def __init__(self, mean, std, weights, bias, threshold=0.5):
        self.mean, self.std = list(mean), [s or 1.0 for s in std]
        self.weights, self.bias, self.threshold = list(weights), float(bias), threshold

    def predict(self, x):
        """P(upright) for one feature vector."""
        z = self.bias + sum(
            w * (v - m) / s for w, v, m, s in zip(self.weights, x, self.mean, self.std)
        )
        return _sigmoid(z)

    def upright(self, x):
        return self.predict(x) >= self.threshold

    def to_json(self):
        return {
            "features": list(FEATURES),
            "mean": self.mean,
            "std": self.std,
            "weights": self.weights,
            "bias": self.bias,
            "threshold": self.threshold,
        }

    @classmethod
    def from_json(cls, d):
        n = len(FEATURES)
        if list(d["features"]) != list(FEATURES):
            raise ValueError(
                "trained for features %s, this version uses %s"
                % (d["features"], list(FEATURES))
            )
        if not all(len(d[k]) == n for k in ("mean", "std", "weights")):
            raise ValueError("wrong vector length")
        return cls(d["mean"], d["std"], d["weights"], d["bias"], d["threshold"])


def fit(X, y, l2=1.0, iters=50):
    """Class-weighted, L2-regularised logistic regression by IRLS (Newton). X: rows of FEATURES, y: 0/1.
    Returns a Model with threshold 0.5."""
    import numpy as np

    X, y = np.asarray(X, float), np.asarray(y, float)
    if X.ndim != 2 or X.shape[1] != len(FEATURES) or len(X) != len(y):
        raise ValueError("X must be rows of %d features" % len(FEATURES))
    pos = float(y.sum())
    if pos == 0 or pos == len(y):
        raise ValueError("need both upright and not-upright samples")
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std < 1e-9] = 1.0
    Z = np.hstack([(X - mean) / std, np.ones((len(X), 1))])
    sw = np.where(
        y == 1, len(y) / (2 * pos), len(y) / (2 * (len(y) - pos))
    )  # balanced classes
    reg = np.eye(Z.shape[1]) * l2
    reg[-1, -1] = 0.0  # the bias is not shrunk
    beta = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ beta, -60, 60)))
        grad = Z.T @ (sw * (p - y)) + reg @ beta
        hess = (Z * (sw * p * (1 - p))[:, None]).T @ Z + reg
        step = np.linalg.solve(hess + 1e-9 * np.eye(len(beta)), grad)
        beta -= step
        if np.abs(step).max() < 1e-8:
            break
    return Model(mean.tolist(), std.tolist(), beta[:-1].tolist(), float(beta[-1]))


def model_path(cfg):
    want = cfg["classifier"]
    if not want:
        return None
    return paths.path(FILE) if want == "auto" else want


def load(cfg):
    """The trained Model for cfg, or None (off, not trained yet, unreadable / outdated file)."""
    path = model_path(cfg)
    if path is None or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return Model.from_json(json.load(f))
    except Exception as e:
        print("classifier %s ignored: %s" % (path, e), file=sys.stderr)
        return None


def save(model, cfg=None):
    path = (model_path(cfg) if cfg else None) or paths.path(FILE)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump(model.to_json(), f, indent=2)
    os.replace(tmp, path)
    return path
