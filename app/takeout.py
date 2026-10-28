"""Выгрузка своей истории одним ZIP (спека 2026-10-21-studio-export-restore-check, раздел 5).

- `POST /api/me/export` `{"sections": [...]}` (подмножество SECTIONS) → `{id, files, bytes}`: токен
  в памяти службы (EXPORTS, как media.ARCHIVES) на EXPORT_TTL_SEC, живых — не больше EXPORT_KEEP на
  пользователя; files и bytes — оценка по строкам media выбранных разделов.
- `GET /api/me/export/<токен>` — ZIP_STORED потоком (писатель — media.ZipWriter): README.txt,
  settings.json, `chats/<id>.json` (диалог — своим файлом), разделы страницами по PAGE записей
  (`images/0001.json` …; пустой раздел — одна пустая страница), `files/<media id><расширение>`,
  последним — manifest.json с sha256 всего записанного. Идёт не больше одной выгрузки у пользователя
  (RUNNING): вторая загрузка, пока идёт первая, — 409 `busy` словами.
- Всё — своё (`user_id` сессии). Нет токенов подключений, хеша пароля, сессий, CSRF, passkey, хешей ключей
  устройств Claude Code (`agents` не выгружается — только `agent_events`) и подписок push.
- База — только с цикла событий (одна state.DB): генератор асинхронный, строки читает порциями —
  диалог, страница раздела. JSON, CRC-32 и sha256 — в потоке (asyncio.to_thread, `_pack`): страница
  картинок с цепочками — мегабайты, и цикл событий (потоки чата всех) их не ждёт; заголовок записи и
  её данные уходят разными кусками, без склейки. Файл открывается один раз (media.open_summed):
  первый проход — суммы, второй — данные с того же дескриптора.
- Это не снимок: строка, ушедшая во время выгрузки, пропускается, её файл — в manifest.missing. Файла
  нет и каталога шары тоже нет (шару отмонтировали) — обрыв ответа, а не «целый» архив без файлов.
- Шару проверяют, только если есть файлы: выгрузка одних диалогов законна и без неё.
- Журнал — «export: N файлов» и предупреждение на сбой, без имён, путей и промптов.
"""
import asyncio
import hashlib
import json
import logging
import secrets
import weakref
import zlib
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import chains
import clock
import db
import media
import state

#: порядок вкладок студии; Claude Code — в конце: отметки чатов (code) и история подключений (agents)
SECTIONS = ("chat", "photo", "video", "transcribe", "speech", "voices", "ocr", "translate", "code", "agents")
#: большая выгрузка качается дольше 10 минут архива выделенного
EXPORT_TTL_SEC = 3600
EXPORT_KEEP = 2
#: блок чтения файла — один шаг потока
CHUNK = 1 << 20
#: записей раздела на страницу JSON: пик памяти — страница (10k картинок с цепочками — 64 МиБ JSON)
PAGE = 500
#: токен → (user_id, разделы, время создания)
EXPORTS: dict[str, tuple[int, tuple[str, ...], float]] = {}
#: user_id → метка идущей выгрузки: одна за раз (снимает конец генератора или его уход из памяти)
RUNNING: dict[int, object] = {}
#: раздел → (таблица, столбцы со ссылкой на media, условие); таблицы и столбцы — из кода
REFS = {
    "chat": ("attachments", ("media_id",), " AND message_id IS NOT NULL"),
    "photo": ("images", ("media_id", "source_media_id", "mask_media_id"), ""),
    "video": ("videos", ("media_id", "source_media_id"), ""),
    "transcribe": ("transcripts", ("source_media_id", "result_media_id"), ""),
    "ocr": ("scans", ("source_media_id",), ""),
    "speech": ("speeches", ("media_id",), ""),
    "voices": ("voices", ("media_id",), ""),
    "translate": ("translations", ("source_media_id",), ""),
}
#: вторые таблицы раздела с файлами — так же, со своим user_id (картинки и фото страниц распознавания)
MORE_REFS = {"ocr": (("scan_pages", ("media_id",), ""),)}
STORAGE = {"code": "storage", "message": "хранилище недоступно"}
BUSY = {"code": "busy", "message": "выгрузка уже идёт — дождитесь, пока она скачается"}

