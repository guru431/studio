"""Корзина (шаг схемы 58): удалённая запись истории сначала получает `deleted` — время переноса — и уходит из
лент и поиска (триггеры `search_<таблица>_hide` и фильтр `search.TRASHED`); окончательно её удаляет уборка через
TRASH_SEC или владелец.

- Таблицы с корзиной — `TABLES` (тот же набор, что `db._TRASH_58` шага 58). Вид записи корзины — `KINDS`
  (ключ — таблица): раздел, окончательное удаление `purge(user_id, id)` раздела, название строки, её файлы.
  Новый раздел — строка в `KINDS`. Чат озвучки, удалённый целиком, — одна запись (`Kind.group`: строки с тем же
  `chat_id` и той же отметкой `deleted`).
- `GET /api/trash` → `[{kind, section, id, title, deleted, bytes}]` своих (новые сверху; разделы, закрытые
  блокировкой, — мимо); `POST /api/trash/{kind}/{id}/restore` — вернуть (строки поиска — `search.reindex_item`);
  `DELETE /api/trash/{kind}/{id} {password}` — окончательно одну (группу — всю, по id любой её строки). Чужая запись и запись не в корзине — 404: администратор
  чужую корзину не видит и не возвращает. «Вернуть» и «Удалить навсегда» закрыты, пока заперт любой раздел
  (`lock_policy.blocks`).
- `POST /api/trash/empty {password}` — окончательно всё своё; пароль — `auth.confirm_password` (те же проверка и
  паузы, что у выключения блокировки), как у `DELETE /api/history` (account_profile.py).
- «Хранилище» (`GET /api/me/storage`, account_profile.py): `storage` — самые тяжёлые живые записи (HEAVIEST, по сумме
  `media.size` их файлов), корзина и неудачные по разделам; `move_failed` — неудачные и отменённые без содержимого
  пользователя (FAILED) в корзину.
- Уборка `start_purger`: раз в PURGE_EVERY_SEC (отметка — `settings` PURGE_KEY, переживает перезапуски, как
  `media.orphans_due`) — `purge` записей старше TRASH_SEC.
- Прежний код (до шага 58) столбец `deleted` не читает и показывает записи корзины как обычные: перед откатом
  `ROLLBACK_58` проверяет, что нет живых диалогов с защищёнными данными, выключает защищённые серверы MCP
  и окончательно удаляет корзину (с каскадами). Файлы потом убирает чистка сирот прежнего кода.
- Журнал — вид, начало id и числа; названий и имён файлов нет.
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

import auth
import chat
import clock
import images
import knowledge
import lock_policy
import mcp_files
import ocr
import operations
import screenlock
import search
import speeches
import state
import transcripts
import translate
import videos
import voices

TABLES = ("chats", "images", "videos", "speeches", "transcripts", "scans", "translations", "voices",
          "chat_media", "knowledge_bases", "kb_documents")

ROLLBACK_MCP_KEY = "rollback_58_protected_mcp"
_ROLLBACK_ALLOWED = "EXISTS (SELECT 1 FROM temp.rollback_58_guard)"
ROLLBACK_58 = f"""BEGIN;
CREATE TEMP TABLE rollback_58_guard (no_protected_chats INTEGER CHECK (no_protected_chats = 1));
INSERT OR ROLLBACK INTO rollback_58_guard SELECT NOT EXISTS (
    SELECT 1 FROM chats WHERE protected_used = 1 AND deleted IS NULL
);
INSERT OR IGNORE INTO settings(key, value)
    SELECT '{ROLLBACK_MCP_KEY}', json_group_array(id) FROM mcp_servers
    WHERE protected = 1 AND enabled = 1 AND {_ROLLBACK_ALLOWED}
    HAVING count(*) > 0;
