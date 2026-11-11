"""Срок теста в быстром наборе: зависший тест падает по сроку один раз и с именем, а не валит процесс xdist снова и
снова (`pytest_xdist_make_scheduler` в conftest.py). Набор — отдельным pytest в подпроцессе: секунды."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

HUNG = '''
import time
import pytest


@pytest.fixture
def hanging_fixture():
    yield
    TEARDOWN


@pytest.mark.timeout(2)
def test_hung(hanging_fixture):
    CALL


def test_fine():
    pass
'''


@pytest.mark.parametrize("phase", ["call", "teardown"])
def test_hung_test_crashes_its_worker_once(tmp_path, request, phase):
    case = HUNG.replace("CALL", "time.sleep(1000)" if phase == "call" else "pass")
    case = case.replace("TEARDOWN", "time.sleep(1000)" if phase == "teardown" else "pass")
    (tmp_path / "test_hung_case.py").write_text(case, encoding="utf-8")
    # Ускоряем тот же диагностический срок, который включает основной набор.
    diagnostic_timeout = min(1, float(request.config.getini("faulthandler_timeout")))
    (tmp_path / "pytest.ini").write_text(
        f"[pytest]\nfaulthandler_timeout = {diagnostic_timeout}\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}     # conftest.py набора — плагином `-p conftest`
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "conftest", "-p", "no:cacheprovider",
                          "-n", "2", "--dist", "loadgroup", "test_hung_case.py"], cwd=tmp_path,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    passed = 1 if phase == "call" else 2  # тело уже прошло, если процесс погиб при teardown
    assert f"1 failed, {passed} passed" in out.stdout, out.stdout[-2000:]
    assert out.stdout.count("node down: Not properly terminated") == 1, out.stdout[-2000:]
    assert "crashed while running 'test_hung_case.py::test_hung'" in out.stdout, out.stdout[-2000:]
    assert 'Timeout (0:00:01)!' in out.stderr, out.stderr[-4000:]
    frame = "in test_hung" if phase == "call" else "in hanging_fixture"
    assert 'test_hung_case.py' in out.stderr and frame in out.stderr, out.stderr[-4000:]


def test_failed_media_scenario_releases_its_writer_before_the_portal_closes(tmp_path):
    case = tmp_path / "test_failed_write.py"
    case.write_text('''import asyncio
import pytest


@pytest.mark.timeout(5)
def test_failed_write(studio, write_gate):
    hold, reached, _ = write_gate
    studio.c.portal.start_task_soon(asyncio.to_thread, hold)
    assert reached.wait(2)
    raise RuntimeError("deliberate write scenario failure")
''', encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "tests")}
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n0", "--tb=short", "-c", str(root / "pytest.ini"),
         "-p", "conftest", "-p", "test_media", str(case)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=20)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "deliberate write scenario failure" in out.stdout, out.stdout + out.stderr
    assert "1 failed in" in out.stdout, out.stdout + out.stderr
