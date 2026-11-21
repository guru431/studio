"""Статус git проекта (протокол §4, `git`): `git status --porcelain=v2 --branch -z`."""

import os
import re
import subprocess
from pathlib import Path

from . import files, limits
from .errors import Refused

GIT_SEC = 25
NO_GIT = "git не найден на компьютере"
UNSAFE = "git отказал: владелец папки другой — проверьте safe.directory для этой папки"
GIT_FAILED = "git не смог прочитать репозиторий"
COMPARE_MAX = 128 * 1024
COMPARE_SIZE = "для сравнения нужен текстовый файл до 128 КиБ"


def _path(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def parse(out: bytes, *, truncate: bool = True, originals: bool = False) -> dict:
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
            row = {"path": _path(fields[9]), "status": fields[1].decode().replace(".", "")}
            if originals:
                row["old_path"] = _path(records[i])
            res["files"].append(row)
            i += 1  # следом — прежний путь переименования
        elif rec.startswith(b"u "):
            res["files"].append({"path": _path(rec.split(b" ", 10)[10]), "status": "U"})
        elif rec.startswith(b"? "):
            res["files"].append({"path": _path(rec[2:]), "status": "??"})
    if truncate:
        del res["files"][limits.GIT_FILES_MAX:]
    return res


# `.git/config` проекта — данные папки (скопированный каталог, архив с `.git`), а не настройки владельца, и мост
# читает Git сам, без участия владельца. Ключи, которыми конфиг исполняет команды при чтениях моста, гасит
# командная строка (она сильнее любых файлов конфигурации): `core.fsmonitor` — при `status`, `gpg.program` —
# при `log`, если `log.showSignature`. Драйверы фильтров — `_filters`; хуки чтения не запускают
# (`post-index-change` — только при записи индекса, а её нет: `GIT_OPTIONAL_LOCKS=0`).
SAFE = ["-c", "core.fsmonitor=false", "-c", "log.showSignature=false"]
# Частичный клон без объекта дозагружает его с promisor-remote и исполняет `remote.<имя>.uploadpack` конфига через
# shell. Чтения моста локальные: дозагрузку гасит `GIT_NO_LAZY_FETCH` (git 2.45+), а старый git без неё — пустой
# список разрешённых протоколов (никакой транспорт не запускается).
OFFLINE = {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
FILTER_KEYS = r"^filter\..*\.(clean|smudge|process)$"
UNTRUSTED = (b"local", b"worktree")  # конфиг самого репозитория, включая его include


def _filters(base: list[str], env: dict, timeout: float) -> list[str] | None:
    """`-c`, гасящие драйверы фильтров из конфига репозитория: `status` пропускает через `clean`/`process` файлы,
    чья отметка времени не совпала с индексом, — у скопированной папки это все файлы. Драйверы системы и
    владельца (git-lfs) остаются. Конфиг не проверить или имя драйвера не выразить через `-c` — `None`:
    статус не читается (без `--show-scope` — git старше 2.26)."""
    r = subprocess.run([*base, "config", "--show-scope", "-z", "--get-regexp", FILTER_KEYS], capture_output=True,
                       env=env, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode not in (0, 1):  # 1 — ключей нет
        return None
    fields = r.stdout.split(b"\0")
    names = set()
    for scope, entry in zip(fields[0::2], fields[1::2]):
        if scope not in UNTRUSTED:
            continue
        key = entry.split(b"\n", 1)[0]
        try:
            name = key[len(b"filter."):key.rindex(b".")].decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if "=" in name:
            return None
        names.add(name)
    return [arg for name in sorted(names)
            for key, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))
            for arg in ("-c", f"filter.{name}.{key}={value}")]


def run(project: Path, args: list[str], *, timeout: float = GIT_SEC) -> subprocess.CompletedProcess:
    """Локальное чтение Git в границах проекта, без окна, запроса пароля и необязательных замков и без команд
    из конфига репозитория (`SAFE`, `_filters`, `OFFLINE`). `status` не спускается в рабочие копии подмодулей
    (`--ignore-submodules=dirty`): там свой конфиг; сдвиг коммита подмодуля по-прежнему виден."""
    env = dict(os.environ, **OFFLINE, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=str(Path(os.path.realpath(project)).parent))
    base = ["git", "-C", str(project), *SAFE]
    if args[:1] == ["status"]:
        guard = _filters(base, env, timeout)
        if guard is None:
            return subprocess.CompletedProcess(args, 128, b"", b"")
        base += guard
        args = ["status", "--ignore-submodules=dirty", *args[1:]]
    return subprocess.run([*base, *args], capture_output=True, env=env, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _status_bytes(project: Path, *, timeout: float = GIT_SEC) -> bytes:
    try:
        r = run(project, ["status", "--porcelain=v2", "--branch", "-z"], timeout=timeout)
    except FileNotFoundError:
        raise Refused("git_unavailable", NO_GIT) from None
    if r.returncode != 0:
        if not os.path.lexists(os.path.join(project, ".git")):
            raise Refused("not_git")
        # Подсказка `git config --global --add safe.directory …` не переводится в локализованном git.
        raise Refused("git_unavailable", UNSAFE if b"safe.directory" in (r.stderr or b"") else GIT_FAILED)
    return r.stdout


def status(project: Path) -> dict:
    """Снимок статуса своего репозитория, без окна, поиска родительского репозитория и необязательных замков."""
    raw = _status_bytes(project)
    state = parse(raw)
    if not state["upstream"]:
        state["unpushed"] = None
        if b"# branch.oid (initial)\0" in raw:
            state["unpushed"] = 0
        elif b"# branch.oid " in raw:
            try:
                count = run(project, ["rev-list", "--count", "HEAD", "--not", "--remotes"], timeout=5)
                if count.returncode == 0:
                    state["unpushed"] = int(count.stdout.strip())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    return state


def _head_text(project: Path, path: str) -> str:
    tree = run(project, ["ls-tree", "-z", "HEAD", "--", ":(literal)" + path], timeout=3)
    if tree.returncode:
        raise Refused("git_unavailable", GIT_FAILED)
    header, _, listed = tree.stdout.partition(b"\t")
    if not header or _path(listed.removesuffix(b"\0")) != path:
        return ""
    mode, kind, oid = header.split()
    if mode not in (b"100644", b"100755") or kind != b"blob" or not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise Refused("binary")
    object_id = oid.decode("ascii")
    size = run(project, ["cat-file", "-s", object_id], timeout=3)
    if size.returncode or not size.stdout.strip().isdigit():
        raise Refused("git_unavailable", GIT_FAILED)
    if int(size.stdout) > COMPARE_MAX:
        raise Refused("too_large", COMPARE_SIZE)
    contents = run(project, ["cat-file", "blob", object_id], timeout=3)
    if contents.returncode:
        raise Refused("git_unavailable", GIT_FAILED)
    return files._text(contents.stdout)


def compare(project: Path, path: str) -> dict:
    """Одна строка статуса: два текста (HEAD и рабочее дерево), без фильтров и внешних diff-драйверов."""
    if not path:
        raise Refused("invalid")
    target = files.resolve(project, path)
    raw = _status_bytes(project, timeout=8)
    row = next((row for row in parse(raw, originals=True)["files"] if row["path"] == path), None)
    if row is None:
        raise Refused("not_found", "файл больше не числится изменённым")
    if target != Path(os.path.realpath(project)).joinpath(*path.split('/')):
        raise Refused("binary")                 # сравнение не открывает содержимое ссылок
    exists = target.is_file()
    if not exists and target.exists():
        raise Refused("binary")
    if exists:
        _, after = files._read(project, path, COMPARE_MAX, COMPARE_SIZE)
    else:
        after = b""
    original = row.get("old_path", path)
    before = "" if row["status"] == "??" or b"# branch.oid (initial)\0" in raw else _head_text(project, original)
    return {"before": before.replace("\r\n", "\n"), "after": files._text(after).replace("\r\n", "\n"), "exists": exists}
