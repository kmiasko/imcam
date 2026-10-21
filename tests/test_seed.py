import hashlib

import cv2
import numpy as np
import pytest

from imcam import autocal, seed, tracker
from imcam.tracker import Tracker
from test_autocal import CFG, H, W, Sim, empty, wriggling


def raw_output(cells):
    """YOLOX head output (1, 8400, 85) with person detections at (stride-8 grid col, row, wh exponent)."""
    out = np.zeros((8400, 85), np.float32)
    for gx, gy, conf in cells:
        i = gy * 80 + gx
        out[i, :2] = 0.5
        out[i, 2:4] = np.log(10.0)  # 10 cells * stride 8 = 80 px
        out[i, 4], out[i, 5] = conf, 0.95
    return out[None]


def test_decode_maps_a_detection_back_to_the_frame_and_merges_duplicates():
    out = raw_output([(40, 40, 0.9), (41, 40, 0.8), (5, 5, 0.1)])
    boxes = seed.decode(out, np, 0.4, 1.0, (0, 0), W, H)
    assert len(boxes) == 1  # NMS merged the neighbours, the weak cell is below conf
    x0, y0, x1, y1 = boxes[0]
    assert x0 == pytest.approx(284 / W) and x1 == pytest.approx(364 / W)
    assert y0 == pytest.approx(284 / H) and y1 == 1.0  # clipped to the frame


def test_decode_undoes_the_letterbox():
    out = raw_output([(40, 40, 0.9)])
    # a 320x180 frame is scaled x2 into the network input
    ((x0, y0, x1, y1),) = seed.decode(out, np, 0.4, 2.0, (0, 0), 320, 180)
    assert x0 == pytest.approx(142 / 320) and x1 == pytest.approx(182 / 320)


def test_decode_without_detections_or_with_wrong_shape_is_empty():
    assert seed.decode(raw_output([]), np, 0.4, 1.0, (0, 0), W, H) == []
    assert (
        seed.decode(np.zeros((1, 10, 85), np.float32), np, 0.0, 1.0, (0, 0), W, H) == []
    )


class FakeResponse:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def model_cfg(tmp_path, monkeypatch):
    data = b"pretend onnx" * 1000
    monkeypatch.setattr(seed, "MODEL_SHA256", hashlib.sha256(data).hexdigest())
    cfg = dict(CFG, seed_model="auto")
    return cfg, data, tmp_path


def test_ensure_model_downloads_once_and_verifies(model_cfg, monkeypatch):
    cfg, data, _ = model_cfg
    calls = []
    monkeypatch.setattr(
        seed.urllib.request,
        "urlopen",
        lambda url, timeout: calls.append(url) or FakeResponse(data),
    )
    path = seed.ensure_model(cfg)
    assert path and open(path, "rb").read() == data
    assert seed.ensure_model(cfg) == path and len(calls) == 1  # cached


def test_ensure_model_rejects_a_hash_mismatch(model_cfg, monkeypatch):
    cfg, _, _ = model_cfg
    monkeypatch.setattr(
        seed.urllib.request, "urlopen", lambda url, timeout: FakeResponse(b"tampered")
    )
    assert seed.ensure_model(cfg) is None
    path = seed.model_path(cfg)
    import os

    assert not os.path.exists(path) and not os.path.exists(path + ".part")


def test_ensure_model_survives_network_failure(model_cfg, monkeypatch):
    cfg, _, _ = model_cfg

    def boom(url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(seed.urllib.request, "urlopen", boom)
    assert seed.ensure_model(cfg) is None


def test_ensure_model_off_and_custom_path(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network wanted")

    monkeypatch.setattr(seed.urllib.request, "urlopen", boom)
    assert seed.ensure_model(dict(CFG, seed_model="")) is None
    mine = tmp_path / "mine.onnx"
    assert seed.ensure_model(dict(CFG, seed_model=str(mine))) is None  # missing
    mine.write_bytes(b"x")
    assert seed.ensure_model(dict(CFG, seed_model=str(mine))) == str(mine)


def test_make_seeder_ignores_an_unloadable_model(tmp_path, monkeypatch):
    # conftest stubs make_seeder out for every test; undo it for this one
    import importlib

    importlib.reload(seed)
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not an onnx file")
    assert seed.make_seeder(dict(CFG, seed_model=str(bad))) is None


# --- Discovery with person boxes ---------------------------------------------------------


def rect_fg(x0, y0, x1, y1):
    fg = np.zeros((H, W), np.uint8)
    fg[y0:y1, x0:x1] = 255
    return fg


def disc():
    return autocal.Discovery(CFG, np, cv2)


def test_boxes_rescue_discovery_when_the_foreground_is_too_weak():
    d = disc()
    for _ in range(6):
        d.feed(np.zeros((H, W), np.uint8), None, [(0.3, 0.5, 0.7, 0.9)])
    roi = d.roi()
    assert roi is not None
    xs, ys = zip(*roi)
    assert min(xs) < 0.3 and max(xs) > 0.7 and max(ys) <= 1.0
    assert autocal.Discovery(CFG, np, cv2).roi() is None  # nothing seen, nothing seeded


def span(roi):
    xs, _ = zip(*roi)
    return min(xs), max(xs)


def test_overlapping_boxes_widen_the_hull_but_distant_ones_are_ignored():
    fg = rect_fg(250, 250, 350, 300)
    plain = disc()
    for _ in range(6):
        plain.feed(fg, None)
    lo0, hi0 = span(plain.roi())

    near = disc()  # a box covering the foreground and reaching further right
    for _ in range(6):
        near.feed(fg, None, [(0.35, 0.65, 0.9, 0.85)])
    assert span(near.roi())[1] > hi0 + 0.1

    far = disc()  # an adult-sized box in the other corner: not the baby
    for _ in range(6):
        far.feed(fg, None, [(0.0, 0.0, 0.1, 0.2)])
    assert span(far.roi()) == (lo0, hi0)


def test_global_foreground_frames_do_not_feed_the_box_layer():
    d = disc()
    for _ in range(6):
        d.feed(rect_fg(0, 0, W, H), None, [(0.3, 0.5, 0.7, 0.9)])
    assert d.box_heat.max() == 0


def test_autodetector_asks_the_seeder_once_per_interval_while_discovering():
    class Fake:
        calls = 0

        def boxes(self, frame):
            Fake.calls += 1
            return [(0.2, 0.6, 0.8, 0.9)]

    found = []
    s = Sim()
    s.det = autocal.AutoDetector(CFG, found.append, seeder=Fake())
    s.feed(empty, 10)
    s.feed(wriggling, 40)
    assert found and s.det.run is not None
    calls = Fake.calls
    s.feed(wriggling, 10)  # calibrated: the seeder is no longer consulted
    assert Fake.calls == calls
    assert 0 < calls <= 50 * CFG["fps"] / (CFG["seed_every_s"] * CFG["fps"]) + 1
    assert s.events == []
