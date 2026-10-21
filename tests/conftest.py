import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Tests never read or write the user's ~/.imcam."""
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path / "imcam-home"))
