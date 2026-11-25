"""Пауза и продолжение хода чата через сервер (спека 2026-11-21-studio-source-capabilities-design, п. 5 и 17).

1. Пауза (`pause`): вызов инструмента MCP «с подтверждением» (`mcp_sources.confirm_needed`) или встроенный вопрос
   `ask_user` (`chat_tools`). Состояние цикла — шифром `STUDIO_DATA_KEY` в `chat_pending.payload`: сообщения модели,
   вызов, круги, шаги, оставшиеся вызовы круга и вид карточки. Страница получает событие SSE `pending`, ход кончается
   статусом `done` — текст до паузы сохранён; у сообщения в выдаче — `pending` (`open_views`).
2. Ответ (`POST /api/chats/{chat_id}/pending/{pending_id}`): одна смена `resolved` (`WHERE resolved IS NULL`) — второй
   ответ, ответ позже PENDING_TTL_SEC и ответ невыбранному варианту (`messages.unchosen`, пауза ждёт его выбора)
   получают 409 `pending_gone`; подключение диалога заново — сменилось — 409
   `connection_changed`. То же сообщение снова `streaming`, `chat.StreamTask` с `resume` продолжает цикл, ответ — поток
   SSE, как у хода. Доступ, политика и «Интернет» перепроверяют каталог хода и `mcp_client.Tools.call`. Квитанция
   `request_id` — в `chat_turn_requests` той же транзакцией, что смена `resolved`: повтор того же ответа (ответ потерян
   в сети) — ответ повтора хода `{replayed: true, ...}`, страница перечитывает диалог и подхватывает идущее сообщение;
   другой ответ с тем же id — 409 `request_conflict`.
3. Новый ход диалога снимает открытую паузу (`supersede`, исход `superseded`), срок — `_expire`. Шаг вызова паузы
   пишется сразу (`waiting_step`): снятая без ответа пауза оставляет вопрос странице и модели следующих ходов.
4. В журнал — только id и вид паузы: ни вопроса, ни аргументов, ни ответа.
5. Пауза шлёт push вида `ask` без содержимого (`notify`), `remind` повторяет его, пока пауза открыта (спека
   2026-11-25-studio-skills-status-local-models, п. 20): интервал и тихие часы — «О чём уведомлять» устройства."""
import asyncio
import hashlib
import json
import logging
import uuid
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import chat
import chat_tools
import cleaner_route
import clock
import connections
import manage
import mcp_client
import mcp_sources
import operations
import profiles
import push
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
MEMORY_REFUSED = "Пользователь не подтвердил запись в память."
GONE = "инструмент больше недоступен в этом диалоге — не вызван"
# шаг паузы, снятой новым сообщением или сроком (`_unanswer`): вопрос и инструмент остаются модели следующих ходов
UNANSWERED = "пользователь не ответил на карточке — ответ, если есть, в следующем сообщении"
NOT_RUN = "Вызов {0} с аргументами {1} не запущен: " + UNANSWERED + "."
VIEW = ("server", "tool", "arguments", "question", "options", "allow_text")
ACTIONS = {"confirm": ("run", "refuse"), "ask": ("answer", "refuse")}
# решённая пауза (любой исход) payload и вид карточки не хранит: в нём сообщения модели, документы и вопрос
RESOLVE = "resolved = ?, payload = '', view = ''"
REMIND_TIMES = 3                   # повторов push на паузу каждому устройству (как bridge.REMIND_TIMES)
REMIND_TICK_SEC = 60
#: повторы напоминаний: id паузы → сессия → (повторов, когда последний)
SENT: dict[str, dict[str, tuple[int, float]]] = {}
#: начало проходов напоминаний этого процесса (`start_reminders`): счёт SENT в памяти, и пауза, созданная до
#: перезапуска службы, отсчитывает интервал от него, а не от своего создания — без залпа повторов после выкладки
STARTED = 0.0


