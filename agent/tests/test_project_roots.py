"""Несколько разрешённых папок: совместимость, стабильные id и границы доступа."""

import json
import _winapi
from pathlib import Path

import pytest

from studio_agent import __main__ as cli_main
from studio_agent import projects, setup, store, worktrees
from studio_agent.errors import Refused


def test_old_config_migrates_without_changing_project_ids(home, tmp_path):
    root = tmp_path / "projects"
    (root / "demo").mkdir(parents=True)
    (home / store.CONFIG).write_text(json.dumps({"projects_root": str(root)}), encoding="utf-8")
    cfg = store.load_config(home)
    assert cfg.projects_roots == [str(root)] and cfg.projects_legacy_root == str(root)
    assert projects.list_projects(cfg) == [{"pid": projects.pid("demo"), "name": "demo"}]
    store.save_config(home, cfg)
    assert store.load_config(home) == cfg


def test_same_names_are_distinct_and_keep_ids_when_the_first_root_is_removed(tmp_path):
    first, second = tmp_path / "work", tmp_path / "personal"
    for root in (first, second):
        (root / "demo").mkdir(parents=True)
    cfg = store.Config(projects_root=str(first))
    cfg.set_project_roots([str(first), str(second)])
    rows = projects.list_projects(cfg)
    assert len(rows) == 2 and len({row["name"] for row in rows}) == 2
    paths = {projects.find_project(cfg, row["pid"]): row["pid"] for row in rows}
    assert paths[first / "demo"] == projects.pid("demo")
    second_id = paths[second / "demo"]
    assert second_id != projects.pid("demo")
    cfg.set_project_roots([str(second)])
    assert projects.list_projects(cfg) == [{"pid": second_id, "name": "demo"}]
    assert projects.find_project(cfg, second_id) == second / "demo"
    with pytest.raises(Refused, match="not_found"):
        projects.find_project(cfg, projects.pid("demo"))
    cfg.set_project_roots([str(second), str(first)])
    assert projects.find_project(cfg, projects.pid("demo")) == first / "demo"
    assert projects.find_project(cfg, second_id) == second / "demo"


def test_roots_and_projects_do_not_duplicate_or_follow_junctions(tmp_path):
    root = tmp_path / "projects"
    (root / "demo").mkdir(parents=True)
    outside = tmp_path / "private"
    (outside / "secret").mkdir(parents=True)
    alias = tmp_path / "alias"
    _winapi.CreateJunction(str(root), str(alias))
    _winapi.CreateJunction(str(outside), str(root / "outside"))
    _winapi.CreateJunction(str(root / "demo"), str(root / "inside"))
    cfg = store.Config(projects_root=str(root))
    cfg.set_project_roots([str(root), str(alias), str(root).upper()])
    assert cfg.projects_roots == [str(root)]
    assert projects.list_projects(cfg) == [{"pid": projects.pid("demo"), "name": "demo"}]
    with pytest.raises(Refused, match="not_found"):
        projects.find_project(cfg, projects.pid("outside"))


@pytest.mark.parametrize("roots", [[], [""], "C:/projects", [1], [None]])
def test_setup_rejects_invalid_root_lists(roots):
    assert setup.folder_errors(setup.folders({"projects_roots": roots})) == [setup.NO_FOLDER]


def test_setup_saves_all_roots_without_a_new_enrollment(home, tmp_path):
    first, second = tmp_path / "work", tmp_path / "personal"
    first.mkdir()
    second.mkdir()
    store.save_config(home, store.Config(projects_root=str(first), agent_id=7, login="owner"))
    form = {"url": "https://studio.example.com", "name": "Work", "projects_roots": [str(first), str(second)]}
    assert setup.validate(form, enrolled=True) == []
    cfg = setup.apply(home, form)
    assert store.load_config(home).projects_roots == [str(first), str(second)]
    assert cfg.agent_id == 7 and cfg.login == "owner" and cfg.projects_legacy_root == str(first)


def test_worktree_created_in_secondary_root_and_listed_with_the_same_id(tmp_path, monkeypatch):
    first, second = tmp_path / "work", tmp_path / "personal"
    first.mkdir()
    project = second / "demo"
    project.mkdir(parents=True)
    cfg = store.Config(projects_roots=[str(first), str(second)])

    def git(_project, args):
        if args[0] == "rev-parse":
            return str(project)
        Path(args[-2]).mkdir()
        return ""

    monkeypatch.setattr(worktrees, "_git", git)
    row = worktrees.create(project, cfg, "task")
    copy = second / "demo--task"
    assert projects.find_project(cfg, row["pid"]) == copy
    assert not (first / "demo--task").exists()
    monkeypatch.setattr(worktrees, "_git", lambda *_: f"worktree {project}\0\0worktree {copy}\0\0")
    rows = worktrees.list_worktrees(project, cfg)["items"]
    assert {item["pid"] for item in rows} == {projects.project_id(cfg, second, "demo"), row["pid"]}
    cfg.set_project_roots([str(first)])
    assert worktrees.list_worktrees(project, cfg) == {"items": []}
    with pytest.raises(Refused, match="outside"):
        worktrees.create(project, cfg, "another")


def test_cli_preserves_existing_roots_and_adds_more(monkeypatch):
    answers = iter(["", "C:/extra", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert cli_main._ask_folders(["C:/work", "C:/personal"]) == ["C:/work", "C:/personal", "C:/extra"]
