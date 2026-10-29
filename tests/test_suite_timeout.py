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


@pytest.mark.timeout(2)
def test_hung():
    time.sleep(1000)


def test_fine():
    pass
'''


def test_hung_test_crashes_its_worker_once(tmp_path):
    (tmp_path / "test_hung_case.py").write_text(HUNG, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}     # conftest.py набора — плагином `-p conftest`
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "conftest", "-p", "no:cacheprovider",
                          "-n", "2", "--dist", "loadgroup", "test_hung_case.py"], cwd=tmp_path,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    assert "1 failed, 1 passed" in out.stdout, out.stdout[-2000:]
    assert out.stdout.count("crashed while running 'test_hung_case.py::test_hung'") == 1, out.stdout[-2000:]
