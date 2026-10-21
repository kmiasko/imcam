# imcam

Unofficial Linux/Windows client for ImCam (`com.cylan.chatcam`) baby cameras: live view, recording, and
"baby is awake" alerts (through the separate baby-tracker program).

Not affiliated with the camera vendor. Protocol knowledge comes from interoperability analysis of the
public Android app; no vendor code or data is included.

## Install

Needs Python 3.10+ and **ffmpeg + ffplay on PATH**
(Linux: your package manager; Windows: `winget install Gyan.FFmpeg`, then reopen the terminal).

    pipx install .          # or: pip install .
    imcam doctor            # checks ffmpeg, Python deps, baby-tracker

## Use

    imcam login <account> <password>
    imcam devlist
    imcam watch <serial> <permit>                 # ffplay window
    imcam watch <serial> <permit> --track         # ... plus awake tracking (runs baby-tracker)
    imcam monitor <serial> <permit> [--record DIR]   # headless, reconnects forever, runs baby-tracker
    imcam snapshot -o frame.png                   # one frame of the running stream, for baby-tracker calibration

Awake tracking, calibration, tuning and training are in the separate
[baby-tracker](../../baby-tracker) program (`baby-tracker calibrate --image frame.png`, `evaluate`, `tune`, `train`).
Install it so that `baby-tracker` is on the PATH; `imcam doctor` checks this. `imcam monitor` starts it with
`~/.imcam/track.sdp` as input and stops it on exit.

Data (session, SDP files) lives in `~/.imcam` (`%USERPROFILE%\.imcam` on Windows); set `IMCAM_HOME` to move it.
baby-tracker keeps its own data in `~/.baby-tracker`. Earlier versions kept `roi.json`, `classifier.json` and `models/` in
`~/.imcam`; they are not migrated, so copy them to `~/.baby-tracker`.

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

## Develop

    pip install -e ".[test]" && pytest
