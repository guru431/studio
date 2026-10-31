"""Изменения файлов: границы проекта, отсутствие перезаписи и настоящий Windows junction."""

import _winapi
from pathlib import Path

import pytest

from studio_agent import files, projects
from studio_agent.errors import Refused
from test_agent import agent, opened, req, root


def test_create_rename_move_and_recursive_delete(tmp_path):
    files.operate(tmp_path, "create_dir", "папка")
    files.operate(tmp_path, "create_file", "папка/новый.txt")
    assert (tmp_path / "папка/новый.txt").read_bytes() == b""
    (tmp_path / "папка/новый.txt").write_bytes(b"content")
    files.operate(tmp_path, "move", "папка/новый.txt", "папка/имя.txt")
    files.operate(tmp_path, "move", "папка", "другая")
    assert (tmp_path / "другая/имя.txt").read_bytes() == b"content"
    assert not (tmp_path / "папка").exists()
    files.operate(tmp_path, "delete", "другая")
    assert list(tmp_path.iterdir()) == []


def test_existing_destinations_are_never_overwritten(tmp_path):
    (tmp_path / "a").write_bytes(b"a")
    (tmp_path / "b").write_bytes(b"b")
    for operation, path, destination in [("move", "a", "b"), ("create_file", "b", None),
                                         ("create_dir", "b", None)]:
        with pytest.raises(Refused, match="exists"):
            files.operate(tmp_path, operation, path, destination)
    assert (tmp_path / "a").read_bytes() == b"a" and (tmp_path / "b").read_bytes() == b"b"


@pytest.mark.parametrize("operation,path,destination", [
    ("delete", "", None), ("move", "folder", ""), ("delete", "../outside", None),
    ("move", "folder", "../outside"), ("move", "folder", "folder/child"),
    ("create_file", "NUL.txt", None), ("delete", "folder", "unrequested"),
    ("move", "folder", None), ("unknown", "folder", None),
])
def test_invalid_operations_preserve_source(tmp_path, operation, path, destination):
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder/keep").write_bytes(b"keep")
    with pytest.raises(Refused):
        files.operate(tmp_path, operation, path, destination)
    assert (tmp_path / "folder/keep").read_bytes() == b"keep"


def test_case_only_rename_and_missing_parent(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"keep")
    files.operate(tmp_path, "move", "file.txt", "File.txt")
    assert [p.name for p in tmp_path.iterdir()] == ["File.txt"]
    with pytest.raises(Refused, match="not_found"):
        files.operate(tmp_path, "move", "File.txt", "missing/file.txt")
    assert (tmp_path / "File.txt").read_bytes() == b"keep"


def test_links_cannot_redirect_mutations_and_recursive_delete_keeps_external_files(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    _winapi.CreateJunction(str(outside), str(project / "link"))
    (project / "source").write_bytes(b"source")
    for operation, path, destination in [("delete", "link", None), ("delete", "link/keep", None),
                                         ("create_file", "link/new", None), ("move", "link", "renamed"),
                                         ("move", "source", "link/new")]:
        with pytest.raises(Refused, match="outside"):
            files.operate(project, operation, path, destination)
    (project / "folder").mkdir()
    _winapi.CreateJunction(str(outside), str(project / "folder/nested"))
    files.operate(project, "delete", "folder")
    assert (outside / "keep").read_bytes() == b"keep"
    assert not (project / "folder").exists()
    assert (project / "source").read_bytes() == b"source" and not (outside / "new").exists()


def test_destination_appearing_during_move_is_preserved(tmp_path, monkeypatch):
    (tmp_path / "source").write_bytes(b"source")
    rename = Path.rename

    def race(source, destination):
        destination.write_bytes(b"arrived")
        return rename(source, destination)

    monkeypatch.setattr(Path, "rename", race)
    with pytest.raises(Refused, match="exists"):
        files.operate(tmp_path, "move", "source", "destination")
    assert (tmp_path / "source").read_bytes() == b"source"
    assert (tmp_path / "destination").read_bytes() == b"arrived"


async def test_operation_requires_open_session_and_validates_both_paths(agent, root):
    frame = req("file_operation", project=projects.pid("demo"), operation="create_file", path="new.txt")
    assert (await agent.handle(frame))[0]["code"] == "closed"
    await opened(agent)
    assert (await agent.handle(frame))[0]["ok"]
    bad = req("file_operation", project=projects.pid("demo"), operation="move", path="new.txt", destination="../bad")
    assert (await agent.handle(bad))[0]["code"] == "invalid"
    assert (root / "demo/new.txt").exists()
    frame["project"] = "0" * 16
    assert (await agent.handle(frame))[0]["code"] == "not_found"
