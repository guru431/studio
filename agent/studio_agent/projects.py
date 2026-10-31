"""Проекты — подпапки первого уровня разрешённых папок проектов (протокол §5)."""

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


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(path))


def roots(config) -> list[str]:
    """Настройки, список или прежний одиночный путь; дубли настоящих путей исключены."""
    paths = getattr(config, "projects_roots", None)
    if paths is None:
        paths = [os.fspath(config)] if isinstance(config, (str, os.PathLike)) else config
    seen, result = set(), []
    for path in paths:
        if path and (key := _key(path)) not in seen:
            seen.add(key)
            result.append(os.fspath(path))
    return result


def project_id(config, root, name: str) -> str:
    """Прежняя папка сохраняет pid имени; у остальных папок постоянное пространство абсолютного пути."""
    paths = roots(config)
    legacy = getattr(config, "projects_legacy_root", "") or (paths[0] if paths else "")
    return pid(name) if legacy and _key(root) == _key(legacy) else pid(_key(root) + "\0" + name)


def catalog(config) -> list[tuple[dict, Path]]:
    """Публичная строка и локальный путь: пути остаются только внутри моста."""
    items, seen = [], set()
    for root in roots(config):
        try:
            real_root = os.path.realpath(root)
            entries = list(os.scandir(real_root))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                attrs = entry.stat(follow_symlinks=False).st_file_attributes
                if attrs & (HIDDEN | stat.FILE_ATTRIBUTE_REPARSE_POINT) or not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(os.path.realpath(entry.path))
                key = _key(path)
                if not inside(real_root, path) or key in seen:
                    continue
            except OSError:
                continue
            seen.add(key)
            row = {"pid": project_id(config, root, entry.name), "name": entry.name}
            items.append((row, path))
    counts = {}
    for row, _ in items:
        counts[row["name"].casefold()] = counts.get(row["name"].casefold(), 0) + 1
    for row, path in items:
        if counts[row["name"].casefold()] > 1:
            # Последние знаки id различают и папки с одинаковым именем родителя, без передачи полного пути.
            row["name"] += f" · {path.parent.name} · {row['pid'][-6:]}"
    items.sort(key=lambda item: (item[0]["name"].casefold(), item[0]["name"]))
    return items


def list_projects(root) -> list[dict]:
    """`[{\"pid\", \"name\"}]` по имени: без скрытых, системных папок и ссылок; корня нет — `[]`."""
    return [row for row, _ in catalog(root)]


def find_project(root, project: str) -> Path:
    """Настоящий путь проекта по `pid`; нет — `not_found` «нет такого проекта»."""
    for row, path in catalog(root):
        if row["pid"] == project:
            return path
    raise Refused("not_found", "нет такого проекта")


def parent_root(config, project) -> str:
    """Рабочая копия создаётся рядом с проектом внутри той же разрешённой папки."""
    for root in roots(config):
        if _key(Path(project).parent) == _key(root):
            return os.path.realpath(root)
    raise Refused("outside")
