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
import asyncio
import json
import logging
import re
import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

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
ANSWER_MAX = 2000                # знаков в словах отказа и в каждом вопросе и ответе
ANSWERS_MAX = 20                 # пар «вопрос → ответ»
NO_TEXT = "задание пустое или длиннее 100 000 знаков"
NO_MODEL = "такой модели нет на компьютере"
NO_EFFORT = "такого усилия нет на компьютере"


class Open(BaseModel):
    pin: str | None = Field(None, max_length=PIN_MAX)


class ChatState(BaseModel):
    state: Literal["open", "closed"]


class Turn(BaseModel):
    text: str                    # пусто и длина — своим 422 `text` (слова для человека), не общим FastAPI
    model: str | None = None
    effort: str | None = None


class Answer(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    decision: Literal["allow", "always", "deny"]
    message: str | None = Field(None, max_length=ANSWER_MAX)
    answers: dict[str, str] | None = None

    @field_validator("answers")
    @classmethod
    def _answers(cls, value):
        if value is not None and (len(value) > ANSWERS_MAX
                                  or any(len(k) > ANSWER_MAX or len(v) > ANSWER_MAX for k, v in value.items())):
            raise ValueError("answers")
        return value

    @model_validator(mode="after")
    def _message_only_with_deny(self):
        if self.message is not None and self.decision != "deny":
            raise ValueError("message")
        return self


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


# ---- ход: поток, наблюдение, карточки, остановка ------------------------------------------

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _events(q: asyncio.Queue, first: tuple[str, dict] | None, live: bool):
    """События SSE из очереди наблюдателя: `event: <k>`, данные — событие без `k`; без событий `PING_SEC` —
    `: ping`. Конец — `done` или `offline` (канал пропал: ход на мосте, может, и жив, но вестей не будет).
    Поток `live` до первого `snapshot` пропускает прочие события: мост шлёт снимок после них, они в нём есть."""
    if first is not None:
        yield _sse(*first)
    waiting = live
    while True:
        try:
            kind, e = await asyncio.wait_for(q.get(), PING_SEC)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        if kind == "offline":
            yield _sse("offline", {})
            return
        k = e["k"]
        if waiting and k not in ("snapshot", "done"):
            continue
        waiting = False
        yield _sse(k, {name: value for name, value in e.items() if name != "k"})
        if k == "done":
            return


class _Watch(StreamingResponse):
    """Поток SSE чата для одного наблюдателя. Очередь снимает сам ответ, а не finally генератора: отмена может
    застать ответ в `send` или ещё до первого куска (тела генератора не было вовсе) — как `chat._EventStream`."""

    def __init__(self, aid: int, chat: str, q: asyncio.Queue, first: tuple[str, dict] | None = None,
                 live: bool = False):
        super().__init__(_events(q, first, live), media_type="text/event-stream", headers=SSE_HEADERS)
        self.aid, self.chat, self.q = aid, chat, q

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            bridge.unwatch(self.aid, self.chat, self.q)
            await self.body_iterator.aclose()


async def _idle():
    yield _sse("idle", {})


async def _watched(request: Request, aid: int, chat: str, op: str, **args) -> tuple[asyncio.Queue, dict]:
    """Очередь наблюдателя — до просьбы: события хода идут сразу за ответом моста и не должны пройти мимо.
    Отказ — очередь прочь."""
    q = bridge.watch(aid, chat)
    try:
        return q, await ask(request, aid, op, chat=chat, **args)
    except BaseException:
        bridge.unwatch(aid, chat, q)
        raise


@router.post("/agents/{aid}/projects/{pid}/chats/{chat}/turn")
async def turn(aid: int, pid: str, chat: str, body: Turn, request: Request):
    """Ход: просьба `turn` мосту, затем его события потоком SSE до `done` или `offline`. Модель и усилие
    сверяются со знакомством моста; канала нет — их не сверить, и `ask` скажет `offline`."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    if not body.text.strip() or len(body.text) > TEXT_MAX:
        raise _refuse(422, "text", NO_TEXT)
    link = bridge.LINKS.get(aid)
    if link is not None:
        if body.model is not None and body.model not in {m["id"] for m in link.hello.get("models", ())}:
            raise _refuse(422, "model", NO_MODEL)
        if body.effort is not None and body.effort not in link.hello.get("efforts", ()):
            raise _refuse(422, "effort", NO_EFFORT)
    q, data = await _watched(request, aid, chat, "turn", project=pid, text=body.text, model=body.model,
                             effort=body.effort)
    log.info("пользователь %d: компьютер %d — ход", request.state.user.id, aid)
    turn_id = data.get("turn")
    return _Watch(aid, chat, q, first=("turn", {"turn": turn_id if isinstance(turn_id, str) else None}))


@router.get("/agents/{aid}/projects/{pid}/chats/{chat}/live")
async def live(aid: int, pid: str, chat: str, request: Request):
    """Вернуться к чату: хода нет — одно `idle`; идёт — снимок (мост шлёт его за ответом на `watch`), дальше
    события. Студия хода не помнит: всё состояние — у моста."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    q, data = await _watched(request, aid, chat, "watch", project=pid)
    if data.get("running") is not True:
        bridge.unwatch(aid, chat, q)
        return StreamingResponse(_idle(), media_type="text/event-stream", headers=SSE_HEADERS)
    return _Watch(aid, chat, q, live=True)


@router.post("/agents/{aid}/projects/{pid}/chats/{chat}/answer", status_code=204)
async def answer(aid: int, pid: str, chat: str, body: Answer, request: Request):
    """Ответ на карточку — мосту как есть: о самой карточке студия ничего не помнит (перезапуск, другая
    вкладка), её нет у моста — 410 `gone`. Закрытие карточки (`asked`) мост шлёт всем наблюдателям."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    await ask(request, aid, "answer", project=pid, chat=chat, request_id=body.request_id, decision=body.decision,
              message=body.message, answers=body.answers)
    return Response(status_code=204)


@router.post("/agents/{aid}/projects/{pid}/chats/{chat}/stop", status_code=204)
async def stop(aid: int, pid: str, chat: str, request: Request):
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    await ask(request, aid, "stop", project=pid, chat=chat)
    return Response(status_code=204)
