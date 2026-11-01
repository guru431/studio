import os
import subprocess

import pytest

from studio_agent import gitinfo, project_summary


def test_journals_count_only_active_entries_outside_comments_and_code(tmp_path):
    text = """# Findings
## First
**Status:** open
## Closed
**Status:** deferred
<!--
## Comment
**Status:** open
-->
~~~markdown
## Example
**Status:** open
~~~
```html
<!-- незакрытый комментарий в примере не скрывает настоящие записи
```
## Second
**Status:** OPEN
### Explanation
text
"""
    (tmp_path / "FINDINGS.md").write_text(text, encoding="utf-8-sig")
    (tmp_path / "IDEAS.md").write_text("## Idea\n**Status:** proposed\n", encoding="utf-8")
    (tmp_path / "FINDINGS-archive.md").write_text("## Old\n**Status:** open\n", encoding="utf-8")
    result = project_summary.read(tmp_path, now=lambda: 1234)
    assert result["findings"] == 2 and result["ideas"] == 1
    assert result["git"] == "not_git" and result["changed"] is None and result["unpushed"] is None
    assert result["checked_at"] == 1234


def test_missing_empty_and_unreadable_journals_are_distinct(tmp_path, monkeypatch):
    assert project_summary.read(tmp_path)["findings"] == 0
    (tmp_path / "FINDINGS.md").write_bytes(b"\xff")
    assert project_summary.read(tmp_path)["findings"] is None
    (tmp_path / "FINDINGS.md").write_text("# Findings\n", encoding="utf-8")
    assert project_summary.read(tmp_path)["findings"] == 0
    monkeypatch.setattr(project_summary, "JOURNAL_MAX", 2)
    assert project_summary.read(tmp_path)["findings"] is None


def test_journal_symlink_outside_project_is_not_read(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "private.md"
    target.write_text("## Secret\n**Status:** open", encoding="utf-8")
    try:
        (project / "FINDINGS.md").symlink_to(target)
    except OSError:
        pytest.skip("символические ссылки недоступны")
    assert project_summary.read(project)["findings"] is None


def test_summary_counts_all_files_and_uses_commit_and_local_modification_times(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a").write_text("new")
    (tmp_path / "b").write_text("new")
    os.utime(tmp_path / "a", (200, 200))
    os.utime(tmp_path / "b", (150, 150))
    monkeypatch.setattr(gitinfo.limits, "GIT_FILES_MAX", 1)
    seen = []

    def run(project, args, **kwargs):
        seen.append(args)
        if args[0] == "config":
            return subprocess.CompletedProcess(args, 1, b"")
        assert kwargs["timeout"] == 5
        out = (b"# branch.head main\0# branch.upstream origin/main\0# branch.ab +3 -1\0? a\0? b\0"
               if args[0] == "status" else b"100\n")
        return subprocess.CompletedProcess(args, 0, out)

    monkeypatch.setattr(gitinfo, "run", run)
    result = project_summary.read(tmp_path, now=lambda: 300)
    assert (result["changed"], result["unpushed"], result["modified"], result["git"]) == (2, 3, 200, "ok")
    assert "--untracked-files=all" in seen[0] and len(seen) == 3
    assert result["github"] is None


def test_git_failure_preserves_journal_counts_and_does_not_report_clean(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "FINDINGS.md").write_text("## Bug\n**Status:** open", encoding="utf-8")

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(gitinfo, "run", fail)
    result = project_summary.read(tmp_path, now=lambda: 300)
    assert result["findings"] == 1 and result["ideas"] == 0
    assert result["git"] == "error" and result["changed"] is None and result["unpushed"] is None


def test_unreadable_git_history_does_not_mean_no_unpushed_commits(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def run(project, args, **kwargs):
        return subprocess.CompletedProcess(args, 0 if args[0] == "status" else 128,
            b"# branch.oid abc\0# branch.head main\0" if args[0] == "status" else b"")

    monkeypatch.setattr(gitinfo, "run", run)
    result = project_summary.read(tmp_path, now=lambda: 300)
    assert result["changed"] == 0 and result["unpushed"] is None and result["modified"] is None


@pytest.mark.integration
def test_real_git_summary_staged_untracked_deleted_and_unpushed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

    def run(*args):
        return subprocess.run([*git, *args], check=True, capture_output=True)

    run("init", "-q")
    assert project_summary.read(repo)["unpushed"] == 0
    (repo / "a.txt").write_text("a")
    (repo / "b.txt").write_text("b")
    run("add", "-A")
    run("commit", "-qm", "initial")
    assert project_summary.read(repo)["unpushed"] == 1  # Без remote первый коммит ещё не опубликован.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
    run("remote", "add", "origin", str(remote))
    run("push", "-u", "origin", "HEAD")
    assert project_summary.read(repo)["unpushed"] == 0
    (repo / "a.txt").write_text("staged")
    run("add", "a.txt")
    (repo / "b.txt").unlink()
    (repo / "folder").mkdir()
    (repo / "folder" / "new.txt").write_text("new")
    result = project_summary.read(repo)
    assert result["changed"] == 3
    run("add", "-A")
    run("commit", "-qm", "second")
    assert project_summary.read(repo)["unpushed"] == 1
    # Ветка без upstream с уже опубликованными коммитами не считается незапушенной.
    run("checkout", "-qb", "other")
    run("push", "origin", "HEAD")
    assert project_summary.read(repo)["unpushed"] == 0
