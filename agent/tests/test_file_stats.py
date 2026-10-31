import os
import _winapi
from pathlib import Path

from studio_agent import file_stats, limits


def test_scan_counts_all_nested_and_hidden_entries_without_reading_files(tmp_path, monkeypatch):
    (tmp_path / "src/lib").mkdir(parents=True)
    (tmp_path / "empty").mkdir()
    (tmp_path / ".git/objects").mkdir(parents=True)
    for name, content in {"README": b"x", "src/a.py": b"abc", "src/lib/b.py": b"12345", ".git/objects/x": b"12"}.items():
        (tmp_path / name).write_bytes(content)
    monkeypatch.setattr(limits, "FILES_MAX", 1)
    monkeypatch.setattr(Path, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("file content read")))
    result = file_stats.scan(tmp_path, now=lambda: 123, monotonic=lambda: 0)
    rows = {row["path"]: row for row in result["items"]}
    assert rows[""] == {"path": "", "size": 11, "files": 4, "folders": 5, "complete": True}
    assert rows["src"] == {"path": "src", "size": 8, "files": 2, "folders": 1, "complete": True}
    assert rows["empty"] == {"path": "empty", "size": 0, "files": 0, "folders": 0, "complete": True}
    assert result["complete"] is True and result["checked_at"] == 123


def test_scan_skips_junctions_outside_and_cycles(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "one").write_bytes(b"abc")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"do not count")
    _winapi.CreateJunction(str(outside), str(project / "outside"))
    _winapi.CreateJunction(str(project), str(project / "cycle"))
    result = file_stats.scan(project, now=lambda: 123, monotonic=lambda: 0)
    assert result["items"] == [{"path": "", "size": 3, "files": 1, "folders": 0, "complete": True}]
    assert result["skipped_links"] == 2


def test_scan_marks_unreadable_subtree_as_partial(tmp_path, monkeypatch):
    (tmp_path / "parent/denied").mkdir(parents=True)
    (tmp_path / "parent/file").write_bytes(b"123")
    original = os.scandir

    def scandir(path):
        if Path(path).name == "denied":
            raise PermissionError()
        return original(path)

    monkeypatch.setattr(file_stats.os, "scandir", scandir)
    result = file_stats.scan(tmp_path, now=lambda: 123, monotonic=lambda: 0)
    assert result["complete"] is False
    assert all(not row["complete"] for row in result["items"])
    assert result["items"][0]["size"] == 3 and result["items"][0]["folders"] == 2


def test_scan_time_and_result_limits_return_explicit_lower_bounds(tmp_path, monkeypatch):
    (tmp_path / "parent/child").mkdir(parents=True)
    (tmp_path / "parent/child/file").write_bytes(b"123")
    ticks = iter([0, file_stats.MAX_SECONDS])
    timed = file_stats.scan(tmp_path, now=lambda: 123, monotonic=lambda: next(ticks))
    assert timed["complete"] is False and timed["items"][0]["files"] == 0
    monkeypatch.setattr(file_stats, "MAX_DIRS", 2)
    limited = file_stats.scan(tmp_path, now=lambda: 123, monotonic=lambda: 0)
    assert limited["complete"] is False and len(limited["items"]) == 2
    assert limited["items"][0]["folders"] == 2
    monkeypatch.setattr(file_stats, "MAX_BYTES", 200)
    bounded = file_stats.scan(tmp_path, now=lambda: 123, monotonic=lambda: 0)
    assert bounded["complete"] is False and len(bounded["items"]) == 1
