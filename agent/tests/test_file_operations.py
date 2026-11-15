"""Изменения файлов: границы проекта, отсутствие перезаписи и настоящий Windows junction."""

import _winapi
import ctypes
import os
import stat
import tempfile
from pathlib import Path

import pytest

from studio_agent import files, projects
from studio_agent.errors import Refused
from test_agent import agent, opened, req, root


@pytest.fixture
def delete_workspace(tmp_path):
    # TemporaryDirectory также убирает read-only файлы после неудачной проверки.
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        yield Path(directory)


@pytest.mark.parametrize("directory", [False, True])
def test_delete_readonly_file_and_nested_tree_preserves_other_files(delete_workspace, directory):
    root = delete_workspace
    target = root / "folder/nested/object" if directory else root / "object"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"remove")
    os.chmod(target, stat.S_IREAD)
    assert ctypes.windll.kernel32.SetFileAttributesW(str(target), stat.FILE_ATTRIBUTE_READONLY | stat.FILE_ATTRIBUTE_HIDDEN)
    if directory:
        assert ctypes.windll.kernel32.SetFileAttributesW(str(root / "folder"), stat.FILE_ATTRIBUTE_HIDDEN)
    keep = root / "keep"
    keep.write_bytes(b"keep")
    os.chmod(keep, stat.S_IREAD)
    files.operate(root, "delete", "folder" if directory else "object")
    assert not target.exists()
    assert not directory or not (root / "folder").exists()
    assert keep.read_bytes() == b"keep" and keep.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY


@pytest.mark.parametrize("readonly", [False, True])
def test_locked_file_reports_busy_without_changing_readonly(delete_workspace, readonly):
    root = delete_workspace
    target = root / "locked"
    target.write_bytes(b"keep")
    if readonly:
        os.chmod(target, stat.S_IREAD)
    with target.open("rb"):
        with pytest.raises(Refused) as caught:
            files.operate(root, "delete", "locked")
    assert caught.value.code == "busy" and "занят другой программой" in caught.value.message
    assert target.read_bytes() == b"keep"
    assert bool(target.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY) == readonly


@pytest.mark.parametrize("readonly", [False, True])
def test_delete_access_denied_does_not_change_attributes_or_retry_indefinitely(delete_workspace, monkeypatch, readonly):
    root = delete_workspace
    target = root / "denied"
    target.write_bytes(b"keep")
    if readonly:
        os.chmod(target, stat.S_IREAD)
    before = target.stat().st_file_attributes
    assert ctypes.windll.kernel32.SetFileAttributesW(str(target), before | stat.FILE_ATTRIBUTE_HIDDEN)
    before = target.stat().st_file_attributes
    calls = []

    def denied(path, **kwargs):
        calls.append(path)
        raise PermissionError(13, "fixture access denied", str(path), 5)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "unlink", denied)
        with pytest.raises(Refused) as caught:
            files.operate(root, "delete", "denied")
    assert caught.value.code == "read_only" and "нет прав" in caught.value.message
    assert target.read_bytes() == b"keep" and target.stat().st_file_attributes == before
    assert len(calls) == (2 if readonly else 1)


def test_delete_does_not_clear_attributes_when_directory_scan_is_denied(delete_workspace, monkeypatch):
    root = delete_workspace
    folder = root / "folder"
    folder.mkdir()
    (folder / "keep").write_bytes(b"keep")
    os.chmod(folder, stat.S_IREAD)
    before = folder.stat().st_file_attributes

    def denied(path):
        raise PermissionError(13, "fixture access denied", str(path), 5)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "scandir", denied)
        with pytest.raises(Refused, match="read_only"):
            files.operate(root, "delete", "folder")
    assert folder.stat().st_file_attributes == before
    assert (folder / "keep").read_bytes() == b"keep"


def test_readonly_retry_rechecks_parent_junction_before_changing_attributes(delete_workspace, monkeypatch):
    project, outside = delete_workspace / "project", delete_workspace / "outside"
    (project / "folder").mkdir(parents=True)
    outside.mkdir()
    for parent in (project / "folder", outside):
        (parent / "keep").write_bytes(b"keep")
        os.chmod(parent / "keep", stat.S_IREAD)

    def replaced(path, **kwargs):
        (project / "folder").rename(project / "original")
        _winapi.CreateJunction(str(outside), str(project / "folder"))
        raise PermissionError(13, "fixture access denied", str(path), 5)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "unlink", replaced)
        with pytest.raises(Refused, match="outside"):
            files.operate(project, "delete", "folder/keep")
    assert (outside / "keep").read_bytes() == b"keep"
    assert (outside / "keep").stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    assert (project / "original/keep").stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY


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
