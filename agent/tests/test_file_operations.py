"""Изменения файлов: границы проекта, отсутствие перезаписи и настоящий Windows junction."""

from platform_fs import create_directory_link
import base64
import ctypes
import os
import stat
import tempfile
from pathlib import Path

import pytest

from studio_agent import files, host, projects
from studio_agent.errors import Refused
from test_agent import agent, opened, req, root


@pytest.fixture
def delete_workspace(tmp_path):
    # TemporaryDirectory также убирает read-only файлы после неудачной проверки.
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        yield Path(directory)


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.skipif(os.name != 'nt', reason='атрибуты Windows')
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
@pytest.mark.skipif(os.name != 'nt', reason='блокировка файла Windows')
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
@pytest.mark.skipif(os.name != 'nt', reason='атрибуты Windows')
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


@pytest.mark.skipif(os.name != 'nt', reason='атрибуты Windows')
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


@pytest.mark.skipif(os.name != 'nt', reason='атрибуты Windows')
def test_readonly_retry_rechecks_parent_junction_before_changing_attributes(delete_workspace, monkeypatch):
    project, outside = delete_workspace / "project", delete_workspace / "outside"
    (project / "folder").mkdir(parents=True)
    outside.mkdir()
    for parent in (project / "folder", outside):
        (parent / "keep").write_bytes(b"keep")
        os.chmod(parent / "keep", stat.S_IREAD)

    def replaced(path, **kwargs):
        (project / "folder").rename(project / "original")
        create_directory_link(outside, project / "folder")
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
    create_directory_link(outside, project / "link")
    (project / "source").write_bytes(b"source")
    for operation, path, destination in [("delete", "link", None), ("delete", "link/keep", None),
                                         ("create_file", "link/new", None), ("move", "link", "renamed"),
                                         ("move", "source", "link/new")]:
        with pytest.raises(Refused, match="outside"):
            files.operate(project, operation, path, destination)
    (project / "folder").mkdir()
    create_directory_link(outside, project / "folder/nested")
    files.operate(project, "delete", "folder")
    assert (outside / "keep").read_bytes() == b"keep"
    assert not (project / "folder").exists()
    assert (project / "source").read_bytes() == b"source" and not (outside / "new").exists()


def test_destination_appearing_during_move_is_preserved(tmp_path, monkeypatch):
    (tmp_path / "source").write_bytes(b"source")
    rename = host.rename

    def race(source, destination):
        destination.write_bytes(b"arrived")
        return rename(source, destination)

    monkeypatch.setattr(host, "rename", race)
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


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_upload_creates_new_file_and_never_overwrites_without_replace(tmp_path):
    (tmp_path / "docs").mkdir()
    assert files.upload(tmp_path, "docs/photo.bin", _b64(b"\x00\x01data"), False) == {
        "version": files.version(b"\x00\x01data")}
    assert (tmp_path / "docs/photo.bin").read_bytes() == b"\x00\x01data"
    with pytest.raises(Refused, match="exists"):
        files.upload(tmp_path, "docs/photo.bin", _b64(b"other"), False)
    with pytest.raises(Refused, match="exists"):
        files.upload(tmp_path, "docs", _b64(b"other"), True)        # папку замена не трогает
    assert (tmp_path / "docs/photo.bin").read_bytes() == b"\x00\x01data"
    files.upload(tmp_path, "docs/photo.bin", _b64(b"replaced"), True)
    assert (tmp_path / "docs/photo.bin").read_bytes() == b"replaced"
    files.upload(tmp_path, "fresh.txt", _b64(b""), True)              # замена разрешена, а файла нет — создаётся
    assert (tmp_path / "fresh.txt").read_bytes() == b""


@pytest.mark.parametrize("path, data, code", [
    ("", "eA==", "invalid"), ("../up.txt", "eA==", "invalid"), ("a.txt", "не base64", "invalid"),
    ("missing/a.txt", "eA==", "not_found")])
def test_upload_refuses_bad_paths_and_data(tmp_path, path, data, code):
    with pytest.raises(Refused, match=code):
        files.upload(tmp_path, path, data, False)
    assert list(tmp_path.iterdir()) == []


def test_upload_limit_and_links(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    create_directory_link(outside, project / "link")
    for replace in (False, True):
        with pytest.raises(Refused, match="outside"):
            files.upload(project, "link/keep", _b64(b"evil"), replace)
        with pytest.raises(Refused, match="outside"):
            files.upload(project, "link/new", _b64(b"evil"), replace)
    assert (outside / "keep").read_bytes() == b"keep" and not (outside / "new").exists()
    monkeypatch.setattr(files.limits, "RAW_MAX", 3)
    with pytest.raises(Refused, match="too_large"):
        files.upload(project, "big.bin", _b64(b"four"), False)
    assert not (project / "big.bin").exists()


def test_upload_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    real = Path.open

    class Broken:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *error):
            self.handle.close()

        def write(self, data):
            self.handle.write(data[:1])
            raise OSError(28, "no space")

    monkeypatch.setattr(Path, "open", lambda self, mode="r", *a, **k: Broken(real(self, mode, *a, **k)))
    with pytest.raises(Refused, match="busy"):
        files.upload(tmp_path, "half.bin", _b64(b"abcdef"), False)
    assert not (tmp_path / "half.bin").exists()


async def test_upload_and_search_through_agent(agent, root):
    (root / "demo/src").mkdir(parents=True, exist_ok=True)
    (root / "demo/src/main.py").write_text("print('hello')\n# Привет, мир\n", encoding="utf-8")
    upload = req("file_upload", project=projects.pid("demo"), path="src/new.bin", data=_b64(b"x"), replace=False)
    search = req("file_search", project=projects.pid("demo"), path="src", query="привет")
    assert (await agent.handle(upload))[0]["code"] == "closed"
    await opened(agent)
    assert agent.hello()["file_upload"] is True and agent.hello()["file_search"] is True
    assert (await agent.handle(upload))[0]["ok"]
    assert (root / "demo/src/new.bin").read_bytes() == b"x"
    assert (await agent.handle(upload))[0]["code"] == "exists"
    for bad in ({"replace": None}, {"data": 5}, {"path": "../x"}):
        assert (await agent.handle({**upload, **bad}))[0]["code"] == "invalid"
    found = (await agent.handle(search))[0]
    assert found["ok"] and found["data"] == {"items": [{"path": "src/main.py", "line": 2, "text": "# Привет, мир"}],
                                             "complete": True, "more": False}