class PendingIn(BaseModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")     # квитанция ответа, как у хода (`_receipt`)
    action: Literal["run", "refuse", "answer"]
    answer: str | None = Field(None, max_length=ANSWER_MAX)


def _fingerprint(pending_id: str, body: PendingIn) -> str:
    raw = json.dumps(["pending", pending_id, body.action, body.answer], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _receipt(chat_id: str, pending_id: str, body: PendingIn) -> "dict | None":
    """Повтор ответа после обрыва связи: квитанция в `chat_turn_requests` (та же, что у хода) — тот же ответ получает
    ответ повтора хода (`{replayed, user_message_id, assistant_message_id}`), другой ответ с тем же id — 409
    `request_conflict`."""
    row = state.DB.execute("SELECT fingerprint FROM chat_turn_requests WHERE chat_id = ? AND request_id = ?",
                           (chat_id, body.request_id)).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != _fingerprint(pending_id, body):
        raise HTTPException(409, {"code": "request_conflict", "message": "Эта отправка уже содержит другой ответ"})
    return {"replayed": True, **chat._turn_receipt(chat_id, body.request_id)}


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
    # картинки — ссылкой на media, не base64 (фото телефона — десятки мегабайт): продолжение прочитает файлы заново
    payload = {"messages": chat.photo_refs(task.messages, task.photos),
               "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
               "rounds": task.rounds, "seen": sorted(task.seen), "steps": task.steps, "rest": rest, "index": task.index,
               "targets": targets, "view": view, "conn_id": task.conn.id, "model": task.model}
    payload["parts"], payload["part_round"] = task.parts, task.part_round   # счёт частей ответа (chat_parts)
    payload["images"] = task.round_images            # id media картинок результатов круга (chat._flush_images)
    sealed = security.encrypt(json.dumps(payload, ensure_ascii=False)).decode("ascii")
    shown = security.encrypt(json.dumps(view, ensure_ascii=False)).decode("ascii")   # выдаче сообщений — только он
    state.DB.execute("INSERT INTO chat_pending(id, chat_id, message_id, kind, payload, view, created)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (pid, task.chat_id, task.message_id, kind, sealed, shown, clock.NOW()))
    # шаг вызова — сразу (в payload его нет: продолжение запишет вместо него итог вызова)
    task._step(waiting_step(kind, view))
    task.paused = pid
    task.fresh = True
    task.put("pending", {"id": pid, "kind": kind, **view})
    notify(task.user_id, task.chat_id)               # push вида `ask` без содержимого
    log.info("чат %s: ответ %s — пауза %s (%s)", task.chat_id[:8], task.message_id[:8], pid[:8], kind)
    return pid


def waiting_step(kind: str, view: dict) -> dict:
    """Шаг вызова, на котором ход встал на паузу (`waiting`): вопрос или инструмент видны странице сразу. Текст — уже
    для снятой паузы (вопрос с UNANSWERED, вызов с аргументами — NOT_RUN): пока пауза открыта, модели он не уходит
    (продолжение берёт сообщения из payload и пишет вместо шага итог), снятую — отдаёт следующий ход (`_unanswer`)."""
    if kind == "ask":
        return {"kind": "ask", "question": view["question"], "waiting": True, "text": json.dumps(
            {"question": view["question"], "answer": None, "note": UNANSWERED}, ensure_ascii=False)}
    if "memory_text" in view:
        return {"kind": "memory", "action": "save", "waiting": True, "text": MEMORY_REFUSED}
    args = json.dumps(dict(view["arguments"]), ensure_ascii=False)
    return {"kind": "mcp", "server": view["server"], "tool": view["tool"], "waiting": True,
            "text": NOT_RUN.format(view["tool"], args)}


def _unanswer(message_id: str) -> None:
    """Пауза снята без ответа (новое сообщение, срок): шаг `waiting` ответа больше не ждёт, вызов MCP — `refused`
    (не запускался); следующий ход отдаёт его текст модели (chat.compose) — вопрос не теряется."""
    row = state.DB.execute("SELECT tools FROM messages WHERE id = ?", (message_id,)).fetchone()
    steps = json.loads(row["tools"] or "[]") if row is not None else []
    if not any(s.get("waiting") for s in steps):
        return
    steps = [{k: v for k, v in s.items() if k != "waiting"} |
             ({"refused": True} if s["kind"] in ("mcp", "memory") else {})
             if s.get("waiting") else s for s in steps]
    state.DB.execute("UPDATE messages SET tools = ? WHERE id = ?", (json.dumps(steps, ensure_ascii=False), message_id))


def _payload(row, column: str = "payload") -> "dict | None":
    try:
        return json.loads(security.decrypt(row[column].encode("ascii")))
    except (InvalidToken, ValueError):
        return None


def _expire(row) -> None:
    """Пауза старше PENDING_TTL_SEC: исход `expired`, payload стёрт, шаг — без ответа (`_unanswer`)."""
    if state.DB.execute(f"UPDATE chat_pending SET {RESOLVE}, outcome = 'expired' WHERE id = ? AND resolved IS NULL",
                        (clock.NOW(), row["id"])).rowcount == 1:
        _unanswer(row["message_id"])


def open_views(chat_id: str) -> dict:
    """Открытые паузы диалога для выдачи сообщений: id сообщения → `{id, kind, server, tool, arguments, question,
    options, allow_text}` — из столбца `view`, payload не расшифровывается. Старше PENDING_TTL_SEC — снимается
    (`_expire`); не расшифровалась — нет."""
    out = {}
    for row in state.DB.execute("SELECT id, message_id, kind, view, created FROM chat_pending"
                                " WHERE chat_id = ? AND resolved IS NULL", (chat_id,)).fetchall():
        if clock.NOW() - row["created"] > PENDING_TTL_SEC:
            _expire(row)
            continue
        view = _payload(row, "view")
        if view is not None:
            show = {"memory_text": view["memory_text"]} if "memory_text" in view else {k: view.get(k) for k in VIEW}
            out[row["message_id"]] = {"id": row["id"], "kind": row["kind"], **show}
    return out


def supersede(chat_id: str) -> None:
    """Новый ход диалога: открытая пауза больше не ждёт ответа, её шаг — без ответа (синхронно — в транзакции хода)."""
    for row in state.DB.execute("SELECT id, message_id FROM chat_pending WHERE chat_id = ? AND resolved IS NULL",
                                (chat_id,)).fetchall():
        state.DB.execute(f"UPDATE chat_pending SET {RESOLVE}, outcome = 'superseded' WHERE id = ?",
                         (clock.NOW(), row["id"]))
        _unanswer(row["message_id"])


def _gone() -> HTTPException:
    return HTTPException(409, {"code": "pending_gone", "message": "на этот вопрос уже ответили или он больше не ждёт"})


def _changed() -> HTTPException:
    return HTTPException(409, {"code": "connection_changed",
                               "message": "подключение или модель диалога изменились — отправьте сообщение заново"})


@router.post("/chats/{chat_id}/pending/{pending_id}")
async def answer_pending(chat_id: str, pending_id: str, body: PendingIn, request: Request):
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    if (receipt := _receipt(chat_id, pending_id, body)) is not None:
        return receipt
    pending = state.DB.execute("SELECT * FROM chat_pending WHERE id = ? AND chat_id = ?", (pending_id, chat_id)).fetchone()
    if pending is None:
        raise _gone()
    if body.action not in ACTIONS[pending["kind"]]:
        raise HTTPException(422, {"code": "invalid", "message": "такой ответ этой карточке не подходит"})
    if pending["resolved"] is not None:
        raise _gone()
    if clock.NOW() - pending["created"] > PENDING_TTL_SEC:
        _expire(pending)
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
    cleaner_route.refuse_forced(conn, "chat", model)  # пауза до принудительного Cleaner: облачная роль srvllm — отказ
    if conn.shared:
        chat._chat_pair(uid, conn.id, model)
    limit = await chat.model_window(conn, model)
    auth.recheck(request)
    operations.check(uid)
    if clock.NOW() - pending["created"] > PENDING_TTL_SEC:
        _expire(pending)
        raise _gone()
    if (receipt := _receipt(chat_id, pending_id, body)) is not None:   # тот же ответ, пока ждали список моделей
        return receipt
    # без await до COMMIT: второй ответ и новый ход не вклинятся
    state.DB.execute("BEGIN")
    try:
        fresh = chat._chat_row(uid, chat_id)
        if (fresh["connection_id"] != row["connection_id"] or fresh["model"] != row["model"]):
            raise _changed()
        if conn.shared:
            chat._chat_pair(uid, conn.id, model)
        try:
            connections.token(conn)
        except upstream.ConnectionChanged:
            raise _changed() from None
        except upstream.UpstreamRefused:
            pass                                     # ключ не расшифровался — скажет запрос к модели
        if state.DB.execute(f"UPDATE chat_pending SET {RESOLVE}, outcome = ? WHERE id = ? AND resolved IS NULL",
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
        # невыбранный вариант не продолжается (карточка в другой вкладке): пауза ждёт, пока его не выберут снова
        if state.DB.execute("UPDATE messages SET status = 'streaming', error = NULL WHERE id = ? AND chat_id = ?"
                            " AND status = 'done' AND unchosen = 0", (mid, chat_id)).rowcount != 1:
            raise _gone()
        message = state.DB.execute("SELECT content, reasoning, reasoning_sec, usage, gen_ms, total_ms FROM messages"
                                   " WHERE id = ?", (mid,)).fetchone()
        user = state.DB.execute("SELECT id FROM messages WHERE chat_id = ? AND role = 'user'"
                                " ORDER BY created DESC, rowid DESC LIMIT 1", (chat_id,)).fetchone()
        kinds = web.tools_for(conn, web.has_exa_key(uid)) if fresh["web"] and not fresh["protected_used"] else []
        try:
            exa_key = web.exa_key(uid) if "search" in kinds else None
        except web.ToolError:
            exa_key = None
        mcp_ids = mcp_sources.dialog_servers(uid, fresh, conn)
        state.DB.execute("INSERT INTO chat_turn_requests VALUES (?, ?, ?, ?, ?)",
                         (chat_id, body.request_id, _fingerprint(pending_id, body), user["id"] if user else "", mid))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("чат %s: ответ %s — продолжение после паузы %s (%s)", chat_id[:8], mid[:8], pending_id[:8], body.action)
    st = chat.StreamTask(user_id=uid, chat_id=chat_id, message_id=mid, conn=conn, model=model,
                         messages=payload["messages"], content=message["content"], tools=kinds, exa_key=exa_key,
                         limit=limit, ratio=chat.ratio(conn, model), known=chat._known_links(chat_id) if kinds else {},
                         mcp_ids=mcp_ids,
                         mcp_explicit=mcp_client.present(uid, fresh["mcp_ids"]), params=profiles.request_params(conn, model),
                         thinking=bool(fresh["thinking"]) if sections.is_local(conn, model) else None,
                         steps=payload["steps"], reasoning=message["reasoning"] or "",
                         reasoning_sec=message["reasoning_sec"] or 0.0, user_message_id=user["id"] if user else None,
                         # токены и время до паузы: продолжение прибавляет своё (ожидание человека не в счёт)
                         usage=json.loads(message["usage"] or "{}"),
                         gen_sec=None if message["gen_ms"] is None else message["gen_ms"] / 1000,
                         spent_ms=message["total_ms"] or 0,
                         resume={"kind": pending["kind"], "action": body.action, "answer": answer, **payload})
    chat.STREAMS[mid] = st
    st.task = asyncio.create_task(st.run())
    return chat._EventStream(st, st.user_message_id or "", getattr(request.state, "digest", None))


def resume_step(kind: str, action: str, answer: "str | None", view: dict) -> dict:
    """Шаг вызова, на котором стояла пауза, когда его не запускают: отказ подтверждению или ответ на вопрос."""
    if kind == "ask":
        return chat_tools.answer_step(view, action, answer)
    if "memory_text" in view:
        return {"kind": "memory", "action": "save", "refused": True, "text": MEMORY_REFUSED}
    return {"kind": "mcp", "server": view["server"], "tool": view["tool"], "refused": True, "text": REFUSED}


# ---- push и напоминание о ждущей карточке (спека 2026-11-25, п. 20) ----------------------------------------

def notify(user_id: int, chat_id: str) -> None:
    """Ход встал на паузу: push вида `ask` без содержимого (ни вопроса, ни инструмента) в диалог — сессиям, чья
    страница не на экране, и устройствам, где вид не снят и не идут тихие часы. Диалог в корзине — молча."""
    if state.DB.execute("SELECT 1 FROM chats WHERE id = ? AND deleted IS NULL", (chat_id,)).fetchone() is None:
        return
    push.notify_later(user_id, push.DECISION_NOTICE, f"chat/{chat_id}", f"chat-ask-{chat_id}", title=push.STUDIO,
                      quiet=True, kind="ask")


def remind() -> None:
    """Проход напоминаний: пауза открыта и моложе PENDING_TTL_SEC, диалог не в корзине, учётка включена — каждому её
    устройству, где напоминание включено (`push.reminders`), вид `ask` не заглушён (`push.muted`) и страница не на
    экране (`push.on_screen`), повтор push с той же меткой через интервал устройства от паузы, прошлого повтора или
    последнего взгляда на диалог (`chats.seen`); не больше REMIND_TIMES на устройство. Тихие часы откладывают
    повтор, а не съедают его. Устройство, чья страница на экране, — как взгляд: его отсчёт начинается заново.
    Пауза невыбранного варианта ответа (`messages.unchosen`: карточки не видно) не напоминает. Счёт — в памяти
    (SENT): перезапуск службы начинает его заново, отсчёт — не раньше STARTED."""
    now = clock.NOW()
    rows = state.DB.execute(
        "SELECT p.id, p.chat_id, p.created, c.user_id, c.seen FROM chat_pending p JOIN chats c ON c.id = p.chat_id"
        " JOIN messages m ON m.id = p.message_id AND m.unchosen = 0"
        " JOIN users u ON u.id = c.user_id WHERE p.resolved IS NULL AND p.created >= ? AND c.deleted IS NULL"
        " AND u.disabled = 0", (now - PENDING_TTL_SEC,)).fetchall()
    for pid in set(SENT) - {row["id"] for row in rows}:
        del SENT[pid]
    screen = push.on_screen()
    for row in rows:
        uid = row["user_id"]
        intervals, muted, sent = push.reminders(uid), push.muted(uid, "ask"), SENT.get(row["id"], {})
        due = set()
        for digest, interval in intervals.items():
            times, last = sent.get(digest, (0, max(row["created"], STARTED)))
            if digest in screen:                    # смотрит на страницу — как `bridge.watching` у «Агентов»
                SENT.setdefault(row["id"], {})[digest] = (times, now)
                continue
            if (interval and times < REMIND_TIMES and digest not in muted
                    and now - max(last, row["seen"] or 0) >= interval):
                due.add(digest)
                SENT.setdefault(row["id"], {})[digest] = (times + 1, now)
        if due:
            push.notify_later(uid, push.DECISION_NOTICE, f"chat/{row['chat_id']}", f"chat-ask-{row['chat_id']}",
                              title=push.STUDIO, quiet=True, kind="ask", skip=frozenset(intervals) - due)


async def _reminders() -> None:
    while True:
        await clock.SLEEP(REMIND_TICK_SEC)
        try:
            remind()
        except Exception as e:           # noqa: BLE001 — напоминания не должны вставать
            log.warning("напоминания чата: сбой (%s)", type(e).__name__)


def start_reminders() -> asyncio.Task:
    global STARTED
    STARTED = clock.NOW()
    return asyncio.create_task(_reminders())
