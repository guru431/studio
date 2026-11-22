"""Зеркала проекта — группа равноправных копий на мостах владельца (`mirror_groups`). Маршруты — от открытого
проекта (текущая копия): список остальных копий, сверка и отправка в выбранную. Содержимое файлов проходит только
по каналу."""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from typing import Annotated, NamedTuple

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

import auth
import bridge
import claude_code as code
import clock
import mirror_groups
import project_access
import state

router = APIRouter(prefix="/api/code/agents/{aid}/projects/{pid}/mirrors", default_response_class=code._BridgeJSON)
log = logging.getLogger("studio")
CHUNK = 4 * 1024 * 1024
MAX_ENTRIES = 150_000
DISCOVERY_CAP = "project_mirror_discovery"
EXCLUDE_CAP = "mirror_exclude"
GROUPS_CAP = "mirror_groups"
EXCLUDE_MAX = mirror_groups.EXCLUDE_MAX
SKIP = ["skip", "", 0, 0]
SKIP_REASONS = ("sqlite", "sqlite-journal")  # причина пропуска из моста (`hello.mirror_groups`)
DIR = ["dir", "", 0, 0]
SHA256 = re.compile(r"[0-9a-f]{64}")
# Как `files.DEVICE` и `files.BAD_CHARS` моста (без `\`: его протокол не пропускает ни на одной ОС).
WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]) *(?:\..*)?", re.IGNORECASE)
WINDOWS_BAD = set(':*?"<>|')
EXTERNAL_GIT = "Папка использует внешний каталог Git. Выберите основной репозиторий или отдельный клон."
TRANSFERS = {}  # id компьютера назначения → задача передачи
RESULTS = {}  # (id текущей копии, id другой копии) → ход передачи
# Токен плана → пути с локальным изменением на момент сравнения (последние `PLANS_MAX`). Отправка во все копии идёт
# по очереди: передача в одну перезаписывает состояние текущей копии в той паре, и правило 3 для следующей копии
# теряет то, что источник держал. Совпавший токен значит, что оба дерева и пара с тех пор не менялись, — решение
# сравнения в силе.
PLANS = {}
PLANS_MAX = 64
REENTER = "настройка зеркала недоступна после смены ключа — отвяжите зеркало и привяжите заново"
CHANGED = "настройки зеркала изменились — повторите"
BAD_PROJECT = "мост вернул неверные сведения о проекте"
BAD_DESTINATION = "мост вернул неверное назначение зеркала"
NOT_FOUND = "зеркало проекта не найдено"
BUSY = "дождитесь окончания передачи зеркала"


class Destination(BaseModel):
    destination: int = Field(gt=0)
    root: str = Field(pattern=r"^[0-9a-f]{16}$")
    name: str = Field(min_length=1, max_length=120)
    existing: bool = False
    #: папка уже в другой группе копий — текущая копия присоединяется к ней (или группы сливаются)
    join: bool = False
    #: в группе папки уже есть прежняя копия этого компьютера (проект переименован) — снять её
    replace: bool = False


class Sync(BaseModel):
    plan: str = Field(pattern=r"^[0-9a-f]{64}$")
    overwrite: bool = False
    keep: list[Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=list, max_length=MAX_ENTRIES)


class Exclusions(BaseModel):
    exclude: list[str] = Field(max_length=EXCLUDE_MAX)


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


def _owner(request, aid, pid, background=False):
    project_access.owner(request, aid)
    code.check_pid(pid)
    if not code._session_open(request.state.digest, background):
        raise code._refuse(409, "closed", "Сеанс Студии закрыт")


def _copy(cid):
    """Копия с именем её компьютера."""
    return state.DB.execute("SELECT c.*, a.name AS computer FROM mirror_copies c JOIN agents a ON a.id=c.agent"
                            " WHERE c.id=?", (cid,)).fetchone()


def _at(agent, root, name, project=None):
    """Копия папки `root`/`name` (или проекта `project`) компьютера `agent`."""
    return state.DB.execute(
        "SELECT * FROM mirror_copies WHERE agent=? AND (root=? AND name_key=? OR project=?) ORDER BY id LIMIT 1",
        (agent, root, mirror_groups.name_key(name), project)).fetchone()


def _computers(rows, without):
    """Имена компьютеров копий без компьютера `without`, по порядку копий, без повторов."""
    return list(dict.fromkeys(row["computer"] for row in rows if row["agent"] != without))


