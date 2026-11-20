"""Группы доступа к общему (шаг схемы 56): общие API моделей, общие MCP и отдельные инструменты MCP.

- Правило — на общее подключение (`kind = 'connection'`) или сервер MCP (`kind = 'mcp'`, `tool = ''`), либо на
  инструмент MCP (`tool` — имя). Нет правила — «все пользователи»; правило с группами — их участникам; правило без
  групп (и правило, все группы которого удалены) — только администраторам.
- Инструмент: доступ к серверу И (нет правила инструмента ИЛИ участник одной из его групп).
- Администратор (`users.is_admin`) видит всё общее; `uid None` — управление общим из панели, без правил.
- Правила бывают только у общих строк (`set_rule` отказывает личным), а id подключений и серверов не повторяются:
  фильтр (`shared_filter`) проверяет только id и личных строк не касается.
- Состояние — только в базе, без кэша: правка администратора действует со следующего запроса.
- Журнал — только id и числа: имена групп и инструментов туда не пишутся.
"""
import hashlib
import hmac
import logging

from fastapi import HTTPException

import clock
import sections
import state

KINDS = {"connection": "connections", "mcp": "mcp_servers"}

#: Перед откатом на код схемы 55 (правил он не читает — закрытое стало бы общим для всех), служба остановлена:
#: общее подключение с правилом теряет ключ (прежний код отвечает «токен не расшифрован» и в фоновых путях, где
#: подключение взято из строки истории), общий MCP с правилом сервера или инструмента выключается. Прежние значения —
#: в `access_rollback`; повторный запуск их не затирает.
ROLLBACK_56 = """BEGIN;
INSERT OR IGNORE INTO access_rollback(kind, item_id, token_enc)
  SELECT 'connection', id, token_enc FROM connections WHERE user_id IS NULL
     AND EXISTS (SELECT 1 FROM access_rules r WHERE r.kind = 'connection' AND r.item_id = connections.id);
UPDATE connections SET token_enc = X'00'
 WHERE id IN (SELECT item_id FROM access_rollback WHERE kind = 'connection');
INSERT OR IGNORE INTO access_rollback(kind, item_id, enabled)
  SELECT 'mcp', id, enabled FROM mcp_servers WHERE user_id IS NULL
     AND EXISTS (SELECT 1 FROM access_rules r WHERE r.kind = 'mcp' AND r.item_id = mcp_servers.id);
UPDATE mcp_servers SET enabled = 0 WHERE id IN (SELECT item_id FROM access_rollback WHERE kind = 'mcp');
COMMIT;
"""

#: После возврата вперёд (служба остановлена): ключи и включённость из `access_rollback`. Ключ, введённый заново,
#: и сервер, включённый прежним кодом, не трогаются.
FORWARD_56 = """BEGIN;
UPDATE connections SET token_enc = (SELECT b.token_enc FROM access_rollback b
       WHERE b.kind = 'connection' AND b.item_id = connections.id)
 WHERE token_enc = X'00' AND id IN (SELECT item_id FROM access_rollback WHERE kind = 'connection');
UPDATE mcp_servers SET enabled = (SELECT b.enabled FROM access_rollback b
       WHERE b.kind = 'mcp' AND b.item_id = mcp_servers.id)
 WHERE enabled = 0 AND id IN (SELECT item_id FROM access_rollback WHERE kind = 'mcp');
DELETE FROM access_rollback;
COMMIT;
"""

log = logging.getLogger("studio")


def _bad(code: str, message: str, status: int = 422) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def is_admin(uid: int) -> bool:
    row = state.DB.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()
    return row is not None and bool(row["is_admin"])


#: правило строки открыто учётке: нет правила или учётка — участник одной из его групп
_OPEN = ("(NOT EXISTS (SELECT 1 FROM access_rules r WHERE r.kind = ? AND r.item_id = {item} AND r.tool = {tool})"
         " OR EXISTS (SELECT 1 FROM access_rules r JOIN access_rule_groups g ON g.rule_id = r.id"
         " JOIN access_group_members m ON m.group_id = g.group_id"
         " WHERE r.kind = ? AND r.item_id = {item} AND r.tool = {tool} AND m.user_id = ?))")


