"""Копии проекта между двумя мостами владельца. Содержимое файлов проходит только по каналу."""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

import auth
import bridge
import claude_code as code
import clock
import project_access
import security
import state

router = APIRouter(prefix="/api/code/agents/{aid}/projects/{pid}/mirrors", default_response_class=code._BridgeJSON)
log = logging.getLogger("studio")
CHUNK = 4 * 1024 * 1024
MAX_ENTRIES = 150_000
DISCOVERY_CAP = "project_mirror_discovery"
SKIP = ["skip", "", 0, 0]
DIR = ["dir", "", 0, 0]
SHA256 = re.compile(r"[0-9a-f]{64}")
EXTERNAL_GIT = "Папка использует внешний каталог Git. Выберите основной репозиторий или отдельный клон."
TRANSFERS = {}
RESULTS = {}


class Destination(BaseModel):
    destination: int = Field(gt=0)
    root: str = Field(pattern=r"^[0-9a-f]{16}$")
    name: str = Field(min_length=1, max_length=120)
    existing: bool = False


class Sync(BaseModel):
    plan: str = Field(pattern=r"^[0-9a-f]{64}$")
    overwrite: bool = False
    keep: list[Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=list, max_length=MAX_ENTRIES)


class FileDiff(BaseModel):
    path: str = Field(min_length=1, max_length=code.PATH_MAX)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


async def _body(request, model, limit=4096):
    raw = await auth.small_body(request, limit)
    if raw is None:
        raise code._refuse(413, "too_large", "настройки зеркала превышают предел")
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        raise code._refuse(422, "invalid", "неверные настройки зеркала") from None


def _owner(request, aid, pid):
    project_access.owner(request, aid)
    code.check_pid(pid)
    if not code._session_open(request.state.digest):
        raise code._refuse(409, "closed", "Сеанс Студии закрыт")


def _rows(aid, pid, mid=None):
    return state.DB.execute(
        "SELECT m.*, a.name AS computer FROM project_mirrors m JOIN agents a ON a.id=m.destination"
        " WHERE m.source=? AND m.project=? AND (? IS NULL OR m.id=?) ORDER BY m.id", (aid, pid, mid, mid))


def _row(request, aid, pid, mid):
    _owner(request, aid, pid)
    row = _rows(aid, pid, mid).fetchone()
    if row is None:
        raise code._refuse(404, "not_found", "зеркало проекта не найдено")
    project_access.owner(request, row["destination"])
    return row


def _item(row):
    aid = row["destination"]
    return {"id": row["id"], "destination": aid, "computer": row["computer"], "online": aid in bridge.LINKS,
            **_location(row), "synced": row["synced"],
            "job": RESULTS.get(row["id"])}


def _location(row):
    return json.loads(security.decrypt(row["location"]))


async def _ask(request, aid, op, *, timeout=180, **args):
    # В обоих направлениях доступ должен оставаться доступом владельца, включая каждый блок данных.
    project_access.owner(request, aid)
    if not code._session_open(request.state.digest):
        raise code._refuse(409, "closed", "Сеанс Студии закрыт")
    code.require_capability(aid, "project_mirror")
    return await code.ask(request, aid, op, quiet=True, reply_timeout=timeout, **args)


def _entries(data, *, missing=False):
    entries = data.get("entries")
    if entries is None and missing and "entries" in data:
        return None
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise code._refuse(502, "bridge", "мост вернул неверный список файлов")
    for path, info in entries.items():
        if not path or path.startswith("/") or len(path) > code.PATH_MAX or not isinstance(info, list) or len(info) != 4:
            raise code._refuse(502, "bridge", "мост вернул неверный список файлов")
        kind, value, size, mode = info
        valid = (info in (DIR, SKIP) or
                 kind == "file" and isinstance(value, str) and SHA256.fullmatch(value) and
                 type(size) is int and 0 <= size < 2**63 and type(mode) is int and mode in (0, 1) or
                 kind == "link" and isinstance(value, str) and 0 < len(value) <= code.PATH_MAX and
                 size == 0 and type(mode) is int and mode in (0, 1))
        if not valid:
            raise code._refuse(502, "bridge", "мост вернул неверный список файлов")
    if ".git" in entries and entries[".git"] != DIR:
        raise code._refuse(409, "conflict", EXTERNAL_GIT)
    return entries


