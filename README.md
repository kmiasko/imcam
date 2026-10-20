# imcam

Unofficial Linux/Windows client for ImCam (`com.cylan.chatcam`) baby cameras: live view, recording, and
"baby is awake" alerts (sitting/standing silhouette inside a crib region you calibrate).

Not affiliated with the camera vendor. Protocol knowledge comes from interoperability analysis of the
public Android app; no vendor code or data is included (see "The app offer" below).

## Install

Needs Python 3.10+ and **ffmpeg + ffplay on PATH**
(Linux: your package manager; Windows: `winget install Gyan.FFmpeg`, then reopen the terminal).

    pipx install .          # or: pip install .
    imcam doctor            # checks ffmpeg, Python deps, the app offer, notification backend

## Use

    imcam login <account> <password>
    imcam devlist
    imcam watch <serial> <permit>                 # ffplay window
    imcam watch <serial> <permit> --track         # ... plus awake tracking
    imcam calibrate                               # with `watch`/`monitor` running; click crib corners, then 2 points of the lying head level
    imcam monitor <serial> <permit> [--record DIR]   # headless, reconnects forever
    imcam monitor --input rec.mkv                 # replay a recording to tune thresholds
    imcam evaluate --input rec.mkv --wakes 120,3400   # score alerts vs. labelled wake times (s); --csv dumps observations

Data (session, `roi.json`, SDP files) lives in `~/.imcam` (`%USERPROFILE%\.imcam` on Windows); set `IMCAM_HOME` to move it.
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

## The app offer

The camera only streams when our WebRTC offer looks like the official app's, so `watch` and `monitor` need a copy of
that offer as `~/.imcam/app_offer.sdp` (or the file named by `IMCAM_APP_OFFER`). It is **not shipped** with imcam, and
it is **not inside the APK**: libwebrtc generates it at runtime when the app starts a call. Without it, `watch` and
`monitor` stop with an error that points here, and `imcam doctor` reports it as missing.

Get it from the official app's own signaling log:

1. Connect an Android phone with the official app and USB debugging (`adb`).
2. `adb logcat -c`, then open a live view of the camera in the app.
3. `adb logcat -d > app.log`
4. `imcam extract-offer app.log` (writes `~/.imcam/app_offer.sdp`; `-o FILE` writes elsewhere)

The extractor looks for the offer SDP (`v=0` ... `m=video` ... `a=fingerprint`) in the log, as a JSON string or as
plain multi-line text, and uses the last one found. If your app build doesn't log it, it reports that no offer was found;
any other way of obtaining the offer text works too, just save it to the path above. Only your own ICE credentials and
fingerprint are substituted into it at call time.

## Notifications

Linux `notify-send`, macOS `osascript`, Windows toast via `winotify`. If none works the alert is still logged.

## Develop

    pip install -e ".[test]" && pytest
