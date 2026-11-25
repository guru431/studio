"""«Поделиться» перепиской Open WebUI и её экспорт (п. 42 спеки 2026-11-25-studio-skills-status-local-models-design).

- Снимок — текущая ветка чата (`owui.read_chat`) в форме снимка чата Студии: роль, текст, имена файлов (сами файлы
  остаются в Open WebUI и в ссылку не идут), шаги инструментов с результатом, ошибка. Рассуждение модели в ссылку и
  экспорт не идёт.
- Ссылка — строка `chat_shares` со своим видом `chat_shares.OWUI` и `item_id` "<учётка>:<чат>": снимок и название —
  шифром `STUDIO_DATA_KEY`; прежний код (откат) этот вид не знает и ссылку не открывает (`chat_shares.LIVE`). Удаление
  учётки Open WebUI уносит её ссылки триггером (db.SKILLS_82).
- Маршруты — под `/api/owui/...`: блокировка раздела Open WebUI закрывает и их. Open WebUI молчит — 503
  `owui_offline`, прежняя ссылка остаётся. Чужая и несуществующая учётка — одинаковый 404.
- В журнал — только id учётки и коды: ни текста, ни названия, ни id чата.
"""
import logging

from fastapi import APIRouter, Request, Response

import chat_exports
import chat_shares
import owui
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/owui")


async def snapshot(acc, chat: str) -> dict:
    """Снимок переписки для ссылки и экспорта; больше предела ссылки — 413 `share_size`."""
    view = await owui.read_chat(acc, chat)
    messages = []
    for m in view["messages"]:
        if m["role"] not in ("user", "assistant"):
            continue
        messages.append({"role": m["role"], "text": m["text"], "attachments": [],
                         "files": [f["name"] for f in m["files"] if f["name"]],
                         "steps": [{"name": t["name"], "text": t["result"] or ""} for t in m["tools"]],
                         "reasoning": None, "status": "done" if m["done"] else "streaming", "tools": [],
                         "error": m["error"], "todo": None})
    shot = {"title": view["title"], "messages": messages}
    chat_shares._encode(shot)
    return shot


def _source(request: Request, aid: int, chat: str):
    """Учётка Open WebUI учётки Студии и источник ссылки; чужая учётка и неверный id чата — 404."""
    acc = owui.load(request.state.user.id, aid)
    chat = owui._owui_id(chat, owui.NO_CHAT)
    return acc, {"item_kind": chat_shares.OWUI, "item_id": f"{aid}:{chat}"}


@router.get("/accounts/{aid}/chats/{chat}/share")
async def chat_share(aid: int, chat: str, request: Request):
    _, source = _source(request, aid, chat)
    return chat_shares._out(chat_shares._owned(request, **source))


@router.post("/accounts/{aid}/chats/{chat}/share", status_code=201)
async def create_chat_share(aid: int, chat: str, body: chat_shares.ShareIn, request: Request):
    acc, source = _source(request, aid, chat)
    expected = chat_shares._owned(request, **source)
    shot = await snapshot(acc, chat)
    out = await chat_shares._store(request, body, shot, expected, **source)
    log.info("owui %d: ссылка «Поделиться» создана", aid)
    return out


@router.put("/accounts/{aid}/chats/{chat}/share")
async def refresh_chat_share(aid: int, chat: str, body: chat_shares.RefreshIn, request: Request):
    acc, source = _source(request, aid, chat)
    expected = chat_shares._owned(request, **source) or chat_shares._gone()
    shot = await snapshot(acc, chat)
    return await chat_shares._store(request, body, shot, expected, refresh=True, **source)


@router.delete("/accounts/{aid}/chats/{chat}/share", status_code=204)
async def revoke_chat_share(aid: int, chat: str, request: Request):
    _, source = _source(request, aid, chat)
    row = chat_shares._owned(request, **source)
    if row:
        state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (row["id"],))
        chat_shares._journal(request, "share_revoke", source)
    return Response(status_code=204)


@router.post("/accounts/{aid}/chats/{chat}/export")
async def export_chat(aid: int, chat: str, body: chat_exports.ExportIn, request: Request):
    """Markdown или PDF переписки (PDF выделенного — из `selection`, без обращения к Open WebUI)."""
    acc, _ = _source(request, aid, chat)
    if body.selection is None:
        with chat_exports._export_size():
            shot = await snapshot(acc, chat)
    else:
        shot = {"title": body.title, "messages": []}

    async def check():              # и после сборки PDF: учётку Open WebUI могли удалить за это время
        owui.load(request.state.user.id, aid)

    return await chat_exports._file(shot, body, request, "owui", check)
