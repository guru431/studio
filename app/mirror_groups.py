"""Зеркала проекта — группа равноправных копий (шаг схемы 63).

Группа — копии одного проекта у одного пользователя и их общие постоянные исключения (шифром). Копия — папка
компьютера: `project` — id проекта на нём, `root` + `name_key` + `location` (шифром `{name, folder}`) — адрес для
записи; неизвестное — NULL, дописывается `place`; `held` (шифром `{путь: [запись, …]}`) — накопленные записи копии на
её синхронизациях, последние `HELD_MAX` на путь: правило 3 «уже держал» не теряет их, когда передача переписывает
состояние копии в паре. Пара — состояния двух копий на их последнюю синхронизацию
(шифром) и её время; стороны хранятся по возрастанию id (`a < b`), наружу — с точки зрения запрошенной копии.

Функции, кроме `adopt`, без своих транзакций: записи вызывающий оборачивает в `transaction()`.
Блобы состояний при переносе пар не расшифровываются. В журнал — только числа.
"""
import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import NamedTuple

from cryptography.fernet import InvalidToken

import security
import state

log = logging.getLogger("studio")
EXCLUDE_MAX = 64
HELD_MAX = 8  # накопленное «держал» копии: последних различных записей на путь


class Pair(NamedTuple):
    state_x: dict | None
    state_y: dict | None
    synced: float | None
    mark: str | None


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


@contextmanager
def transaction():
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        yield
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise


def name_key(name: str) -> str:
    return security.digest(name.casefold())


def find(aid: int, pid: str) -> sqlite3.Row | None:
    return state.DB.execute("SELECT * FROM mirror_copies WHERE agent=? AND project=? ORDER BY id LIMIT 1",
                            (aid, pid)).fetchone()


def get(cid: int) -> sqlite3.Row | None:
    return state.DB.execute("SELECT * FROM mirror_copies WHERE id=?", (cid,)).fetchone()


def members(grp: int) -> list[sqlite3.Row]:
    return state.DB.execute("SELECT c.*, a.name AS computer FROM mirror_copies c JOIN agents a ON a.id=c.agent"
                            " WHERE c.grp=? ORDER BY c.id", (grp,)).fetchall()


def unlocated(aid: int) -> list[sqlite3.Row]:
    return state.DB.execute("SELECT * FROM mirror_copies WHERE agent=? AND (project IS NULL OR root IS NULL)"
                            " ORDER BY id", (aid,)).fetchall()


def location(row) -> dict | None:
    """`{name, folder}` копии; адрес неизвестен — None. Сменённый ключ — `InvalidToken`."""
    if row["root"] is None:
        return None
    data = json.loads(security.decrypt(row["location"]))
    return {"name": data.get("name"), "folder": data.get("folder")}


def create_group(user_id: int) -> int:
    return state.DB.execute("INSERT INTO mirror_groups(user_id) VALUES(?)", (user_id,)).lastrowid


def add(grp: int, agent: int, project: str | None, root: str | None, name: str | None, folder: str | None) -> int:
    located = root is not None
    return state.DB.execute(
        "INSERT INTO mirror_copies(grp, agent, project, root, name_key, location) VALUES(?,?,?,?,?,?)",
        (grp, agent, project, root, name_key(name) if located else None,
         security.encrypt(_json({"name": name, "folder": folder})) if located else None)).lastrowid


def exclusions(grp: int) -> list[str]:
    blob = state.DB.execute("SELECT exclude FROM mirror_groups WHERE id=?", (grp,)).fetchone()[0]
    return json.loads(security.decrypt(blob)) if blob is not None else []


def set_exclusions(grp: int, paths: list[str]) -> None:
    state.DB.execute("UPDATE mirror_groups SET exclude=? WHERE id=?",
                     (security.encrypt(_json(paths)) if paths else None, grp))


def clashes(g1: int, g2: int) -> list[str]:
    return [row[0] for row in state.DB.execute(
        "SELECT a.name FROM agents a WHERE EXISTS (SELECT 1 FROM mirror_copies WHERE grp=? AND agent=a.id)"
        " AND EXISTS (SELECT 1 FROM mirror_copies WHERE grp=? AND agent=a.id) ORDER BY a.id", (g1, g2))]


def merge(into: int, other: int) -> None:
    """Копии `other` — в `into`, исключения — объединением (нечитаемые уступают читаемым), `other` удаляется."""
    if into == other:
        return
    lists = []
    for grp in (into, other):
        try:
            lists.append(exclusions(grp))
        except InvalidToken:
            lists.append(None)
    if lists[0] is not None and lists[1] is not None:
        set_exclusions(into, list(dict.fromkeys(lists[0] + lists[1]))[:EXCLUDE_MAX])
    elif lists[0] is None and lists[1] is not None:
        set_exclusions(into, lists[1])
    state.DB.execute("UPDATE mirror_copies SET grp=? WHERE grp=?", (into, other))
    state.DB.execute("DELETE FROM mirror_groups WHERE id=?", (other,))


