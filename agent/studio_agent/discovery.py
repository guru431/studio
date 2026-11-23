"""Подсказки путей, поиск по содержимому файлов и ограниченный поиск по локальной истории. Без постоянного индекса
и журналирования."""

import asyncio
import os
import stat
import time

from . import files, limits
from .errors import Refused

IGNORED = frozenset((".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                     "target", ".next", ".cache", ".pytest_cache", ".mypy_cache", ".ssh", ".aws"))
PRIVATE = frozenset((".env", "vault.env", "auth.json", "credentials.json", "pin.bin"))
MAX_FILES = 5000
MAX_DIRS = 1000
MAX_SECONDS = 3
MAX_CHATS = 60
MAX_MESSAGES = 1500
SEARCH_SECONDS = 15
GREP_SECONDS = 10
GREP_HITS = 100
GREP_PER_FILE = 10


def normalize(text):
    return str(text).casefold().replace("ё", "е")


def _walk(project, start, deadline, state, monotonic):
    """Пути обычных файлов ниже `start` (генератор): без служебных каталогов, известных файлов секретов, symlink и
    reparse points; пределы — `deadline`, MAX_DIRS каталогов, MAX_FILES файлов. Неполный обход —
    `state["complete"] = False`."""
    pending, visited, count = [start], 0, 0
    while pending:
        if monotonic() >= deadline or visited >= MAX_DIRS or count >= MAX_FILES:
            state["complete"] = False
            break
        relative = pending.pop()
        visited += 1
        try:
            with os.scandir(files.resolve(project, relative)) as iterator:
                for entry in iterator:
                    if monotonic() >= deadline or count >= MAX_FILES:
                        state["complete"] = False
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        path = f"{relative}/{entry.name}" if relative else entry.name
                        files._parts(path)
                        name = entry.name.casefold()
                        if stat.S_ISDIR(info.st_mode):
                            if name not in IGNORED and len(pending) + visited < MAX_DIRS:
                                pending.append(path)
                            elif name not in IGNORED:
                                state["complete"] = False
                        elif stat.S_ISREG(info.st_mode):
                            count += 1
                            if name in PRIVATE or name.startswith(".env.") and not name.endswith((".example", ".sample")):
                                continue
                            if name.endswith((".pem", ".key", ".p12", ".pfx", ".local.ps1", ".local.py")):
                                continue
                            yield path, info
                    except (OSError, Refused):
                        state["complete"] = False
        except (OSError, Refused):
            state["complete"] = False


def suggest(project, query, *, monotonic=time.monotonic, limit=50):
    """Только имена; не следуем reparse points, результат явно помечает предел обхода."""
    deadline = monotonic() + MAX_SECONDS
    state = {"complete": True}
    needle = normalize(query.strip())
    matches = [path for path, _ in _walk(project, "", deadline, state, monotonic)
               if all(word in normalize(path) for word in needle.split())]
    complete = state["complete"]
    matches.sort(key=lambda path: (not normalize(path.rsplit("/", 1)[-1]).startswith(needle), len(path), normalize(path)))
    return {"items": matches[:limit] if limit is not None else matches, "complete": complete,
            "more": limit is not None and len(matches) > limit}


class FileIndex:
    """Восемь индексов имён на 30 с; одновременные запросы не запускают повторные обходы."""

    def __init__(self, *, now=time.monotonic):
        self.now, self.cache, self.lock = now, {}, asyncio.Lock()

    async def read(self, project, query):
        key = os.path.normcase(os.path.realpath(project))
        async with self.lock:
            hit = self.cache.get(key)
            if hit is None or self.now() - hit[0] >= 30:
                data = await asyncio.to_thread(suggest, project, "", limit=None)
                self.cache.pop(key, None)
                self.cache[key] = (self.now(), data)
                while len(self.cache) > 8:
                    self.cache.pop(next(iter(self.cache)))
            else:
                data = hit[1]
        needle = normalize(query.strip())
        paths = [path for path in data["items"] if all(word in normalize(path) for word in needle.split())]
        paths.sort(key=lambda path: (not normalize(path.rsplit("/", 1)[-1]).startswith(needle), len(path), normalize(path)))
        return {"items": paths[:50], "complete": data["complete"], "more": len(paths) > 50}


def snippet(text, query):
    flat = " ".join(str(text).split())
    at = normalize(flat).find(normalize(query))
    start = max(0, at - 65)
    end = min(len(flat), max(0, at) + len(query) + 100)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def grep(project, path, query, *, monotonic=time.monotonic):
    """Строки с `query` (без регистра, ё = е) в файлах папки `path` (0.47.0): обход и исключения — как у подсказок
    (`_walk`), файл больше `limits.TEXT_MAX`, с NUL или не UTF-8 — пропуск, общий срок GREP_SECONDS. До GREP_HITS
    строк, из одного файла — до GREP_PER_FILE; `more` — совпадений больше показанных. Ничего не запоминает."""
    target = files.resolve(project, path)
    if not target.is_dir():
        raise Refused("not_found", files.NO_DIR)
    query = query.strip()
    needle = normalize(query)
    if not needle:
        return {"items": [], "complete": True, "more": False}
    deadline = monotonic() + GREP_SECONDS
    state = {"complete": True}
    hits, more = [], False
    for relative, info in _walk(project, path, deadline, state, monotonic):
        if info.st_size > limits.TEXT_MAX:
            continue
        try:
            with open(files.resolve(project, relative), "rb") as source:
                data = source.read(limits.TEXT_MAX + 1)
        except (OSError, Refused):
            state["complete"] = False
            continue
        if len(data) > limits.TEXT_MAX or b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if needle not in normalize(text):
            continue
        found = 0
        for number, line in enumerate(text.splitlines(), 1):
            if needle in normalize(line):
                if found == GREP_PER_FILE or len(hits) == GREP_HITS:
                    more = True
                    break
                hits.append({"path": relative, "line": number, "text": snippet(line, query)})
                found += 1
        if more and len(hits) == GREP_HITS:
            break
    return {"items": hits, "complete": state["complete"], "more": more}


async def search(query, catalog, history, *, monotonic=time.monotonic, timeout=SEARCH_SECONDS):
    """Чтение через существующие адаптеры CLI. Пределы относятся ко всему запросу."""
    needle = normalize(query.strip())
    if not needle:
        return {"items": [], "complete": True, "scanned": 0}
    ordered = sorted(catalog, key=lambda row: row.get("modified") or 0, reverse=True)
    hits, scanned = [], 0
    complete = len(ordered) <= MAX_CHATS
    deadline = monotonic() + timeout
    for row in ordered[:MAX_CHATS]:
        if monotonic() >= deadline or scanned >= MAX_MESSAGES:
            complete = False
            break
        found = []
        before = None
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0 or scanned >= MAX_MESSAGES:
                    complete = False
                    break
                page = await asyncio.wait_for(history(row, before), remaining)
                items = page.get("items", [])
                for item in reversed(items):
                    if item.get("role") not in ("user", "assistant"):
                        continue
                    scanned += 1
                    text = item.get("text", "")
                    if isinstance(text, str) and needle in normalize(text):
                        found.append({"text": snippet(text, query), "role": item["role"], "id": str(item.get("id", ""))[:100]})
                    if scanned >= MAX_MESSAGES:
                        complete = False
                        break
                start = page.get("start", 0)
                if len(found) >= 3 or not items or not isinstance(start, int) or start <= 0:
                    break
                if before is not None and start >= before:
                    complete = False
                    break
                before = start
        except Exception:
            # Исчезнувший чат/отказ одного CLI не скрывает найденное в остальных.
            complete = False
        if found or needle in normalize(row.get("title", "")):
            hits.append({"chat": row["chat"], "engine": row.get("engine", "claude"),
                         "title": row.get("title", "")[:200], "snippets": found[:3]})
    return {"items": hits, "complete": complete, "scanned": scanned}
