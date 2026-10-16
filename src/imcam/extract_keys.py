"""Read the vendor constants imcam needs out of the official Android app's APK.

    imcam extract-keys base.apk            # (or app.xapk / .apks / .apkm) prints IMCAM_VKEY=... and IMCAM_AES_PREFIX=... lines

Nothing is written to disk; the user decides where the lines go (see config.py for where imcam looks).

Needs the optional dependency androguard (`pip install "imcam[keys]"`). Values are located structurally, not by
content: the `vkey` is the only 32-character alphanumeric string constant in the login request class
(`LoginReq`), and the AES prefix is the string constant containing "prefix" in `AESCipher`.
"""

import io
import re
import sys
import zipfile

from . import paths

_STR = re.compile(r'"(.*)"$', re.S)
_VKEY = re.compile(r"[A-Za-z0-9]{32}")


def _const_strings(cls):
    for m in cls.get_methods():
        for ins in m.get_instructions():
            if ins.get_name().startswith("const-string"):
                mo = _STR.search(ins.get_output())
                if mo:
                    yield m.get_name(), mo.group(1)


def scan_dex(dex):
    """({vkey candidates}, {prefix candidates}) from one parsed DEX (androguard DEX or compatible)."""
    vkeys, prefixes = set(), set()
    for cls in dex.get_classes():
        name = cls.get_name()
        if name.endswith("/LoginReq;"):
            vkeys |= {s for _, s in _const_strings(cls) if _VKEY.fullmatch(s)}
        elif name.endswith("/AESCipher;"):
            prefixes |= {s for _, s in _const_strings(cls) if "prefix" in s.lower()}
    return vkeys, prefixes


def find_keys(dexes):
    """dexes: iterable of parsed DEX objects. Returns {setting name: value}; raises SystemExit if not found."""
    vkeys, prefixes = set(), set()
    for dex in dexes:
        v, p = scan_dex(dex)
        vkeys |= v
        prefixes |= p
    problems = []
    for label, found in (
        ("vkey (a 32-character string in LoginReq)", vkeys),
        ('AES prefix (a "prefix" string in AESCipher)', prefixes),
    ):
        if len(found) != 1:
            problems.append(
                "%s: %s"
                % (
                    label,
                    "not found"
                    if not found
                    else "%d different candidates" % len(found),
                )
            )
    if problems:
        raise SystemExit(
            "could not identify the constants (the app's layout may have changed):\n  "
            + "\n  ".join(problems)
        )
    return {"IMCAM_VKEY": next(iter(vkeys)), "IMCAM_AES_PREFIX": next(iter(prefixes))}


def _has_dex(names):
    return any(n.startswith("classes") and n.endswith(".dex") for n in names)


def code_apks(path):
    """Where the app's code is: [None] when `path` is itself an APK with classes*.dex, or the bytes of each inner
    APK that has code when it is a bundle (.xapk / .apks / .apkm: a zip of a base APK plus config splits)."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise SystemExit("cannot open %s as an APK or XAPK: %s" % (path, e))
    with z:
        names = z.namelist()
        if _has_dex(names):
            return [None]
        found = []
        for n in names:
            if n.lower().endswith(".apk"):
                data = z.read(n)
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        if _has_dex(inner.namelist()):
                            found.append(data)
                except zipfile.BadZipFile:
                    pass
    if not found:
        raise SystemExit(
            "no app code (classes.dex) in %s; pass the app's base APK or the XAPK"
            % path
        )
    return found


def _dexes(apk_path):
    try:
        from loguru import logger

        logger.remove()  # androguard logs every parsed XML tag at debug level
        from androguard.core.apk import APK
        from androguard.core.dex import DEX
    except ImportError:
        raise SystemExit(
            'extract-keys needs androguard: pip install "imcam[keys]"  '
            "(pipx: pipx inject imcam androguard)"
        )
    for data in code_apks(apk_path):
        try:
            apk = APK(apk_path) if data is None else APK(data, raw=True)
        except Exception as e:
            raise SystemExit("cannot read %s as an APK: %s" % (apk_path, e))
        for blob in apk.get_all_dex():
            if (
                b"LoginReq" in blob or b"AESCipher" in blob
            ):  # class names are in the string table: skip other dex files
                yield DEX(blob)


def main(argv):
    if len(argv) != 1:
        raise SystemExit("usage: imcam extract-keys <app.apk|app.xapk>")
    values = find_keys(_dexes(argv[0]))
    print(
        "# Add these lines to the environment, to ./.env, or to %s"
        % paths.path("config.env"),
        file=sys.stderr,
    )
    for name in sorted(values):
        print("%s=%s" % (name, values[name]))


if __name__ == "__main__":
    main(sys.argv[1:])