def remove(cid: int, *, prune: bool = True) -> None:
    """Удалить копию; при `prune` группа, где осталось меньше двух копий, удаляется (с оставшейся копией)."""
    row = get(cid)
    if row is None:
        return
    state.DB.execute("DELETE FROM mirror_copies WHERE id=?", (cid,))
    if prune and state.DB.execute("SELECT count(*) FROM mirror_copies WHERE grp=?", (row["grp"],)).fetchone()[0] < 2:
        state.DB.execute("DELETE FROM mirror_groups WHERE id=?", (row["grp"],))


def _row(x, y):
    a, b = sorted((x, y))
    return state.DB.execute("SELECT * FROM mirror_pairs WHERE a=? AND b=?", (a, b)).fetchone()


def _sides(row, x):
    """Блобы состояний (своё, чужое) с точки зрения копии `x`."""
    return (row["state_a"], row["state_b"]) if row["a"] == x else (row["state_b"], row["state_a"])


def _state(blob):
    if blob is None:
        return None
    try:
        return json.loads(security.decrypt(blob))
    except InvalidToken:
        return None


def pair(x: int, y: int) -> Pair:
    row = _row(x, y)
    if row is None:
        return Pair(None, None, None, None)
    mine, theirs = _sides(row, x)
    return Pair(_state(mine), _state(theirs), row["synced"],
                hashlib.sha256(theirs).hexdigest() if theirs is not None else None)


def synced(x: int, y: int) -> float | None:
    row = _row(x, y)
    return row["synced"] if row is not None else None


def _kept(cid) -> dict[str, list]:
    """Накопленное копии `{путь: [запись, …]}` (новые — в конце); нет копии или нечитаемо (сменился ключ) — пусто."""
    row = state.DB.execute("SELECT held FROM mirror_copies WHERE id=?", (cid,)).fetchone()
    return (_state(row[0]) or {}) if row is not None else {}


def _add(kept, path, entry):
    if entry[0] != "skip":
        versions = [item for item in kept.get(path, ()) if item != list(entry)]
        kept[path] = [*versions, list(entry)][-HELD_MAX:]


def _store(cid, kept):
    state.DB.execute("UPDATE mirror_copies SET held=? WHERE id=?", (_seal(kept) if kept else None, cid))


def _remember(cid, entries):
    """Записи состояния копии на этой синхронизации — в её накопленное."""
    if entries:
        kept = _kept(cid)
        for path, entry in entries.items():
            _add(kept, path, entry)
        _store(cid, kept)


def held(x: int) -> dict[str, set[tuple]]:
    """Записи `(вид, значение, размер, режим)` (кроме пропущенных), которые копия `x` держала на момент любой из своих
    синхронизаций: накопленное копии и её состояния в парах (пары перенесённых зеркал накопленного не имеют)."""
    result = {}
    for path, versions in _kept(x).items():
        result.setdefault(path, set()).update(tuple(entry) for entry in versions)
    for row in state.DB.execute("SELECT * FROM mirror_pairs WHERE a=? OR b=?", (x, x)):
        entries = _state(_sides(row, x)[0]) or {}
        for path, entry in entries.items():
            if entry[0] != "skip":
                result.setdefault(path, set()).add(tuple(entry))
    return result


def _put(x, y, blob_x, blob_y, when, *, only_new=False) -> bool:
    """Пара из блобов; копии `x` или `y` нет — ничего. `only_new` — только если у `y` в паре ещё нет состояния.
    Записано ли."""
    a, b, state_a, state_b = (x, y, blob_x, blob_y) if x < y else (y, x, blob_y, blob_x)
    if only_new:
        column = "state_b" if y == b else "state_a"
        update = f"state_a=excluded.state_a, state_b=excluded.state_b WHERE mirror_pairs.{column} IS NULL"
    else:
        update = "state_a=excluded.state_a, state_b=excluded.state_b, synced=excluded.synced"
    return state.DB.execute(
        "INSERT INTO mirror_pairs(a, b, state_a, state_b, synced) SELECT ?,?,?,?,?"
        " WHERE (SELECT count(*) FROM mirror_copies WHERE id IN (?, ?)) = 2"
        f" ON CONFLICT(a, b) DO UPDATE SET {update}", (a, b, state_a, state_b, when, a, b)).rowcount > 0


def _seal(value):
    return security.encrypt(_json(value)) if value is not None else None


def save(x: int, y: int, state_x: dict | None, state_y: dict | None, synced: float | None) -> None:
    """Синхронизация X → Y: оба состояния — в пару и в накопленное своей копии."""
    if _put(x, y, _seal(state_x), _seal(state_y), synced):
        _remember(x, state_x)
        _remember(y, state_y)


def seal(x: int, y: int, state_x: dict, state_y: dict) -> None:
    """Первая сверка совпадающих копий: записать, только если состояния `y` в паре ещё нет."""
    if _put(x, y, _seal(state_x), _seal(state_y), None, only_new=True):
        _remember(x, state_x)
        _remember(y, state_y)


def _later(new, old):
    return new is not None and (old is None or new > old)


