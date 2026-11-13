"""Корень pytest моста изолирован; явный выбор каталога сохраняется."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest


@pytest.mark.parametrize("override", [None, "environment", "basetemp"])
def test_agent_tmp_root_preserves_explicit_selection(tmp_path, monkeypatch, override):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    selected = str(tmp_path / "selected")
    project = tmp_path / "project"
    monkeypatch.setattr(conftest, "ROOT", project)
    config = SimpleNamespace(option=SimpleNamespace(basetemp=selected if override == "basetemp" else None))
    if override == "environment":
        monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", selected)
    conftest.pytest_configure(config)
    root = project / ".tmp" / ".pytest_cache" / "agent"
    assert root.exists() == (override is None)
    assert os.environ.get("PYTEST_DEBUG_TEMPROOT") == (
        selected if override == "environment" else str(root) if override is None else None)
    assert config.option.basetemp == (selected if override == "basetemp" else None)


@pytest.mark.integration
def test_concurrent_agent_runs_finish_with_separate_numbered_directories(tmp_path):
    case = tmp_path / "test_concurrent.py"
    case.write_text('''import os
import time
from pathlib import Path


def test_concurrent(tmp_path, tmp_path_factory):
    barrier = Path(os.environ["STUDIO_TEST_BARRIER"])
    (barrier / f"{os.getpid()}.base").write_text(str(tmp_path_factory.getbasetemp()), encoding="utf-8")
    deadline = time.monotonic() + 5
    while len(list(barrier.glob("*.base"))) != 2:
        assert time.monotonic() < deadline, "второй прогон не достиг барьера"
        time.sleep(0.01)
''', encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    env = {**os.environ, "PYTHONPATH": str(root / "tests"), "STUDIO_TEST_BARRIER": str(tmp_path),
           "TEMP": str(system_temp), "TMP": str(system_temp)}
    env.pop("PYTEST_DEBUG_TEMPROOT", None)
    command = [sys.executable, "-m", "pytest", "-q", "-c", str(root / "pytest.ini"),
               "-p", "conftest", str(case)]
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, encoding="utf-8", errors="replace"))
        for process in processes:
            out, err = process.communicate(timeout=15)
            assert process.returncode == 0, out + err
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    bases = [Path(path.read_text(encoding="utf-8")) for path in tmp_path.glob("*.base")]
    assert len(bases) == 2 and bases[0] != bases[1]
    assert all(base.is_relative_to(conftest.ROOT / ".tmp" / ".pytest_cache" / "agent") for base in bases)
