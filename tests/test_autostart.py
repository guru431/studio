"""Автозапуск (настоящий реестр HKCU, тестовый ключ) и install.ps1 (парсер PowerShell) — `integration`."""

import subprocess
import winreg

import pytest

from studio_agent import autostart

pytestmark = pytest.mark.integration

TEST_ROOT = r"Software\studio-agent-test"
TEST_KEY = TEST_ROOT + r"\Run"


@pytest.fixture
def key():
    yield TEST_KEY
    for k in (TEST_KEY, TEST_ROOT):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
        except FileNotFoundError:
            pass


def test_set_enabled_writes_and_removes_the_value(key):
    assert not autostart.enabled(key=key)
    autostart.set_enabled(True, key=key)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        assert winreg.QueryValueEx(k, autostart.VALUE) == (autostart.command(), winreg.REG_SZ)
    assert autostart.enabled(key=key)
    autostart.set_enabled(True, key=key)  # повтор безопасен
    autostart.set_enabled(False, key=key)
    assert not autostart.enabled(key=key)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k, pytest.raises(FileNotFoundError):
        winreg.QueryValueEx(k, autostart.VALUE)
    autostart.set_enabled(False, key=key)


def test_install_ps1_has_bom_crlf_and_parses():
    path = autostart.REPO / "install.ps1"
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and not data.startswith(b"\xef\xbb\xbf" * 2)
    assert data.count(b"\n") == data.count(b"\r\n") > 0
    script = ("$t = $null; $e = $null; "
              f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$t, [ref]$e); "
              "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       timeout=60)
    assert r.returncode == 0, r.stdout.decode("cp866", "replace")
