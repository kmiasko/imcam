"""Vendor constants that imcam does not ship: the app's `vkey` and the AES key prefix.

Looked up in this order: environment variable, `.env` in the current directory, `~/.imcam/config.env`
(`%USERPROFILE%\\.imcam\\config.env` on Windows). Files hold `NAME=value` lines; `#` starts a comment.
"""
import os

from . import paths

SETTINGS = {
    "IMCAM_VKEY": "the app's `vkey` (32 characters)",
    "IMCAM_AES_PREFIX": "the app's AES key prefix string",
}


class ConfigMissing(Exception):
    """A required vendor constant is not configured."""


def env_files():
    return [os.path.join(os.getcwd(), ".env"), paths.path("config.env")]


def _parse(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


def get(name):
    """Value of a setting, or raise ConfigMissing with instructions."""
    if os.environ.get(name):
        return os.environ[name]
    for path in env_files():
        v = _parse(path).get(name)
        if v:
            return v
    raise ConfigMissing(
        "%s is not set: %s.\n"
        "imcam does not ship vendor constants. Put it in the environment, in ./.env, or in %s as:\n"
        "    %s=<value>\n"
        "Or print both from the official app's APK: imcam extract-keys <app.apk>\n"
        "See the README section \"Vendor constants\"." % (name, SETTINGS[name], paths.path("config.env"), name))


def require(*names):
    for n in names:
        get(n)


def vkey():
    return get("IMCAM_VKEY")


def aes_prefix():
    return get("IMCAM_AES_PREFIX")
