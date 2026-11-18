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
