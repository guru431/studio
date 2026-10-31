"""Рабочие копии только внутри разрешённого корня проектов. Без переноса правок и удаления каталогов."""

import os
import re
import secrets
import subprocess

from . import files, projects
from .errors import Refused

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def _git(project, args):
    try:
        result = subprocess.run(["git", "-C", str(project), *args], capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("error", "Git не ответил при работе с рабочими копиями") from None
    if result.returncode:
        raise Refused("not_git" if args[:1] == ["rev-parse"] else "error",
                      "Git не выполнил операцию; проверьте репозиторий на компьютере")
    return result.stdout.decode("utf-8", "replace")


def list_worktrees(project, root):
    text = _git(project, ["worktree", "list", "--porcelain", "-z"])
    allowed = {os.path.normcase(os.path.realpath(os.path.join(root, row["name"]))): row for row in projects.list_projects(str(root))}
    rows, current = [], {}
    for field in text.split("\0"):
        if not field:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        if key in ("worktree", "branch", "HEAD"):
            current[key] = value
    if current:
        rows.append(current)
    items = []
    for row in rows:
        path = row.get("worktree", "")
        known = allowed.get(os.path.normcase(os.path.realpath(path)))
        if known:
            items.append({**known, "branch": row.get("branch", "").removeprefix("refs/heads/"),
                          "current": os.path.normcase(os.path.realpath(project)) == os.path.normcase(os.path.realpath(path))})
    return {"items": items}


def create(project, root, name):
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise Refused("invalid", "Имя: до 31 латинской буквы, цифры или дефиса")
    top = _git(project, ["rev-parse", "--show-toplevel"]).strip()
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(project)):
        raise Refused("not_git", "Выбранная папка должна быть корнем репозитория")
    root = os.path.realpath(root)
    folder = project.name[:48] + "--" + name
    target = os.path.join(root, folder)
    if not files.inside(root, os.path.realpath(target)) or os.path.lexists(target):
        raise Refused("conflict", "Папка с таким именем уже существует")
    branch = f"studio/{name}-{secrets.token_hex(3)}"
    _git(project, ["worktree", "add", "-b", branch, target, "HEAD"])
    created = projects.find_project(root, projects.pid(folder))
    return {"pid": projects.pid(folder), "name": created.name, "branch": branch}
