"""«Продолжить иначе» (спека 2026-11-25-studio-skills-status-local-models п. 18): у своего сообщения — новый диалог с
историей до него. Переносятся выбранные варианты ответов (`messages.unchosen` = 0) и их вложения — новыми строками
`attachments` со ссылками на те же файлы (`media_id`): файл держит любая из строк (`media.referenced`), удаление
одного диалога его не уносит. Документы знаний больших вложений ветке не копируются — ход заводит их сам
(`knowledge.adopt`). Вложения самого сообщения-точки не переносятся; его текст (без текста навыка) уходит странице
для поля ввода.

Ветка — диалог-наследник `new_dialog`: подключение, модель, роль, MCP, базы знаний и отметка защищённого источника
(`chats.protected_used`) — как у исходного; хранимый пересказ, закрепление, согласия и медиа диалога — нет. Диалог
через Cleaner (предпросмотр или шлюз `cleaner_route`) не ветвится: контексты замен Cleaner привязаны к диалогу."""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import chat
import chat_skills
import cleaner_client
import clock
import connections
import ocr
import operations
import state

CLEANER = "диалог через Cleaner не ветвится: замены Cleaner привязаны к диалогу"
#: столбцы сообщения, которые ветка переносит; запрос и ответ Cleaner (`cleaner_raw`, `cleaner_request`) и живость
#: хода (`alive_at`) принадлежат исходному диалогу
MESSAGE_COLUMNS = ("role", "content", "status", "via", "model", "created", "tools", "reasoning", "reasoning_sec",
                   "error", "todo", "skill", "usage", "gen_ms", "total_ms", "transcript_consent")
#: столбцы вложения, которые ветка переносит (тот же файл `media_id`)
ATTACHMENT_COLUMNS = ("kind", "name", "media_id", "text", "chars", "created", "source_transcript_id", "cleaner_used")

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class ForkIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)


def new_dialog(uid: int, row, *, title: "str | None" = None) -> str:
    """Диалог-наследник `row` (строка `chats` учётки `uid`) без сообщений; итог — его id. Наследует подключение,
    модель, роль, рассуждение, MCP (набор, выключенные серверы и инструменты), отметку защищённого источника, базы
    знаний и название (`title` — вместо него); «Интернет» — исходный, после защищённого источника — выключен.
    Согласия (`web_consent`, `mcp_file_consent`), закрепление, хранимый пересказ, медиа и паузы — не наследует.
    Синхронно: зовущий держит транзакцию и `operations.check`."""
    chat_id, now = uuid.uuid4().hex, clock.NOW()
    web = 0 if row["protected_used"] else row["web"]
    state.DB.execute(
        "INSERT INTO chats(id, user_id, title, connection_id, model, mcp_model_sid, role_id, thinking, effort, mcp_ids,"
        " mcp_off, mcp_tools_off, protected_used, web, created, updated, seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, uid, row["title"] if title is None else title, row["connection_id"], row["model"],
         row["mcp_model_sid"], row["role_id"], row["thinking"], row["effort"], row["mcp_ids"], row["mcp_off"],
         row["mcp_tools_off"], row["protected_used"], web, now, now, now))
    state.DB.execute("INSERT INTO chat_kbs(chat_id, kb_id) SELECT ?, kb_id FROM chat_kbs WHERE chat_id = ?"
                     " ORDER BY rowid", (chat_id, row["id"]))
    return chat_id


def _through_cleaner(uid: int, conn_id: "int | None") -> bool:
    """Подключение диалога — Cleaner: предпросмотр (`cleaner`) или шлюз (`routed`); и закрытое группами доступа."""
    if conn_id is None:
        return False
    try:
        conn = connections.get(uid, conn_id, gate=False)
    except HTTPException:                            # подключение удалено
        return False
    return conn.cleaner or conn.routed


@router.post("/chats/{chat_id}/fork", status_code=201)
async def fork(chat_id: str, body: ForkIn, request: Request):
    """Ветка от своего сообщения `message_id`: 201 `{chat_id, text}`. Нет диалога или своего выбранного сообщения —
    404 `gone`; диалог через Cleaner — 409 `cleaner`; документ истории до точки ещё распознаётся — 409 `not_ready`."""
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    target = chat._user_turn(chat_id, body.message_id)
    if target["unchosen"]:
        raise chat._gone("такого сообщения нет")
    if _through_cleaner(uid, row["connection_id"]):
        raise cleaner_client.refused(CLEANER)
    point = (target["created"], target["rowid"])
    unread = [r["id"] for r in state.DB.execute(
        "SELECT a.id FROM attachments a JOIN messages m ON m.id = a.message_id WHERE m.chat_id = ? AND m.unchosen = 0"
        " AND (m.created, m.rowid) < (?, ?) AND a.kind = 'document' AND a.text IS NULL", (chat_id, *point))]
    if ocr.pending(uid, unread):
        raise HTTPException(409, {"code": "not_ready", "message": chat.NOT_READY})
    operations.check(uid)
    # дальше без await: строки ветки — одной транзакцией
    state.DB.execute("BEGIN")
    try:
        new = new_dialog(uid, row)
        rows = state.DB.execute(
            f"SELECT id, {', '.join(MESSAGE_COLUMNS)} FROM messages WHERE chat_id = ? AND unchosen = 0"
            " AND (created, rowid) < (?, ?) ORDER BY created, rowid", (chat_id, *point)).fetchall()
        ids = {}
        for m in rows:
            ids[m["id"]] = uuid.uuid4().hex
            state.DB.execute(
                f"INSERT INTO messages(id, chat_id, {', '.join(MESSAGE_COLUMNS)})"
                f" VALUES (?, ?, {', '.join('?' * len(MESSAGE_COLUMNS))})",
                (ids[m["id"]], new, *(m[c] for c in MESSAGE_COLUMNS)))
        if ids:
            marks = ", ".join("?" * len(ids))
            for a in state.DB.execute(
                    f"SELECT message_id, {', '.join(ATTACHMENT_COLUMNS)} FROM attachments WHERE message_id IN ({marks})"
                    " ORDER BY created, rowid", tuple(ids)).fetchall():
                state.DB.execute(
                    f"INSERT INTO attachments(id, user_id, message_id, {', '.join(ATTACHMENT_COLUMNS)})"
                    f" VALUES (?, ?, ?, {', '.join('?' * len(ATTACHMENT_COLUMNS))})",
                    (uuid.uuid4().hex, uid, ids[a["message_id"]], *(a[c] for c in ATTACHMENT_COLUMNS)))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("чат %s: ветка %s, сообщений %d", chat_id[:8], new[:8], len(rows))
    return {"chat_id": new, "text": chat_skills.human(target)}   # в поле — только уточнение, без навыка
