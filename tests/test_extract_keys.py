import io
import zipfile

import pytest

from imcam import extract_keys

V = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"  # synthetic 32-char alphanumeric
P = "Example-AES-password-prefix-"


class Ins:
    def __init__(self, s, name="const-string"):
        self.s, self.name = s, name

    def get_name(self):
        return self.name

    def get_output(self):
        return 'v0, "%s"' % self.s


class Method:
    def __init__(self, name, strings):
        self.name, self.ins = name, [Ins(s) for s in strings] + [Ins("", "return-void")]

    def get_name(self):
        return self.name

    def get_instructions(self):
        return self.ins


class Cls:
    def __init__(self, name, methods):
        self.name, self.methods = name, methods

    def get_name(self):
        return self.name

    def get_methods(self):
        return self.methods


class Dex:
    def __init__(self, *classes):
        self.classes = classes

    def get_classes(self):
        return self.classes


def dex(vkeys=(V,), prefixes=(P,)):
    return Dex(
        Cls(
            "Lcom/x/biz/LoginReq;",
            [Method("<init>", list(vkeys) + ["com.example.app", "2.0.0"])],
        ),
        Cls(
            "Lcom/x/biz/OpenLoginReq;", [Method("<init>", ["Z" * 32])]
        ),  # other classes are ignored
        Cls(
            "Lcom/x/AESCipher;",
            [Method("<clinit>", list(prefixes)), Method("md5", ["MD5"])],
        ),
    )


def test_find_keys():
    assert extract_keys.find_keys([Dex(), dex()]) == {
        "IMCAM_VKEY": V,
        "IMCAM_AES_PREFIX": P,
    }


@pytest.mark.parametrize(
    "d, what",
    [
        (dex(vkeys=()), "vkey"),
        (dex(prefixes=()), "AES prefix"),
        (dex(vkeys=(V, V[::-1])), "2 different candidates"),
    ],
)
def test_find_keys_problems(d, what):
    with pytest.raises(SystemExit, match=what):
        extract_keys.find_keys([d])


def test_main_prints_lines_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(extract_keys, "_dexes", lambda path: [dex()])
    extract_keys.main(["x.apk"])
    out = capsys.readouterr()
    assert out.out.splitlines() == ["IMCAM_AES_PREFIX=" + P, "IMCAM_VKEY=" + V]
    assert not (tmp_path / "home").exists()


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_code_apks_plain_apk(tmp_path):
    f = tmp_path / "a.apk"
    f.write_bytes(_zip({"classes.dex": b"x", "AndroidManifest.xml": b"m"}))
    assert extract_keys.code_apks(str(f)) == [None]


def test_code_apks_xapk_skips_splits(tmp_path):
    base = _zip({"classes.dex": b"x", "classes2.dex": b"y"})
    f = tmp_path / "a.xapk"
    f.write_bytes(
        _zip(
            {
                "manifest.json": b"{}",
                "pkg.apk": base,
                "config.en.apk": _zip({"resources.arsc": b"r"}),
                "icon.png": b"p",
            }
        )
    )
    assert extract_keys.code_apks(str(f)) == [base]


@pytest.mark.parametrize(
    "entries, msg",
    [
        (
            {"config.en.apk": _zip({"resources.arsc": b"r"}), "icon.png": b"p"},
            "no app code",
        ),
        ({"readme.txt": b"hi"}, "no app code"),
    ],
)
def test_code_apks_no_code(tmp_path, entries, msg):
    f = tmp_path / "a.xapk"
    f.write_bytes(_zip(entries))
    with pytest.raises(SystemExit, match=msg):
        extract_keys.code_apks(str(f))


def test_code_apks_not_a_zip(tmp_path):
    f = tmp_path / "a.xapk"
    f.write_text("nope")
    with pytest.raises(SystemExit, match="cannot open"):
        extract_keys.code_apks(str(f))
