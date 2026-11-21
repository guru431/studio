"""Пауза и продолжение хода чата через сервер (спека 2026-11-21-studio-source-capabilities-design, п. 5 и 17).

1. Пауза (`pause`): вызов инструмента MCP «с подтверждением» (`mcp_sources.confirm_needed`) или встроенный вопрос
   `ask_user` (`chat_tools`). Состояние цикла — шифром `STUDIO_DATA_KEY` в `chat_pending.payload`: сообщения модели,
   вызов, круги, шаги, оставшиеся вызовы круга и вид карточки. Страница получает событие SSE `pending`, ход кончается
   статусом `done` — текст до паузы сохранён; у сообщения в выдаче — `pending` (`open_views`).
2. Ответ (`POST /api/chats/{chat_id}/pending/{pending_id}`): одна смена `resolved` (`WHERE resolved IS NULL`) — второй
   ответ и ответ позже PENDING_TTL_SEC получают 409 `pending_gone`; подключение диалога заново — сменилось — 409
   `connection_changed`. То же сообщение снова `streaming`, `chat.StreamTask` с `resume` продолжает цикл, ответ — поток
   SSE, как у хода. Доступ, политика и «Интернет» перепроверяют каталог хода и `mcp_client.Tools.call`.
3. Новый ход диалога снимает открытую паузу (`supersede`, исход `superseded`).
4. В журнал — только id и вид паузы: ни вопроса, ни аргументов, ни ответа."""
import asyncio
import json
import logging
import uuid
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import chat
import chat_tools
import clock
import connections
import manage
import mcp_client
import mcp_sources
import operations
import profiles
import security
import sections
import state
import upstream
import web

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")

PENDING_TTL_SEC = 86400
ARGS_MAX = 8                       # строк аргументов в карточке подтверждения
ARG_CHARS = 200                    # знаков ключа и значения
ANSWER_MAX = 4000
REFUSED = "Пользователь отказался запускать этот инструмент."
GONE = "инструмент больше недоступен в этом диалоге — не вызван"
VIEW = ("server", "tool", "arguments", "question", "options", "allow_text")
ACTIONS = {"confirm": ("run", "refuse"), "ask": ("answer", "refuse")}


