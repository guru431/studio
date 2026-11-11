"""Сбой завершения asyncio сохраняет имя теста и стек до завершения процесса."""
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
def test_runner_teardown_timeout_keeps_test_name_and_stack(tmp_path, request):
    case = tmp_path / "test_hung_runner.py"
    case.write_text('''
import asyncio


async def ignores_cancel():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.Event().wait()


async def test_hung_runner():
    asyncio.create_task(ignores_cancel())
    await asyncio.sleep(0)
''', encoding="utf-8")
    config = Path(__file__).resolve().parents[1] / "pytest.ini"
    # Те же настройки набора; диагностические сроки сокращены только для этой пробы.
    diagnostic_timeout = min(1, float(request.config.getini("faulthandler_timeout")))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", str(config), "-m", "",
         "-o", f"faulthandler_timeout={diagnostic_timeout}", "--timeout=2", "--timeout-method=thread", str(case)],
        cwd=tmp_path, capture_output=True, encoding="utf-8", errors="replace", timeout=20)
    assert result.returncode != 0
    assert "::test_hung_runner" in result.stdout, result.stdout
    assert "Timeout (0:00:01)!" in result.stderr, result.stderr
    assert "_cancel_all_tasks" in result.stderr and "in close" in result.stderr, result.stderr
