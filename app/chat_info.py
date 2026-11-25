"""Сведения «Чата» для строки состояния (спека 2026-11-25-studio-skills-status-local-models, п. 14 и 15).

- `GET /api/chats/stats?days=N` — «Статистика» учётки: ответы, токены ввода и вывода (`messages.usage`) и время
  генерации (`messages.gen_ms`) по дням пояса учётки (`users.timezone`) и моделям за N дней (1–STATS_DAYS) — по
  сохранённой истории своих диалогов не из корзины; невыбранные варианты ответов входят, идущие ответы — нет.
- `GET /api/chats/{chat_id}/files` — «Файлы диалога»: вложения выбранных сообщений, медиа диалога (`chat_media` не
  из корзины) и файлы результатов MCP выбранных ответов одним списком со ссылками на прежние маршруты
  (`/api/media/{id}`, `…/messages/{mid}/files/{step}/{n}`): их проверки — владелец, блокировка раздела, сервер и
  его ключ — действуют как прежде.

Только своё: чужой диалог — 404 (`chat._chat_row`), статистика — по `chats.user_id` сессии; администратор чужого не
видит. В журнал не пишет.
"""
import json
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Query, Request

import chat
import clock
import mcp_files
import media
import state

router = APIRouter(prefix="/api")

STATS_DAYS = 31        # дней «Статистики» (спека, «Числа»)


def _url(media_id: str, mime: str) -> str:
    """Картинку страница открывает окном — ссылка без скачивания; остальное — скачать с именем файла."""
    return f"/api/media/{media_id}" + ("" if mime.startswith("image/") else "?download=1")


def _add(total: "int | None", value) -> "int | None":
    return total if not isinstance(value, int) else (total or 0) + value


@router.get("/chats/stats")
async def stats(request: Request, days: int = Query(STATS_DAYS, ge=1, le=STATS_DAYS)):
    """`{days: [{date, model, answers, in, out, gen_ms}]}` — новые дни сверху, внутри дня — по модели; `null` — ни у
    одного ответа группы числа нет. Окно — с полуночи дня «сегодня − (days − 1)» в поясе учётки."""
    uid = request.state.user.id
    zone = media.user_zone(uid)
    today = datetime.fromtimestamp(clock.NOW(), zone).date()
    start = datetime.combine(today - timedelta(days=days - 1), time.min, tzinfo=zone).timestamp()
    groups: dict[tuple[str, str], dict] = {}
    for row in state.DB.execute(
            "SELECT m.created, m.model, m.usage, m.gen_ms FROM messages m JOIN chats c ON c.id = m.chat_id"
            " WHERE c.user_id = ? AND c.deleted IS NULL AND m.role = 'assistant' AND m.status <> 'streaming'"
            " AND m.created >= ?", (uid, start)):
        day = datetime.fromtimestamp(row["created"], zone).date().isoformat()
        model = row["model"] or ""
        group = groups.setdefault((day, model), {"date": day, "model": model, "answers": 0, "in": None, "out": None,
                                                 "gen_ms": None})
        usage = json.loads(row["usage"]) if row["usage"] else {}
        group["answers"] += 1
        group["in"] = _add(group["in"], usage.get("in"))
        group["out"] = _add(group["out"], usage.get("out"))
        group["gen_ms"] = _add(group["gen_ms"], row["gen_ms"])
    ordered = sorted(groups.values(), key=lambda g: g["model"])
    ordered.sort(key=lambda g: g["date"], reverse=True)
    return {"days": ordered}


@router.get("/chats/{chat_id}/files")
async def files(chat_id: str, request: Request):
    """`{items: [{kind, name, size, mime, url}]}`: `kind` — `attachment`, `media` или `result`; `size` у файла
    результата MCP — null (размер знает только сервер)."""
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    items = [{"kind": "attachment", "name": a["name"], "size": a["size"], "mime": a["mime"],
              "url": _url(a["media_id"], a["mime"])} for a in state.DB.execute(
        "SELECT a.name, a.media_id, md.size, md.mime FROM attachments a JOIN messages m ON m.id = a.message_id"
        " JOIN media md ON md.id = a.media_id AND md.user_id = a.user_id WHERE m.chat_id = ? AND m.unchosen = 0"
        " ORDER BY m.created, m.rowid, a.created, a.rowid", (chat_id,))]
    items += [{"kind": "media", "name": m["name"], "size": m["size"], "mime": m["mime"],
               "url": _url(m["media_id"], m["mime"])} for m in state.DB.execute(
        "SELECT name, size, mime, media_id FROM chat_media WHERE chat_id = ? AND deleted IS NULL"
        " ORDER BY created, rowid", (chat_id,))]
    servers = None
    for message in state.DB.execute(
            "SELECT id, tools FROM messages WHERE chat_id = ? AND role = 'assistant' AND unchosen = 0"
            " AND tools IS NOT NULL ORDER BY created, rowid", (chat_id,)).fetchall():
        for i, step in enumerate(json.loads(message["tools"] or "[]")):
            if not isinstance(step, dict):
                continue
            if servers is None:
                servers = mcp_files.chat_result_servers(uid, row)
            found = mcp_files.chat_result(uid, row, step, servers=servers).get("files")
            for n, entry in enumerate(found if isinstance(found, list) else []):
                if isinstance(entry, dict):
                    items.append({"kind": "result", "name": entry.get("name", ""), "size": None,
                                  "mime": entry.get("mime", ""),
                                  "url": f"/api/chats/{chat_id}/messages/{message['id']}/files/{i}/{n}"})
    return {"items": items}