def _item(mine, row):
    """Другая копия группы с точки зрения текущей `mine`. Адрес не расшифровать (сменился `STUDIO_DATA_KEY`) — без
    имени и папки, с `reenter`: список грузится, и копию можно отвязать; адрес ещё неизвестен (копия-источник
    прежнего зеркала) — `unknown`."""
    aid = row["agent"]
    item = {"id": row["id"], "destination": aid, "computer": row["computer"], "online": aid in bridge.LINKS,
            "name": None, "folder": None, "synced": mirror_groups.synced(mine["id"], row["id"]),
            "job": RESULTS.get((mine["id"], row["id"]))}
    try:
        location = mirror_groups.location(row)
    except InvalidToken:
        return {**item, "reenter": True}
    return {**item, **location} if location is not None else {**item, "unknown": True}


def _listing(aid, pid):
    """Список без обращений к мостам (его берёт и цифра на кнопке). `pending` — у компьютера есть копии без `pid`
    или адреса, а текущая не найдена или без адреса: `locate` может их дописать."""
    mine = mirror_groups.find(aid, pid)
    if mine is None:
        return {"copy": None, "items": [], "exclude": [], "pending": bool(mirror_groups.unlocated(aid))}
    result = {"copy": mine["id"], "pending": mine["root"] is None,
              "items": [_item(mine, row) for row in mirror_groups.members(mine["grp"]) if row["id"] != mine["id"]]}
    try:
        result["exclude"] = mirror_groups.exclusions(mine["grp"])
    except InvalidToken:
        result.update(exclude=[], exclude_reenter=True)
    return result


def _gone():
    """Копии (или группы) с этим id больше нет — отвязана или слита с другой строкой той же папки. Окно узнаёт
    отказ по признаку `gone`, а не по словам (их переводит страница), и перечитывает список."""
    return code._refuse(404, "not_found", NOT_FOUND, gone=True)


def _pair(request, aid, pid, cid):
    """(текущая копия, другая копия `cid` той же группы); иначе 404."""
    _owner(request, aid, pid)
    mine, other = mirror_groups.find(aid, pid), _copy(cid)
    if mine is None or other is None or other["grp"] != mine["grp"] or other["id"] == mine["id"]:
        raise _gone()
    project_access.owner(request, other["agent"])
    return mine, other


def _where(row):
    """`{name, folder}` копии для просьб мосту; сменился ключ — 409 `reenter`."""
    try:
        return mirror_groups.location(row)
    except InvalidToken:
        raise code._refuse(409, "reenter", REENTER) from None


def _exclude(grp, agents):
    """Постоянные исключения группы: оба моста пары обязаны их понимать — иначе прежний мост назначения увидел бы
    исключённое как лишнее и удалил бы его в копии."""
    try:
        exclude = mirror_groups.exclusions(grp)
    except InvalidToken:
        raise code._refuse(409, "reenter", REENTER) from None
    if exclude:
        for aid in agents:
            code.require_capability(aid, EXCLUDE_CAP)
        return {"exclude": exclude}
    return {}


def _groups(aid):
    link = bridge.LINKS.get(aid)
    return bool(link and link.hello.get(GROUPS_CAP) is True)


def _reasons(aid):
    """Причины пропуска просим только у мостов с `hello.mirror_groups`: прежний мост поля не знает."""
    return {"reasons": True} if _groups(aid) else {}


def _forget():
    """Итоги передач копий, которых больше нет (отвязаны, слиты, сняты заменой)."""
    for key in list(RESULTS):
        if any(mirror_groups.get(cid) is None for cid in key):
            del RESULTS[key]


def _identity(data):
    """`root`, `name`, `folder` копии из `mirror_identity`; проект не прямо в папке проектов — None."""
    root, name, folder = (data.get(key) for key in ("root", "name", "folder"))
    if not isinstance(name, str) or not 1 <= len(name) <= 120:
        raise code._refuse(502, "bridge", BAD_PROJECT)
    if root is None:
        return None
    if (not isinstance(root, str) or not code.PID_RE.fullmatch(root)
            or not isinstance(folder, str) or not 1 <= len(folder) <= 4096):
        raise code._refuse(502, "bridge", BAD_PROJECT)
    return {"root": root, "name": name, "folder": folder}


def _pid(target):
    project = target.get("pid")
    if not isinstance(project, str) or not code.PID_RE.fullmatch(project):
        raise code._refuse(502, "bridge", BAD_DESTINATION)
    return project


