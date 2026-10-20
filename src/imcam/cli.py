#!/usr/bin/env python3
"""ImCam (com.cylan.chatcam) Linux/Windows client: command dispatch, the WebRTC call and the monitor loop.
(Cloud protocol: cloud.py. Video relay: relay.py. Awake tracking: tracker.py.)

Commands:
  imcam selftest | doctor
  imcam extract-keys <app.apk|.xapk>        print the app's vkey / AES prefix as NAME=value lines (needs imcam[keys])
  imcam extract-offer <app-log> [-o FILE]   install the official app's offer SDP (required by watch/monitor)
  imcam login <account> <passwd>
  imcam code-get <account>
  imcam code-check <account> <code> [token]
  imcam register <account> <passwd> <code> [token] [country]
  imcam devlist | account | logout
  imcam call <msgid> [json-body]
  imcam watch <serial> <permit> [--track] [--record DIR]   (ffplay window)
  imcam monitor <serial> <permit> [--record DIR] [--input FILE]
                            headless awake-tracking with reconnect; --input replays a file
  imcam calibrate [--input FILE]   click crib ROI + lying line -> ~/.imcam/roi.json
  imcam evaluate --input FILE --wakes T1,T2,... [--tol SEC] [--csv OUT]
                            replay a recording, score awake alerts against labelled wake times (s)
  (needs ffmpeg/ffplay on PATH; `imcam doctor` checks the installation)
"""

import json
import os
import sys
import time

from . import (
    call,
    cloud,
    config,
    extract_keys,
    extract_offer,
    paths,
    relay,
    tracker,
)  # tracker is stdlib-only at import; numpy/cv2 load lazily inside it


def do_watch(c, serial, permit, track=False, record=None):
    """Sign in to the /im signaling server, send a WebRTC callRequest to the device
    and show the incoming video in ffplay until Ctrl-C. Needs aiortc (~/.imcam/venv).
    --track / --record also feed the stream to tracker.py's ffmpeg."""
    call.offer_template_path()  # fail before opening any window
    relay.install((relay.RTP_PORT,) + ((relay.TRACK_PORT,) if record or track else ()))
    player = relay.start_ffplay()
    live = _start_tracker(record, track)
    try:
        call.call_once(c, serial, permit)
    except KeyboardInterrupt:
        pass
    finally:
        player.terminate()
        if live:
            live.stop()


def _start_tracker(record, track):
    if not (record or track):
        return None
    paths.require_tool("ffmpeg")
    return tracker.LiveTracker(record, track)


def do_monitor(c, serial, permit, record=None):
    """Headless: no ffplay, tracking on, reconnects forever with exponential backoff (cap 30 s)."""
    call.offer_template_path()  # fail before connecting
    relay.install((relay.TRACK_PORT,))
    live = _start_tracker(record, True)
    backoff = 1
    try:
        while True:
            started = time.monotonic()
            try:
                if not c.alive:
                    c.connect()
                cloud.ensure_logged_in(c)
                call.call_once(c, serial, permit)
            except (call.OfferTemplateMissing, config.ConfigMissing):
                raise  # retrying cannot fix this
            except (Exception, SystemExit) as e:
                print("monitor: %s" % (e or type(e).__name__), file=sys.stderr)
            # a call that lived a while means the link was good: start backing off from scratch
            backoff = 1 if time.monotonic() - started > 60 else min(backoff * 2, 30)
            print("monitor: reconnecting in %ds" % backoff, file=sys.stderr)
            time.sleep(backoff)
    except KeyboardInterrupt:
        pass
    finally:
        live.stop()


def selftest():
    """Run the repo's pytest suite when pytest and tests/ are available (a source checkout)."""
    tests = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "tests")
    try:
        import pytest
    except ImportError:
        pytest = None
    if pytest is None or not os.path.isdir(tests):
        print("selftest skipped: needs pytest and the source checkout's tests/")
        return
    code = pytest.main(["-q", os.path.normpath(tests)])
    if code:
        raise SystemExit(code)
    print("selftest ok")


def doctor():
    """Report what is installed; returns the process exit code."""
    import importlib
    from . import notify

    ok = True
    for tool in ("ffmpeg", "ffplay"):
        found = __import__("shutil").which(tool)
        print(
            "%-8s %s"
            % (
                tool,
                found
                or "MISSING (needed for %s)"
                % ("watch/monitor" if tool == "ffmpeg" else "watch"),
            )
        )
        ok &= bool(found) or tool == "ffplay"
    for mod in ("aiortc", "av", "numpy", "cv2"):
        try:
            importlib.import_module(mod)
            print("%-8s ok" % mod)
        except ImportError as e:
            print("%-8s MISSING (%s)" % (mod, e))
            ok = False
    for name in config.SETTINGS:
        try:
            config.get(name)
            print("%-8s %s set" % ("config", name))
        except config.ConfigMissing as e:
            print("%-8s %s MISSING\n%s" % ("config", name, e))
            ok = False
    try:
        print("offer    %s" % call.offer_template_path())
    except call.OfferTemplateMissing as e:
        print("offer    MISSING (run `imcam extract-offer`)\n%s" % e)
        ok = False
    print("notify   %s" % (notify.backend() or "none (messages are only logged)"))
    print("data dir %s" % paths.home())
    return 0 if ok else 1