UPDATE mcp_servers SET enabled = 0 WHERE protected = 1 AND {_ROLLBACK_ALLOWED};
""" + "".join(f"DELETE FROM {t} WHERE deleted IS NOT NULL AND {_ROLLBACK_ALLOWED};\n" for t in TABLES) + """
DROP TABLE temp.rollback_58_guard;
COMMIT;
"""

TRASH_SEC = 14 * 86400
PURGE_EVERY_SEC = 86400
CHECK_SEC = 3600                 # как часто уборка смотрит на отметку
PURGE_KEY = "trash"              # settings: {at, purged} последнего прохода уборки
GONE = {"code": "gone", "message": "такой записи в корзине нет"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/trash")


def forward() -> None:
    """Вернуть включённые до отката защищённые MCP при запуске версии с их защитой."""
    row = state.DB.execute("SELECT value FROM settings WHERE key = ?", (ROLLBACK_MCP_KEY,)).fetchone()
    if row is None:
        return
    state.DB.execute("BEGIN")
    try:
        changed = state.DB.execute(
            "UPDATE mcp_servers SET enabled = 1 WHERE protected = 1 AND enabled = 0"
            " AND id IN (SELECT value FROM json_each(?))", (row["value"],)).rowcount
        state.DB.execute("DELETE FROM settings WHERE key = ?", (ROLLBACK_MCP_KEY,))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("корзина: после отката возвращено защищённых MCP %d", changed)


@dataclass(frozen=True)
class Kind:
    table: str
    section: str                                  # раздел страницы (`sections-meta.js`) и блокировки
    purge: Callable[[int, str], Awaitable[None]]  # окончательное удаление раздела; записи нет — ничего
    title: Callable[[object], str | None]         # название строки таблицы для списка
    files: str                                    # SQL: id файлов записи (`:id`), для `bytes`
    search: "str | None" = None                   # раздел поиска (`search.TITLES`), если запись в индексе
    live: str = ""                                # условие строки, которую видно и можно вернуть
    group: str = ""                               # столбец группы: строки с тем же значением и той же отметкой
                                                  # `deleted` (удалены одним действием) — одна запись корзины
    column: str = "name"                          # столбец, который читает `title`: списки корзины и «Хранилища»
                                                  # не читают полных текстов записей на цикле событий
    fallback: str = ""


HEAD = 80                                         # знаков текста записи в названии строки корзины
NO_FILES = "SELECT NULL WHERE :id IS NULL"        # у записи нет файлов


def _head(text: "str | None") -> str:
    text = " ".join((text or "").split())
    return text[:HEAD].rstrip() + "…" if len(text) > HEAD else text


KINDS: dict[str, Kind] = {
    "chats": Kind("chats", "chat", chat.purge, lambda row: row["title"],
                  "SELECT a.media_id FROM attachments a JOIN messages m ON m.id = a.message_id WHERE m.chat_id = :id"
                  " UNION SELECT media_id FROM chat_media WHERE chat_id = :id", search="chat", column="title",
                  fallback="Новый диалог"),
    # медиа диалога в корзине вместе с диалогом — часть его строки, не отдельная
    "chat_media": Kind("chat_media", "chat", mcp_files.purge_media, lambda row: row["name"],
                       "SELECT media_id FROM chat_media WHERE id = :id",
                       live=" AND chat_id IN (SELECT id FROM chats WHERE deleted IS NULL)"),
    "images": Kind("images", "photo", images.purge, lambda row: _head(row["prompt"]),
                   "SELECT media_id FROM images WHERE id = :id UNION SELECT source_media_id FROM images WHERE id = :id"
                   " UNION SELECT mask_media_id FROM images WHERE id = :id", search="photo", column="prompt",
                   fallback="Картинка"),
    "videos": Kind("videos", "video", videos.purge, lambda row: _head(row["prompt"]),
                   "SELECT media_id FROM videos WHERE id = :id UNION SELECT source_media_id FROM videos WHERE id = :id",
                   search="video", column="prompt", fallback="Ролик"),
    # чат озвучки, удалённый целиком («Удалить чат»), — одна запись корзины: возвращается и удаляется весь
    "speeches": Kind("speeches", "speech", speeches.purge, lambda row: _head(row["text"]),
                     "SELECT media_id FROM speeches WHERE id = :id", search="speech", group="chat_id", column="text",
                     fallback="Озвучка"),
    "transcripts": Kind("transcripts", "transcribe", transcripts.purge, lambda row: _head(row["name"]),
                        "SELECT source_media_id FROM transcripts WHERE id = :id"
                        " UNION SELECT result_media_id FROM transcripts WHERE id = :id", search="transcribe", fallback="Запись"),
    "scans": Kind("scans", "ocr", ocr.purge, lambda row: _head(row["name"]),
                  "SELECT source_media_id FROM scans WHERE id = :id UNION SELECT media_id FROM scan_pages WHERE scan_id = :id",
                  search="ocr", fallback="Документ"),
    "translations": Kind("translations", "translate", translate.purge, lambda row: _head(row["name"]),
                         "SELECT source_media_id FROM translations WHERE id = :id", search="translate", fallback="Перевод"),
    "voices": Kind("voices", "speech", voices.purge, lambda row: _head(row["name"]),
                   "SELECT media_id FROM voices WHERE id = :id UNION SELECT media_id FROM voice_samples WHERE voice_id = :id",
                   fallback="Голос"),
    # личные базы знаний и их документы (общие удаляет панель сразу); файлов нет — только куски текста.
    # Документ базы в корзине вместе с базой — часть её строки
    "knowledge_bases": Kind("knowledge_bases", "chat", knowledge.purge_base, lambda row: _head(row["name"]),
                            NO_FILES, fallback="База знаний"),
    "kb_documents": Kind("kb_documents", "chat", knowledge.purge_document, lambda row: _head(row["name"]),
                         NO_FILES, live=" AND (kb_id IS NULL OR kb_id IN"
                                        " (SELECT id FROM knowledge_bases WHERE deleted IS NULL))", fallback="Документ"),
}


def _kind(name: str) -> Kind:
    if name not in KINDS:
        raise HTTPException(404, GONE)
    return KINDS[name]


def _bytes(kind: Kind, user_id: int, item_id: str) -> int:
    return state.DB.execute(f"SELECT COALESCE(SUM(size), 0) FROM media WHERE user_id = :uid AND id IN ({kind.files})",
                            {"uid": user_id, "id": item_id}).fetchone()[0]


def _members(kind: Kind, uid: int, item_id: str, live: str = "") -> list[str]:
    """id строк записи корзины `item_id`: она сама, а у вида с `group` — все строки её группы (тот же столбец группы
    и та же отметка `deleted`). Чужая или не в корзине — []."""
    row = state.DB.execute(f"SELECT deleted{', ' + kind.group if kind.group else ''} FROM {kind.table}"
                           f" WHERE id = ? AND user_id = ? AND deleted IS NOT NULL{live}", (item_id, uid)).fetchone()
    if row is None or not kind.group:
        return [] if row is None else [item_id]
    return [r["id"] for r in state.DB.execute(f"SELECT id FROM {kind.table} WHERE user_id = ? AND deleted = ?"
                                              f" AND {kind.group} IS ?", (uid, row["deleted"], row[kind.group]))]


def _locked(kind: Kind, uid: int, hidden) -> "tuple[str, tuple]":
    """Условие (`AND …`) и аргументы: строки вида, закрытые блокировкой сверх своего раздела, — сканы запертых
    «Чата» и «Перевода» (`lock_policy.hidden_scans`, как лента и поиск «Сканов»: имя документа — содержимое)."""
    if kind.table != "scans":
        return "", ()
    sub, args = lock_policy.hidden_scans(uid, hidden)
    return (f" AND id NOT IN ({sub})", args) if sub else ("", ())


@router.get("")
async def list_trash(request: Request):
    """Группа (`Kind.group`) — одна строка: id и название первой записи, объём — всех."""
    uid = request.state.user.id
    hidden = lock_policy.hidden(request.state.digest)
    items = []
    for name, kind in KINDS.items():
        if kind.section in hidden:
            continue
        locked, args = _locked(kind, uid, hidden)
        groups = {}
        cols = ", ".join(dict.fromkeys(("id", "deleted", *([kind.group] if kind.group else []), kind.column)))
        for row in state.DB.execute(f"SELECT {cols} FROM {kind.table} WHERE user_id = ? AND deleted IS NOT NULL"
                                    f"{kind.live}{locked} ORDER BY created, id", (uid, *args)).fetchall():
            key = (row[kind.group], row["deleted"]) if kind.group else row["id"]
            size = _bytes(kind, uid, row["id"])
            if key in groups:
                groups[key]["bytes"] += size
                continue
            title = kind.title(row)
            groups[key] = {"kind": name, "section": kind.section, "id": row["id"], "title": title or kind.fallback,
                           "deleted": row["deleted"], "bytes": size}
            if not title and kind.fallback:
                groups[key]["title_default"] = True
            items.append(groups[key])
    items.sort(key=lambda item: item["deleted"], reverse=True)
    return items


@router.post("/{kind}/{item_id}/restore", status_code=204)
async def restore(kind: str, item_id: str, request: Request):
    uid, found = request.state.user.id, _kind(kind)
    operations.check(uid)
    ids = _members(found, uid, item_id, found.live)
    if not ids:
        raise HTTPException(404, GONE)
    state.DB.execute(f"UPDATE {found.table} SET deleted = NULL WHERE user_id = ?"
                     " AND id IN (SELECT value FROM json_each(?))", (uid, json.dumps(ids)))
    if found.search is not None:
        for one in ids:
            search.reindex_item(found.search, one)
    log.info("корзина: %s %s возвращено, строк %d", kind, item_id[:8], len(ids))
    return Response(status_code=204)


class Confirm(BaseModel):
    """Пароль учётки для окончательного удаления."""
    password: str = Field("", max_length=screenlock.PASSWORD_MAX)


@router.delete("/{kind}/{item_id}", status_code=204)
async def delete_forever(kind: str, item_id: str, request: Request, body: Confirm | None = None):
    uid, found = request.state.user.id, _kind(kind)
    confirmed = set(_members(found, uid, item_id))
    if not confirmed:
        raise HTTPException(404, GONE)
    await auth.confirm_password(uid, body.password if body else "", "удаление записи корзины",
                                digest=request.state.digest)
    operations.check(uid)
    ids = [member for member in _members(found, uid, item_id) if member in confirmed]
    for one in ids:
        # перед каждой строкой группы — заново: пока удалялась прежняя, группу могли вернуть
        if state.DB.execute(f"SELECT 1 FROM {found.table} WHERE id = ? AND deleted IS NOT NULL",
                            (one,)).fetchone() is not None:
            await found.purge(uid, one)
    log.info("корзина: %s %s удалено навсегда, строк %d", kind, item_id[:8], len(ids))
    return Response(status_code=204)


@router.post("/empty")
async def empty(body: Confirm, request: Request):
    """Окончательно всё своё из корзины. Запись, которая сейчас не удаляется (скан перевода ещё останавливается —
    409), остаётся в корзине: `left`."""
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "очистка корзины", digest=request.state.digest)
    done = left = 0
    for name, kind in KINDS.items():
        for row in state.DB.execute(f"SELECT id FROM {kind.table} WHERE user_id = ? AND deleted IS NOT NULL",
                                    (uid,)).fetchall():
            # перед каждой — заново: окончательное удаление прежней могло унести и эту (медиа диалога, скан перевода)
            if state.DB.execute(f"SELECT 1 FROM {kind.table} WHERE id = ? AND deleted IS NOT NULL",
                                (row["id"],)).fetchone() is None:
                continue
            try:
                await kind.purge(uid, row["id"])
                done += 1
            except HTTPException:
                left += 1
    log.info("пользователь %d: корзина очищена — %d, осталось %d", uid, done, left)
    return {"purged": done, **({"left": left} if left else {})}


# ---- «Хранилище» ----------------------------------------------------------------

HEAVIEST = 50
#: неудачные и отменённые записи по таблицам — только без содержимого пользователя: «Хранилище» переносит их в
#: корзину разом. Остаются: озвучка с прежним звуком после сбоя повтора, расшифровка с единственной ссылкой на
#: запись (другая живая попытка той же записи — не неудачная), скан с распознанными страницами, перевод с
#: переведёнными кусками. Условие — SQL по строке таблицы без псевдонима (его берут и SELECT, и UPDATE)
FAILED = {
    "images": "state IN ('error', 'cancelled') AND media_id IS NULL",
    "videos": "state IN ('failed', 'cancelled') AND media_id IS NULL",
    "speeches": "state IN ('error', 'cancelled') AND media_id IS NULL AND previous_media_id IS NULL",
    "transcripts": "state IN ('error', 'cancelled') AND result_media_id IS NULL"
                   " AND (NOT EXISTS (SELECT 1 FROM media WHERE id = transcripts.source_media_id)"
                   " OR EXISTS (SELECT 1 FROM transcripts o WHERE o.source_media_id = transcripts.source_media_id"
                   " AND o.id != transcripts.id AND o.deleted IS NULL AND o.state NOT IN ('error', 'cancelled')))",
    "scans": "state IN ('error', 'cancelled') AND NOT EXISTS (SELECT 1 FROM scan_pages WHERE scan_id = scans.id"
             " AND state IN ('done', 'layer'))",
    "translations": "state IN ('error', 'cancelled') AND done_parts = 0",
}
FAILED_SECTIONS = {KINDS[table].section: table for table in FAILED}


def _sized(kind: Kind) -> str:
    """SQL: сумма `media.size` файлов строки `t` (тот же запрос файлов, что у `_bytes`, по `t.id`)."""
    return (f"(SELECT COALESCE(SUM(size), 0) FROM media WHERE user_id = t.user_id"
            f" AND id IN ({kind.files.replace(':id', 't.id')}))")


def storage(user_id: int, hidden: "set[str] | frozenset[str]") -> dict:
    """`heaviest` — без разделов, закрытых блокировкой (названия — содержимое); медиа диалога — в весе диалога."""
    heaviest, trashed, failed = [], {}, {}
    for name, kind in KINDS.items():
        if not kind.live and kind.section not in hidden:
            locked, args = _locked(kind, user_id, hidden)
            heaviest += [{"kind": name, "section": kind.section, "id": row["id"], "title": kind.title(row) or kind.fallback,
                          "bytes": row["bytes"]}
                         for row in state.DB.execute(
                             f"SELECT * FROM (SELECT t.id, t.{kind.column}, {_sized(kind)} AS bytes FROM {kind.table} t"
                             f" WHERE t.user_id = ? AND t.deleted IS NULL{locked}) WHERE bytes > 0"
                             " ORDER BY bytes DESC LIMIT ?", (user_id, *args, HEAVIEST))]
        group = f"t.{kind.group}, t.deleted" if kind.group else "t.id"
        count, size = state.DB.execute(
            f"SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM ("
            f"SELECT SUM({_sized(kind)}) AS bytes FROM {kind.table} t"
            f" WHERE t.user_id = ? AND t.deleted IS NOT NULL{kind.live} GROUP BY {group})", (user_id,)).fetchone()
        if count:
            got = trashed.setdefault(kind.section, {"count": 0, "bytes": 0})
            got["count"] += count
            got["bytes"] += size
    for table, where in FAILED.items():
        count = state.DB.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ? AND deleted IS NULL AND {where}",
                                 (user_id,)).fetchone()[0]
        if count:
            failed[KINDS[table].section] = count
    heaviest.sort(key=lambda item: item["bytes"], reverse=True)
    return {"heaviest": heaviest[:HEAVIEST], "trash": trashed, "failed": failed}


def move_failed(user_id: int, sections: list[str]) -> int:
    """Неудачные и отменённые записи разделов без содержимого (FAILED) — в корзину (кончившиеся: идущих среди них
    нет). Итог — сколько."""
    moved, now = 0, clock.NOW()
    for section in dict.fromkeys(sections):
        table = FAILED_SECTIONS[section]
        moved += state.DB.execute(f"UPDATE {table} SET deleted = ? WHERE user_id = ? AND deleted IS NULL"
                                  f" AND {FAILED[table]}", (now, user_id)).rowcount
    log.info("пользователь %d: неудачные в корзину — %d", user_id, moved)
    return moved


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
