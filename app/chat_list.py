"""Список диалогов «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 23): метки строки и взгляд на диалог.

- `marks` — «идёт ответ» (`busy`), «ждёт ответа» (`asking`) и «не прочитано» (`unread`) по последнему выбранному сообщению каждого диалога
  учётки, одним запросом. `busy` — оно `streaming` и идёт через сервер или его ещё читает вкладка (`chat._alive`).
  `unread` — ответ кончился (не `streaming`) позже последнего взгляда (`chats.updated > chats.seen`); `seen` NULL —
  строка, вставленная прежним кодом после шага 82 (шаг сам ставит `seen = updated`): не «не прочитано».
- `POST /api/chats/{id}/seen` — страница открыла диалог: `chats.seen` = сейчас; чужой или в корзине — 404 `gone`.
  От взгляда же отсчитывается напоминание о ждущей карточке (`chat_pending.remind`).
- Закрепление — `PATCH /api/chats/{id}` `{pinned}` (`chat.patch_chat`), порядок групп — на странице."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

import chat
import chat_pending
import clock
import state

router = APIRouter(prefix="/api")


def marks(uid: int, rows) -> dict[str, dict]:
    """Метки строк `rows` (строки `chats` учётки `uid`): busy, asking, unread."""
    last = {r["chat_id"]: r for r in state.DB.execute(
        "SELECT c.id AS chat_id, m.role, m.status, m.via, m.alive_at,"
        " EXISTS (SELECT 1 FROM chat_pending p WHERE p.chat_id = c.id AND p.message_id = m.id"
        " AND p.resolved IS NULL AND p.created >= ?) AS asking FROM chats c JOIN messages m ON m.rowid = ("
        " SELECT rowid FROM messages WHERE chat_id = c.id AND unchosen = 0 ORDER BY created DESC, rowid DESC LIMIT 1)"
        " WHERE c.user_id = ? AND c.deleted IS NULL", (clock.NOW() - chat_pending.PENDING_TTL_SEC, uid))}
    out = {}
    for row in rows:
        m = last.get(row["id"])
        busy = m is not None and m["status"] == "streaming" and (m["via"] == "server" or chat._alive(m))
        asking = m is not None and bool(m["asking"])
        unread = (m is not None and not asking and row["seen"] is not None and m["role"] == "assistant"
                  and m["status"] != "streaming" and row["updated"] > row["seen"])
        out[row["id"]] = {"busy": busy, "asking": asking, "unread": unread}
    return out


@router.post("/chats/{chat_id}/seen", status_code=204)
async def seen(chat_id: str, request: Request):
    uid = request.state.user.id
    chat._chat_row(uid, chat_id)
    state.DB.execute("UPDATE chats SET seen = ? WHERE id = ? AND user_id = ?", (clock.NOW(), chat_id, uid))
    return Response(status_code=204)
