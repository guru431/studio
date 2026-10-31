"""Один обход дерева: только размеры и числа файлов/папок, без чтения содержимого."""

import json
import os
import stat
import time

from . import files
from .errors import Refused

MAX_SECONDS = 20
MAX_DIRS = 5000
MAX_BYTES = 4 * 1024 * 1024  # запас под ASCII-JSON кадра; длинные имена тоже ограничены


def scan(project, path="", *, now=time.time, monotonic=time.monotonic):
    target = files.resolve(project, path)
    if not target.is_dir():
        raise Refused("not_found", files.NO_DIR)
    deadline = monotonic() + MAX_SECONDS
    totals = {}

    def row(relative):
        return {"path": relative, "size": 0, "files": 0, "folders": 0, "complete": True}

    totals[path] = row(path)
    pending = [path]
    budget = len(json.dumps(path, ensure_ascii=True)) + 160
    skipped_links = 0
    while pending:
        if monotonic() >= deadline:
            for relative in pending:
                totals[relative]["complete"] = False
            break
        relative = pending.pop()
        current = totals[relative]
        try:
            # Повторная проверка границы перед каждой папкой: её могли заменить ссылкой во время обхода.
            directory = files.resolve(project, relative)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if monotonic() >= deadline:
                        current["complete"] = False
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        current["complete"] = False
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        skipped_links += 1
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        current["folders"] += 1
                        child = f"{relative}/{entry.name}" if relative else entry.name
                        try:
                            files._parts(child)
                        except Refused:
                            current["complete"] = False
                            continue
                        cost = len(json.dumps(child, ensure_ascii=True)) + 160
                        if len(totals) >= MAX_DIRS or budget + cost > MAX_BYTES:
                            current["complete"] = False
                            continue
                        budget += cost
                        totals[child] = row(child)
                        pending.append(child)
                    elif stat.S_ISREG(info.st_mode):
                        current["files"] += 1
                        current["size"] += info.st_size
        except (OSError, Refused):
            current["complete"] = False
    # От листьев к корню: родитель получает все вложенные файлы, папки и признак неполного обхода.
    for relative in sorted(totals, key=lambda p: (p.count("/"), len(p)), reverse=True):
        if relative == path:
            continue
        parent = totals[relative.rpartition("/")[0]]
        child = totals[relative]
        for key in ("size", "files", "folders"):
            parent[key] += child[key]
        parent["complete"] = parent["complete"] and child["complete"]
    return {"items": list(totals.values()), "complete": totals[path]["complete"],
            "skipped_links": skipped_links, "checked_at": int(now())}