def _opt(args, name, flag=False):
    """Remove `name` (and its value unless flag) from args in place and return it."""
    if name not in args:
        return False if flag else None
    i = args.index(name)
    if flag:
        del args[i]
        return True
    if i + 1 >= len(args):
        raise SystemExit("%s needs a value" % name)
    val = args[i + 1]
    del args[i : i + 2]
    return val


def main():
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd == "selftest":
        selftest()
        return
    record = _opt(args, "--record")
    infile = _opt(args, "--input")
    track = _opt(args, "--track", flag=True)
    if cmd == "doctor":
        sys.exit(doctor())
    if cmd == "extract-keys":
        extract_keys.main(args[1:])
        return
    if cmd == "extract-offer":
        extract_offer.main(args[1:])
        return
    if cmd in ("calibrate", "monitor") and infile:  # offline: no network
        tracker.calibrate(infile) if cmd == "calibrate" else tracker.run_file(infile)
        return
    if cmd == "evaluate":
        wakes, tol, csv_out = (
            _opt(args, "--wakes"),
            _opt(args, "--tol"),
            _opt(args, "--csv"),
        )
        if not infile or wakes is None:
            raise SystemExit("usage: imcam evaluate --input FILE --wakes T1,T2,...")
        tracker.evaluate_file(
            infile,
            [float(t) for t in wakes.split(",") if t],
            float(tol) if tol else 30.0,
            csv_out,
        )
        return
    if cmd == "calibrate":
        tracker.calibrate()
        return
    if cmd in ("watch", "monitor"):
        try:
            call.offer_template_path()  # before any network traffic
        except call.OfferTemplateMissing as e:
            sys.exit("error: %s" % e)
    try:
        config.require(
            "IMCAM_VKEY",
            *(("IMCAM_AES_PREFIX",) if cmd in ("watch", "monitor") else ()),
        )
    except config.ConfigMissing as e:
        sys.exit("error: %s" % e)
    url = args[1] if len(args) > 1 and args[1].startswith(("ws://", "wss://")) else None
    c = cloud.Client(url=url)
    c.connect()
    try:
        if cmd == "login":
            cloud.login(c, args[1], args[2])
        elif cmd == "code-get":
            cloud.ensure_logged_in(c)
            print(json.dumps(c.send("cli_code_get", {"account": args[1]})))
        elif cmd == "code-check":
            cloud.ensure_logged_in(c)
            print(
                json.dumps(
                    cloud.code_check(
                        c, args[1], args[2], args[3] if len(args) > 3 else ""
                    )
                )
            )
        elif cmd == "register":
            cloud.ensure_logged_in(c)
            print(
                json.dumps(
                    cloud.register(
                        c,
                        args[1],
                        args[2],
                        args[3] if len(args) > 3 else "",
                        args[4] if len(args) > 4 else "",
                    )
                )
            )
        elif cmd == "devlist":
            cloud.ensure_logged_in(c)
            print(json.dumps(c.send("cli_dev_list", {})))
        elif cmd == "account":
            cloud.ensure_logged_in(c)
            print(json.dumps(c.send("cli_account_info_get", {})))
        elif cmd == "call":
            cloud.ensure_logged_in(c)
            body = json.loads(args[2]) if len(args) > 2 else {}
            print(json.dumps(c.send(args[1], body)))
        elif cmd == "watch":
            if len(args) < 3:
                raise SystemExit("usage: imcam watch <device-serial> <permit-code>")
            cloud.ensure_logged_in(c)
            do_watch(c, args[1], args[2], track, record)
        elif cmd == "monitor":
            if len(args) < 3:
                raise SystemExit(
                    "usage: imcam monitor <device-serial> <permit-code> [--record DIR] [--input FILE]"
                )
            cloud.ensure_logged_in(c)
            do_monitor(c, args[1], args[2], record)
        elif cmd == "logout":
            cloud.ensure_logged_in(c)
            c.send("cli_logout", {})
            print("logged out")
        else:
            print("unknown command: %s" % cmd, file=sys.stderr)
            sys.exit(1)
    except (
        cloud.RspException,
        TimeoutError,
        call.OfferTemplateMissing,
        config.ConfigMissing,
    ) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    finally:
        c.close()


if __name__ == "__main__":
    main()