def _open(uid: int, kind: str, item_id: int, tool: str = "") -> bool:
    return bool(state.DB.execute("SELECT " + _OPEN.format(item="?", tool="?"),
                                 (kind, item_id, tool, kind, item_id, tool, uid)).fetchone()[0])


def allowed(uid: int | None, kind: str, item_id: int) -> bool:
    """Общее подключение или сервер MCP доступен учётке; личным строкам правил не бывает — True."""
    return uid is None or is_admin(uid) or _open(uid, kind, item_id)


def tool_allowed(uid: int | None, sid: int, tool: str) -> bool:
    """Инструмент сервера MCP: доступ к серверу и правило инструмента (без правила — как сервер)."""
    if uid is None or is_admin(uid):
        return True
    return _open(uid, "mcp", sid) and _open(uid, "mcp", sid, tool)


def shared_filter(uid: int | None, kind: str, column: str) -> tuple[str, tuple]:
    """SQL-условие для WHERE: строка с id `column` (выражение из кода, не из запроса) личная или общая с доступом
    учётки. Администратор и `uid None` — без ограничений."""
    if uid is None or is_admin(uid):
        return "1", ()
    return _OPEN.format(item=column, tool="''"), (kind, kind, uid)


def studio_user(uid: int, sid: int) -> str:
    """Постоянный непрозрачный идентификатор учётки для общего MCP `sid` (`X-Studio-User`): не логин и не id;
    у каждого сервера свой — разные внешние службы не сопоставят одного человека."""
    key = hashlib.sha256(state.CFG.data_key.encode()).digest()
    return hmac.new(key, f"studio-user:{sid}:{uid}".encode(), hashlib.sha256).hexdigest()[:32]


# ---- группы ------------------------------------------------------------------------------

def _group_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > 60 or any(ord(c) < 32 for c in clean):
        raise _bad("name", "имя группы — от 1 до 60 знаков")
    return clean


def _taken(name: str, gid: int | None = None) -> str:
    """Ключ уникальности имени (`access_groups.name_key`): casefold складывает и кириллицу, NOCASE — только латиницу."""
    key = name.casefold()
    if state.DB.execute("SELECT 1 FROM access_groups WHERE name_key = ? AND id IS NOT ?", (key, gid)).fetchone():
        raise _bad("taken", "группа с таким именем уже есть", 409)
    return key


def _group(gid: int):
    row = state.DB.execute("SELECT id, name FROM access_groups WHERE id = ?", (gid,)).fetchone()
    if row is None:
        raise _bad("gone", "такой группы нет", 404)
    return row


def _members(gid: int) -> list[int]:
    return [r[0] for r in state.DB.execute(
        "SELECT user_id FROM access_group_members WHERE group_id = ? ORDER BY user_id", (gid,))]


def groups() -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "members": _members(r["id"])}
            for r in state.DB.execute("SELECT id, name FROM access_groups ORDER BY name COLLATE NOCASE, id").fetchall()]


def user_groups(uid: int) -> list[int]:
    return [r[0] for r in state.DB.execute(
        "SELECT group_id FROM access_group_members WHERE user_id = ? ORDER BY group_id", (uid,))]


def create_group(name: str) -> dict:
    clean = _group_name(name)
    with sections.transaction():
        key = _taken(clean)
        gid = state.DB.execute("INSERT INTO access_groups(name, name_key, created) VALUES (?, ?, ?)",
                               (clean, key, clock.NOW())).lastrowid
    log.info("группа доступа %d создана", gid)
    return {"id": gid, "name": clean, "members": []}


def rename_group(gid: int, name: str) -> dict:
    clean = _group_name(name)
    with sections.transaction():
        _group(gid)
        key = _taken(clean, gid)
        state.DB.execute("UPDATE access_groups SET name = ?, name_key = ? WHERE id = ?", (clean, key, gid))
    log.info("группа доступа %d переименована", gid)
    return {"id": gid, "name": clean, "members": _members(gid)}


def delete_group(gid: int) -> None:
    """Правила с этой группой остаются — без неё; правило без групп — только администраторам."""
    with sections.transaction():
        _group(gid)
        state.DB.execute("DELETE FROM access_groups WHERE id = ?", (gid,))
    log.info("группа доступа %d удалена", gid)


