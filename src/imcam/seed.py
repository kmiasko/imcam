"""Optional person detector that hints where the baby is while the crib ROI is being discovered.

The model is OpenCV Zoo's YOLOX-S (Apache-2.0, COCO; class 0 = person), fetched once into ~/.imcam/models/ and
checked against a pinned sha256. Everything about it is optional: with `seed_model: ""`, no network, a hash
mismatch or a model that fails to load, ensure_model() / make_seeder() give None and the classical foreground
heatmap in autocal.py is used alone. COCO detectors often miss infants lying in IR, so boxes are only a hint.

A seeder is any object with `boxes(frame) -> [(x0, y0, x1, y1), ...]` (normalized, confidence-filtered);
PersonSeed is the production one, tests pass a fake. numpy/cv2 are imported lazily, like tracker.py.
"""

import hashlib
import os
import sys
import urllib.request

from . import debug, paths

MODEL_URL = "https://huggingface.co/opencv/object_detection_yolox/resolve/main/object_detection_yolox_2022nov.onnx"
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODEL_FILE = "object_detection_yolox_2022nov.onnx"
SIZE = 640  # square network input
STRIDES = (8, 16, 32)
NMS_IOU = 0.45

_warned = False


def _warn(msg):
    global _warned
    if not _warned:
        _warned = True
        print("seed model: %s (continuing without it)" % msg, file=sys.stderr)


def model_path(cfg):
    """Where the model is (or would be) for cfg["seed_model"]; None when disabled."""
    want = cfg["seed_model"]
    if not want:
        return None
    return os.path.join(paths.path("models"), MODEL_FILE) if want == "auto" else want


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(cfg):
    """Path of a usable model file, downloading it on first use; None if disabled or unobtainable."""
    path = model_path(cfg)
    if path is None:
        return None
    if os.path.isfile(path):
        return path
    if cfg["seed_model"] != "auto":
        _warn("%s not found" % path)
        return None
    tmp = path + ".part"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print("downloading person model (35 MB) to %s" % path, file=sys.stderr)
        h = hashlib.sha256()
        with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != MODEL_SHA256:
            raise ValueError("sha256 mismatch")
        os.replace(tmp, path)
        return path
    except Exception as e:
        _warn("download failed: %s" % e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def decode(out, np, conf, ratio, pad, frame_w, frame_h):
    """Raw YOLOX output (1, N, 85) -> person boxes [(x0, y0, x1, y1), ...] normalized to the frame, after NMS.
    `ratio` / `pad` undo the letterbox (pad = (left, top) in network pixels)."""
    out = out.reshape(-1, out.shape[-1]).astype(np.float32)
    grids, strides = [], []
    for s in STRIDES:
        n = SIZE // s
        gy, gx = np.mgrid[0:n, 0:n]
        grids.append(np.stack([gx, gy], -1).reshape(-1, 2))
        strides.append(np.full((n * n, 1), s, np.float32))
    grid, stride = np.vstack(grids), np.vstack(strides)
    if len(grid) != len(out):
        return []
    xy = (out[:, :2] + grid) * stride
    wh = np.exp(out[:, 2:4]) * stride
    score = out[:, 4] * out[:, 5]  # objectness x P(person)
    keep = score >= conf
    if not keep.any():
        return []
    xy, wh, score = xy[keep], wh[keep], score[keep]
    x0 = (xy[:, 0] - wh[:, 0] / 2 - pad[0]) / ratio
    y0 = (xy[:, 1] - wh[:, 1] / 2 - pad[1]) / ratio
    x1 = (xy[:, 0] + wh[:, 0] / 2 - pad[0]) / ratio
    y1 = (xy[:, 1] + wh[:, 1] / 2 - pad[1]) / ratio
    order = np.argsort(-score)
    picked = []
    for i in order:
        a = (x1[i] - x0[i]) * (y1[i] - y0[i])
        if a <= 0:
            continue
        for j in picked:
            iw = min(x1[i], x1[j]) - max(x0[i], x0[j])
            ih = min(y1[i], y1[j]) - max(y0[i], y0[j])
            inter = max(0.0, iw) * max(0.0, ih)
            union = a + (x1[j] - x0[j]) * (y1[j] - y0[j]) - inter
            if inter / union > NMS_IOU:
                break
        else:
            picked.append(i)
    boxes = []
    for i in picked:
        b = (
            max(0.0, float(x0[i]) / frame_w),
            max(0.0, float(y0[i]) / frame_h),
            min(1.0, float(x1[i]) / frame_w),
            min(1.0, float(y1[i]) / frame_h),
        )
        if b[2] > b[0] and b[3] > b[1]:
            boxes.append(b)
    return boxes


class PersonSeed:
    """YOLOX person detector through cv2.dnn on CPU."""

    def __init__(self, path, conf):
        import cv2
        import numpy as np

        self.cv2, self.np, self.conf = cv2, np, conf
        self.net = cv2.dnn.readNetFromONNX(path)

    def boxes(self, frame):
        cv2, np = self.cv2, self.np
        h, w = frame.shape[:2]
        ratio = min(SIZE / h, SIZE / w)
        nw, nh = int(round(w * ratio)), int(round(h * ratio))
        canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)  # the zoo's letterbox grey
        canvas[:nh, :nw] = cv2.resize(frame, (nw, nh))
        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32)  # BGR, 0..255
        self.net.setInput(blob)
        out = self.net.forward()
        return decode(out, np, self.conf, ratio, (0, 0), w, h)


def make_seeder(cfg):
    """PersonSeed for cfg, or None (disabled / no model / model cannot be loaded)."""
    path = ensure_model(cfg)
    if path is None:
        return None
    try:
        seeder = PersonSeed(path, cfg["seed_conf"])
    except Exception as e:
        _warn("cannot load %s: %s" % (path, e))
        return None
    debug.autocal("person seed from %s" % path)
    return seeder
