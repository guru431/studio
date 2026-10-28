import os
import subprocess

import pytest

from studio_agent import errors, gitinfo, limits


def test_parse_branch_upstream_and_files():
    out = (b"# branch.oid abc\0# branch.head main\0# branch.upstream origin/main\0# branch.ab +3 -1\0"
           b"1 .M N... 100644 100644 100644 a b \xd0\xbf\xd0\xb0\xd0\xbf\xd0\xba\xd0\xb0/f 1.txt\0"
           b"2 R. N... 100644 100644 100644 a b R100 new.txt\0old.txt\0? x.log\0")
    assert gitinfo.parse(out) == {"branch": "main", "upstream": "origin/main", "ahead": 3, "behind": 1,
        "files": [{"path": "папка/f 1.txt", "status": "M"}, {"path": "new.txt", "status": "R"},
                  {"path": "x.log", "status": "??"}]}


def test_parse_detached_without_upstream_conflict_and_both_sides():
    out = (b"# branch.oid abc\0# branch.head (detached)\0"
           b"1 MM N... 100644 100644 100644 a b both.txt\0"
           b"u UU N... 100644 100644 100644 100644 a b c conflict.txt\0")
    assert gitinfo.parse(out) == {"branch": None, "upstream": None, "ahead": 0, "behind": 0,
        "files": [{"path": "both.txt", "status": "MM"}, {"path": "conflict.txt", "status": "U"}]}


def test_parse_keeps_at_most_git_files_max(monkeypatch):
    monkeypatch.setattr(limits, "GIT_FILES_MAX", 2)
    out = b"# branch.head main\0? a\0? b\0? c\0"
    assert [f["path"] for f in gitinfo.parse(out)["files"]] == ["a", "b"]


def test_status_runs_git_without_shell_window_and_locks(monkeypatch, tmp_path):
    seen = {}

    def run(args, **kw):
        seen.update(args=args, **kw)
        return subprocess.CompletedProcess(args, 0, b"# branch.head main\0", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert gitinfo.status(tmp_path)["branch"] == "main"
    assert seen["args"] == ["git", "-C", str(tmp_path), "status", "--porcelain=v2", "--branch", "-z"]
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0" and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["PATH"] == os.environ["PATH"]
    assert seen["env"]["GIT_CEILING_DIRECTORIES"] == os.path.realpath(tmp_path.parent)  # выше проекта не искать
    assert seen["timeout"] == 25 and seen["capture_output"] is True
    assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW and not seen.get("shell")


@pytest.mark.parametrize("outcome", ["fail", "missing"])
def test_status_is_not_git_on_exit_code_or_no_git(monkeypatch, tmp_path, outcome):
    def run(args, **kw):
        if outcome == "missing":
            raise FileNotFoundError(2, "нет git")
        return subprocess.CompletedProcess(args, 128, b"", b"fatal")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(errors.Refused) as e:
        gitinfo.status(tmp_path)
    assert (e.value.code, e.value.message) == ("not_git", "не репозиторий git")


@pytest.mark.integration
def test_status_of_a_real_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"; repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["init", "-q"], check=True)
    (repo / "новый файл.txt").write_text("a")
    assert gitinfo.status(repo)["files"] == [{"path": "новый файл.txt", "status": "??"}]
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
    (repo / "новый файл.txt").write_text("b")
    got = gitinfo.status(repo)
    assert got["files"] == [{"path": "новый файл.txt", "status": "M"}] and got["upstream"] is None
    plain = tmp_path / "plain"; plain.mkdir()
    with pytest.raises(errors.Refused) as e:
        gitinfo.status(plain)
    assert e.value.code == "not_git"


@pytest.mark.integration
def test_a_project_inside_someone_elses_repository_is_not_git(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    outer = tmp_path / "outer"
    project = outer / "projects" / "demo"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    (project / "a.txt").write_text("a")
    with pytest.raises(errors.Refused) as e:
        gitinfo.status(project)                                # ветка и пути внешнего репозитория не уходят
    assert e.value.code == "not_git"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    assert gitinfo.status(project)["files"] == [{"path": "a.txt", "status": "??"}]  # свой .git — как обычно
