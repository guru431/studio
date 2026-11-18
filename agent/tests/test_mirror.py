import os
import sqlite3

import pytest

from studio_agent import mirror
from studio_agent.errors import Refused


def test_open_sqlite_is_not_copied_and_closed_database_is_included(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    db = project / "data.db"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE records(value)")
        conn.execute("INSERT INTO records VALUES(1)")
        conn.commit()
        with pytest.raises(Refused) as refused:
            mirror.scan(project)
        assert refused.value.code == "busy"
    finally:
        conn.close()
    assert mirror.scan(project)["entries"]["data.db"][0] == "file"


def test_failed_replacement_restores_previous_files_and_clears_staging(tmp_path, monkeypatch):
    destination = tmp_path / "copy"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"old")
    before = mirror.scan(destination)["entries"]
    transfer = mirror.Transfer(destination, "test", "ticket")
    transfer.write([["keep.txt", 0, "bmV3"], ["new.txt", 0, "bmV3"]])
    sha = mirror.files.version(b"new")
    updates = {path: ["file", sha, 3, 0] for path in ("keep.txt", "new.txt")}
    move = os.rename if os.name == "nt" else os.link

    def fail_second(source, target):
        if target.name == "new.txt":
            raise PermissionError
        move(source, target)

    monkeypatch.setattr(mirror.os, "rename" if os.name == "nt" else "link", fail_second)
    try:
        with pytest.raises(PermissionError):
            transfer.apply(mirror.fingerprint(before), updates, [])
        assert mirror.scan(destination)["entries"] == before
    finally:
        transfer.close()
    assert not transfer.temp.exists()


@pytest.mark.skipif(os.name != "posix", reason="В Windows нет исполняемых битов POSIX")
def test_replacing_posix_file_keeps_its_execute_bits(tmp_path):
    destination = tmp_path / "copy"
    destination.mkdir()
    script = destination / "run"
    script.write_bytes(b"old")
    script.chmod(0o651)
    before = mirror.scan(destination)["entries"]
    transfer = mirror.Transfer(destination, "test", "ticket")
    try:
        transfer.write([["run", 0, "bmV3"]])
        transfer.apply(mirror.fingerprint(before), {"run": ["file", mirror.files.version(b"new"), 3, 1]}, [])
        assert script.read_bytes() == b"new"
        assert script.stat().st_mode & 0o777 == 0o651
    finally:
        transfer.close()


def test_discovers_renamed_clones_across_roots_and_labels_name_only_matches(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    original_name = first / "Project"
    no_git = second / "project"
    renamed = second / "clone"
    for folder in (original_name, no_git, renamed):
        folder.mkdir(parents=True)
    matching = mirror._repository_id("https://git.example.com/team/work.git")
    assert matching == mirror._repository_id("git@git.example.com:team/work")
    assert matching == mirror._repository_id("ssh://git@git.example.com:22/team/work.git")
    assert matching == mirror._repository_id("https://user:example@git.example.com:443/team/work.git?private=example")
    assert matching != mirror._repository_id("https://git.example.com:9443/team/work.git")
    different = mirror._repository_id("https://git.example.com/team/unrelated.git")
    monkeypatch.setattr(mirror, "_repositories", lambda path: {
        renamed: {matching}, original_name: {different}, no_git: set(),
    }[path])
    result = mirror.candidates([first, second], "project", [matching])
    assert result["complete"]
    assert {(item["name"], item["folder"], item["match"]) for item in result["items"]} == {
        ("project", str(second), "name"), ("clone", str(second), "repository"),
    }


def test_discovery_preserves_results_from_accessible_roots(tmp_path):
    available = tmp_path / "available"
    (available / "project").mkdir(parents=True)
    result = mirror.candidates([available, tmp_path / "missing"], "project", [])
    assert result["complete"] is False
    assert [item["name"] for item in result["items"]] == ["project"]


def test_linked_worktree_is_rejected_before_transfer(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../../project/.git/worktrees/worktree\n", encoding="utf-8")
    root, = mirror.projects.list_roots([tmp_path])
    for operation in (lambda: mirror.scan(worktree), lambda: mirror.read(worktree, []),
                      lambda: mirror.destination([tmp_path], root["id"], worktree.name, metadata_only=True)):
        with pytest.raises(Refused) as error:
            operation()
        assert error.value.code == "conflict" and "отдельный клон" in error.value.message


def test_remote_config_ignores_include_and_returns_only_repository_signatures(tmp_path, monkeypatch):
    import subprocess

    project = tmp_path / "demo"
    (project / ".git").mkdir(parents=True)
    (project / ".git/config").touch()
    def git(path, args, *, timeout):
        assert path == project
        assert args == ["config", "--no-includes", "--file", str(project / ".git/config"), "-z",
                        "--get-regexp", mirror.REMOTE_KEY]
        assert timeout == 2
        return subprocess.CompletedProcess(args, 0,
            b"remote.origin.url\nhttps://user:token@git.example.com/team/work.git\0", b"")
    monkeypatch.setattr(mirror.gitinfo, "run", git)
    assert mirror.identity(project) == {
        "name": "demo", "repositories": [mirror._repository_id("https://git.example.com/team/work")],
    }
