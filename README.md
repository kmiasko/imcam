# imcam

Unofficial Linux/Windows client for ImCam (`com.cylan.chatcam`) baby cameras: live view, recording, and
"baby is awake" alerts (sitting/standing silhouette inside a crib region you calibrate).

Not affiliated with the camera vendor. Protocol knowledge comes from interoperability analysis of the
public Android app; no vendor code or data is included.

## Install

Needs Python 3.10+ and **ffmpeg + ffplay on PATH**
(Linux: your package manager; Windows: `winget install Gyan.FFmpeg`, then reopen the terminal).

    pipx install .          # or: pip install .
    imcam doctor            # checks ffmpeg, Python deps, notification backend

## Use

    imcam login <account> <password>
    imcam devlist
    imcam watch <serial> <permit>                 # ffplay window
    imcam watch <serial> <permit> --track         # ... plus awake tracking
    imcam calibrate                               # optional hand calibration (with `watch`/`monitor` running): crib corners, then 2 points of the lying head level
    imcam monitor <serial> <permit> [--record DIR]   # headless, reconnects forever
    imcam monitor --input rec.mkv                 # replay a recording to tune thresholds
    imcam evaluate --input rec.mkv --wakes 120,3400   # score alerts vs. labelled wake times (s); --csv dumps observations

Data (session, `roi.json`, SDP files) lives in `~/.imcam` (`%USERPROFILE%\.imcam` on Windows); set `IMCAM_HOME` to move it.
Without a hand calibration, `monitor` self-calibrates: it spends the first `auto_cal_s` (2 min) learning the crib region and the
baby's lying appearance from the stream, writes the region to `roi.json` (`"calibrated": "auto"`), and recalibrates if the camera moves.
`imcam tune --clip FILE:T1,T2,...` fits the remaining thresholds on labelled recordings.
Two optional extras sharpen the self-calibration (without them the behaviour above is unchanged).
While discovering, a small person detector (OpenCV Zoo YOLOX, Apache-2.0, ~35 MB) is downloaded once to `~/.imcam/models/`
(sha256-checked; `"seed_model": ""` in `roi.json` turns it off) and its boxes help find the crib when the baby barely moves.
`imcam train --clip FILE:T1,T2,... [--clip ...]` fits a small per-user classifier on labelled wakes and saves it to
`~/.imcam/classifier.json` only if its leave-one-clip-out results beat the built-in rules; `"classifier": ""` ignores it.
`roi.json` is hand-editable; see `DEFAULTS` in `src/imcam/tracker.py` for every setting (`hook` runs a command per alert
with `IMCAM_EVENT` / `IMCAM_MESSAGE` in its environment).

## Vendor constants

imcam does not ship the vendor's `vkey` or AES key prefix. Extract them from the official app's APK yourself:

    pip install "imcam[keys]"            # pipx: pipx inject imcam androguard
    imcam extract-keys app.xapk          # or base.apk; prints the two values as NAME=value lines

Give it the app's APK or an XAPK bundle (also `.apks` / `.apkm`), from your own phone
(`adb shell pm path com.cylan.chatcam`, then `adb pull`) or any APK source you trust. For a bundle it looks inside
for the APK that contains the app's code and ignores the config splits; a bare `config.*.apk` has no code and is rejected.
The command finds the `vkey` (the only 32-character string constant in the app's login request class) and the AES
prefix (the "prefix" string constant in its `AESCipher` class). It only prints them and writes nothing; copy the lines
to where you want them (below). If the app's layout changes it says which constant it could not identify.

Put the values in environment variables, in `./.env`, or in `~/.imcam/config.env`
(`%USERPROFILE%\.imcam\config.env` on Windows), looked up in that order, as `NAME=value` lines:

    IMCAM_VKEY=<the app's vkey>
    IMCAM_AES_PREFIX=<the app's AES key prefix>

`imcam login` and the other online commands need `IMCAM_VKEY`; `watch` and `monitor` also need `IMCAM_AES_PREFIX`.
Without them, imcam stops with an error naming the missing setting. `imcam doctor` shows what is set.

## Notifications

Linux `notify-send`, macOS `osascript`, Windows toast via `winotify`. If none works the alert is still logged.

## Develop

    pip install -e ".[test]" && pytest