README = """Выгрузка истории студии

Что внутри:
- settings.json — профиль (логин, имя, тема, виды лент), подключения (адреса без ключей), разделы
  (подключения и отмеченные модели, умолчания) и пресеты.
- chats/<id>.json — диалог: сообщения по порядку, вложения (текст документа и ссылка на файл), у ответов —
  найденное «Интернетом» (tools: поиски, страницы, субтитры с их текстом) и рассуждение модели (reasoning,
  reasoning_sec — секунды).
- images/0001.json, images/0002.json … — картинки страницами по 500: промпт, модель, размер, seed,
  цепочка промптов, файлы итога, исходника, маски.
- videos/ — ролики, transcripts/ — расшифровки (и правки, если были), speeches/ — озвучки, voices/ —
  образцы голосов, так же страницами. Файлы — ссылками «files/…».
- scans/ — распознанные документы, так же страницами: имя, состояние, страницы с текстом (Markdown) и
  состоянием; исходный PDF и картинки страниц — ссылками «files/…».
- translations/0001.json … — переводы, так же страницами: вид (текст, файл, снимок), языки (выбранные,
  определённый, итоговый), исходный текст и перевод, куски по порядку (у снимка — рамка блока в долях
  0…1000) с состоянием; исходный файл или снимок — ссылкой «files/…».
- code_chats/ — чаты Claude Code: компьютер, проект (id), номер сессии, открыт или закрыт; текста нет — он на компьютере.
- agent_events/ — история подключений компьютеров Claude Code.
- files/<id>.<расширение> — сами файлы (без миниатюр).
- manifest.json — последним: что в архиве, сколько записей в разделах (counts), sha256 каждого файла
  (files) и каждой записи JSON и этого описания (entries), id файлов, которых на хранилище уже не
  было (missing).

Ключей подключений, ключа Exa, ключей устройств, пароля и сессий в выгрузке нет. Обратно в студию выгрузка не загружается —
это формат для чтения и хранения. Время — секунды Unix (UTC).
"""

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class ExportIn(BaseModel):
    sections: list[str]


# ---- чтение базы (цикл событий) --------------------------------------------------

def _maybe_json(text):
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return text


def _media_rows(user_id: int, sections: tuple[str, ...]) -> list:
    """Свои строки media, на которые ссылаются записи выбранных разделов, — по времени."""
    parts = [f"SELECT {col} FROM {table} WHERE user_id = ?{extra}"
             for table, cols, extra in (r for s in sections if s in REFS for r in (REFS[s], *MORE_REFS.get(s, ())))
             for col in cols]
    if not parts:
        return []
    return state.DB.execute(
        "SELECT id, rel_path, mime, size, download_name, created FROM media WHERE user_id = ?"
        f" AND id IN ({' UNION '.join(parts)}) ORDER BY created, id", (user_id,) * (len(parts) + 1)).fetchall()


def _file_name(row) -> str:
    return f"files/{row['id']}{media.EXT.get(row['mime'], '.bin')}"


def _settings(user_id: int) -> dict:
    user = state.DB.execute("SELECT login, name, theme, views, screen_lock FROM users WHERE id = ?",
                            (user_id,)).fetchone()
    return {
        "profile": {"login": user["login"], "name": user["name"], "theme": user["theme"],
                    "views": _maybe_json(user["views"]), "screen_lock": bool(user["screen_lock"])},
        "connections": [{"id": r["id"], "name": r["name"], "kind": r["kind"], "base_url": r["base_url"]}
                        for r in state.DB.execute("SELECT id, name, kind, base_url FROM connections"
                                                  " WHERE user_id = ? ORDER BY id", (user_id,))],
        "sections": {
            "links": [{"section": r["section"], "connection_id": r["connection_id"], "models": _maybe_json(r["models"])}
                      for r in state.DB.execute("SELECT section, connection_id, models FROM section_links"
                                                " WHERE user_id = ? ORDER BY section, connection_id", (user_id,))],
            "defaults": [dict(r) for r in state.DB.execute("SELECT section, connection_id, model FROM section_defaults"
                                                           " WHERE user_id = ? ORDER BY section", (user_id,))],
        },
        "presets": [{k: _maybe_json(r[k]) if k == "params" else r[k] for k in r.keys() if k != "user_id"}
                    for r in state.DB.execute("SELECT * FROM presets WHERE user_id = ? ORDER BY created, id",
                                              (user_id,))],
    }


