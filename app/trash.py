"""Корзина (шаг схемы 58): удалённая запись истории сначала получает `deleted` — время переноса — и уходит из
лент и поиска (триггеры `search_<таблица>_hide` и фильтр `search.TRASHED`); окончательно её удаляет уборка через
TRASH_SEC или владелец.

- Таблицы с корзиной — `TABLES` (тот же набор, что `db._TRASH_58` шага 58). Вид записи корзины — `KINDS`
  (ключ — таблица): раздел, окончательное удаление `purge(user_id, id)` раздела, название строки, её файлы.
  Новый раздел — строка в `KINDS`.
- `GET /api/trash` → `[{kind, section, id, title, deleted, bytes}]` своих (новые сверху; разделы, закрытые
  блокировкой, — мимо); `POST /api/trash/{kind}/{id}/restore` — вернуть (строки поиска — `search.reindex_item`);
  `DELETE /api/trash/{kind}/{id}` — окончательно одну. Чужая запись и запись не в корзине — 404: администратор
  чужую корзину не видит и не возвращает. «Вернуть» и «Удалить навсегда» закрыты, пока заперт любой раздел
  (`lock_policy.blocks`).
- Уборка `start_purger`: раз в PURGE_EVERY_SEC (отметка — `settings` PURGE_KEY, переживает перезапуски, как
  `media.orphans_due`) — `purge` записей старше TRASH_SEC.
- Прежний код (до шага 58) столбец `deleted` не читает и показывает записи корзины как обычные: перед откатом
  `ROLLBACK_58` окончательно удаляет корзину (с каскадами), файлы потом убирает чистка сирот прежнего кода.
- Журнал — вид, начало id и числа; названий и имён файлов нет.
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, Response

import chat
import clock
import images
import lock_policy
import mcp_files
import operations
import search
import speeches
import state
import videos

TABLES = ("chats", "images", "videos", "speeches", "transcripts", "scans", "translations", "voices",
          "chat_media", "knowledge_bases", "kb_documents")

ROLLBACK_58 = "BEGIN;\n" + "".join(f"DELETE FROM {t} WHERE deleted IS NOT NULL;\n" for t in TABLES) + "COMMIT;\n"

TRASH_SEC = 14 * 86400
PURGE_EVERY_SEC = 86400
CHECK_SEC = 3600                 # как часто уборка смотрит на отметку
PURGE_KEY = "trash"              # settings: {at, purged} последнего прохода уборки
GONE = {"code": "gone", "message": "такой записи в корзине нет"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/trash")


@dataclass(frozen=True)
class Kind:
    table: str
    section: str                                  # раздел страницы (`sections-meta.js`) и блокировки
    purge: Callable[[int, str], Awaitable[None]]  # окончательное удаление раздела; записи нет — ничего
    title: Callable[[object], str]                # название строки таблицы для списка
    files: str                                    # SQL: id файлов записи (`:id`), для `bytes`
    search: "str | None" = None                   # раздел поиска (`search.TITLES`), если запись в индексе
    live: str = ""                                # условие строки, которую видно и можно вернуть


HEAD = 80                                         # знаков текста записи в названии строки корзины


def _head(text: "str | None", fallback: str) -> str:
    text = " ".join((text or "").split())
    return (text[:HEAD].rstrip() + "…" if len(text) > HEAD else text) or fallback


KINDS: dict[str, Kind] = {
    "chats": Kind("chats", "chat", chat.purge, lambda row: row["title"] or "Новый диалог",
                  "SELECT a.media_id FROM attachments a JOIN messages m ON m.id = a.message_id WHERE m.chat_id = :id"
                  " UNION SELECT media_id FROM chat_media WHERE chat_id = :id", search="chat"),
    # медиа диалога в корзине вместе с диалогом — часть его строки, не отдельная
    "chat_media": Kind("chat_media", "chat", mcp_files.purge_media, lambda row: row["name"],
                       "SELECT media_id FROM chat_media WHERE id = :id",
                       live=" AND chat_id IN (SELECT id FROM chats WHERE deleted IS NULL)"),
    "images": Kind("images", "photo", images.purge, lambda row: _head(row["prompt"], "Картинка"),
                   "SELECT media_id FROM images WHERE id = :id UNION SELECT source_media_id FROM images WHERE id = :id"
                   " UNION SELECT mask_media_id FROM images WHERE id = :id", search="photo"),
    "videos": Kind("videos", "video", videos.purge, lambda row: _head(row["prompt"], "Ролик"),
                   "SELECT media_id FROM videos WHERE id = :id UNION SELECT source_media_id FROM videos WHERE id = :id",
                   search="video"),
    "speeches": Kind("speeches", "speech", speeches.purge, lambda row: _head(row["text"], "Озвучка"),
                     "SELECT media_id FROM speeches WHERE id = :id", search="speech"),
}


def _kind(name: str) -> Kind:
    if name not in KINDS:
        raise HTTPException(404, GONE)
    return KINDS[name]


def _bytes(kind: Kind, user_id: int, item_id: str) -> int:
    return state.DB.execute(f"SELECT COALESCE(SUM(size), 0) FROM media WHERE user_id = :uid AND id IN ({kind.files})",
                            {"uid": user_id, "id": item_id}).fetchone()[0]


@router.get("")
async def list_trash(request: Request):
    uid = request.state.user.id
    hidden = lock_policy.hidden(request.state.digest)
    items = []
    for name, kind in KINDS.items():
        if kind.section in hidden:
            continue
        for row in state.DB.execute(f"SELECT * FROM {kind.table} WHERE user_id = ? AND deleted IS NOT NULL{kind.live}",
                                    (uid,)).fetchall():
            items.append({"kind": name, "section": kind.section, "id": row["id"], "title": kind.title(row),
                          "deleted": row["deleted"], "bytes": _bytes(kind, uid, row["id"])})
    items.sort(key=lambda item: item["deleted"], reverse=True)
    return items


@router.post("/{kind}/{item_id}/restore", status_code=204)
async def restore(kind: str, item_id: str, request: Request):
    uid, found = request.state.user.id, _kind(kind)
    operations.check(uid)
    if state.DB.execute(f"UPDATE {found.table} SET deleted = NULL WHERE id = ? AND user_id = ?"
                        f" AND deleted IS NOT NULL{found.live}", (item_id, uid)).rowcount != 1:
        raise HTTPException(404, GONE)
    if found.search is not None:
        search.reindex_item(found.search, item_id)
    log.info("корзина: %s %s возвращено", kind, item_id[:8])
    return Response(status_code=204)


@router.delete("/{kind}/{item_id}", status_code=204)
async def delete_forever(kind: str, item_id: str, request: Request):
    uid, found = request.state.user.id, _kind(kind)
    if state.DB.execute(f"SELECT 1 FROM {found.table} WHERE id = ? AND user_id = ? AND deleted IS NOT NULL",
                        (item_id, uid)).fetchone() is None:
        raise HTTPException(404, GONE)
    await found.purge(uid, item_id)
    log.info("корзина: %s %s удалено навсегда", kind, item_id[:8])
    return Response(status_code=204)


# ---- уборка ---------------------------------------------------------------------

def last_purge() -> "dict | None":
    row = state.DB.execute("SELECT value FROM settings WHERE key = ?", (PURGE_KEY,)).fetchone()
    return json.loads(row["value"]) if row else None


def purge_due_now() -> bool:
    last = last_purge()
    return last is None or clock.NOW() - last["at"] >= PURGE_EVERY_SEC


async def purge_expired() -> int:
    """Записи корзины старше TRASH_SEC — окончательно (`Kind.purge`); перед каждой — проверка заново: её могли
    вернуть, пока шла предыдущая. Сбой одной записи не останавливает прочие. Итог — число удалённых."""
    edge, done = clock.NOW() - TRASH_SEC, 0
    for name, kind in KINDS.items():
        rows = state.DB.execute(f"SELECT id, user_id FROM {kind.table} WHERE deleted IS NOT NULL AND deleted < ?",
                                (edge,)).fetchall()
        for row in rows:
            if state.DB.execute(f"SELECT 1 FROM {kind.table} WHERE id = ? AND deleted IS NOT NULL AND deleted < ?",
                                (row["id"], edge)).fetchone() is None:
                continue
            try:
                await kind.purge(row["user_id"], row["id"])
                done += 1
            except Exception as e:                   # noqa: BLE001 — уборка не должна вставать
                log.warning("корзина: %s %s — не удалено (%s)", name, row["id"][:8], type(e).__name__)
    state.DB.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                     (PURGE_KEY, json.dumps({"at": clock.NOW(), "purged": done})))
    if done:
        log.info("корзина: удалено по сроку — %d", done)
    return done


async def _purge_loop() -> None:
    while True:
        try:
            if purge_due_now():
                await purge_expired()
        except Exception as e:                       # noqa: BLE001
            log.warning("корзина: сбой уборки (%s)", type(e).__name__)
        await clock.SLEEP(CHECK_SEC)


def start_purger() -> asyncio.Task:
    """Первый взгляд на отметку — сразу при старте службы."""
    return asyncio.create_task(_purge_loop())
