"""Источники «Знаний» под ответом «Чата» (спека 2026-11-25, п. 28): шаг `knowledge` (и `knowledge_search`, и
автоматический блок `auto`) несёт `hits: [{doc, n, id}]`. Страница получает текст куска из базы знаний или из
исходного вложения с проверкой доступа (`knowledge.chunk`). Журнал маршрут не пишет."""
import json

from fastapi import APIRouter, HTTPException, Request

import chat
import knowledge
import state

GONE = "кусок недоступен: документ изменён, удалён или закрыт"

router = APIRouter(prefix="/api")


@router.get("/chats/{chat_id}/messages/{message_id}/sources/{step}/{n}")
async def source(chat_id: str, message_id: str, step: int, n: int, request: Request):
    uid = request.state.user.id
    chat._chat_row(uid, chat_id)
    found = state.DB.execute("SELECT tools FROM messages WHERE id = ? AND chat_id = ?", (message_id, chat_id)).fetchone()
    steps = json.loads(found["tools"] or "[]") if found is not None else []
    item = steps[step] if 0 <= step < len(steps) and isinstance(steps[step], dict) else {}
    hits = item.get("hits") if item.get("kind") == "knowledge" else None
    hit = hits[n] if isinstance(hits, list) and 0 <= n < len(hits) and isinstance(hits[n], dict) else {}
    doc_id, number = hit.get("id"), hit.get("n")
    out = (knowledge.chunk(uid, chat_id, doc_id, number)
           if isinstance(doc_id, str) and type(number) is int else None)
    if out is None:
        raise HTTPException(404, {"code": "gone", "message": GONE})
    return out