def _chat(user_id: int, chat_id: str, files: dict) -> "dict | None":
    chat = state.DB.execute("SELECT id, title, model, created, updated FROM chats WHERE id = ? AND user_id = ?",
                            (chat_id, user_id)).fetchone()
    if chat is None:
        return None                                        # удалён во время выгрузки
    attachments: dict[str, list] = {}
    for a in state.DB.execute(
            "SELECT id, message_id, kind, name, chars, text, media_id FROM attachments WHERE user_id = ?"
            " AND message_id IN (SELECT id FROM messages WHERE chat_id = ?) ORDER BY created, id", (user_id, chat_id)):
        attachments.setdefault(a["message_id"], []).append(
            {"id": a["id"], "kind": a["kind"], "name": a["name"], "chars": a["chars"], "text": a["text"],
             "file": files.get(a["media_id"])})
    messages = [{"id": m["id"], "role": m["role"], "content": m["content"], "status": m["status"], "model": m["model"],
                 "created": m["created"], "attachments": attachments.get(m["id"], []),
                 "tools": _maybe_json(m["tools"] or "[]"),                 # шаги «Интернета» целиком
                 "reasoning": m["reasoning"], "reasoning_sec": m["reasoning_sec"]}
                for m in state.DB.execute("SELECT id, role, content, status, model, created, tools, reasoning,"
                                          " reasoning_sec FROM messages"
                                          " WHERE chat_id = ? ORDER BY created, id", (chat_id,))]
    return {**dict(chat), "messages": messages}


def _pages(table: str, user_id: int):
    """Строки раздела страницами по PAGE — курсором (created, id) последней строки, без OFFSET: у записей
    бывает одно время. Пустой раздел — одна пустая страница."""
    where, args = "", ()
    while True:
        rows = state.DB.execute(f"SELECT * FROM {table} WHERE user_id = ?{where} ORDER BY created, id LIMIT ?",
                                (user_id, *args, PAGE)).fetchall()
        if rows or not args:
            yield rows
        if len(rows) < PAGE:
            return
        where = " AND (created > ? OR (created = ? AND id > ?))"
        args = (rows[-1]["created"], rows[-1]["created"], rows[-1]["id"])


def _pick(row, *keys) -> dict:
    return {k: row[k] for k in keys}


def _images(user_id: int, rows: list, files: dict) -> list:
    return [_pick(r, "id", "kind", "model", "prompt", "size", "seed", "strength", "state", "detail", "created",
                  "finished")
            | {"chain": chains.of_image(user_id, r), "file": files.get(r["media_id"]),
               "source_file": files.get(r["source_media_id"]), "mask_file": files.get(r["mask_media_id"])}
            for r in rows]


def _videos(user_id: int, rows: list, files: dict) -> list:
    return [_pick(r, "id", "prompt", "prompt_source", "seconds", "quality", "smooth", "seed", "state", "progress",
                  "detail", "created", "finished")
            | {"chain": chains.of_video(user_id, r), "file": files.get(r["media_id"]),
               "source_file": files.get(r["source_media_id"])}
            for r in rows]


def _transcripts(user_id: int, rows: list, files: dict) -> list:
    return [_pick(r, "id", "model", "name", "duration", "speakers", "state", "detail", "created", "finished")
            | {"file": files.get(r["source_media_id"]), "result_file": files.get(r["result_media_id"]),
               "edits": _maybe_json(r["edits"])}
            for r in rows]


def _scans(user_id: int, rows: list, files: dict) -> list:
    """Документы распознавания со страницами (страницы — одним запросом на страницу выгрузки)."""
    pages: dict[str, list] = {}
    if rows:
        marks = ", ".join("?" * len(rows))
        for p in state.DB.execute(f"SELECT scan_id, n, state, text, detail, media_id FROM scan_pages WHERE user_id = ?"
                                  f" AND scan_id IN ({marks}) ORDER BY scan_id, n", (user_id, *(r["id"] for r in rows))):
            pages.setdefault(p["scan_id"], []).append(_pick(p, "n", "state", "text", "detail")
                                                      | {"file": files.get(p["media_id"])})
    return [_pick(r, "id", "model", "name", "origin", "state", "detail", "created", "finished")
            | {"file": files.get(r["source_media_id"]), "pages": pages.get(r["id"], [])}
            for r in rows]


def _speeches(user_id: int, rows: list, files: dict) -> list:
    return [_pick(r, "id", "model", "voice", "text", "duration", "state", "detail", "created", "finished")
            | {"file": files.get(r["media_id"])} for r in rows]


