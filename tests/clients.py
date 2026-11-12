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


# Windows PowerShell пишет в трубу кодовой страницей унаследованной консоли (ручной прогон — 866, ночной — 65001):
# в 866 нет «—» и «→», и он сам меняет их на «-» и «\x1a» — декодированием этого не вернуть. Поэтому скрипт
# запускается из -Command, где вывод сперва переключён на UTF-8; коды выхода — как у -File: exit N скрипта,
# 1 при исключении или ошибке разбора (-Command сам вернул бы 0).
PS_WRAP = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; & {script} {args}; $ok = $?; "
           "if ($LASTEXITCODE) {{ exit $LASTEXITCODE }}; if (-not $ok) {{ exit 1 }}; exit 0")
PS_PARAM = re.compile(r"-[A-Z]\w*")


def ps_literal(text):
    return "'" + text.replace("'", "''") + "'"


def flat(text):
    """Вывод одной строкой: Write-Warning переносит текст по ширине консоли процесса тестов
    (из окна в 80 знаков «(503): карта занята» приходит как «(503): \\r\\nкарта занята»), а
    проверки ищут фразы — сравнивать их можно только со схлопнутыми пробелами."""
    return re.sub(r"\s+", " ", text)


def py_args(args):
    """-PromptsFile x -NoSmooth → --prompts-file x --no-smooth."""
    return ["--" + re.sub(r"(?<!^)(?=[A-Z])", "-", a[1:]).lower() if re.fullmatch(r"-[A-Z]\w*", a) else a
            for a in args]


def run_client(kind, script, args):
    """script — путь к .ps1; Python-версия лежит рядом под тем же именем с .py. Вывод обеих — UTF-8."""
    if kind == "ps":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            pytest.skip("нет powershell — прогон скрипта пропущен")
        command = PS_WRAP.format(script=ps_literal(str(script)),
                                 args=" ".join(a if PS_PARAM.fullmatch(a) else ps_literal(a) for a in args))
        return subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", command], capture_output=True, timeout=25)
    return subprocess.run([sys.executable, str(script.with_suffix(".py")), *py_args(args)],
                          capture_output=True, timeout=25, env=UTF8_ENV)
