import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.pytest_support import new_event_loop


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Очистка pytest моста не затрагивает временные каталоги других проектов."""
    if config.option.basetemp is not None or os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        return
    root = ROOT / ".tmp" / ".pytest_cache" / "agent"
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


@pytest.fixture(autouse=True)
def macos_test_secrets(monkeypatch, request):
    """Быстрые тесты не открывают Keychain и не создают в нём сотни временных записей."""
    if (sys.platform == 'darwin' and request.node.get_closest_marker('integration') is None
            and request.module.__name__ != 'test_macos'):
        from cryptography.fernet import Fernet
        from studio_agent import macos_keychain
        key = Fernet.generate_key()
        monkeypatch.setattr(macos_keychain, '_key', lambda account: key)