def _voices(user_id: int, rows: list, files: dict) -> list:
    return [_pick(r, "id", "name", "ref_text", "duration", "created") | {"file": files.get(r["media_id"])}
            for r in rows]


def _translations(user_id: int, rows: list, files: dict) -> list:
    """Переводы с кусками (куски — одним запросом на страницу выгрузки; своего user_id у них нет — только
    записи из `rows`, а те — свои). Тексты кусков — только у снимка (блоки с рамками): у текста и файла исходник
    и перевод уже в записи, куски — номер, состояние и слова отказа."""
    parts: dict[str, list] = {}
    if rows:
        marks = ", ".join("?" * len(rows))
        for p in state.DB.execute(f"SELECT p.translation_id, t.kind = 'photo' AS photo, p.n, p.state, p.detail,"
                                  f" CASE WHEN t.kind = 'photo' THEN p.source END AS source,"
                                  f" CASE WHEN t.kind = 'photo' THEN p.text END AS text, p.box"
                                  f" FROM translation_parts p JOIN translations t ON t.id = p.translation_id"
                                  f" WHERE p.translation_id IN ({marks}) ORDER BY p.translation_id, p.n",
                                  tuple(r["id"] for r in rows)):
            part = (_pick(p, "n", "source", "text", "state", "detail") | {"box": _maybe_json(p["box"])} if p["photo"]
                    else _pick(p, "n", "state", "detail"))
            parts.setdefault(p["translation_id"], []).append(part)
    return [_pick(r, "id", "kind", "model", "name", "source_lang", "target_lang", "detected_lang", "result_lang",
                  "source", "text", "state", "detail", "created", "finished")
            | {"file": files.get(r["source_media_id"]), "parts": parts.get(r["id"], [])}
            for r in rows]


def _code_chats(user_id: int, rows: list, files: dict) -> list:
    """Отметки студии о чатах Claude Code — без текста: он на компьютере."""
    return [_pick(r, "agent_id", "project", "session_id", "state", "created", "updated") for r in rows]


def _agent_events(user_id: int, rows: list, files: dict) -> list:
    """История подключений компьютеров (с именем компьютера на момент события); ключа устройства тут нет."""
    return [_pick(r, "id", "created", "kind", "agent_id", "agent_name", "device", "ip") for r in rows]


#: раздел → (таблица и каталог страниц, сборщик страницы); чат — отдельно, по файлу на диалог
TABLES = {"photo": ("images", _images), "video": ("videos", _videos), "transcribe": ("transcripts", _transcripts),
          "ocr": ("scans", _scans), "speech": ("speeches", _speeches), "voices": ("voices", _voices),
          "translate": ("translations", _translations), "code": ("code_chats", _code_chats),
          "agents": ("agent_events", _agent_events)}


# ---- запись: JSON и суммы — в потоке ------------------------------------------------

def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")


def _pack(value) -> tuple[bytes, int, str]:
    """Байты записи (JSON или готовые), CRC-32 и sha256 — в потоке: данные — уже словари, базы здесь нет."""
    data = value if isinstance(value, bytes) else _json(value)
    return data, zlib.crc32(data), hashlib.sha256(data).hexdigest()


def _release(user_id: int, mark: object) -> None:
    if RUNNING.get(user_id) is mark:
        del RUNNING[user_id]


