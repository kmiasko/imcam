# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unofficial Linux/Windows client for ImCam (`com.cylan.chatcam`) baby cameras: live view, recording, and "baby is up" alerts (via the separate babywatch program). Python ≥3.10, package in `src/imcam`, entry point `imcam.cli:main`. Needs `ffmpeg`/`ffplay` on PATH at runtime.

## Commands

    pip install -e ".[test]"          # dev install
    pytest -q                         # all tests (CI runs this on Linux+Windows, py3.10 and 3.13)
    pytest tests/test_call.py -k name # single test
    imcam selftest                    # CI also runs this (runs the test suite from a source checkout)
    imcam doctor                      # checks ffmpeg, deps, vkey/AES prefix, babywatch/notify-send on PATH

**Before every commit, run `ruff format .` on the codebase** and include the result in the commit.

Optional extras: `.[keys]` (androguard, for `extract-keys`).

## Architecture

`cli.py` only dispatches commands and runs the `watch` / `monitor` loops; the real work is in sibling modules, each with a docstring describing its protocol/contract:

- **cloud.py** – WebSocket request/response client to the vendor cloud (`wss://fast.jfgou.com/fast`), saved session (`~/.imcam/session.json`), login, and the `/im` signaling sign-in. Built on **wsproto.py** (hand-rolled RFC 6455 client, no deps) and **accesskey.py** (from-scratch AES for the signaling access key).
- **call.py** – WebRTC call to the camera: signaling messages, call state machine. The peer connection is a seam: `AiortcPeer` in production, a scripted fake in tests (see `FakeSig` in `tests/test_call.py`).
- **stock_offer.py** – the offer sent to the camera: generated in code in stock Android libwebrtc shape (the camera streams for it); only our ICE credentials and fingerprint come from aiortc.
- **relay.py** – aiortc has no H.265, so decrypted RTP packets (video, and audio on port+2 when the answer negotiated `relay.AUDIO`) are copied to local UDP ports; `ffplay` (watch) and watcher.py's bridge ffmpeg read them via SDP files. Constraints: one reader per port (sending to an unread port breaks playback), ports ≥2 apart (ffmpeg binds port+1 for RTCP). `imcam snapshot` runs in a separate process and requests a one-frame grab by touching `SNAP_FLAG`; the relay then also feeds `SNAP_PORT`.
- **testvideo.py** – the camera streams only after the viewer sends video, so a libx265-encoded test pattern is pushed through the sender's SRTP transport.
- **watcher.py** – launches/stops a bridge ffmpeg (`~/.imcam/track.sdp` → video MPEG-TS on `udp://127.0.0.1:47308`, audio MPEG-TS on `udp://127.0.0.1:47314`, plus `--record` segments) and the separate `babywatch live --rtsp udp://127.0.0.1:47308` (babywatch's PyAV input can't open RTP .sdp). `Alerts` turns babywatch's JSON stdout into `awake` (cooldown) / `down` events, `Health` turns the relay's `last_rx` into `stream_lost` / `stream_restored`; the optional `IMCAM_AUDIO_ANALYZER` reads the audio URL and its JSON events are delivered as is; a supervisor thread restarts dead children (the bridge only while video flows). Detection, crib ROI and training live in the babywatch repo. `launch` is the seam tests fake, `record_event` the event-log one. **eventlog.py** – thread-safe SQLite event log `~/.imcam/events.db` (WAL, one `events` table with the producer's raw JSON in `data`); like notify.py it never raises. **notify.py** – event delivery: stderr log, desktop backend (notify-send / osascript / winotify), `IMCAM_NOTIFY_HOOK`. **children.py** – every child process goes through `children.popen` (own session, PDEATHSIG on Linux) and `children.stop`; `install()` maps SIGTERM/SIGHUP to KeyboardInterrupt so the Ctrl+C cleanup path runs and sweeps leftovers at exit. **snapshot.py** – `imcam snapshot` one-frame PNG grab.
- **config.py / paths.py** – vendor constants (`IMCAM_VKEY`, `IMCAM_AES_PREFIX`) are deliberately not shipped; looked up in env, `./.env`, then `~/.imcam/config.env`. Optional settings (`config.OPTIONAL`, alert hook/cooldown/stream-lost) use the same lookup. Data dir is `~/.imcam` (override `IMCAM_HOME`).
- **extract_keys.py** – obtains the vendor constants from the official APK. Not shipped; keep vendor code/data out of the repo.
- **debug.py** – `IMCAM_DEBUG` tracing; every function is a no-op unless set.

Runtime flow: `cloud` login → `/im` signaling → `call` negotiates WebRTC with the stock-shaped offer → `relay` fans RTP to UDP → ffplay / bridge ffmpeg → `babywatch` subprocess.

Wake detection lives in the separate babywatch program. Recent history is a series of module extractions out of `cli.py`; keep new logic out of `cli.py` and behind seams that tests can fake.
