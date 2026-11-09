"""Рабочие копии только внутри разрешённого корня проектов. Без переноса правок и удаления каталогов."""

import os
import re
import secrets
import subprocess

from . import files, gitinfo, projects
from .errors import Refused

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def _git(project, args):
    """`core.fsmonitor` конфига репозитория не исполняется (`gitinfo.SAFE`). `worktree add` — по нажатию владельца:
    фильтры (git-lfs) и хуки выгрузки файлов в новой копии работают, как у его собственного git."""
    try:
        result = subprocess.run(["git", "-C", str(project), *gitinfo.SAFE, *args], capture_output=True,
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
    allowed = {os.path.normcase(os.path.realpath(path)): row for row, path in projects.catalog(root)}
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
    config = root
    root = projects.parent_root(config, project)
    top = _git(project, ["rev-parse", "--show-toplevel"]).strip()
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(project)):
        raise Refused("not_git", "Выбранная папка должна быть корнем репозитория")
    folder = project.name[:48] + "--" + name
    target = os.path.join(root, folder)
    if not files.inside(root, os.path.realpath(target)) or os.path.lexists(target):
        raise Refused("conflict", "Папка с таким именем уже существует")
    branch = f"studio/{name}-{secrets.token_hex(3)}"
    _git(project, ["worktree", "add", "-b", branch, target, "HEAD"])
    project_id = projects.project_id(config, root, folder)
    created = projects.find_project(config, project_id)
    return {"pid": project_id, "name": created.name, "branch": branch}