async def _stream(user_id: int, sections: tuple[str, ...], rows: list, mark: object):
    out, now = media.ZipWriter(), clock.NOW()
    files = {r["id"]: _file_name(r) for r in rows}
    entries, counts = [], {}

    async def record(name: str, value, listed: bool = True):
        """Заголовок записи и её данные — разными кусками: склейка — ещё одна копия страницы."""
        data, crc, digest = await asyncio.to_thread(_pack, value)
        if listed:
            entries.append({"path": name, "sha256": digest})
        yield out.entry(name, now, crc, len(data))
        yield data

    try:
        async for piece in record("README.txt", README.encode("utf-8")):
            yield piece
        async for piece in record("settings.json", _settings(user_id)):
            yield piece
        if "chat" in sections:
            ids = [r["id"] for r in state.DB.execute("SELECT id FROM chats WHERE user_id = ? ORDER BY created, id",
                                                     (user_id,))]
            counts["chats"] = 0
            for chat_id in ids:
                chat = _chat(user_id, chat_id, files)
                if chat is not None:
                    counts["chats"] += 1
                    async for piece in record(f"chats/{chat_id}.json", chat):
                        yield piece
        for section, (name, collect) in TABLES.items():
            if section in sections:
                counts[name] = 0
                for n, page in enumerate(_pages(name, user_id), 1):
                    counts[name] += len(page)
                    async for piece in record(f"{name}/{n:04d}.json", collect(user_id, page, files)):
                        yield piece
        listed, missing = [], []
        try:
            for row in rows:
                alive = state.DB.execute("SELECT rel_path FROM media WHERE id = ? AND user_id = ?",
                                         (row["id"], user_id)).fetchone()
                if alive is None:                              # строка ушла во время выгрузки
                    missing.append(row["id"])
                    continue
                try:
                    f, crc, size, digest = await asyncio.to_thread(
                        media.open_summed, state.CFG.media_dir / alive["rel_path"], CHUNK, True)
                except FileNotFoundError:
                    # каталога шары нет (отмонтировали) — OSError и обрыв, а не «missing» у всех остальных
                    await asyncio.to_thread(state.CFG.media_dir.stat)
                    missing.append(row["id"])
                    continue
                try:
                    yield out.entry(files[row["id"]], row["created"], crc, size)
                    left = size
                    while left > 0:
                        block = await asyncio.to_thread(f.read, min(CHUNK, left))
                        if not block:
                            raise OSError("файл стал короче")
                        left -= len(block)
                        yield block
                finally:
                    f.close()
                listed.append({"path": files[row["id"]], "media_id": row["id"], "mime": row["mime"], "size": size,
                               "sha256": digest, "download_name": row["download_name"], "created": row["created"]})
        except OSError:
            log.warning("export: хранилище оборвало чтение")
            raise media.StorageError("хранилище оборвало чтение") from None
        counts["files"] = len(listed)
        user = state.DB.execute("SELECT login, name FROM users WHERE id = ?", (user_id,)).fetchone()
        manifest = {"format": "studio-export", "version": 1, "created": now, "schema": db.VERSION,
                    "user": {"login": user["login"], "name": user["name"]}, "sections": list(sections),
                    "counts": counts, "files": listed, "entries": entries, "missing": missing}
        async for piece in record("manifest.json", manifest, listed=False):
            yield piece
        yield out.end()
    finally:
        _release(user_id, mark)


# ---- маршруты ----------------------------------------------------------------------

async def _share_ready() -> None:
    try:
        await asyncio.to_thread(state.CFG.media_dir.stat)
    except OSError:
        log.warning("export: хранилище недоступно")
        raise HTTPException(503, STORAGE) from None


@router.post("/me/export")
async def create_export(body: ExportIn, request: Request):
    if not body.sections or any(s not in SECTIONS for s in body.sections):
        raise HTTPException(400, {"code": "invalid", "message": "выберите разделы выгрузки"})
    chosen = tuple(s for s in SECTIONS if s in body.sections)
    uid, now = request.state.user.id, clock.NOW()
    for token in [t for t, (_, _, at) in EXPORTS.items() if now - at > EXPORT_TTL_SEC]:
        del EXPORTS[token]
    rows = _media_rows(uid, chosen)
    if rows:
        await _share_ready()
    token = secrets.token_urlsafe(18)
    EXPORTS[token] = (uid, chosen, now)
    for old in [t for t, (owner, _, _) in EXPORTS.items() if owner == uid][:-EXPORT_KEEP]:
        del EXPORTS[old]
    return {"id": token, "files": len(rows), "bytes": sum(r["size"] for r in rows)}


@router.get("/me/export/{token}")
async def get_export(token: str, request: Request):
    uid = request.state.user.id
    held = EXPORTS.get(token)
    if held is None or held[0] != uid or clock.NOW() - held[2] > EXPORT_TTL_SEC:
        raise HTTPException(404, media.GONE)
    rows = _media_rows(uid, held[1])
    if rows:
        await _share_ready()
    # проверка и метка — без await между ними: две загрузки разом не проскочат обе
    if uid in RUNNING:
        raise HTTPException(409, BUSY)
    mark = object()
    RUNNING[uid] = mark
    body = _stream(uid, held[1], rows, mark)
    weakref.finalize(body, _release, uid, mark)            # ответ так и не начался — метку снимет уход из памяти
    log.info("export: %d файлов", len(rows))
    day = datetime.fromtimestamp(clock.NOW()).strftime("%Y-%m-%d")
    return StreamingResponse(body, media_type="application/zip",
                             headers={"Content-Disposition": media.disposition(f"studio-export-{day}.zip"),
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