async def _ask(request, aid, op, *, timeout=180, background=False, **args):
    # В обоих направлениях доступ должен оставаться доступом владельца, включая каждый блок данных.
    # `background` — передача (`_sync`): её разрешение проверено при запуске незапертой сессией, блокировка экрана
    # по таймеру её не обрывает; выход, гашение сессии, отключение учётки и отзыв компьютера — обрывают.
    project_access.owner(request, aid)
    if not code._session_open(request.state.digest, background):
        raise code._refuse(409, "closed", "Сеанс Студии закрыт")
    code.require_capability(aid, "project_mirror")
    return await code.ask(request, aid, op, quiet=True, reply_timeout=timeout, while_locked=background, **args)


def _entries(data, *, missing=False, reasons=None):
    """Список файлов моста. Пропуск с причиной из `SKIP_REASONS` становится обычным `SKIP`, причина — в `reasons[путь]`."""
    entries = data.get("entries")
    if entries is None and missing and "entries" in data:
        return None
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise code._refuse(502, "bridge", "мост вернул неверный список файлов")
    for path, info in list(entries.items()):
        if not path or path.startswith("/") or len(path) > code.PATH_MAX or not isinstance(info, list) or len(info) != 4:
            raise code._refuse(502, "bridge", "мост вернул неверный список файлов")
        kind, value, size, mode = info
        if kind == "skip" and value in SKIP_REASONS and size == 0 and mode == 0:
            if reasons is not None:
                reasons[path] = value
            entries[path] = info = SKIP
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


def _plan(source, destination, baseline, *, windows=False, held=None):
    """`held` — `{путь: {(вид, значение, размер)}}`: что источник уже держал на момент своих синхронизаций
    (`mirror_groups.held`). Запись копии из этого набора — не локальное изменение (правило 3)."""
    held = held or {}
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
        conflict = old != previous.get(path) and not (
            old is not None and old[0] != "skip" and tuple(old[:3]) in held.get(path, ()))
        conflicts += int(conflict)
        text = all(entry is None or entry[0] == "file" and entry[2] <= 128 * 1024 for entry in (old, new))
        keepable = old is not None and old[0] == "file"
        change = [path, "removed" if new is None else "added" if old is None else "changed", conflict,
                  text, keepable, deletion]
        if windows and (key := path.casefold()) != path:
            change.append(key)
        changes.append(change)
    return updates, remove, changes, conflicts, skipped


def _windows_name(path):
    """Путь, который Windows может создать: без `: * ? " < > |`, имён устройств и точки или пробела в конце части."""
    return not WINDOWS_BAD.intersection(path) and not any(
        part[-1:] in (".", " ") or WINDOWS_DEVICE.fullmatch(part) for part in path.split("/"))


def _portable(entries, platform, source_platform):
    if platform == "win32":
        # Windows без режима разработчика не создаёт символических ссылок (`node_modules/.bin`, `.venv/bin`), а
        # имён вроде `aux.c` или `a:b` не создаёт вовсе: такие пути — «пропущен», а не откат всей синхронизации.
        for path, item in entries.items():
            if item[0] == "link" or not _windows_name(path):
                entries[path] = SKIP
        folded = set()
        for path, item in entries.items():
            if item == SKIP:
                continue
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
        # `local` — признак для окна («Отправить во все копии» помечает такую копию), слова переводит страница.
        raise code._refuse(409, "conflict", "в зеркале есть локальные изменения", local=True)
    if keep:
        updates = {path: entry for path, entry in updates.items() if apply(path)}
        remove = [path for path in remove if apply(path)]
        if not updates and not remove:
            raise code._refuse(422, "invalid", "в плане не осталось изменений")
    return updates, remove


def _source_state(raw, previous, pending):
    """Состояние источника пары: его манифест до приведения к платформе, а для путей, не применённых в копии
    (`pending`), — прежняя запись пары, если она есть."""
    result = {path: entry for path, entry in raw.items() if path not in pending}
    result.update((path, previous[path]) for path in pending if previous and path in previous)
    return result


def _next_baseline(source, current, previous, updates, remove):
    current = current or {}
    confirmed = {path: entry for path, entry in (previous or {}).items() if path in source or path in current}
    confirmed.update((path, entry) for path, entry in current.items()
                     if entry != SKIP and _source_entry(source.get(path), entry) == entry)
    for path in remove:
        confirmed.pop(path, None)
    confirmed.update(updates)
    return confirmed


