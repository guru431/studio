import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.pytest_support import new_event_loop


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Очистка pytest моста не затрагивает временные каталоги других проектов."""
    if config.option.basetemp is not None or os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        return
    root = Path(tempfile.gettempdir()) / "studio-agent-pytest"
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)


def pytest_asyncio_loop_factories(config, item):
    """Настоящий Windows Proactor с восстановлением чтения после WinError 995."""
    return {"studio": new_event_loop}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Каталог моста во временной папке теста."""
    monkeypatch.setenv("STUDIO_AGENT_HOME", str(tmp_path))
    return tmp_path
