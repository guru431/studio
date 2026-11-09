import os
import subprocess

import pytest

from studio_agent import errors, gitinfo, limits


def test_parse_branch_upstream_and_files():
    out = (b"# branch.oid abc\0# branch.head main\0# branch.upstream origin/main\0# branch.ab +3 -1\0"
           b"1 .M N... 100644 100644 100644 a b \xd0\xbf\xd0\xb0\xd0\xbf\xd0\xba\xd0\xb0/f 1.txt\0"
           b"2 R. N... 100644 100644 100644 a b R100 new  name with spaces.txt\0old  name.txt\0? x.log\0")
    assert gitinfo.parse(out) == {"branch": "main", "upstream": "origin/main", "ahead": 3, "behind": 1,
        "files": [{"path": "папка/f 1.txt", "status": "M"}, {"path": "new  name with spaces.txt", "status": "R"},
                  {"path": "x.log", "status": "??"}]}


def test_parse_detached_without_upstream_conflict_and_both_sides():
    out = (b"# branch.oid abc\0# branch.head (detached)\0"
           b"1 MM N... 100644 100644 100644 a b both.txt\0"
           b"u UU N... 100644 100644 100644 100644 a b c conflict with  spaces.txt\0")
    assert gitinfo.parse(out) == {"branch": None, "upstream": None, "ahead": 0, "behind": 0,
        "files": [{"path": "both.txt", "status": "MM"}, {"path": "conflict with  spaces.txt", "status": "U"}]}


def test_parse_keeps_at_most_git_files_max(monkeypatch):
    monkeypatch.setattr(limits, "GIT_FILES_MAX", 2)
    out = b"# branch.head main\0? a\0? b\0? c\0"
    assert [f["path"] for f in gitinfo.parse(out)["files"]] == ["a", "b"]


SAFE = ["-c", "core.fsmonitor=false", "-c", "log.showSignature=false"]


def test_status_runs_git_without_shell_window_and_locks(monkeypatch, tmp_path):
    seen = {}

    def run(args, **kw):
        if "config" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"")  # фильтров в конфиге нет
        seen.update(args=args, **kw)
        return subprocess.CompletedProcess(args, 0, b"# branch.head main\0", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert gitinfo.status(tmp_path)["branch"] == "main"
    assert seen["args"] == ["git", "-C", str(tmp_path), *SAFE, "status", "--ignore-submodules=dirty",
                            "--porcelain=v2", "--branch", "-z"]
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0" and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["PATH"] == os.environ["PATH"]
    assert seen["env"]["GIT_CEILING_DIRECTORIES"] == os.path.realpath(tmp_path.parent)  # выше проекта не искать
    assert seen["timeout"] == 25 and seen["capture_output"] is True
    assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW and not seen.get("shell")


def test_status_switches_off_filter_drivers_of_the_repository_config(monkeypatch, tmp_path):
    """Драйверы из конфига репозитория (local/worktree, с include) гасятся; драйверы системы и владельца — нет."""
    calls = []
    scopes = (b"system\0filter.lfs.clean\ngit-lfs clean -- %f\0local\0filter.evil.clean\ntouch x; cat\0"
              b"worktree\0filter.My Driver.process\nrun\0global\0filter.own.smudge\nown\0")

    def run(args, **kw):
        calls.append(args)
        if "config" in args:
            assert args[args.index("config"):] == ["config", "--show-scope", "-z", "--get-regexp",
                                                   r"^filter\..*\.(clean|smudge|process)$"]
            return subprocess.CompletedProcess(args, 0, scopes, b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    gitinfo.run(tmp_path, ["status", "-z"])
    status = calls[-1]
    for name in ("evil", "My Driver"):
        for key, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            assert f"filter.{name}.{key}={value}" in status
    assert not any("lfs" in arg or ".own." in arg for arg in status)
    assert status[-3:] == ["status", "--ignore-submodules=dirty", "-z"]
    calls.clear()
    gitinfo.run(tmp_path, ["log", "-1"])                      # прочие команды фильтров не запускают
    assert calls == [["git", "-C", str(tmp_path), *SAFE, "log", "-1"]]


@pytest.mark.parametrize("answer", [
    subprocess.CompletedProcess([], 129, b"", b"usage"),                      # git без --show-scope
    subprocess.CompletedProcess([], 0, b"local\0filter.a=b.clean\nx\0", b""),  # имя не выразить через -c
    subprocess.CompletedProcess([], 0, b"local\0filter.\xff.clean\nx\0", b""),
])
def test_status_is_not_read_when_the_filter_config_cannot_be_checked(monkeypatch, tmp_path, answer):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return answer

    monkeypatch.setattr(subprocess, "run", run)
    assert gitinfo.run(tmp_path, ["status"]).returncode != 0
    assert len(calls) == 1                                    # сам статус не запускался
    with pytest.raises(errors.Refused):
        gitinfo.status(tmp_path)


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
    assert gitinfo.status(repo)["unpushed"] == 1
    (repo / "новый файл.txt").write_text("b")
    got = gitinfo.status(repo)
    assert got["files"] == [{"path": "новый файл.txt", "status": "M"}] and got["upstream"] is None
    plain = tmp_path / "plain"; plain.mkdir()
    with pytest.raises(errors.Refused) as e:
        gitinfo.status(plain)
    assert e.value.code == "not_git"


@pytest.mark.integration
def test_real_merge_conflict_and_rename_keep_all_path_spaces(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

    def run(*args, check=True):
        return subprocess.run([*git, *args], check=check, capture_output=True)

    run("init", "-q", "-b", "main")
    conflict = tmp_path / "conflict with  spaces.txt"
    conflict.write_text("base\n")
    (tmp_path / "old  name.txt").write_text("rename\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    run("checkout", "-qb", "side")
    conflict.write_text("side\n")
    run("commit", "-qam", "side")
    run("checkout", "-q", "main")
    conflict.write_text("main\n")
    run("commit", "-qam", "main")
    assert run("merge", "side", check=False).returncode == 1
    run("mv", "old  name.txt", "new name with  spaces.txt")
    assert sorted(gitinfo.status(tmp_path)["files"], key=lambda row: row["path"]) == [
        {"path": "conflict with  spaces.txt", "status": "U"},
        {"path": "new name with  spaces.txt", "status": "R"}]


def test_no_upstream_counts_unpublished_commits_and_keeps_failed_count_unknown(monkeypatch, tmp_path):
    outputs = iter([b"# branch.oid abc\0# branch.head main\0", b"3\n"])
    monkeypatch.setattr(gitinfo, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, next(outputs)))
    assert gitinfo.status(tmp_path)["unpushed"] == 3
    outputs = iter([subprocess.CompletedProcess([], 0, b"# branch.oid abc\0"), subprocess.CompletedProcess([], 1, b"")])
    monkeypatch.setattr(gitinfo, "run", lambda *a, **kw: next(outputs))
    assert gitinfo.status(tmp_path)["unpushed"] is None


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
