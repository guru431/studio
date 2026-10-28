"""Проекты — подпапки первого уровня папки проектов (протокол §5)."""

import hashlib
import os
import stat
from pathlib import Path

from .errors import Refused
from .files import inside

HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM


def pid(name: str) -> str:
    """Первые 16 знаков hex sha256 имени папки (UTF-8 как отдала ОС, одинокие суррогаты — как есть)."""
    return hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def list_projects(root: str) -> list[dict]:
    """`[{"pid", "name"}]` по имени: без `.`-папок, скрытых, системных и ведущих наружу; корня нет — `[]`."""
    if not root:
        return []
    try:
        real_root = os.path.realpath(root)
        entries = list(os.scandir(real_root))
    except OSError:
        return []
    items = []
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.stat(follow_symlinks=False).st_file_attributes & HIDDEN or not e.is_dir():
                continue
            if not inside(real_root, os.path.realpath(e.path)):
                continue
        except OSError:
            continue
        items.append({"pid": pid(e.name), "name": e.name})
    items.sort(key=lambda i: (i["name"].casefold(), i["name"]))
    return items


def find_project(root: str, project: str) -> Path:
    """Настоящий путь проекта по `pid`; нет — `not_found` «нет такого проекта»."""
    for p in list_projects(root):
        if p["pid"] == project:
            return Path(os.path.realpath(os.path.join(os.path.realpath(root), p["name"])))
    raise Refused("not_found", "нет такого проекта")