def _private_destination(aid, project):
    if state.DB.execute("SELECT 1 FROM project_members WHERE agent_id=? AND project=? LIMIT 1",
                        (aid, project)).fetchone() is not None:
        raise code._refuse(409, "conflict", "папка назначения открыта другим пользователям Студии")


def _can_discover(aid):
    link = bridge.LINKS.get(aid)
    return bool(link and link.hello.get("project_mirror") and link.hello.get(DISCOVERY_CAP))


def _candidates(data, aid, folders, projects, names, shared, own):
    """Найденные папки компьютера `aid`. `folders` — `(компьютер, root, name_key) → группа`, `projects` —
    `(компьютер, pid) → группа`, `names` — группа → её копии с именами компьютеров; `own` — группа текущей копии."""
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
        grp = folders.get((aid, root, mirror_groups.name_key(name))) or projects.get((aid, project))
        if grp is not None and grp == own or (aid, project) in shared:
            continue
        # Копия другой группы — с её компьютерами: привязка присоединит к ней (или сольёт группы).
        items.append({"root": root, "name": name, "folder": folder, "match": match,
                      **({"grouped": _computers(names[grp], aid)} if grp is not None else {})})
    return items


async def _fill(request, row, **ask):
    """Дописать неизвестное о копии по ответу её моста: копии-источнику прежнего зеркала — адрес
    (`mirror_identity`), копии-назначению — `pid` (`mirror_destination`). id оставшейся строки или None."""
    aid = row["agent"]
    if row["root"] is None:
        found = _identity(await _ask(request, aid, "mirror_identity", project=row["project"], **ask))
        if found is None:
            raise code._refuse(502, "bridge", BAD_PROJECT)
        update = found
    else:
        flags = {"metadata_only": True} if _can_discover(aid) else {}
        target = await _ask(request, aid, "mirror_destination", root=row["root"], name=_where(row)["name"],
                            **flags, **ask)
        update = {"project": _pid(target)}
    with mirror_groups.transaction():
        return mirror_groups.place(row["id"], **update) if mirror_groups.get(row["id"]) is not None else None


async def _locate(request, aid):
    """Копии компьютера без `pid` или адреса (перенос прежних зеркал) — по ответам его моста. Отказ по одной копии
    не прерывает остальные; адрес копии-источника умеет назвать только мост с `mirror_groups`."""
    groups = _groups(aid)
    for row in mirror_groups.unlocated(aid):
        if row["root"] is None and not groups:
            continue
        try:
            await _fill(request, row, passive=True, timeout=25)
        except HTTPException as error:
            log.info("зеркало: копия %d без адреса: %s", row["id"], error.detail.get("code"))


async def _address(request, other, background=False):
    """(копия, её `{name, folder}`) для записи; у копии-источника прежнего зеркала адреса ещё нет — спросить её мост."""
    if other["root"] is None:
        if other["agent"] not in bridge.LINKS:
            raise code._refuse(409, "offline", "компьютер не на связи")
        if not _groups(other["agent"]):
            raise code._refuse(409, "unsupported", "Обновите мост на компьютере копии")
        cid = await _fill(request, other, background=background, timeout=25)
        other = _copy(cid) if cid is not None else None
        if other is None:
            raise _gone()
    return other, _where(other)


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
    mine = mirror_groups.find(aid, pid)
    own = mine["grp"] if mine is not None else None
    copies = state.DB.execute(
        "SELECT c.grp, c.agent, c.project, c.root, c.name_key, a.name AS computer FROM mirror_copies c"
        " JOIN agents a ON a.id=c.agent WHERE a.user_id=? ORDER BY c.id", (uid,)).fetchall()
    # Компьютеры, где копия группы уже есть, не опрашиваются: вторую копию туда не добавить.
    grouped = {row["agent"] for row in copies if row["grp"] == own}
    agents = [agent for agent in state.DB.execute(
        "SELECT id, name, user_id, has_pin FROM agents WHERE user_id=? AND id<>? AND (? IS NULL OR id=?) ORDER BY id",
        (uid, aid, destination, destination)) if agent["id"] not in grouped]
    if not agents:
        return {"available": True, "devices": []}
    source = await _ask(request, aid, "mirror_identity", project=pid, passive=True, timeout=5)
    name, repositories = source.get("name"), source.get("repositories")
    if (not isinstance(name, str) or not 1 <= len(name) <= 120 or not isinstance(repositories, list)
            or len(repositories) > 16 or not all(isinstance(item, str) and SHA256.fullmatch(item) for item in repositories)):
        raise code._refuse(502, "bridge", BAD_PROJECT)
    folders = {(row["agent"], row["root"], row["name_key"]): row["grp"] for row in copies if row["root"] is not None}
    projects = {(row["agent"], row["project"]): row["grp"] for row in copies if row["project"] is not None}
    names = {}
    for row in copies:
        names.setdefault(row["grp"], []).append(row)
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
                data = await code.open_without_pin(request, agent, lambda: _ask(
                    request, target, "mirror_candidates", name=name, repositories=repositories,
                    passive=True, timeout=25))
            except HTTPException as error:
                reason = error.detail.get("code")
                stage = ("locked" if reason in ("closed", "pin_needed", "pin_locked")
                         else "offline" if reason == "offline" else "error")
                return {**result, "state": stage}
        return {**result, "state": "ready", "complete": data.get("complete") is True,
                "items": _candidates(data, target, folders, projects, names, shared, own)}

    devices = await asyncio.gather(*(inspect(agent) for agent in agents))
    _owner(request, aid, pid)
    return {"available": True, "devices": devices}