def _source_entry(new, old):
    if new is not None and new[0] == "file" and new[3] is None:
        # Windows не сообщает признак исполнения: сохраняем режим существующего файла POSIX.
        return [*new[:3], old[3] if old is not None and old[0] == "file" else 0]
    return new


def _plan(source, destination, baseline, *, windows=False):
    target = destination or {}
    previous = baseline or {}
    updates = {}
    remove = []
    changes = []
    conflicts = 0
    skipped = 0
    # Папка со специальным объектом не может быть удалена или заменена целиком.
    protected = set()
    ignored = {path for tree in (source, target) for path, entry in tree.items() if entry == SKIP}
    for path, entry in target.items():
        if entry == SKIP:
            while "/" in path:
                path = path.rsplit("/", 1)[0]
                protected.add(path)
    for path in sorted(source.keys() | target.keys()):
        old = target.get(path)
        new = _source_entry(source.get(path), old)
        parent = path
        while parent not in ignored and "/" in parent:
            parent = parent.rsplit("/", 1)[0]
        if parent in ignored or path in protected and new != DIR:
            skipped += 1
            changes.append([path, "skipped", False])
            continue
        if old == new:
            continue
        if new is not None:
            updates[path] = new
        deletion = old is not None and (new is None or old[0] != new[0])
        if deletion:
            remove.append(path)
        conflict = old != previous.get(path)
        conflicts += int(conflict)
        text = all(entry is None or entry[0] == "file" and entry[2] <= 128 * 1024 for entry in (old, new))
        keepable = old is not None and old[0] == "file"
        change = [path, "removed" if new is None else "added" if old is None else "changed", conflict,
                  text, keepable, deletion]
        if windows and (key := path.casefold()) != path:
            change.append(key)
        changes.append(change)
    return updates, remove, changes, conflicts, skipped


def _portable(entries, platform, source_platform):
    if platform == "win32":
        folded = set()
        for path in entries:
            key = path.casefold()
            if key in folded:
                raise code._refuse(409, "conflict", "в проекте есть пути, различающиеся только регистром")
            folded.add(key)
    if platform == "win32" or source_platform == "win32":
        for path, item in entries.items():
            if item[0] == "file":
                entries[path] = [*item[:3], 0 if platform == "win32" else None]
    return entries


def _ancestors(path):
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        yield path


def _selection(plan, selected, updates, remove):
    changes = plan["changes"]
    keys = {change[0]: change[6] for change in changes if len(change) > 6}

    def key(path):
        return keys.get(path, path)

    keep = set()
    for index in selected.keep:
        if index >= len(changes) or len(changes[index]) < 5 or not changes[index][4]:
            raise code._refuse(422, "invalid", "выбранный файл нельзя оставить в копии")
        keep.add(key(changes[index][0]))
    protected = {parent for path in keep for parent in _ancestors(path)}

    def apply(path):
        path = key(path)
        return path not in keep and path not in protected and all(parent not in keep for parent in _ancestors(path))

    if any(conflict and apply(path) for path, _, conflict, *_ in changes) and not selected.overwrite:
        raise code._refuse(409, "conflict", "в зеркале есть локальные изменения")
    if keep:
        updates = {path: entry for path, entry in updates.items() if apply(path)}
        remove = [path for path in remove if apply(path)]
        if not updates and not remove:
            raise code._refuse(422, "invalid", "в плане не осталось изменений")
    return updates, remove


def _next_baseline(source, current, previous, updates, remove):
    current = current or {}
    confirmed = {path: entry for path, entry in (previous or {}).items() if path in source or path in current}
    confirmed.update((path, entry) for path, entry in current.items()
                     if entry != SKIP and _source_entry(source.get(path), entry) == entry)
    for path in remove:
        confirmed.pop(path, None)
    confirmed.update(updates)
    return security.encrypt(_json(confirmed))


def _private_destination(aid, project):
    if state.DB.execute("SELECT 1 FROM project_members WHERE agent_id=? AND project=? LIMIT 1",
                        (aid, project)).fetchone() is not None:
        raise code._refuse(409, "conflict", "папка назначения открыта другим пользователям Студии")


def _can_discover(aid):
    link = bridge.LINKS.get(aid)
    return bool(link and link.hello.get("project_mirror") and link.hello.get(DISCOVERY_CAP))