class PendingIn(BaseModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")
    action: Literal["run", "refuse", "answer"]
    answer: str | None = Field(None, max_length=ANSWER_MAX)


def arguments_view(arguments: str) -> list:
    """Аргументы вызова для карточки: [[ключ, значение]] не больше ARGS_MAX, по ARG_CHARS знаков."""
    args = web.args_of(arguments) or {}
    return [[str(key)[:ARG_CHARS], (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[:ARG_CHARS]]
            for key, value in list(args.items())[:ARGS_MAX]]


def _target(task, name: str) -> "list | None":
    """Инструмент MCP хода по имени вызова: [id сервера, инструмент, имя сервера] — продолжение найдёт его по ним,
    а не по номеру в имени (каталог продолжения может пронумеровать иначе)."""
    if task.mcp is None or name not in task.mcp.calls:
        return None
    session, tool, server, _ = task.mcp.calls[name]
    return [session.sid, tool, server]


async def pause(task, kind: str, call, view: dict) -> str:
    """Пауза хода `task` (`chat.StreamTask`) на вызове `call`: строка `chat_pending`, событие `pending {id, kind,
    ...view}`; `task.paused` — её id, ход кончается статусом `done` (`chat.StreamTask._calls`)."""
    pid = uuid.uuid4().hex
    rest = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in task.rest]
    targets = {c.name: t for c in [call, *task.rest] if (t := _target(task, c.name)) is not None}
    payload = {"messages": task.messages, "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
               "rounds": task.rounds, "seen": [], "steps": task.steps, "rest": rest, "index": task.index,
               "targets": targets, "view": view, "conn_id": task.conn.id, "model": task.model}
    sealed = security.encrypt(json.dumps(payload, ensure_ascii=False)).decode("ascii")
    state.DB.execute("INSERT INTO chat_pending(id, chat_id, message_id, kind, payload, created) VALUES (?, ?, ?, ?, ?, ?)",
                     (pid, task.chat_id, task.message_id, kind, sealed, clock.NOW()))
    task.paused = pid
    task.fresh = True
    task.put("pending", {"id": pid, "kind": kind, **view})
    log.info("чат %s: ответ %s — пауза %s (%s)", task.chat_id[:8], task.message_id[:8], pid[:8], kind)
    return pid


def _payload(row) -> "dict | None":
    try:
        return json.loads(security.decrypt(row["payload"].encode("ascii")))
    except (InvalidToken, ValueError):
        return None


def open_views(chat_id: str) -> dict:
    """Открытые паузы диалога для выдачи сообщений: id сообщения → `{id, kind, server, tool, arguments, question,
    options, allow_text}`. Старше PENDING_TTL_SEC или не расшифровалась — нет."""
    out = {}
    for row in state.DB.execute("SELECT * FROM chat_pending WHERE chat_id = ? AND resolved IS NULL", (chat_id,)):
        payload = _payload(row) if clock.NOW() - row["created"] <= PENDING_TTL_SEC else None
        if payload is not None:
            out[row["message_id"]] = {"id": row["id"], "kind": row["kind"], **{k: payload["view"].get(k) for k in VIEW}}
    return out


def supersede(chat_id: str) -> None:
    """Новый ход диалога: открытая пауза больше не ждёт ответа (синхронно — в транзакции хода)."""
    state.DB.execute("UPDATE chat_pending SET resolved = ?, outcome = 'superseded' WHERE chat_id = ? AND resolved IS NULL",
                     (clock.NOW(), chat_id))


def _gone() -> HTTPException:
    return HTTPException(409, {"code": "pending_gone", "message": "на этот вопрос уже ответили или он больше не ждёт"})


def _changed() -> HTTPException:
    return HTTPException(409, {"code": "connection_changed",
                               "message": "подключение или модель диалога изменились — отправьте сообщение заново"})


@router.post("/chats/{chat_id}/pending/{pending_id}")
async def answer_pending(chat_id: str, pending_id: str, body: PendingIn, request: Request):
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    pending = state.DB.execute("SELECT * FROM chat_pending WHERE id = ? AND chat_id = ?", (pending_id, chat_id)).fetchone()
    if pending is None:
        raise _gone()
    if body.action not in ACTIONS[pending["kind"]]:
        raise HTTPException(422, {"code": "invalid", "message": "такой ответ этой карточке не подходит"})
    if pending["resolved"] is not None:
        raise _gone()
    if clock.NOW() - pending["created"] > PENDING_TTL_SEC:
        state.DB.execute("UPDATE chat_pending SET resolved = ?, outcome = 'expired' WHERE id = ? AND resolved IS NULL",
                         (clock.NOW(), pending_id))
        raise _gone()
    payload = _payload(pending)
    if payload is None:
        raise _gone()
    view, answer = payload["view"], None
    if body.action == "answer":
        answer = (body.answer or "").strip()
        if not answer or (not view["allow_text"] and answer not in view["options"]):
            raise HTTPException(422, {"code": "invalid", "message": "выберите один из вариантов"})
    chat._busy(chat_id)
    conn = chat._chat_connection(uid, row["connection_id"])
    model = row["model"] or manage.DEFAULT_MODEL
    if (conn is None or conn.id != payload["conn_id"] or model != payload["model"] or not conn.proxied
            or conn.cleaner):
        raise _changed()
    try:
        connections.token(conn)
    except upstream.ConnectionChanged:
        raise _changed() from None
    except upstream.UpstreamRefused:
        pass                                         # ключ не расшифровался — скажет запрос к модели
    if conn.shared:
        chat._chat_pair(uid, conn.id, model)
    limit = await chat._limit(conn, model)
    operations.check(uid)
    # без await до COMMIT: второй ответ и новый ход не вклинятся
    state.DB.execute("BEGIN")
    try:
        if state.DB.execute("UPDATE chat_pending SET resolved = ?, outcome = ? WHERE id = ? AND resolved IS NULL",
                            (clock.NOW(), body.action, pending_id)).rowcount != 1:
            raise _gone()
        chat._busy(chat_id)
        if state.DB.execute(
                "SELECT COUNT(*) FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.user_id = ?"
                " AND m.via = 'server' AND m.status = 'streaming'", (uid,)).fetchone()[0] >= chat.USER_STREAMS:
            raise HTTPException(429, {"code": "streams", "message": (
                f"Сразу идёт не больше {chat.USER_STREAMS} ответов в разных диалогах — дождитесь одного из них"
                " или остановите")})
        mid = pending["message_id"]
        if state.DB.execute("UPDATE messages SET status = 'streaming', error = NULL WHERE id = ? AND chat_id = ?"
                            " AND status = 'done'", (mid, chat_id)).rowcount != 1:
            raise _gone()
        message = state.DB.execute("SELECT content, reasoning, reasoning_sec FROM messages WHERE id = ?", (mid,)).fetchone()
        user = state.DB.execute("SELECT id FROM messages WHERE chat_id = ? AND role = 'user'"
                                " ORDER BY created DESC, rowid DESC LIMIT 1", (chat_id,)).fetchone()
        fresh = state.DB.execute("SELECT web, thinking, mcp_ids, mcp_off FROM chats WHERE id = ?", (chat_id,)).fetchone()
        kinds = web.tools_for(conn, web.has_exa_key(uid)) if fresh["web"] else []
        try:
            exa_key = web.exa_key(uid) if "search" in kinds else None
        except web.ToolError:
            exa_key = None
        mcp_ids = mcp_sources.dialog_servers(uid, fresh, conn)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("чат %s: ответ %s — продолжение после паузы %s (%s)", chat_id[:8], mid[:8], pending_id[:8], body.action)
    st = chat.StreamTask(user_id=uid, chat_id=chat_id, message_id=mid, conn=conn, model=model,
                         messages=payload["messages"], content=message["content"], tools=kinds, exa_key=exa_key,
                         limit=limit, known=chat._known_links(chat_id) if kinds else {}, mcp_ids=mcp_ids,
                         mcp_explicit=mcp_client.present(uid, fresh["mcp_ids"]), params=profiles.request_params(conn),
                         thinking=bool(fresh["thinking"]) if sections.is_local(conn, model) else None,
                         steps=payload["steps"], reasoning=message["reasoning"] or "",
                         reasoning_sec=message["reasoning_sec"] or 0.0, user_message_id=user["id"] if user else None,
                         resume={"kind": pending["kind"], "action": body.action, "answer": answer, **payload})
    chat.STREAMS[mid] = st
    st.task = asyncio.create_task(st.run())
    return chat._EventStream(st, st.user_message_id or "", getattr(request.state, "digest", None))


def resume_step(kind: str, action: str, answer: "str | None", view: dict) -> dict:
    """Шаг вызова, на котором стояла пауза, когда его не запускают: отказ подтверждению или ответ на вопрос."""
    if kind == "ask":
        return chat_tools.answer_step(view, action, answer)
    return {"kind": "mcp", "server": view["server"], "tool": view["tool"], "refused": True, "text": REFUSED}
