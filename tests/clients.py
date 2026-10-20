"""Прогон клиентских скриптов: PowerShell-версия (Windows) и Python-версия (Linux, macOS).

Одни и те же сценарии гоняются против обеих. Параметры в тестах пишутся по-PowerShell
(-NoSmooth); у Python-клиента это ключи --no-smooth — так тесты заодно сторожат, что имена
параметров у двух версий совпадают.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

# PowerShell стартует секунды — integration; Python-клиент — в быстром наборе
RUNNERS = [pytest.param("ps", marks=pytest.mark.integration), "py"]
# Вывод клиента — UTF-8 и в трубу Windows (иначе cp1251 не примет «→»)
UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def py_args(args):
    """-PromptsFile x -NoSmooth → --prompts-file x --no-smooth."""
    return ["--" + re.sub(r"(?<!^)(?=[A-Z])", "-", a[1:]).lower() if re.fullmatch(r"-[A-Z]\w*", a) else a
            for a in args]


def run_client(kind, script, args):
    """script — путь к .ps1; Python-версия лежит рядом под тем же именем с .py."""
    if kind == "ps":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            pytest.skip("нет powershell — прогон скрипта пропущен")
        return subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(script), *args], capture_output=True, timeout=25)
    return subprocess.run([sys.executable, str(script.with_suffix(".py")), *py_args(args)],
                          capture_output=True, timeout=25, env=UTF8_ENV)
