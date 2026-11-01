"""Живая анонимная проверка публичной копии GitHub без fetch, ключей и изменения refs."""

import re
import subprocess
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import gitinfo

REFS_MAX = 2 * 1024 * 1024
GIT_SEC = 3
HTTP_SEC = 5
UNKNOWN = {"state": "unknown", "behind": None, "ahead": None}


class _GithubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc != "github.com":
            raise ValueError("redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _repository(raw: str) -> str | None:
    # Удаляем userinfo локально: ни PAT из HTTPS, ни SSH-ключ в запрос не попадают.
    match = re.fullmatch(r"(?:https://(?:[^/@]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                         r"([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", raw, re.I)
    return match[1] if match else None


def _refs(raw: bytes) -> dict[str, str]:
    """Только discovery smart HTTP v0: pkt-line с hash/ref, без загрузки объектов."""
    if not raw.startswith(b"001e# service=git-upload-pack\n0000"):
        raise ValueError("advertisement")
    refs = {}
    offset = 34
    while offset < len(raw):
        size = int(raw[offset:offset + 4], 16)
        offset += 4
        if size == 0:
            if offset != len(raw):
                raise ValueError("trailing data")
            return refs
        if size < 4 or offset + size - 4 > len(raw):
            raise ValueError("packet")
        line = raw[offset:offset + size - 4].split(b"\0", 1)[0].rstrip(b"\n")
        offset += size - 4
        sha, ref = line.split(b" ", 1)
        if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValueError("hash")
        refs[ref.decode("utf-8")] = sha.decode("ascii")
    raise ValueError("incomplete advertisement")


def read(project: Path, branch: str | None, head: str | None) -> dict | None:
    """behind/ahead — сколько коммитов отстаёт/опережает GitHub относительно текущего HEAD.

    Успешное анонимное чтение подтверждает публичность; приватный/отсутствующий remote скрыт.
    Незнакомый объект, shallow history, сеть или ошибка — unknown, никогда не ложный current.
    """
    repo = None
    try:
        config = gitinfo.run(project, ["config", "--get-regexp", r"^remote\.(github|origin)\.url$"], timeout=GIT_SEC)
        if config.returncode != 0:
            return None
        remotes = dict(line.split(None, 1) for line in config.stdout.decode("utf-8").splitlines())
        repo = _repository(remotes.get("remote.github.url", "")) or _repository(remotes.get("remote.origin.url", ""))
        if not repo:
            return None
        req = Request(f"https://github.com/{repo}.git/info/refs?service=git-upload-pack",
                      headers={"User-Agent": "studio-agent", "Cache-Control": "no-cache"})
        # Собственный opener не наследует cookie/auth handlers приложения или настройки Git/GCM.
        with build_opener(_GithubRedirect()).open(req, timeout=HTTP_SEC) as response:
            raw = response.read(REFS_MAX + 1)
        if len(raw) > REFS_MAX:
            return dict(UNKNOWN)
        refs = _refs(raw)
        if not branch or not head:
            return dict(UNKNOWN)
        remote_head = refs.get("refs/heads/" + branch)
        if remote_head is None:
            return {"state": "missing", "behind": None, "ahead": None}
        if remote_head == head:
            return {"state": "current", "behind": 0, "ahead": 0}
        shallow = gitinfo.run(project, ["rev-parse", "--is-shallow-repository"], timeout=GIT_SEC)
        if shallow.returncode != 0 or shallow.stdout.strip() != b"false":
            return dict(UNKNOWN)
        count = gitinfo.run(project, ["rev-list", "--left-right", "--count", f"{remote_head}...{head}"],
                            timeout=GIT_SEC)
        if count.returncode != 0:
            return dict(UNKNOWN)
        ahead, behind = map(int, count.stdout.split())
        if min(ahead, behind) < 0:
            return dict(UNKNOWN)
        state = "diverged" if ahead and behind else "behind" if behind else "ahead" if ahead else "current"
        return {"state": state, "behind": behind, "ahead": ahead}
    except HTTPError as exc:
        return None if exc.code in (401, 404) else dict(UNKNOWN)
    except (OSError, ValueError, HTTPException, subprocess.TimeoutExpired):
        return dict(UNKNOWN) if repo else None