def _candidates(data, aid, reserved, shared):
    raw = data.get("items")
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw[:100]:
        if not isinstance(item, dict):
            continue
        project, root, name, folder, match = (item.get(key) for key in ("pid", "root", "name", "folder", "match"))
        if (not all(isinstance(value, str) and code.PID_RE.fullmatch(value) for value in (project, root))
                or not isinstance(name, str) or not 1 <= len(name) <= 120
                or not isinstance(folder, str) or not 1 <= len(folder) <= 4096
                or match not in ("repository", "name")):
            continue
        if (aid, root, security.digest(name.casefold())) in reserved or (aid, project) in shared:
            continue
        items.append({"root": root, "name": name, "folder": folder, "match": match})
    return items


@router.post("/discover")
async def discover(aid: int, pid: str, request: Request, destination: int | None = None):
    _owner(request, aid, pid)
    if destination is not None:
        project_access.owner(request, destination)
    if aid not in bridge.LINKS:
        raise code._refuse(409, "offline", "компьютер не на связи")
    if not _can_discover(aid):
        return {"available": False, "devices": []}
    uid = request.state.user.id
    agents = list(state.DB.execute(
        "SELECT id, name, has_pin FROM agents WHERE user_id=? AND id<>? AND (? IS NULL OR id=?) ORDER BY id",
        (uid, aid, destination, destination)))
    if not agents:
        return {"available": True, "devices": []}
    source = await _ask(request, aid, "mirror_identity", project=pid, passive=True, timeout=5)
    name, repositories = source.get("name"), source.get("repositories")
    if (not isinstance(name, str) or not 1 <= len(name) <= 120 or not isinstance(repositories, list)
            or len(repositories) > 16 or not all(isinstance(item, str) and SHA256.fullmatch(item) for item in repositories)):
        raise code._refuse(502, "bridge", "мост вернул неверные сведения о проекте")
    reserved = {tuple(row) for row in state.DB.execute(
        "SELECT m.destination, m.root, m.name_key FROM project_mirrors m JOIN agents a ON a.id=m.destination WHERE a.user_id=?",
        (uid,))}
    shared = {tuple(row) for row in state.DB.execute(
        "SELECT m.agent_id, m.project FROM project_members m JOIN agents a ON a.id=m.agent_id WHERE a.user_id=?",
        (uid,))}
    parallel = asyncio.Semaphore(4)

    async def inspect(agent):
        target = agent["id"]
        result = {"id": target, "computer": agent["name"], "items": []}
        if target not in bridge.LINKS:
            return {**result, "state": "offline"}
        if not _can_discover(target):
            return {**result, "state": "unsupported"}
        async with parallel:
            try:
                if not agent["has_pin"]:
                    await _ask(request, target, "open", pin=None, passive=True, timeout=5)
                data = await _ask(request, target, "mirror_candidates", name=name, repositories=repositories,
                                  passive=True, timeout=25)
            except HTTPException as error:
                reason = error.detail.get("code")
                stage = ("locked" if reason in ("closed", "pin_needed", "pin_locked")
                         else "offline" if reason == "offline" else "error")
                return {**result, "state": stage}
        return {**result, "state": "ready", "complete": data.get("complete") is True,
                "items": _candidates(data, target, reserved, shared)}

    devices = await asyncio.gather(*(inspect(agent) for agent in agents))
    _owner(request, aid, pid)
    return {"available": True, "devices": devices}


async def _compare(request, row):
    aid, pid, destination = row["source"], row["project"], row["destination"]
    to = {"root": row["root"], "name": _location(row)["name"]}
    source, target = await asyncio.gather(_ask(request, aid, "mirror_scan", project=pid),
                                          _ask(request, destination, "mirror_destination", **to))
    if target.get("pid") is None or not code.PID_RE.fullmatch(str(target["pid"])):
        raise code._refuse(502, "bridge", "мост вернул неверное назначение зеркала")
    _private_destination(destination, target["pid"])
    link, from_link = bridge.LINKS.get(destination), bridge.LINKS.get(aid)
    if link is None or from_link is None:
        raise code._refuse(409, "offline", "компьютер не на связи")
    platform = link.hello.get("platform")
    source_platform = from_link.hello.get("platform")

    def compare_trees():
        entries = _portable(_entries(source), platform, source_platform)
        current = _entries(target, missing=True)
        baseline = json.loads(security.decrypt(row["baseline"])) if row["baseline"] else None
        updates, remove, changes, conflicts, skipped = _plan(entries, current, baseline, windows=platform == "win32")
        marker = None
        if row["baseline"] is not None and (updates or remove):
            marker = hashlib.sha256(row["baseline"]).hexdigest()
        token = _digest([row["id"], row["synced"], marker, entries, current])
        result = {"plan": token, "exists": current is not None, "project": target["pid"],
                  "changes": changes, "conflicts": conflicts, "skipped": skipped, "deletions": len(remove),
                  "bytes": sum(info[2] for info in updates.values())}
        return result, to, entries, current, updates, remove, platform, source_platform, baseline

    result = await asyncio.to_thread(compare_trees)
    _owner(request, aid, pid)
    return result


