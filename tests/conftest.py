import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Каталог моста во временной папке теста."""
    monkeypatch.setenv("STUDIO_AGENT_HOME", str(tmp_path))
    return tmp_path
