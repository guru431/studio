import subprocess

import pytest

from studio_agent import remote_status

HEAD = "a" * 40


@pytest.mark.parametrize("counts, expected", [
    (b"0\t0\n", {"state": "current", "behind": 0, "ahead": 0}),
    (b"0\t3\n", {"state": "behind", "behind": 3, "ahead": 0}),
    (b"2\t0\n", {"state": "ahead", "behind": 0, "ahead": 2}),
    (b"2\t3\n", {"state": "diverged", "behind": 3, "ahead": 2}),
])
def test_configured_remote_names_and_local_comparison_without_network(tmp_path, monkeypatch, counts, expected):
    commands = []

    def run(project, args, **kwargs):
        commands.append(args)
        assert 0 < kwargs["timeout"] <= 3
        if args == ["remote"]:
            raw = b"cld\norigin\nupstream\nbackup\n"
        elif args == ["rev-parse", "--is-shallow-repository"]:
            raw = b"false\n"
        else:
            assert args[:3] == ["rev-list", "--left-right", "--count"] and args[-1] == "--"
            assert args[3].endswith("/feature/name..." + HEAD)
            raw = counts
        return subprocess.CompletedProcess(args, 0, raw)

    monkeypatch.setattr(remote_status.gitinfo, "run", run)
    assert remote_status.read(tmp_path, "feature/name", HEAD, now=lambda: 10) == [
        {"name": name, **expected} for name in ("cld", "origin", "backup")]
    assert all(args[0] in ("remote", "rev-parse", "rev-list") for args in commands)
    assert not any("refs/remotes/upstream/" in arg for args in commands for arg in args)


@pytest.mark.parametrize("failure", ["detached", "initial", "missing", "shallow", "timeout", "malformed"])
def test_unavailable_history_keeps_remote_visible_without_false_current(tmp_path, monkeypatch, failure):
    def run(project, args, **kwargs):
        if args == ["remote"]:
            return subprocess.CompletedProcess(args, 0, b"cld\n")
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, b"true\n" if failure == "shallow" else b"false\n")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git", 3)
        return subprocess.CompletedProcess(args, 128 if failure == "missing" else 0, b"broken")

    monkeypatch.setattr(remote_status.gitinfo, "run", run)
    assert remote_status.read(tmp_path, None if failure == "detached" else "main",
                              None if failure == "initial" else HEAD, now=lambda: 10) == [
        {"name": "cld", **remote_status.UNKNOWN}]


def test_total_budget_keeps_remaining_names_visible(tmp_path, monkeypatch):
    stamps = iter([0, 0, 1, 2, 6])
    commands = []

    def run(project, args, **kwargs):
        commands.append(args)
        raw = b"cld\nbackup\n" if args[0] == "remote" else b"false\n" if args[0] == "rev-parse" else b"0\t2\n"
        return subprocess.CompletedProcess(args, 0, raw)

    monkeypatch.setattr(remote_status.gitinfo, "run", run)
    assert remote_status.read(tmp_path, "main", HEAD, now=lambda: next(stamps)) == [
        {"name": "cld", "state": "behind", "behind": 2, "ahead": 0},
        {"name": "backup", **remote_status.UNKNOWN}]
    assert len(commands) == 3


@pytest.mark.integration
def test_real_git_cld_tracking_refs_and_unfetched_remote_are_reported(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True).stdout.strip().decode()

    git("init", "-qb", "main")
    git("commit", "--allow-empty", "-qm", "first")
    git("remote", "add", "cld", "https://cld.example.invalid/RD/project.git")
    git("remote", "add", "backup", "https://backup.example.invalid/project.git")
    git("remote", "add", "upstream", "https://upstream.example.invalid/project.git")
    git("update-ref", "refs/remotes/upstream/main", "HEAD")
    git("update-ref", "refs/remotes/cld/main", "HEAD")
    git("commit", "--allow-empty", "-qm", "second")
    result = remote_status.read(repo, "main", git("rev-parse", "HEAD"))
    assert result == [{"name": "backup", **remote_status.UNKNOWN},
                      {"name": "cld", "state": "behind", "behind": 1, "ahead": 0}]
    git("update-ref", "refs/remotes/cld/main", "HEAD")
    assert remote_status.read(repo, "main", git("rev-parse", "HEAD"))[1]["state"] == "current"
