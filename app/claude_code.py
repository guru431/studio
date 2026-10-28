"""Раздел «Claude Code»: `/api/code/*` поверх канала моста (спека 2026-10-28-studio-claude-code-design, 6.3,
6.5–6.7; просьбы и отказы — протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md, §2.2, §3–§5).

- Студия ретранслирует: каждый запрос владельца — просьба мосту (`bridge.call`) с признаком сессии
  `bridge.tag` (не дайджест и не токен); ответ моста — браузеру как есть (`data`), отказ — его кодом и словами
  со статусом по `STATUS`. Проекты, чаты, история, файлы на сервере не оседают: источник правды — машина.
- Открыть компьютер — `open` с PIN-ом: PIN проверяет мост, сервер его не хранит и в журнал не пишет; итог —
  в историю подключений (`open`, `pin_ok`, `pin_fail`). Мост исполняет просьбы только открытых сессий, иначе —
  отказ `closed`, у нас 409: 423 раздел не отдаёт никогда — его ловит экран блокировки страницы.
- Своё студия держит только в `code_chats`: какие чаты она завела и открыт ли чат (понятие студии, в Claude
  Code его нет). Список чатов — слияние сессий машины с этими отметками; записи на GET нет.
- Чужой компьютер, `pid` и `chat` не того вида — 404 и 422 до моста: ему не уходит ни одной просьбы.
- В строке запроса — только id (`agent_id`, `pid` — 16 hex, `chat` — UUID): её пишет журнал Apache. Имена
  проектов, PIN, тексты — только в теле. Журнал службы — «пользователь N: компьютер M — <op>: <код>» на
  отказ, без имён и текстов.
- Имя модуля — не `code.py`: каталог службы стоит в `sys.path` первым, и такой файл заслонил бы стандартный
  `code`, а его берёт `pdb` (pytest импортирует `pdb` при старте и падал бы).
"""
import logging
import re
import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import auth
import bridge
import clock
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/code")

PID_RE = re.compile(r"[0-9a-f]{16}")
STATUS = {"closed": 409, "pin": 403, "pin_locked": 403, "pin_needed": 409, "not_found": 404, "outside": 403,
          "too_large": 413, "binary": 415, "conflict": 409, "busy": 409, "running": 409, "idle": 409, "gone": 410,
          "not_git": 409, "invalid": 422}
PIN_MAX = 64
TEXT_MAX = 100_000
FILE_MAX = 2 * 1024 * 1024
FILE_BODY_MAX = 3 * 1024 * 1024
RAW_MAX = 10 * 1024 * 1024
PATH_MAX = 1024
HISTORY_PAGE = 100
PING_SEC = 15
PDF_TTL_SEC = 300


class Open(BaseModel):
    pin: str | None = Field(None, max_length=PIN_MAX)


class ChatState(BaseModel):
    state: Literal["open", "closed"]


