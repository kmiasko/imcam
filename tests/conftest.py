import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Tests never read the user's ~/.imcam (classifier.json, roi.json) or download the person model."""
    monkeypatch.setenv("IMCAM_HOME", str(tmp_path / "imcam-home"))
    from imcam import seed

    monkeypatch.setattr(seed, "make_seeder", lambda cfg: None)