class _Comparison(NamedTuple):
    result: dict
    to: dict
    raw: dict  # манифест текущей копии до приведения к платформе назначения — состояние источника пары
    entries: dict
    current: dict | None
    updates: dict
    remove: list
    pair: mirror_groups.Pair
    exclude: dict
    copy: int  # id другой копии (после дописывания адреса строка могла смениться)
    platform: str | None
    source_platform: str | None


async def _compare(request, mine, other, background=False):
    """Сверка пары «текущая копия `mine` → другая копия `other`»."""
    aid, pid = mine["agent"], mine["project"]
    other, location = await _address(request, other, background)
    if other["grp"] != mine["grp"] or other["id"] == mine["id"]:
        raise _gone()
    destination = other["agent"]
    to = {"root": other["root"], "name": location["name"]}
    exclude = _exclude(mine["grp"], (aid, destination))
    pair = mirror_groups.pair(mine["id"], other["id"])
    held = mirror_groups.held(mine["id"])
    source, target = await asyncio.gather(
        _ask(request, aid, "mirror_scan", project=pid, background=background, **exclude, **_reasons(aid)),
        _ask(request, destination, "mirror_destination", background=background, **to, **exclude,
             **_reasons(destination)))
    project = _pid(target)
    _private_destination(destination, project)
    link, from_link = bridge.LINKS.get(destination), bridge.LINKS.get(aid)
    if link is None or from_link is None:
        raise code._refuse(409, "offline", "компьютер не на связи")
    platform = link.hello.get("platform")
    source_platform = from_link.hello.get("platform")

    def compare_trees():
        reasons = {}
        raw = _entries(source, reasons=reasons)
        entries = _portable(dict(raw), platform, source_platform)
        current = _entries(target, missing=True, reasons=reasons)
        updates, remove, changes, conflicts, skipped = _plan(entries, current, pair.state_y,
                                                             windows=platform == "win32", held=held)
        marker = pair.mark if pair.state_y is not None and (updates or remove) else None
        token = _digest([mine["id"], other["id"], pair.synced, marker, entries, current])
        result = {"plan": token, "exists": current is not None, "project": project,
                  "changes": changes, "conflicts": conflicts, "skipped": skipped, "deletions": len(remove),
                  "bytes": sum(info[2] for info in updates.values()), "reasons": reasons}
        return _Comparison(result, to, raw, entries, current, updates, remove, pair, exclude, other["id"],
                           platform, source_platform)

    comparison = await asyncio.to_thread(compare_trees)
    _owner(request, aid, pid, background)
    if other["project"] != project:
        with mirror_groups.transaction():
            if mirror_groups.get(other["id"]) is not None:
                mirror_groups.place(other["id"], project=project)
    return comparison


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


def _states(comparison, updates, remove):
    """Оба состояния пары после отправки. Пути плана, не применённые в копии (оставленные, пропущенные), сохраняют
    прежнее состояние источника: иначе обратная отправка молча заменила бы правку источника."""
    applied = set(updates) | set(remove)
    pending = {change[0] for change in comparison.result["changes"]} - applied
    return (_source_state(comparison.raw, comparison.pair.state_x, pending),
            _next_baseline(comparison.entries, comparison.current, comparison.pair.state_y, updates, remove))