def _merge_copies(keep, drop):
    """Две строки одной папки — в одну: группы сливаются, пары `drop` переходят к `keep` (из двух пар с одной
    третьей копией остаётся более поздняя), накопленное `drop` — в накопленное `keep`, строка `drop` удаляется,
    недостающие поля `keep` — из неё."""
    if keep["grp"] != drop["grp"]:
        merge(keep["grp"], drop["grp"])
    k, d = keep["id"], drop["id"]
    if (dropped := _kept(d)):
        kept = _kept(k)
        for path, versions in dropped.items():
            for entry in versions:
                _add(kept, path, entry)
        _store(k, kept)
    for row in state.DB.execute("SELECT * FROM mirror_pairs WHERE a=? OR b=?", (d, d)).fetchall():
        other = row["b"] if row["a"] == d else row["a"]
        if other == k:
            continue
        current = _row(k, other)
        if current is None or _later(row["synced"], current["synced"]):
            mine, theirs = _sides(row, d)
            _put(k, other, mine, theirs, row["synced"])
    state.DB.execute("DELETE FROM mirror_copies WHERE id=?", (d,))
    if keep["project"] is None and drop["project"] is not None:
        state.DB.execute("UPDATE mirror_copies SET project=? WHERE id=?", (drop["project"], k))
    if keep["root"] is None and drop["root"] is not None:
        state.DB.execute("UPDATE mirror_copies SET root=?, name_key=?, location=? WHERE id=?",
                         (drop["root"], drop["name_key"], drop["location"], k))


def place(cid: int, *, project: str | None = None, root: str | None = None, name: str | None = None,
          folder: str | None = None) -> int:
    """Дописать известное о копии. Та же папка уже есть другой строкой того же компьютера (тот же `project` или
    тот же адрес) — строки сливаются; возвращается id оставшейся."""
    row = get(cid)
    if row is None:
        raise LookupError(cid)
    key = name_key(name) if root is not None else None
    while True:
        duplicate = state.DB.execute(
            "SELECT * FROM mirror_copies WHERE agent=? AND id<>? AND (project=? OR root=? AND name_key=?)"
            " ORDER BY id LIMIT 1", (row["agent"], row["id"], project, root, key)).fetchone()
        if duplicate is None:
            break
        keep, drop = (row, duplicate) if row["root"] is not None and duplicate["root"] is None else (duplicate, row)
        _merge_copies(keep, drop)
        row = get(keep["id"])
    if project is not None:
        state.DB.execute("UPDATE mirror_copies SET project=? WHERE id=?", (project, row["id"]))
    if root is not None:
        state.DB.execute("UPDATE mirror_copies SET root=?, name_key=?, location=? WHERE id=?",
                         (root, key, security.encrypt(_json({"name": name, "folder": folder})), row["id"]))
    return row["id"]


def adopt() -> int:
    """Строки `project_mirrors`, ещё не перенесённые (прежний код, в том числе после отката), — в группы."""
    dropped = 0
    with transaction():
        rows = state.DB.execute(
            "SELECT m.*, a.user_id FROM project_mirrors m JOIN agents a ON a.id=m.source"
            " WHERE m.id NOT IN (SELECT id FROM mirror_legacy) ORDER BY m.id").fetchall()
        for row in rows:
            source = find(row["source"], row["project"])
            if source is None:
                grp = create_group(row["user_id"])
                sid = add(grp, row["source"], row["project"], None, None, None)
            else:
                grp, sid = source["grp"], source["id"]
            try:
                data = json.loads(security.decrypt(row["location"]))
                exclude = data.pop("exclude", None) or []
                blob = security.encrypt(_json(data))
            except InvalidToken:
                blob, exclude = row["location"], []
            if exclude:
                try:
                    joined = list(dict.fromkeys(exclusions(grp) + exclude))
                except InvalidToken:
                    joined = []
                dropped += max(0, len(joined) - EXCLUDE_MAX)
                if joined:
                    set_exclusions(grp, joined[:EXCLUDE_MAX])
            target = state.DB.execute("SELECT * FROM mirror_copies WHERE agent=? AND root=? AND name_key=?",
                                      (row["destination"], row["root"], row["name_key"])).fetchone()
            if target is None:
                did = state.DB.execute(
                    "INSERT INTO mirror_copies(grp, agent, root, name_key, location) VALUES(?,?,?,?,?)",
                    (grp, row["destination"], row["root"], row["name_key"], blob)).lastrowid
            else:
                merge(grp, target["grp"])
                did = target["id"]
            a, b = sorted((sid, did))
            state.DB.execute("INSERT OR IGNORE INTO mirror_pairs(a, b, state_a, state_b, synced) VALUES(?,?,?,?,?)",
                             (a, b, *((None, row["baseline"]) if a == sid else (row["baseline"], None)), row["synced"]))
            state.DB.execute("INSERT INTO mirror_legacy(id) VALUES(?)", (row["id"],))
    if rows:
        log.info("зеркала: перенесено строк %d, лишних исключений %d", len(rows), dropped)
    return len(rows)