def _refuse(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def own_agent(request: Request, aid: int) -> sqlite3.Row:
    row = state.DB.execute("SELECT id, name, has_pin FROM agents WHERE id = ? AND user_id = ?",
                           (aid, request.state.user.id)).fetchone()
    if row is None:
        raise _refuse(404, "not_found", "нет такого компьютера")
    return row


def check_pid(pid: str) -> str:
    if PID_RE.fullmatch(pid) is None:
        raise _refuse(422, "project", "неверный проект")
    return pid


def check_chat(chat: str) -> str:
    try:
        ok = str(uuid.UUID(chat)) == chat
    except ValueError:
        ok = False
    if not ok:
        raise _refuse(422, "chat", "неверный чат")
    return chat


async def ask(request: Request, aid: int, op: str, **args) -> dict:
    """Просьба мосту от сессии запроса; отказы — HTTPException с кодом моста или своим."""
    try:
        return await bridge.call(aid, op, bridge.tag(request.state.digest), **args)
    except bridge.Offline:
        error = _refuse(409, "offline", "компьютер не на связи")
    except TimeoutError:
        error = _refuse(504, "timeout", "компьютер не ответил")
    except bridge.BridgeError as e:
        status = STATUS.get(e.code)
        error = (_refuse(502, "bridge", "мост ответил непонятно") if status is None
                 else _refuse(status, e.code, e.message, **e.extra))
    log.warning("пользователь %d: компьютер %d — %s: %s", request.state.user.id, aid, op, error.detail["code"])
    raise error


def _event(request: Request, agent: sqlite3.Row, kind: str) -> None:
    bridge.record(request.state.user.id, agent["id"], agent["name"], kind, device=bridge.device(request),
                  ip=auth.client_ip(request))


@router.post("/agents/{aid}/open", status_code=204)
async def open_agent(aid: int, body: Open, request: Request):
    agent = own_agent(request, aid)
    try:
        await ask(request, aid, "open", pin=body.pin)
    except HTTPException as e:
        if e.detail["code"] in ("pin", "pin_locked"):
            _event(request, agent, "pin_fail")
        raise
    _event(request, agent, "open" if body.pin is None else "pin_ok")
    return Response(status_code=204)


@router.get("/agents/{aid}/projects")
async def projects(aid: int, request: Request):
    own_agent(request, aid)
    items = (await ask(request, aid, "projects")).get("items")
    return {"items": [{"pid": i["pid"], "name": i["name"]} for i in (items if isinstance(items, list) else [])
                      if isinstance(i, dict) and isinstance(i.get("pid"), str) and PID_RE.fullmatch(i["pid"])
                      and isinstance(i.get("name"), str)]}


def _number(value) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


@router.get("/agents/{aid}/projects/{pid}/chats")
async def chats(aid: int, pid: str, request: Request):
    """Сессии машины с отметками студии; строки `code_chats` проекта без сессии на машине (новый чат без
    хода) — тоже. Открытые, затем закрытые, внутри — новые сверху."""
    own_agent(request, aid)
    check_pid(pid)
    found = (await ask(request, aid, "chats", project=pid)).get("items")
    marks = {r["session_id"]: r for r in state.DB.execute(
        "SELECT project, session_id, state, created, updated FROM code_chats WHERE agent_id = ? AND user_id = ?",
        (aid, request.state.user.id))}
    items, seen = [], set()
    for i in (found if isinstance(found, list) else []):
        if not isinstance(i, dict) or not isinstance(i.get("chat"), str) or i["chat"] in seen:
            continue
        seen.add(i["chat"])
        mark = marks.get(i["chat"])
        items.append({"chat": i["chat"], "title": i.get("title"), "created": i.get("created"),
                      "modified": i.get("modified"), "origin": "studio" if i.get("tag") == "studio" else "other",
                      "state": mark["state"] if mark is not None else "open", "running": bool(i.get("running")),
                      "asking": bool(i.get("asking"))})
    for chat, mark in marks.items():
        if chat not in seen and mark["project"] == pid:
            items.append({"chat": chat, "title": None, "created": mark["created"], "modified": mark["updated"],
                          "origin": "studio", "state": mark["state"], "running": False, "asking": False})
    items.sort(key=lambda i: (i["state"] == "closed", -_number(i["modified"])))
    return {"items": items}


@router.post("/agents/{aid}/projects/{pid}/chats")
async def new_chat(aid: int, pid: str, request: Request):
    """Номер сессии заводит студия (SDK примет заданный `session_id`); мост не зовётся — строка без хода
    безвредна, даже если компьютер уже закрыт."""
    agent = own_agent(request, aid)
    check_pid(pid)
    chat, now = str(uuid.uuid4()), clock.NOW()
    state.DB.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                     " VALUES (?, ?, ?, ?, 'open', ?, ?)", (request.state.user.id, aid, pid, chat, now, now))
    _event(request, agent, "session_start")
    return JSONResponse({"chat": chat}, status_code=201)


@router.patch("/agents/{aid}/projects/{pid}/chats/{chat}")
async def set_chat_state(aid: int, pid: str, chat: str, body: ChatState, request: Request):
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    now = clock.NOW()
    state.DB.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT(agent_id, session_id) DO UPDATE SET state = excluded.state, updated = excluded.updated",
                     (request.state.user.id, aid, pid, chat, body.state, now, now))
    return {"state": body.state}


@router.get("/agents/{aid}/projects/{pid}/chats/{chat}")
async def history(aid: int, pid: str, chat: str, request: Request, before: int | None = None):
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    return await ask(request, aid, "history", project=pid, chat=chat, before=before, limit=HISTORY_PAGE)
