"""Статус git проекта (протокол §4, `git`): `git status --porcelain=v2 --branch -z`."""

import os
import subprocess
from pathlib import Path

from . import limits
from .errors import Refused

GIT_SEC = 25


def _path(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def parse(out: bytes, *, truncate: bool = True) -> dict:
    """Вывод porcelain v2 с `-z` → ветка, upstream, ahead/behind и файлы (`XY` без точек)."""
    res = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "files": []}
    records = out.split(b"\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if rec.startswith(b"# branch.head "):
            head = _path(rec[len(b"# branch.head "):])
            res["branch"] = None if head == "(detached)" else head
        elif rec.startswith(b"# branch.upstream "):
            res["upstream"] = _path(rec[len(b"# branch.upstream "):])
        elif rec.startswith(b"# branch.ab "):
            ahead, behind = rec[len(b"# branch.ab "):].split()
            res["ahead"], res["behind"] = abs(int(ahead)), abs(int(behind))
        elif rec.startswith(b"1 "):
            fields = rec.split(b" ", 8)
            res["files"].append({"path": _path(fields[8]), "status": fields[1].decode().replace(".", "")})
        elif rec.startswith(b"2 "):
            fields = rec.split(b" ", 9)
            res["files"].append({"path": _path(fields[9]), "status": fields[1].decode().replace(".", "")})
            i += 1  # следом — прежний путь переименования
        elif rec.startswith(b"u "):
            res["files"].append({"path": _path(rec.split(b" ", 10)[10]), "status": "U"})
        elif rec.startswith(b"? "):
            res["files"].append({"path": _path(rec[2:]), "status": "??"})
    if truncate:
        del res["files"][limits.GIT_FILES_MAX:]
    return res


def run(project: Path, args: list[str], *, timeout: float = GIT_SEC) -> subprocess.CompletedProcess:
    """Локальное чтение Git в границах проекта, без окна, запроса пароля и необязательных замков."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=str(Path(os.path.realpath(project)).parent))
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, env=env, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def status(project: Path) -> dict:
    """`git -C <проект> status` без окна и без необязательных замков; код ≠ 0 или нет git — `not_git`.

    Выше проекта git не ищет (`GIT_CEILING_DIRECTORIES` — родитель настоящего пути): проект без своего `.git` внутри
    чужого репозитория — `not_git`, ветка и пути внешнего не уходят в студию.
    Срок вышел — `subprocess.TimeoutExpired` летит наружу (просьба получит `error`)."""
    try:
        r = run(project, ["status", "--porcelain=v2", "--branch", "-z"])
    except FileNotFoundError:
        raise Refused("not_git") from None
    if r.returncode != 0:
        raise Refused("not_git")
    state = parse(r.stdout)
    if not state["upstream"]:
        state["unpushed"] = None
        if b"# branch.oid (initial)\0" in r.stdout:
            state["unpushed"] = 0
        elif b"# branch.oid " in r.stdout:
            try:
                count = run(project, ["rev-list", "--count", "HEAD", "--not", "--remotes"], timeout=5)
                if count.returncode == 0:
                    state["unpushed"] = int(count.stdout.strip())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    return state