def set_members(gid: int, user_ids: list[int]) -> dict:
    wanted = sorted(set(user_ids))
    with sections.transaction():
        _group(gid)
        if wanted:
            known = {r[0] for r in state.DB.execute(
                f"SELECT id FROM users WHERE id IN ({','.join('?' * len(wanted))})", wanted)}
            if known != set(wanted):
                raise _bad("user", "такой учётки нет")
        state.DB.execute("DELETE FROM access_group_members WHERE group_id = ?", (gid,))
        state.DB.executemany("INSERT INTO access_group_members(group_id, user_id) VALUES (?, ?)",
                             [(gid, uid) for uid in wanted])
    log.info("группа доступа %d: участников %d", gid, len(wanted))
    return {"id": gid, "name": _group(gid)["name"], "members": wanted}


# ---- правила -----------------------------------------------------------------------------

def _shared_item(kind: str, item_id: int) -> None:
    """Правило — только общей строке: личная или чужая — как отсутствующая."""
    if kind not in KINDS:
        raise ValueError(kind)
    if state.DB.execute(f"SELECT 1 FROM {KINDS[kind]} WHERE id = ? AND user_id IS NULL", (item_id,)).fetchone() is None:
        raise _bad("gone", "такого общего подключения нет", 404)


def _check_groups(wanted: list[int]) -> list[int]:
    clean = sorted(set(wanted))
    if clean:
        known = {r[0] for r in state.DB.execute(
            f"SELECT id FROM access_groups WHERE id IN ({','.join('?' * len(clean))})", clean)}
        if known != set(clean):
            raise _bad("group", "такой группы нет")
    return clean


def rule(kind: str, item_id: int, tool: str = "") -> list[int] | None:
    """Группы правила; None — правила нет («все пользователи»), [] — только администраторам."""
    row = state.DB.execute("SELECT id FROM access_rules WHERE kind = ? AND item_id = ? AND tool = ?",
                           (kind, item_id, tool)).fetchone()
    if row is None:
        return None
    return [r[0] for r in state.DB.execute(
        "SELECT group_id FROM access_rule_groups WHERE rule_id = ? ORDER BY group_id", (row[0],))]


def _write_rule(kind: str, item_id: int, groups: list[int] | None, tool: str) -> None:
    state.DB.execute("DELETE FROM access_rules WHERE kind = ? AND item_id = ? AND tool = ?", (kind, item_id, tool))
    if groups is None:
        return
    rid = state.DB.execute("INSERT INTO access_rules(kind, item_id, tool) VALUES (?, ?, ?)",
                           (kind, item_id, tool)).lastrowid
    state.DB.executemany("INSERT INTO access_rule_groups(rule_id, group_id) VALUES (?, ?)",
                         [(rid, gid) for gid in groups])


def set_rule(kind: str, item_id: int, groups: list[int] | None, tool: str = "") -> None:
    """None — снять правило («все пользователи»); список — только этим группам (пустой — администраторам)."""
    with sections.transaction():
        _shared_item(kind, item_id)
        clean = None if groups is None else _check_groups(groups)
        _write_rule(kind, item_id, clean, tool)
    log.info("доступ %s %d%s: %s", kind, item_id, " (инструмент)" if tool else "",
             "все" if groups is None else f"групп {len(clean)}")


def tool_rules(sid: int) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for r in state.DB.execute("SELECT id, tool FROM access_rules WHERE kind = 'mcp' AND item_id = ? AND tool != ''"
                              " ORDER BY tool", (sid,)).fetchall():
        out[r["tool"]] = [g[0] for g in state.DB.execute(
            "SELECT group_id FROM access_rule_groups WHERE rule_id = ? ORDER BY group_id", (r["id"],))]
    return out


def set_tool_rules(sid: int, rules: dict[str, list[int]]) -> None:
    """Полная замена правил инструментов сервера: имени нет в `rules` — правило снято."""
    with sections.transaction():
        _shared_item("mcp", sid)
        clean = {tool: _check_groups(groups) for tool, groups in rules.items()}
        state.DB.execute("DELETE FROM access_rules WHERE kind = 'mcp' AND item_id = ? AND tool != ''", (sid,))
        for tool, groups in clean.items():
            _write_rule("mcp", sid, groups, tool)
    log.info("доступ mcp %d: правил инструментов %d", sid, len(rules))
