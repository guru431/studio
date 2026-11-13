"""Автоматическая изоляция временных каталогов сохраняет явный выбор владельца."""
import os
from types import SimpleNamespace

import pytest

import conftest


@pytest.mark.parametrize("override", [None, "environment", "basetemp"])
def test_tmp_root_is_project_specific_unless_explicitly_selected(tmp_path, monkeypatch, override):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    selected = tmp_path / "selected"
    project = tmp_path / "project"
    monkeypatch.setattr(conftest, "ROOT", project)
    config = SimpleNamespace(option=SimpleNamespace(basetemp=str(selected) if override == "basetemp" else None))
    if override == "environment":
        monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(selected))

    conftest.pytest_configure(config)
    expected = project / ".tmp" / ".pytest_cache" / "main"
    if override is None:
        assert os.environ["PYTEST_DEBUG_TEMPROOT"] == str(expected) and expected.is_dir()
    else:
        assert not expected.exists()
        assert os.environ.get("PYTEST_DEBUG_TEMPROOT") == (str(selected) if override == "environment" else None)
        assert config.option.basetemp == (str(selected) if override == "basetemp" else None)
