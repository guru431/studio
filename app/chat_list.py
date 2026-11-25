"""Список диалогов «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 23): метки строки и взгляд на диалог.

- `marks` — «идёт ответ» (`busy`) и «не прочитано» (`unread`) по последнему выбранному сообщению каждого диалога
  учётки, одним запросом. `busy` — оно `streaming` и идёт через сервер или его ещё читает вкладка (`chat._alive`).
  `unread` — ответ кончился (не `streaming`) позже последнего взгляда (`chats.updated > chats.seen`); `seen` NULL —
  диалог до шага 82 или созданный прежним кодом: не «не прочитано».
- `POST /api/chats/{id}/seen` — страница открыла диалог: `chats.seen` = сейчас; чужой или в корзине — 404 `gone`.
  От взгляда же отсчитывается напоминание о ждущей карточке (`chat_pending.remind`).
- Закрепление — `PATCH /api/chats/{id}` `{pinned}` (`chat.patch_chat`), порядок групп — на странице."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

import chat
import clock
import state

router = APIRouter(prefix="/api")


def marks(uid: int, rows) -> dict[str, dict]:
    """Метки строк `rows` (строки `chats` учётки `uid`): {id: {"busy": bool, "unread": bool}}."""
    last = {r["chat_id"]: r for r in state.DB.execute(
        "SELECT c.id AS chat_id, m.role, m.status, m.via, m.alive_at FROM chats c JOIN messages m ON m.rowid = ("
        " SELECT rowid FROM messages WHERE chat_id = c.id AND unchosen = 0 ORDER BY created DESC, rowid DESC LIMIT 1)"
        " WHERE c.user_id = ? AND c.deleted IS NULL", (uid,))}
    out = {}
    for row in rows:
        m = last.get(row["id"])
        busy = m is not None and m["status"] == "streaming" and (m["via"] == "server" or chat._alive(m))
        unread = (m is not None and row["seen"] is not None and m["role"] == "assistant"
                  and m["status"] != "streaming" and row["updated"] > row["seen"])
        out[row["id"]] = {"busy": busy, "unread": unread}
    return out


@router.post("/chats/{chat_id}/seen", status_code=204)
async def seen(chat_id: str, request: Request):
    uid = request.state.user.id
    chat._chat_row(uid, chat_id)
    state.DB.execute("UPDATE chats SET seen = ? WHERE id = ? AND user_id = ?", (clock.NOW(), chat_id, uid))
    return Response(status_code=204)
