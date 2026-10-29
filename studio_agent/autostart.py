"""Автозапуск при входе в Windows: значение `StudioAgent` в `HKCU\\...\\Run` (Task Scheduler не трогаем)."""

import os
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "StudioAgent"
REPO = Path(__file__).resolve().parent.parent


def command() -> str:
    """`"<repo>\\.venv\\Scripts\\pythonw.exe" "<repo>\\studio_agent.pyw"` — без консоли."""
    return f'"{REPO / ".venv" / "Scripts" / "pythonw.exe"}" "{REPO / "studio_agent.pyw"}"'


def enabled(key: str = RUN_KEY) -> bool:
    """Значение есть и запускает эту копию (`command()`); указывает на другую копию моста — не включён."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, VALUE)
    except OSError:
        return False
    return isinstance(value, str) and os.path.normcase(value.strip()) == os.path.normcase(command())


def set_enabled(on: bool, key: str = RUN_KEY) -> None:
    if on:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE)
    except FileNotFoundError:
        pass
