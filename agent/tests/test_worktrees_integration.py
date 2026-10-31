import subprocess

import pytest

from studio_agent import worktrees

pytestmark = pytest.mark.integration


def test_real_worktree_preserves_dirty_source_and_opens_as_an_allowed_project(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()

    def git(*args):
        result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        return result.stdout

    git("init", "-b", "main")
    (project / "source.txt").write_text("committed", encoding="utf-8")
    git("add", "source.txt")
    git("-c", "user.name=Studio Test", "-c", "user.email=studio-test@example.invalid", "commit", "-m", "initial")
    (project / "source.txt").write_text("uncommitted", encoding="utf-8")
    (project / "private.local").write_text("not moved", encoding="utf-8")
    result = worktrees.create(project, tmp_path, "task")
    copy = tmp_path / result["name"]
    assert (copy / "source.txt").read_text(encoding="utf-8") == "committed"
    assert (project / "source.txt").read_text(encoding="utf-8") == "uncommitted"
    assert (project / "private.local").read_text(encoding="utf-8") == "not moved"
    assert not (copy / "private.local").exists()
    rows = worktrees.list_worktrees(project, tmp_path)["items"]
    assert {row["name"] for row in rows} == {"demo", "demo--task"}