async def _sync(request, mine, other, selected, progress):
    aid, pid, destination = mine["agent"], mine["project"], other["agent"]
    ticket = secrets.token_hex(16)
    started = False
    try:
        comparison = await _compare(request, mine, other, background=True)
        plan, to, exclude = comparison.result, comparison.to, comparison.exclude
        if plan["plan"] != selected.plan:
            raise code._refuse(409, "conflict", "проект или зеркало изменились — обновите сравнение")
        if (conflicted := PLANS.get(selected.plan)) is not None:
            plan = {**plan, "changes": [[path, kind, conflict and path in conflicted, *rest]
                                        for path, kind, conflict, *rest in plan["changes"]]}
        updates, remove = await asyncio.to_thread(_selection, plan, selected, comparison.updates, comparison.remove)
        progress.update(bytes=0, total=sum(info[2] for info in updates.values()))
        if updates or remove or comparison.current is None:
            started = True
            await _ask(request, destination, "mirror_begin", ticket=ticket, background=True, **to)
            for reads in _batches(updates):
                data = await _ask(request, aid, "mirror_read", project=pid, reads=reads, background=True)
                chunks = data.get("chunks")
                if (not isinstance(chunks, list) or len(chunks) != len(reads) or
                        any(not isinstance(chunk, list) or len(chunk) != 3 or chunk[:2] != read[:2]
                            or not isinstance(chunk[2], str) or len(chunk[2]) != ((read[2] + 2) // 3) * 4
                            for chunk, read in zip(chunks, reads))):
                    raise code._refuse(502, "bridge", "мост вернул неверный блок файла")
                await _ask(request, destination, "mirror_chunk", ticket=ticket, chunks=chunks, background=True)
                progress["bytes"] += sum(read[2] for read in reads)
            latest = await _ask(request, aid, "mirror_scan", project=pid, background=True, **exclude,
                                **_reasons(aid))
            fresh = await asyncio.to_thread(
                lambda: _portable(_entries(latest), comparison.platform, comparison.source_platform))
            if fresh != comparison.entries:
                raise code._refuse(409, "conflict", "исходный проект изменился во время передачи — обновите сравнение")
            expected = await asyncio.to_thread(_digest, comparison.current)
            _private_destination(destination, plan["project"])
            await _ask(request, destination, "mirror_apply", ticket=ticket, expected=expected,
                       updates=updates, remove=remove, background=True, **to, **exclude)
            started = False
        state_x, state_y = await asyncio.to_thread(_states, comparison, updates, remove)
        with mirror_groups.transaction():
            mirror_groups.save(mine["id"], comparison.copy, state_x, state_y, clock.NOW())
        progress["state"] = "done"
    except HTTPException as error:
        progress.update(state="failed", error=error.detail)
    except (InvalidToken, ValueError, OSError, sqlite3.Error) as error:
        log.warning("зеркало %d→%d: %s", mine["id"], other["id"], type(error).__name__)
        progress.update(state="failed", error={"code": "mirror", "message": "Не удалось синхронизировать зеркало"})
    finally:
        try:
            if started:
                await _ask(request, destination, "mirror_abort", ticket=ticket, timeout=5, background=True)
        except HTTPException:
            pass
        finally:
            TRANSFERS.pop(destination, None)


@router.get("")
async def list_mirrors(aid: int, pid: str, request: Request):
    _owner(request, aid, pid)
    return _listing(aid, pid)


@router.post("/locate")
async def locate(aid: int, pid: str, request: Request):
    """Дописать `pid` и адреса копий этого компьютера через его мост, затем — список."""
    _owner(request, aid, pid)
    if aid in bridge.LINKS:
        await _locate(request, aid)
        _owner(request, aid, pid)
    return _listing(aid, pid)


def _attach(uid, aid, pid, found, body, project, folder):
    """Решение добавления по свежему чтению (внутри `mirror_groups.transaction()`): H — копия выбранной папки,
    C — текущая копия. (id копии папки, id текущей копии, id снятой прежней копии или None)."""
    current = mirror_groups.find(aid, pid)
    if current is None and found is not None:
        current = _at(aid, found["root"], found["name"])  # та же папка — уже копия (перенос: без `pid`)
    if current is not None and found is not None:
        current = mirror_groups.get(mirror_groups.place(current["id"], project=pid, **found))
    if current is None and found is None:
        raise code._refuse(409, "conflict", CHANGED)
    held = _at(body.destination, body.root, body.name, project)
    if held is not None:
        held = mirror_groups.get(mirror_groups.place(held["id"], project=project, root=body.root, name=body.name,
                                                     folder=folder))
        if current is not None:  # слияние строк папки могло слить и группы
            current = mirror_groups.get(current["id"])
    if held is not None and current is not None and held["grp"] == current["grp"]:
        raise code._refuse(409, "exists", "эта папка уже зеркало этого проекта")
    if held is None:
        if current is not None and any(row["agent"] == body.destination
                                       for row in mirror_groups.members(current["grp"])):
            raise code._refuse(409, "exists", "на этом компьютере уже есть копия проекта")
        if current is None:
            current = mirror_groups.get(mirror_groups.add(mirror_groups.create_group(uid), aid, pid, **found))
        hid = mirror_groups.add(current["grp"], body.destination, project, body.root, body.name, folder)
        log.info("зеркало: копия %d в группе %d", hid, current["grp"])
        return hid, current["id"], None
    group = mirror_groups.members(held["grp"])
    if not body.join:
        raise code._refuse(409, "grouped", "папка уже входит в группу копий",
                           computers=_computers(group, held["agent"]))
    if current is None:
        former = next((row for row in group if row["agent"] == aid), None)
        if former is not None:
            if not body.replace:
                raise code._refuse(409, "exists", "в группе уже есть прежняя копия этого компьютера", former=True)
            if aid in TRANSFERS:
                raise code._refuse(409, "busy", BUSY)
            mirror_groups.remove(former["id"], prune=False)
        cid = mirror_groups.add(held["grp"], aid, pid, **found)
        log.info("зеркало: копия %d в группе %d", cid, held["grp"])
        return held["id"], cid, former["id"] if former is not None else None
    clash = mirror_groups.clashes(held["grp"], current["grp"])
    if clash:
        raise code._refuse(409, "conflict", "в обеих группах есть копии на одном компьютере — отвяжите лишнюю",
                           computers=clash)
    mirror_groups.merge(held["grp"], current["grp"])
    log.info("зеркало: группа %d слита с %d", current["grp"], held["grp"])
    return held["id"], current["id"], None


@router.post("", status_code=201)
async def create(aid: int, pid: str, request: Request):
    _owner(request, aid, pid)
    body = await _body(request, Destination)
    if body.destination == aid or "/" in body.name or body.name.startswith("."):
        raise code._refuse(422, "invalid", "выберите другой компьютер и имя папки проекта")
    current = mirror_groups.find(aid, pid)
    found = None
    if current is None or current["root"] is None:
        # Первое зеркало проекта: текущая копия становится адресуемой как назначение для остальных.
        code.require_capability(aid, GROUPS_CAP)
        found = _identity(await _ask(request, aid, "mirror_identity", project=pid, timeout=25))
        if found is None:
            raise code._refuse(502, "bridge", BAD_PROJECT)
    else:
        await _ask(request, aid, "mirror_read", project=pid, reads=[])
    metadata_only = _can_discover(body.destination)
    flags = {"metadata_only": True} if metadata_only else {}
    target = await _ask(request, body.destination, "mirror_destination", root=body.root, name=body.name, **flags)
    folder, project = target.get("folder"), _pid(target)
    if not isinstance(folder, str) or not 1 <= len(folder) <= 4096:
        raise code._refuse(502, "bridge", BAD_DESTINATION)
    exists = target.get("exists") is True if metadata_only else target.get("entries") is not None
    if body.existing and not exists:
        raise code._refuse(404, "not_found", "найденная папка больше недоступна — обновите поиск")
    _private_destination(body.destination, project)
    _owner(request, aid, pid)
    try:
        with mirror_groups.transaction():
            hid, cid, former = _attach(request.state.user.id, aid, pid, found, body, project, folder)
    except sqlite3.IntegrityError:
        raise code._refuse(409, "exists", "эта папка уже назначена зеркалом") from None
    if former is not None:
        _forget()
        log.info("зеркало: прежняя копия %d заменена %d", former, cid)
    return _item(mirror_groups.get(cid), _copy(hid))


@router.put("/exclude")
async def set_exclude(aid: int, pid: str, request: Request):
    """Точные относительные пути без шаблонов, общие для группы; мост не переносит их и не удаляет в копии. `.git`
    — всегда целиком. Нечитаемые прежние исключения (сменился ключ) перезаписываются."""
    _owner(request, aid, pid)
    mine = mirror_groups.find(aid, pid)
    if mine is None:
        raise _gone()
    body = await _body(request, Exclusions, 4096 + EXCLUDE_MAX * (code.PATH_MAX + 8))
    exclude = list(dict.fromkeys(code.check_path(path.strip("/")) for path in body.exclude if path.strip("/")))
    if any(path.split("/")[0] == ".git" for path in exclude):
        raise code._refuse(422, "invalid", "каталог .git переносится целиком")
    agents = list(dict.fromkeys(row["agent"] for row in mirror_groups.members(mine["grp"])))
    if any(agent in TRANSFERS for agent in agents):
        raise code._refuse(409, "busy", BUSY)
    if exclude:
        for agent in agents:
            code.require_capability(agent, EXCLUDE_CAP)
    with mirror_groups.transaction():
        if (mine := mirror_groups.find(aid, pid)) is not None:
            mirror_groups.set_exclusions(mine["grp"], exclude)
    return _listing(aid, pid)


@router.get("/{cid}")
async def get(aid: int, pid: str, cid: int, request: Request):
    mine, other = _pair(request, aid, pid, cid)
    return _item(mine, _copy(other["id"]))


@router.post("/{cid}/compare")
async def compare(aid: int, pid: str, cid: int, request: Request):
    mine, other = _pair(request, aid, pid, cid)
    comparison = await _compare(request, mine, other)
    PLANS.pop(comparison.result["plan"], None)
    PLANS[comparison.result["plan"]] = {change[0] for change in comparison.result["changes"] if change[2]}
    while len(PLANS) > PLANS_MAX:
        PLANS.pop(next(iter(PLANS)))
    if (comparison.pair.state_y is None and comparison.current is not None
            and not comparison.updates and not comparison.remove):
        # Первая сверка совпадающих копий фиксирует оба состояния пары.
        state_y = await asyncio.to_thread(_next_baseline, comparison.entries, comparison.current, None, {}, [])
        _owner(request, aid, pid)
        with mirror_groups.transaction():
            mirror_groups.seal(mine["id"], comparison.copy, comparison.raw, state_y)
    return comparison.result


@router.post("/{cid}/diff")
async def file_diff(aid: int, pid: str, cid: int, request: Request):
    mine, other = _pair(request, aid, pid, cid)
    body = await _body(request, FileDiff)
    path = code.check_path(body.path)
    other, location = await _address(request, other)

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
        text(other["agent"], "mirror_text", root=other["root"], name=location["name"]),
        text(aid, "read", project=pid))
    if before is None and after is None:
        raise code._refuse(404, "not_found", "файл больше не существует")
    return {"before": before or "", "after": after or ""}


