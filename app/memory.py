"""Личная память учётки для «Чата» (спека 2026-11-21-studio-source-capabilities-design, п. 18).

Записи — `user_memory`, текст шифром `STUDIO_DATA_KEY` (`security.encrypt`). Модель пишет и стирает их встроенными
инструментами `memory_save` / `memory_forget` (`chat_tools`), только пока включён `users.memory_on`; тогда же
`chat_system.memory_block` ставит их в системное сообщение после роли. Выключатель, список с удалением и «Очистить
всё» — «Настройки → Чат» (`GET/POST/DELETE /api/memory`, `PATCH /api/me {memory_on}`).

Память — только владельца: администратор чужую не видит. «Удалить всю историю» её уносит (`history`), выгрузка
(`takeout`) её содержит, удаление учётки — каскадом. В журнал — только id учётки и числа: ни текста, ни id записей.
"""
import logging
import uuid

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import clock
import security
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/memory")

ITEM_MAX = 500
MAX_ITEMS = 100
BLOCK_MAX = 4000
UNREADABLE = "[запись не расшифровалась]"


class MemoryIn(BaseModel):
    text: str = Field(max_length=ITEM_MAX * 2)        # пробелы по краям срежет save; длину сверит он же


INVALID = {"code": "invalid", "message": f"запись памяти — от 1 до {ITEM_MAX} знаков"}


def list_for(uid: int, project_id: str | None = None) -> list[dict]:
    """Записи учётки, новые первыми: `{id, text, created}`; не расшифровавшаяся (сменили ключ) — UNREADABLE."""
    out = []
    table, column, scope = _scope(uid, project_id)
    for row in state.DB.execute(f"SELECT id, text_enc, created FROM {table} WHERE {column} = ?"
                                " ORDER BY created DESC, rowid DESC", (scope,)):
        try:
            text = security.decrypt(row["text_enc"])
        except (InvalidToken, TypeError, ValueError):
            text = UNREADABLE
        out.append({"id": row["id"], "text": text, "created": row["created"]})
    return out


def _scope(uid: int, project_id: str | None) -> tuple:
    if project_id is None:
        return "user_memory", "user_id", uid
    return "chat_project_memory", "project_id", project_id


def save(uid: int, text: str, project_id: str | None = None) -> str:
    """Новая запись шифром; итог — её id. Пустая или длиннее ITEM_MAX — 422 `invalid`, уже MAX_ITEMS — 409
    `memory_full`. Проверка числа и вставка — без await между ними."""
    text = " ".join(text.split())                    # одной строкой: блок системного сообщения — строки записей
    if not 1 <= len(text) <= ITEM_MAX:
        raise HTTPException(422, INVALID)
    table, column, scope = _scope(uid, project_id)
    if state.DB.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (scope,)).fetchone()[0] >= MAX_ITEMS:
        raise HTTPException(409, {"code": "memory_full",
                                  "message": f"в памяти уже {MAX_ITEMS} записей — удалите ненужные"})
    item_id = uuid.uuid4().hex
    state.DB.execute(f"INSERT INTO {table}(id, {column}, text_enc, created) VALUES (?, ?, ?, ?)",
                     (item_id, scope, security.encrypt(text), clock.NOW()))
    log.info("пользователь %d: память — запись добавлена", uid)
    return item_id


def forget(uid: int, item_id: str, project_id: str | None = None) -> bool:
    """Удалить свою запись; чужой или нет — False."""
    table, column, scope = _scope(uid, project_id)
    gone = state.DB.execute(f"DELETE FROM {table} WHERE id = ? AND {column} = ?", (item_id, scope)).rowcount == 1
    if gone:
        log.info("пользователь %d: память — запись удалена", uid)
    return gone


def clear(uid: int) -> int:
    """Все записи учётки; итог — сколько удалено."""
    count = state.DB.execute("DELETE FROM user_memory WHERE user_id = ?", (uid,)).rowcount
    log.info("пользователь %d: память очищена (%d)", uid, count)
    return count


def enabled(uid: int, project_id: str | None = None) -> bool:
    row = (state.DB.execute("SELECT memory_on FROM users WHERE id = ?", (uid,)).fetchone() if project_id is None else
           state.DB.execute("SELECT memory_on FROM chat_projects WHERE user_id = ? AND id = ?",
                            (uid, project_id)).fetchone())
    return row is not None and bool(row[0])


@router.get("")
async def get_memory(request: Request):
    uid = request.state.user.id
    return {"on": enabled(uid), "items": list_for(uid)}


@router.post("", status_code=201)
async def post_memory(body: MemoryIn, request: Request):
    item_id = save(request.state.user.id, body.text)
    return next(item for item in list_for(request.state.user.id) if item["id"] == item_id)


@router.delete("", status_code=204)
async def delete_memory(request: Request):
    clear(request.state.user.id)
    return Response(status_code=204)


@router.delete("/{item_id}", status_code=204)
async def delete_item(item_id: str, request: Request):
    if not forget(request.state.user.id, item_id):
        raise HTTPException(404, {"code": "not_found", "message": "такой записи памяти нет"})
    return Response(status_code=204)
