# imcam

Unofficial Linux/Windows client for ImCam (`com.cylan.chatcam`) baby cameras: live view, recording, and
"baby is up" alerts (through the separate babywatch program).

Not affiliated with the camera vendor. Protocol knowledge comes from interoperability analysis of the
public Android app; no vendor code or data is included.

## Install

Needs Python 3.10+ and **ffmpeg + ffplay on PATH**
(Linux: your package manager; Windows: `winget install Gyan.FFmpeg`, then reopen the terminal).

    pipx install .          # or: pip install .
    imcam doctor            # checks ffmpeg, ffplay, babywatch, notify-send, audio analyzer, Python deps, vendor constants

## Use

    imcam login <account> <password>
    imcam devlist
    imcam account                                 # account info
    imcam logout
    imcam watch <serial> <permit>                 # ffplay window
    imcam watch <serial> <permit> --track         # ... plus wake alerts (runs babywatch)
    imcam watch <serial> <permit> --record DIR    # ... plus .mkv recording (also starts babywatch)
    imcam monitor <serial> <permit> [--record DIR]   # headless, reconnects forever, runs babywatch
    imcam snapshot -o frame.png                   # one frame of the running stream (e.g. for `babywatch set-roi --ref`)

Other commands: `code-get <account>`, `code-check <account> <code> [token]`, `register <account> <password> <code> [token] [country]`
(account creation), `call <msgid> [json-body]` (raw cloud request), `selftest` (runs the test suite from a source checkout).

Wake detection is done by the separate babywatch program (its own commands handle crib positions and training; see its
`docs/architecture-and-usage.md`). Install it so that `babywatch` is on the PATH; `imcam doctor` checks this. babywatch cannot read
imcam's RTP `.sdp`, so `--track`, `--record` and `monitor` start an ffmpeg that copies `~/.imcam/track.sdp` to MPEG-TS on
`udp://127.0.0.1:47308` (and writes 10-minute `.mkv` segments with `--record`), plus `babywatch live --rtsp udp://127.0.0.1:47308`.
If either process exits, imcam starts it again (the ffmpeg once video arrives).

### Streams

While `--track`, `--record` or `monitor` runs, the ffmpeg bridge publishes two local MPEG-TS streams:

| URL | Content | Read by |
| --- | --- | --- |
| `udp://127.0.0.1:47308` | camera video (H.265) | babywatch |
| `udp://127.0.0.1:47314` | camera audio (Opus) | anything you like, e.g. `ffplay udp://127.0.0.1:47314` or an audio analyzer |

UDP output needs no reader; packets nobody reads are dropped. `--record` segments hold video and audio, and
`imcam watch` plays the sound in the ffplay window. The camera must answer with Opus audio; otherwise imcam logs that
audio is not relayed and video works as before.

An audio analyzer (a separate program, like babywatch for video) can be run on the audio stream with
`IMCAM_AUDIO_ANALYZER`. imcam runs that command with the audio URL appended, restarts it if it exits, and reads JSON
lines from its stdout: `{"event": "noise", "message": "loud for 5 s", "push": true}` becomes event `noise` (below),
pushed when `push` is true. The analyzer does its own cooldown. Other stdout lines are ignored.

### Alerts

| Event | When | Pushed |
| --- | --- | --- |
| `awake` | babywatch reports the baby upright in the crib with no adult in view | yes, at most once per cooldown |
| `down` | the baby is no longer up (lying, out of view, or an adult came) | no, logged only |
| `stream_lost` | no video from the camera for `IMCAM_STREAM_LOST_S` seconds | yes |
| `stream_restored` | video is back after `stream_lost` | yes |
| any name | printed by the `IMCAM_AUDIO_ANALYZER` program | when it says `"push": true` |

Every event is a line on stderr. Pushed events also become a desktop notification (`notify-send` on Linux, `osascript` on
macOS, `winotify` toasts on Windows, a beep if that fails) and run your hook command, if one is set. Optional settings
go in the same places as the vendor constants below (environment, `./.env`, `~/.imcam/config.env`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `IMCAM_NOTIFY_HOOK` | (none) | Command run on each pushed event, with `IMCAM_EVENT` and `IMCAM_MESSAGE` in its environment. It is split into words and run without a shell; use a script for pipes, e.g. to send the message with ntfy or a phone push service. |
| `IMCAM_NOTIFY_COOLDOWN` | 600 | Minimum seconds between pushed `awake` alerts |
| `IMCAM_STREAM_LOST_S` | 60 | Seconds without video before `stream_lost` |
| `IMCAM_AUDIO_ANALYZER` | (none) | Command run on the audio stream (URL appended; split like the hook). Its JSON lines become events. |
| `IMCAM_PLAYER` | (none) | `watch` player instead of ffplay (path of `~/.imcam/live.sdp` appended; split like the hook), e.g. `mpv --demuxer-lavf-o=protocol_whitelist=[file,udp,rtp] --profile=low-latency --untimed` |

### Event log

`monitor` writes every event to an SQLite database, `~/.imcam/events.db`, so a night can be looked at later. It is in WAL
mode: `sqlite3` or another program can read it while imcam runs. A failed write prints one `eventlog:` line and imcam
keeps running.

```sql
CREATE TABLE events (
  id      INTEGER PRIMARY KEY,
  ts      REAL NOT NULL,      -- unix time when imcam received the event
  source  TEXT NOT NULL,      -- 'imcam', 'babywatch', or the analyzer program name (e.g. 'babylisten')
  name    TEXT NOT NULL,      -- event name
  message TEXT,
  push    INTEGER NOT NULL DEFAULT 0,
  data    TEXT                -- the producer's JSON object, as received
);
```

| Source | Names | data |
| --- | --- | --- |
| `imcam` | `session_start`, `session_stop`, `restart` (a child exited), and the alerts `awake`, `down`, `stream_lost`, `stream_restored` | none |
| `babywatch` | `state`, one row per JSON line (also lines that change no alert); `message` is babywatch's `state` | the line |
| analyzer (`IMCAM_AUDIO_ANALYZER`) | the line's `event` | the line, with any extra fields (`db`, `rate_bpm`, ...) |

A new analyzer that prints the same JSON lines is logged with no change to imcam. Example, the last 8 hours:

```sh
sqlite3 ~/.imcam/events.db "select datetime(ts,'unixepoch','localtime'), source, name, message from events where ts > unixepoch('now','-8 hours') order by ts"
```

### Stopping

Ctrl+C, closing the terminal (SIGHUP) and `kill` (SIGTERM) end the call with the camera and stop every process imcam started
(ffplay, the ffmpeg bridge, babywatch, the audio analyzer). ffmpeg gets time to finish the current `.mkv`. If imcam is killed with SIGKILL, the
kernel stops those processes on Linux. On other systems they are only stopped when imcam exits normally.

Data (session, SDP files, the event log `events.db`) lives in `~/.imcam` (`%USERPROFILE%\.imcam` on Windows); set `IMCAM_HOME` to move it.
babywatch keeps its own data (model, crib positions) in `~/.babywatch`.

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
