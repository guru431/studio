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
# Вывод клиента — UTF-8 и в трубу Windows (иначе cp1251 заменит «→» на «?», а тесты ищут слова)
UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def ps_encoding():
    """Кодировка вывода PowerShell в трубу — кодовая страница консоли: дочерний powershell
    наследует консоль процесса тестов (ночной прогон идёт под 65001, ручной — под 866).
    Консоли нет (0) — дочерний получит новую с умолчанием, то есть OEM."""
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except (ImportError, AttributeError, OSError):
        return "utf-8"      # не Windows: pwsh пишет UTF-8
    return f"cp{cp}" if cp else "oem"


def flat(text):
    """Вывод одной строкой: Write-Warning переносит текст по ширине консоли процесса тестов
    (из окна в 80 знаков «(503): карта занята» приходит как «(503): \\r\\nкарта занята»), а
    проверки ищут фразы — сравнивать их можно только со схлопнутыми пробелами."""
    return re.sub(r"\s+", " ", text)


def py_args(args):
    """-PromptsFile x -NoSmooth → --prompts-file x --no-smooth."""
    return ["--" + re.sub(r"(?<!^)(?=[A-Z])", "-", a[1:]).lower() if re.fullmatch(r"-[A-Z]\w*", a) else a
            for a in args]


# Строки, которые -MakeLocal / --make-local вписывает в копию: ключ и умолчание адреса шлюза.
LOCAL_LINES = re.compile(r"^(\$EmbeddedKey = |EMBEDDED_KEY = |URL = "
                         r"|\s*\[Parameter\(ParameterSetName = 'Run'\)\]\[string\]\$Url = )")


def local_copy_drift(script, local):
    """Номера строк (с 1), где копия с ключом расходится с отслеживаемым скриптом, — кроме строк ключа и
    адреса. Пусто — копия собрана из этой версии. Текст строк не возвращается: в копии ключ."""
    ours, theirs = (p.read_bytes().decode("utf-8-sig").splitlines() for p in (script, local))
    drift = [n for n, (a, b) in enumerate(zip(ours, theirs), start=1)
             if a != b and not (LOCAL_LINES.match(a) and LOCAL_LINES.match(b))]
    if len(ours) != len(theirs):
        drift.append(min(len(ours), len(theirs)) + 1)
    return drift


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