def _batches(updates):
    reads = []
    left = CHUNK
    for path, (kind, _, size, _) in updates.items():
        if kind != "file":
            continue
        offset = 0
        while True:
            if len(reads) == 128 or left == 0:
                yield reads
                reads, left = [], CHUNK
            count = min(size - offset, left)
            reads.append([path, offset, count])
            offset += count
            left -= count
            if offset == size:
                break
    if reads:
        yield reads


async def _sync(request, row, selected, progress):
    aid, pid, destination, mid = row["source"], row["project"], row["destination"], row["id"]
    ticket = secrets.token_hex(16)
    started = False
    try:
        plan, to, source, current, updates, remove, platform, source_platform, baseline = await _compare(request, row)
        if plan["plan"] != selected.plan:
            raise code._refuse(409, "conflict", "проект или зеркало изменились — обновите сравнение")
        updates, remove = await asyncio.to_thread(_selection, plan, selected, updates, remove)
        progress.update(bytes=0, total=sum(info[2] for info in updates.values()))
        if updates or remove or current is None:
            started = True
            await _ask(request, destination, "mirror_begin", ticket=ticket, **to)
            for reads in _batches(updates):
                data = await _ask(request, aid, "mirror_read", project=pid, reads=reads)
                chunks = data.get("chunks")
                if (not isinstance(chunks, list) or len(chunks) != len(reads) or
                        any(not isinstance(chunk, list) or len(chunk) != 3 or chunk[:2] != read[:2]
                            or not isinstance(chunk[2], str) or len(chunk[2]) != ((read[2] + 2) // 3) * 4
                            for chunk, read in zip(chunks, reads))):
                    raise code._refuse(502, "bridge", "мост вернул неверный блок файла")
                await _ask(request, destination, "mirror_chunk", ticket=ticket, chunks=chunks)
                progress["bytes"] += sum(read[2] for read in reads)
            latest = await _ask(request, aid, "mirror_scan", project=pid)
            fresh = await asyncio.to_thread(lambda: _portable(_entries(latest), platform, source_platform))
            if fresh != source:
                raise code._refuse(409, "conflict", "исходный проект изменился во время передачи — обновите сравнение")
            expected = await asyncio.to_thread(_digest, current)
            _private_destination(destination, plan["project"])
            await _ask(request, destination, "mirror_apply", ticket=ticket, expected=expected,
                       updates=updates, remove=remove, **to)
            started = False
        sealed = await asyncio.to_thread(_next_baseline, source, current, baseline, updates, remove)
        state.DB.execute("UPDATE project_mirrors SET baseline=?, synced=? WHERE id=?", (sealed, clock.NOW(), mid))
        progress["state"] = "done"
    except HTTPException as error:
        progress.update(state="failed", error=error.detail)
    except (InvalidToken, ValueError, OSError, sqlite3.Error) as error:
        log.warning("зеркало %d: %s", mid, type(error).__name__)
        progress.update(state="failed", error={"code": "mirror", "message": "Не удалось синхронизировать зеркало"})
    finally:
        try:
            if started:
                await _ask(request, destination, "mirror_abort", ticket=ticket, timeout=5)
        except HTTPException:
            pass
        finally:
            TRANSFERS.pop(destination, None)


@router.get("")
async def list_mirrors(aid: int, pid: str, request: Request):
    _owner(request, aid, pid)
    return {"items": [_item(row) for row in _rows(aid, pid)]}


@router.post("", status_code=201)
async def create(aid: int, pid: str, request: Request):
    _owner(request, aid, pid)
    body = await _body(request, Destination)
    if body.destination == aid or "/" in body.name or body.name.startswith("."):
        raise code._refuse(422, "invalid", "выберите другой компьютер и имя папки проекта")
    metadata_only = _can_discover(body.destination)
    flags = {"metadata_only": True} if metadata_only else {}
    target = await _ask(request, body.destination, "mirror_destination", root=body.root, name=body.name, **flags)
    folder, project = target.get("folder"), target.get("pid")
    if (not isinstance(folder, str) or not 1 <= len(folder) <= 4096
            or not isinstance(project, str) or not code.PID_RE.fullmatch(project)):
        raise code._refuse(502, "bridge", "мост вернул неверное назначение зеркала")
    exists = target.get("exists") is True if metadata_only else target.get("entries") is not None
    if body.existing and not exists:
        raise code._refuse(404, "not_found", "найденная папка больше недоступна — обновите поиск")
    _private_destination(body.destination, project)
    await _ask(request, aid, "mirror_read", project=pid, reads=[])
    try:
        mid = state.DB.execute(
            "INSERT INTO project_mirrors(source, project, destination, root, name_key, location) VALUES(?,?,?,?,?,?)",
            (aid, pid, body.destination, body.root, security.digest(body.name.casefold()),
             security.encrypt(_json({"name": body.name, "folder": folder})))).lastrowid
    except sqlite3.IntegrityError:
        raise code._refuse(409, "exists", "эта папка уже назначена зеркалом") from None
    return _item(_rows(aid, pid, mid).fetchone())


@router.get("/{mid}")
async def get(aid: int, pid: str, mid: int, request: Request):
    return _item(_row(request, aid, pid, mid))


@router.post("/{mid}/compare")
async def compare(aid: int, pid: str, mid: int, request: Request):
    row = _row(request, aid, pid, mid)
    result, _, source, current, updates, remove, *_ = await _compare(request, row)
    if row["baseline"] is None and current is not None and not updates and not remove:
        sealed = await asyncio.to_thread(_next_baseline, source, current, None, {}, [])
        _row(request, aid, pid, mid)
        state.DB.execute("UPDATE project_mirrors SET baseline=? WHERE id=? AND baseline IS NULL", (sealed, mid))
    return result


@router.post("/{mid}/diff")
async def file_diff(aid: int, pid: str, mid: int, request: Request):
    row = _row(request, aid, pid, mid)
    body = await _body(request, FileDiff)
    path = code.check_path(body.path)

    async def text(agent, op, **args):
        try:
            result = await _ask(request, agent, op, path=path, **args)
        except HTTPException as error:
            if error.status_code == 404 and not error.detail.get("computer"):
                return None
            raise
        value = result.get("text")
        if not isinstance(value, str) or len(value) > 128 * 1024:
            raise code._refuse(413, "too_large", "файл слишком велик для просмотра разницы")
        return value

    before, after = await asyncio.gather(
        text(row["destination"], "mirror_text", root=row["root"], name=_location(row)["name"]),
        text(aid, "read", project=pid))
    if before is None and after is None:
        raise code._refuse(404, "not_found", "файл больше не существует")
    return {"before": before or "", "after": after or ""}


@router.post("/{mid}/sync", status_code=202)
async def sync(aid: int, pid: str, mid: int, request: Request):
    _row(request, aid, pid, mid)
    selected = await _body(request, Sync, 4096 + 8 * MAX_ENTRIES)
    row = _row(request, aid, pid, mid)
    destination = row["destination"]
    if destination in TRANSFERS or len(TRANSFERS) >= 4:
        raise code._refuse(409, "busy", "уже выполняется передача зеркала — дождитесь окончания")
    progress = {"state": "running"}
    if len(RESULTS) >= 128:
        for key, value in RESULTS.copy().items():
            if value["state"] != "running":
                del RESULTS[key]
                break
    RESULTS[mid] = progress
    TRANSFERS[destination] = asyncio.create_task(_sync(request, row, selected, progress))
    return progress


@router.delete("/{mid}", status_code=204)
async def unlink(aid: int, pid: str, mid: int, request: Request):
    row = _row(request, aid, pid, mid)
    if row["destination"] in TRANSFERS:
        raise code._refuse(409, "busy", "дождитесь окончания передачи зеркала")
    state.DB.execute("DELETE FROM project_mirrors WHERE id=?", (mid,))
    RESULTS.pop(mid, None)
    return Response(status_code=204)


async def stop():
    tasks = list(TRANSFERS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    TRANSFERS.clear()
    RESULTS.clear()
