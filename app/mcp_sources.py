"""Каталоги MCP-серверов: инструменты, правила сервера (`instructions`), подсказки (`prompts`) и пометки
инструментов, которые Studio читает сама.

Кэш — по (сервер, отпечаток ключа, пользователь): общий сервер видит учётку по `access.studio_user` и может
отдавать разным учёткам разные списки; личный — по id учётки. Неудача не запоминается; правка, удаление и
«Проверить» сервера сбрасывают его записи (`forget`). Правила доступа (группы) в кэш не попадают — их применяет
потребитель. В журнал — только id и числа.

Пометки — свои ключи `_meta` (стандартного поля в MCP нет): `studio/confirm` — вызов с подтверждением,
`studio/chat-files` — инструмент принимает файлы чата. Веб-инструмент — только при явном
`annotations.openWorldHint = true` (вопреки умолчанию спецификации MCP: неразмеченные серверы не попадают под
«Интернет»).
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import math
import re

from fastapi import HTTPException

import access
import clock
import mcp_client
import security
import state

log = logging.getLogger("studio")

CATALOG_TTL_SEC = 300                        # срок записи кэша
CACHE_KEEP = 512                             # записей кэша; лишние — старейшие
INSTRUCTIONS_MAX = 4000                      # правила одного сервера в системном сообщении (режет chat_system)
INSTRUCTIONS_TOTAL = 12000                   # правила всех серверов хода
LIST_SEC = 8                                 # срок опроса сервера для списка каталога
REFRESH_MIN_SEC = 10                         # «Обновить список» опрашивает сервер не чаще
DEFAULT_WAIT_SEC = 5                         # общий срок каталогов серверов по умолчанию на холодном кэше хода
ON = "on"                                    # ключ `chats.mcp_tools_off`: явно включённые инструменты {sid: [имена]}
CONFIRM_MESSAGE_MAX = 300
CONFIRM_WHEN_MAX = 16
ARG = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True)
class Catalog:
    at: float
    tools: list
    instructions: str | None
    prompts: list
    capabilities: dict
    trimmed: bool = False                    # сервер предлагает больше `mcp_client.MAX_TOOLS` — список урезан


CACHE: dict[tuple, Catalog] = {}


def _key(session) -> tuple:
    if session.owner_id is None and session.uid is not None:
        user = access.studio_user(session.uid, session.sid)
    else:
        user = session.uid
    return session.sid, stamp(session.stamp), user


def stamp(secret_enc) -> str:
    """Отпечаток шифра ключа сервера (Fernet — ASCII): правка ключа или адреса меняет ключ кэша."""
    return security.digest(secret_enc.decode("ascii") if isinstance(secret_enc, bytes) else secret_enc)


async def catalog(session, timeout, *, refresh=False) -> Catalog:
    """Каталог сервера из кэша или опросом в срок `timeout`. `refresh` — опросить заново, если запись старше
    REFRESH_MIN_SEC. Отказ (HTTPException) и истёкший срок (TimeoutError) списка инструментов уходят вызывающему
    и не кэшируются; подсказки — по возможности: их отказ или срок оставляют каталог без подсказок."""
    key = _key(session)
    found = CACHE.get(key)
    if found is not None:
        age = clock.NOW() - found.at
        if age < (REFRESH_MIN_SEC if refresh else CATALOG_TTL_SEC):
            return found
    async with asyncio.timeout(timeout) as limit:
        tools = await session.tools()
    prompts = []
    # Возможность объявляется объектом, часто пустым: `"prompts": {}`.
    if isinstance(session.capabilities.get("prompts"), dict):
        try:
            async with asyncio.timeout_at(limit.when()):
                prompts = await session.prompts()
        except (HTTPException, TimeoutError):
            log.info("mcp %d: подсказки не прочитаны", session.sid)
    item = Catalog(clock.NOW(), tools, session.instructions, prompts, session.capabilities, session.trimmed)
    CACHE.pop(key, None)
    CACHE[key] = item
    while len(CACHE) > CACHE_KEEP:
        del CACHE[next(iter(CACHE))]
    log.info("mcp %d: каталог, инструментов %d", session.sid, len(tools))
    return item


def forget(sid) -> None:
    for key in [key for key in CACHE if key[0] == sid]:
        del CACHE[key]


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _condition(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("arg"), str) or not ARG.fullmatch(raw["arg"]):
        return None
    if set(raw) == {"arg", "gt"} and _number(raw["gt"]):
        return {"arg": raw["arg"], "gt": raw["gt"]}
    if set(raw) == {"arg", "nonempty"} and raw["nonempty"] is True:
        return {"arg": raw["arg"], "nonempty": True}
    return None


def _confirm(raw):
    """`true` или `{"message", "when"}` → `{"message": str | None, "when": [...]}`; иначе None. Пустой `when` —
    подтверждение всегда; неверное условие тоже делает подтверждение безусловным (ошибка разметки не снимает его)."""
    if raw is True:
        return {"message": None, "when": []}
    if not isinstance(raw, dict):
        return None
    message = raw.get("message")
    message = message.strip()[:CONFIRM_MESSAGE_MAX] or None if isinstance(message, str) else None
    when = raw.get("when", [])
    conditions = [_condition(item) for item in when] if isinstance(when, list) and len(when) <= CONFIRM_WHEN_MAX else [None]
    return {"message": message, "when": [] if None in conditions else conditions}


def hints(tool) -> dict:
    """Пометки инструмента: `open_world` (только явное true), `confirm` (см. `_confirm`), `chat_files` —
    `max_bytes` из `_meta["studio/chat-files"]` или None."""
    annotations = tool.get("annotations")
    meta = tool.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    files = meta.get("studio/chat-files")
    limit = files.get("max_bytes") if isinstance(files, dict) else None
    return {"open_world": isinstance(annotations, dict) and annotations.get("openWorldHint") is True,
            "confirm": _confirm(meta.get("studio/confirm")),
            "chat_files": limit if type(limit) is int and limit > 0 else None}


def chat_files(found: Catalog) -> "int | None":
    """Предел файлов чата сервера: `max_bytes` пометки `studio/chat-files` у его `artifact_upload`; нет — None
    (сервер файлов диалога не получает)."""
    upload = next((tool for tool in found.tools if isinstance(tool, dict) and tool.get("name") == "artifact_upload"),
                  None)
    return hints(upload)["chat_files"] if upload is not None else None


def _exceeds(value, bound):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return False
    return _number(value) and value > bound


def _filled(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None and value is not False


def confirm_needed(hint, args) -> bool:
    """Нужно ли подтверждение вызова с аргументами `args`: `hint` — результат `hints`. Без условий — всегда,
    с условиями — если выполнено хотя бы одно."""
    confirm = hint.get("confirm")
    if confirm is None:
        return False
    if not confirm["when"]:
        return True
    args = args if isinstance(args, dict) else {}
    return any(_exceeds(args.get(item["arg"]), item["gt"]) if "gt" in item else _filled(args.get(item["arg"]))
               for item in confirm["when"])


# ---- MCP в диалоге: действующий набор, выключенные и явно включённые инструменты, политика админа ----

def dialog_servers(uid, row, conn) -> list:
    """id действующих серверов диалога `row` (строка `chats`) по порядку: явно включённые (`mcp_ids`, только
    ещё доступные — `mcp_client.present`), затем общие включённые и открытые учётке, кроме `mcp_off`, по id; не
    больше `mcp_client.MAX_SERVERS`. Личные — только явно. Прямое подключение, Cleaner или диалог без
    подключения — общие по умолчанию молча не действуют (явные проверяет 409 хода и PATCH)."""
    explicit = mcp_client.present(uid, row["mcp_ids"])
    if conn is None or not conn.proxied or conn.cleaner:
        return explicit
    off = set(json.loads(row["mcp_off"]))
    defaults = [item["id"] for item in mcp_client.servers(uid, enabled_only=True)
                if item["user_id"] is None and item["id"] not in off and item["id"] not in explicit]
    return (explicit + defaults)[:mcp_client.MAX_SERVERS]


def tool_state(raw) -> tuple[dict, dict]:
    """`chats.mcp_tools_off` → (выключенные {sid: set(имён)}, явно включённые {sid: set(имён)}). Явно
    включённые лежат под ключом ON того же JSON: инструмент «с подтверждением» входит в набор хода только так."""
    data = json.loads(raw)
    on = data.pop(ON, {})
    return ({int(sid): set(names) for sid, names in data.items()},
            {int(sid): set(names) for sid, names in on.items()})


def policy(sid) -> dict:
    """Политика администратора для инструментов общего сервера: {имя: строка `mcp_tool_policy`}."""
    return {r["tool"]: r for r in state.DB.execute(
        "SELECT tool, open_world, confirm FROM mcp_tool_policy WHERE server_id = ?", (sid,))}


def effective(tool, rule=None) -> dict:
    """Пометки инструмента для Studio: подсказки сервера (`hints`), поверх — политика администратора `rule`
    (строка `mcp_tool_policy` общего сервера): `open_world` 0/1 — вместо подсказки; `confirm` 'never' —
    без подтверждения, JSON — по нему (неверный — подтверждение всегда), NULL — как у сервера."""
    hint = hints(tool)
    if rule is None:
        return hint
    if rule["open_world"] is not None:
        hint["open_world"] = bool(rule["open_world"])
    if rule["confirm"] == "never":
        hint["confirm"] = None
    elif rule["confirm"] is not None:
        try:
            hint["confirm"] = _confirm(json.loads(rule["confirm"])) or {"message": None, "when": []}
        except ValueError:
            hint["confirm"] = {"message": None, "when": []}
    return hint