@router.post("/{cid}/sync", status_code=202)
async def sync(aid: int, pid: str, cid: int, request: Request):
    _pair(request, aid, pid, cid)
    selected = await _body(request, Sync, 4096 + 8 * MAX_ENTRIES)
    mine, other = _pair(request, aid, pid, cid)
    destination = other["agent"]
    if destination in TRANSFERS or len(TRANSFERS) >= 4:
        raise code._refuse(409, "busy", "уже выполняется передача зеркала — дождитесь окончания")
    progress = {"state": "running"}
    if len(RESULTS) >= 128:
        for key, value in RESULTS.copy().items():
            if value["state"] != "running":
                del RESULTS[key]
                break
    RESULTS[(mine["id"], other["id"])] = progress
    TRANSFERS[destination] = asyncio.create_task(_sync(request, mine, other, selected, progress))
    return progress


@router.delete("/{cid}", status_code=204)
async def unlink(aid: int, pid: str, cid: int, request: Request):
    """Только настройка: папка копии остаётся. Осталась одна копия — группа удаляется с ней и с исключениями."""
    _, other = _pair(request, aid, pid, cid)
    if other["agent"] in TRANSFERS:
        raise code._refuse(409, "busy", BUSY)
    with mirror_groups.transaction():
        mirror_groups.remove(other["id"])
    _forget()
    log.info("зеркало: копия %d отвязана", other["id"])
    return Response(status_code=204)


async def stop():
    tasks = list(TRANSFERS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    TRANSFERS.clear()
    RESULTS.clear()
    PLANS.clear()
