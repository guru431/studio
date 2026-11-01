import io
import subprocess
from urllib.error import HTTPError

import pytest

from studio_agent import github_status, project_summary

LOCAL = "a" * 40
REMOTE = "b" * 40


def packet(data):
    return f"{len(data) + 4:04x}".encode() + data


def advertisement(sha=REMOTE, branch="main"):
    return (packet(b"# service=git-upload-pack\n") + b"0000"
            + packet(f"{sha} HEAD\0multi_ack\n".encode())
            + packet(f"{sha} refs/heads/{branch}\n".encode()) + b"0000")


@pytest.fixture
def github(monkeypatch):
    state = {"url": "git@github.com:owner/public.git", "raw": advertisement(), "counts": b"0\t3\n",
             "error": None, "shallow": b"false\n", "git_error": False, "requests": [], "commands": []}

    def run(project, args, **kwargs):
        state["commands"].append(args)
        if args[0] == "config":
            raw = f"remote.github.url {state['url']}\n".encode() if state["url"] else b""
            return subprocess.CompletedProcess(args, 0 if raw else 1, raw)
        if args == ["rev-parse", "--is-shallow-repository"]:
            return subprocess.CompletedProcess(args, 0, state["shallow"])
        assert args == ["rev-list", "--left-right", "--count", f"{REMOTE}...{LOCAL}"]
        return subprocess.CompletedProcess(args, 128 if state["git_error"] else 0, state["counts"])

    class Opener:
        def open(self, req, timeout):
            state["requests"].append(req)
            assert timeout == 5
            assert req.full_url == "https://github.com/owner/public.git/info/refs?service=git-upload-pack"
            assert not req.has_header("Authorization") and not req.has_header("Cookie")
            if state["error"]:
                raise state["error"]
            return io.BytesIO(state["raw"])

    monkeypatch.setattr(github_status.gitinfo, "run", run)
    monkeypatch.setattr(github_status, "build_opener", lambda *handlers: Opener())
    return state


@pytest.mark.parametrize("url", ["git@github.com:owner/public.git", "ssh://git@github.com/owner/public.git",
    "https://github.com/owner/public.git", "https://user:private-token@github.com/owner/public.git"])
def test_live_public_check_ignores_local_remote_refs_and_never_sends_credentials(tmp_path, github, url):
    github["url"] = url
    assert github_status.read(tmp_path, "main", LOCAL) == {"state": "behind", "behind": 3, "ahead": 0}
    assert len(github["requests"]) == 1
    assert all("fetch" not in args and "push" not in args for args in github["commands"])


@pytest.mark.parametrize(("counts", "expected"), [(b"2 0", {"state": "ahead", "ahead": 2, "behind": 0}),
    (b"2 3", {"state": "diverged", "ahead": 2, "behind": 3})])
def test_distinguishes_github_ahead_and_diverged(tmp_path, github, counts, expected):
    github["counts"] = counts
    assert github_status.read(tmp_path, "main", LOCAL) == expected


def test_current_sha_and_unpublished_branch(tmp_path, github):
    github["raw"] = advertisement(LOCAL)
    assert github_status.read(tmp_path, "main", LOCAL) == {"state": "current", "behind": 0, "ahead": 0}
    assert github_status.read(tmp_path, "feature/new", LOCAL) == {"state": "missing", "behind": None, "ahead": None}
    github["raw"] = advertisement(branch="feature/new")
    assert github_status.read(tmp_path, "feature/new", LOCAL)["state"] == "behind"


@pytest.mark.parametrize("url", [None, "https://git.example.net/owner/private.git",
    "https://github.com.evil.test/owner/public.git", "ext::command", "file:///private"])
def test_no_github_remote_never_uses_network(tmp_path, github, url):
    github["url"] = url
    assert github_status.read(tmp_path, "main", LOCAL) is None
    assert not github["requests"]


@pytest.mark.parametrize("code", [401, 404, 403, 500])
def test_private_repository_hidden_and_server_errors_unknown(tmp_path, github, code):
    github["error"] = HTTPError("", code, "", {}, None)
    result = github_status.read(tmp_path, "main", LOCAL)
    assert result == (None if code in (401, 404) else github_status.UNKNOWN)


@pytest.mark.parametrize("failure", ["timeout", "malformed", "truncated", "oversize", "unknown_sha", "shallow", "detached"])
def test_incomplete_checks_never_report_current(tmp_path, github, failure):
    if failure == "timeout":
        github["error"] = TimeoutError()
    elif failure == "malformed":
        github["raw"] = b"<html>login</html>"
    elif failure == "truncated":
        github["raw"] = advertisement()[:-4]
    elif failure == "oversize":
        github["raw"] = b"x" * (github_status.REFS_MAX + 1)
    elif failure == "unknown_sha":
        github["git_error"] = True
    elif failure == "shallow":
        github["shallow"] = b"true\n"
    assert github_status.read(tmp_path, None if failure == "detached" else "main", LOCAL) == github_status.UNKNOWN


def test_summary_keeps_local_metrics_when_github_check_fails(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def run(project, args, **kwargs):
        raw = (f"# branch.oid {LOCAL}\0# branch.head main\0# branch.upstream origin/main\0# branch.ab +2 -0\0".encode()
               if args[0] == "status" else b"100\n")
        return subprocess.CompletedProcess(args, 0, raw)

    def read(project, branch, head):
        assert branch == "main" and head == LOCAL
        return dict(github_status.UNKNOWN)

    monkeypatch.setattr(github_status.gitinfo, "run", run)
    monkeypatch.setattr(github_status, "read", read)
    result = project_summary.read(tmp_path, now=lambda: 200)
    assert result["git"] == "ok" and result["changed"] == 0 and result["unpushed"] == 2
    assert result["github"] == github_status.UNKNOWN


def test_redirect_stays_on_github():
    from urllib.request import Request

    handler = github_status._GithubRedirect()
    req = Request("https://github.com/owner/old.git/info/refs?service=git-upload-pack")
    assert handler.redirect_request(req, None, 301, "", {}, "https://github.com/owner/new.git")
    with pytest.raises(ValueError):
        handler.redirect_request(req, None, 301, "", {}, "https://other.test/owner/new.git")


@pytest.mark.integration
def test_real_git_clean_origin_still_detects_outdated_github_with_stale_tracking_refs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True).stdout.strip().decode()

    git("init", "-qb", "main")
    git("commit", "--allow-empty", "-qm", "first")
    first = git("rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
    git("remote", "add", "origin", str(remote))
    git("remote", "add", "github", "https://github.com/owner/public.git")
    git("push", "-qu", "origin", "main")
    git("commit", "--allow-empty", "-qm", "second")
    git("push", "-q", "origin", "main")
    # Намеренно неверный tracking ref говорит «актуально», живой ответ GitHub отстаёт.
    git("update-ref", "refs/remotes/github/main", "HEAD")

    class Opener:
        def open(self, req, timeout):
            return io.BytesIO(advertisement(first))

    monkeypatch.setattr(github_status, "build_opener", lambda *handlers: Opener())
    result = project_summary.read(repo, now=lambda: 1234)
    assert result["changed"] == result["unpushed"] == 0
    assert result["github"] == {"state": "behind", "behind": 1, "ahead": 0}
