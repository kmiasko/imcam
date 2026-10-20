# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unofficial Linux/Windows client for ImCam (`com.cylan.chatcam`) baby cameras: live view, recording, and "baby is awake" alerts. Python ≥3.10, package in `src/imcam`, entry point `imcam.cli:main`. Needs `ffmpeg`/`ffplay` on PATH at runtime.

## Commands

    pip install -e ".[test]"          # dev install
    pytest -q                         # all tests (CI runs this on Linux+Windows, py3.10 and 3.13)
    pytest tests/test_call.py -k name # single test
    imcam selftest                    # CI also runs this (runs the test suite from a source checkout)
    imcam doctor                      # checks ffmpeg, deps, vkey/AES prefix, notifier

**Before every commit, run `ruff format .` on the codebase** and include the result in the commit.

Optional extras: `.[keys]` (androguard, for `extract-keys`).

## Architecture

`cli.py` only dispatches commands and runs the `watch` / `monitor` loops; the real work is in sibling modules, each with a docstring describing its protocol/contract:

- **cloud.py** – WebSocket request/response client to the vendor cloud (`wss://fast.jfgou.com/fast`), saved session (`~/.imcam/session.json`), login, and the `/im` signaling sign-in. Built on **wsproto.py** (hand-rolled RFC 6455 client, no deps) and **accesskey.py** (from-scratch AES for the signaling access key).
- **call.py** – WebRTC call to the camera: signaling messages, call state machine. The peer connection is a seam: `AiortcPeer` in production, a scripted fake in tests (see `FakeSig` in `tests/test_call.py`).
- **stock_offer.py** – the offer sent to the camera: generated in code in stock Android libwebrtc shape (the camera streams for it); only our ICE credentials and fingerprint come from aiortc.
- **relay.py** – aiortc has no H.265, so decrypted RTP packets are copied to local UDP ports; `ffplay` (watch) and the tracker's `ffmpeg` read them via SDP files. Constraints: one reader per port (sending to an unread port breaks playback), ports ≥2 apart (ffmpeg binds port+1 for RTCP). `imcam calibrate` runs in a separate process and requests a one-frame grab by touching `SNAP_FLAG`; the relay then also feeds `SNAP_PORT`.
- **testvideo.py** – the camera streams only after the viewer sends video, so a libx265-encoded test pattern is pushed through the sender's SRTP transport.
- **tracker.py** – awake detection: background subtraction in a calibrated crib ROI (`~/.imcam/roi.json`, normalized coords; all settings in `DEFAULTS`). numpy/cv2 are imported lazily so the module stays stdlib-only at import.
- **notify.py** – event delivery (log, desktop notification, user `hook` command with `IMCAM_EVENT`/`IMCAM_MESSAGE`); `send()` never raises.
- **config.py / paths.py** – vendor constants (`IMCAM_VKEY`, `IMCAM_AES_PREFIX`) are deliberately not shipped; looked up in env, `./.env`, then `~/.imcam/config.env`. Data dir is `~/.imcam` (override `IMCAM_HOME`).
- **extract_keys.py** – obtains the vendor constants from the official APK. Not shipped; keep vendor code/data out of the repo.
- **debug.py** – `IMCAM_DEBUG` tracing; every function is a no-op unless set.

Runtime flow: `cloud` login → `/im` signaling → `call` negotiates WebRTC with the stock-shaped offer → `relay` fans RTP to UDP → ffplay / `tracker` (ffmpeg → OpenCV) → `notify`.

Recent history is a series of module extractions out of `cli.py`; keep new logic out of `cli.py` and behind seams that tests can fake.
