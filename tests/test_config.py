import pytest

from imcam import cloud, config


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    for n in config.SETTINGS:
        monkeypatch.delenv(n, raising=False)


def test_missing_setting_has_instructions(tmp_path):
    with pytest.raises(config.ConfigMissing, match="IMCAM_VKEY") as e:
        config.vkey()
    assert "config.env" in str(e.value) and "README" in str(e.value)


def test_lookup_order(tmp_path, monkeypatch):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.env").write_text(
        "# c\nIMCAM_VKEY=from-home\nexport IMCAM_AES_PREFIX='pre-'\n"
    )
    assert config.vkey() == "from-home" and config.aes_prefix() == "pre-"
    (tmp_path / ".env").write_text('IMCAM_VKEY="from-cwd"\n')
    assert config.vkey() == "from-cwd"
    monkeypatch.setenv("IMCAM_VKEY", "from-env")
    assert config.vkey() == "from-env"


def test_login_body_uses_config(monkeypatch):
    monkeypatch.setenv("IMCAM_VKEY", "k")
    assert cloud.login_body("a", "b")["vkey"] == "k"
