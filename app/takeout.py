"""Выгрузка своей истории одним ZIP (спека 2026-10-21-studio-export-restore-check, раздел 5).

- `POST /api/me/export` `{"sections": [...]}` (подмножество SECTIONS) → `{id, files, bytes}`: токен
  в памяти службы (EXPORTS, как media.ARCHIVES) на EXPORT_TTL_SEC, живых — не больше EXPORT_KEEP на
  пользователя; files и bytes — оценка по строкам media выбранных разделов.
- `GET /api/me/export/<токен>` — ZIP_STORED потоком (писатель — media.ZipWriter): README.txt,
  settings.json, `chats/<id>.json` (диалог — своим файлом), images/videos/transcripts/speeches/voices.json,
  `files/<media id><расширение>`, последним — manifest.json с sha256 всего записанного.
- Всё — своё (`user_id` сессии). Нет токенов подключений, хеша пароля, сессий, CSRF, passkey.
- База — только с цикла событий (одна state.DB): генератор асинхронный, строки читает по одной
  записи; файлы — в потоке (asyncio.to_thread): первый проход — CRC-32 и sha256, второй — данные.
  Это не снимок: строка, ушедшая во время выгрузки, пропускается, её файл — в manifest.missing.
- Шару проверяют, только если есть файлы: выгрузка одних диалогов законна и без неё.
- Журнал — «export: N файлов» и предупреждение на сбой, без имён, путей и промптов.
"""
import asyncio
import hashlib
import json
import logging
import secrets
import zlib
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import chains
import clock
import db
import media
import state

SECTIONS = ("chat", "photo", "video", "transcribe", "speech", "voices")
#: большая выгрузка качается дольше 10 минут архива выделенного
EXPORT_TTL_SEC = 3600
EXPORT_KEEP = 2
#: блок чтения файла — один шаг потока
CHUNK = 1 << 20
#: токен → (user_id, разделы, время создания)
EXPORTS: dict[str, tuple[int, tuple[str, ...], float]] = {}
#: раздел → (таблица, столбцы со ссылкой на media, условие); таблицы и столбцы — из кода
REFS = {
    "chat": ("attachments", ("media_id",), " AND message_id IS NOT NULL"),
    "photo": ("images", ("media_id", "source_media_id", "mask_media_id"), ""),
    "video": ("videos", ("media_id", "source_media_id"), ""),
    "transcribe": ("transcripts", ("source_media_id", "result_media_id"), ""),
    "speech": ("speeches", ("media_id",), ""),
    "voices": ("voices", ("media_id",), ""),
}
STORAGE = {"code": "storage", "message": "хранилище недоступно"}

README = """Выгрузка истории студии

Что внутри:
- settings.json — профиль (логин, имя, тема, виды лент), подключения (адреса без ключей), разделы
  (подключения и отмеченные модели, умолчания) и пресеты, если они есть.
- chats/<id>.json — диалог: сообщения по порядку, вложения (текст документа и ссылка на файл).
- images.json — картинки: промпт, модель, размер, seed, цепочка промптов, файлы итога, исходника, маски.
- videos.json — ролики, transcripts.json — расшифровки (и правки, если были), speeches.json — озвучки,
  voices.json — образцы голосов. Файлы — ссылками «files/…».
- files/<id>.<расширение> — сами файлы (без миниатюр).
- manifest.json — последним: что в архиве, sha256 каждого файла (files) и каждой записи JSON и этого
  описания (entries), id файлов, которых на хранилище уже не было (missing).

Ключей подключений, пароля и сессий в выгрузке нет. Обратно в студию выгрузка не загружается —
это формат для чтения и хранения. Время — секунды Unix (UTC).
"""

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class ExportIn(BaseModel):
    sections: list[str]


# ---- чтение базы (цикл событий) --------------------------------------------------

def _has_table(name: str) -> bool:
    return state.DB.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (name,)).fetchone() is not None


def _has_column(table: str, column: str) -> bool:
    return any(row["name"] == column for row in state.DB.execute(f"PRAGMA table_info({table})"))


def _maybe_json(text):
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return text


def _media_rows(user_id: int, sections: tuple[str, ...]) -> list:
    """Свои строки media, на которые ссылаются записи выбранных разделов, — по времени."""
    parts = [f"SELECT {col} FROM {table} WHERE user_id = ?{extra}"
             for table, cols, extra in (REFS[s] for s in sections) for col in cols]
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
    out = {
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
    }
    if _has_table("presets"):
        out["presets"] = [{k: _maybe_json(r[k]) if k == "params" else r[k] for k in r.keys() if k != "user_id"}
                          for r in state.DB.execute("SELECT * FROM presets WHERE user_id = ? ORDER BY created, id",
                                                    (user_id,))]
    return out


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
                 "created": m["created"], "attachments": attachments.get(m["id"], [])}
                for m in state.DB.execute("SELECT id, role, content, status, model, created FROM messages"
                                          " WHERE chat_id = ? ORDER BY created, id", (chat_id,))]
    return {**dict(chat), "messages": messages}


def _rows(table: str, user_id: int) -> list:
    return state.DB.execute(f"SELECT * FROM {table} WHERE user_id = ? ORDER BY created, id", (user_id,)).fetchall()


def _pick(row, *keys) -> dict:
    return {k: row[k] for k in keys}


def _images(user_id: int, files: dict) -> list:
    return [_pick(r, "id", "kind", "model", "prompt", "size", "seed", "strength", "state", "detail", "created",
                  "finished")
            | {"chain": chains.of_image(user_id, r), "file": files.get(r["media_id"]),
               "source_file": files.get(r["source_media_id"]), "mask_file": files.get(r["mask_media_id"])}
            for r in _rows("images", user_id)]


def _videos(user_id: int, files: dict) -> list:
    return [_pick(r, "id", "prompt", "prompt_source", "seconds", "quality", "smooth", "seed", "state", "progress",
                  "detail", "created", "finished")
            | {"chain": chains.of_video(user_id, r), "file": files.get(r["media_id"]),
               "source_file": files.get(r["source_media_id"])}
            for r in _rows("videos", user_id)]


def _transcripts(user_id: int, files: dict) -> list:
    edits = _has_column("transcripts", "edits")           # правки расшифровки — если они уже есть в базе
    return [_pick(r, "id", "model", "name", "duration", "speakers", "state", "detail", "created", "finished")
            | {"file": files.get(r["source_media_id"]), "result_file": files.get(r["result_media_id"])}
            | ({"edits": _maybe_json(r["edits"])} if edits else {})
            for r in _rows("transcripts", user_id)]


def _speeches(user_id: int, files: dict) -> list:
    return [_pick(r, "id", "model", "voice", "text", "duration", "state", "detail", "created", "finished")
            | {"file": files.get(r["media_id"])} for r in _rows("speeches", user_id)]


def _voices(user_id: int, files: dict) -> list:
    return [_pick(r, "id", "name", "ref_text", "duration", "created") | {"file": files.get(r["media_id"])}
            for r in _rows("voices", user_id)]


#: раздел → (имя JSON, сборщик); чат — отдельно, по файлу на диалог
TABLES = {"photo": ("images", _images), "video": ("videos", _videos), "transcribe": ("transcripts", _transcripts),
          "speech": ("speeches", _speeches), "voices": ("voices", _voices)}


# ---- файлы (поток) -----------------------------------------------------------------

def _sums(file: Path) -> tuple[int, int, str]:
    """CRC-32, размер и sha256 — одним чтением: заголовок записи уходит раньше её данных."""
    crc = size = 0
    digest = hashlib.sha256()
    with open(file, "rb") as f:
        while block := f.read(CHUNK):
            crc = zlib.crc32(block, crc)
            digest.update(block)
            size += len(block)
    return crc, size, digest.hexdigest()


async def _blocks(file: Path, size: int):
    f = await asyncio.to_thread(open, file, "rb")
    try:
        left = size
        while left > 0:
            block = await asyncio.to_thread(f.read, min(CHUNK, left))
            if not block:
                raise OSError("файл стал короче")
            left -= len(block)
            yield block
    finally:
        f.close()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")


async def _stream(user_id: int, sections: tuple[str, ...], rows: list):
    out, now = media.ZipWriter(), clock.NOW()
    files = {r["id"]: _file_name(r) for r in rows}
    entries, counts = [], {}

    def record(name: str, data: bytes) -> bytes:
        entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
        return out.entry(name, now, zlib.crc32(data), len(data)) + data

    yield record("README.txt", README.encode("utf-8"))
    yield record("settings.json", _json(_settings(user_id)))
    if "chat" in sections:
        ids = [r["id"] for r in state.DB.execute("SELECT id FROM chats WHERE user_id = ? ORDER BY created, id",
                                                 (user_id,))]
        counts["chats"] = 0
        for chat_id in ids:
            chat = _chat(user_id, chat_id, files)
            if chat is not None:
                counts["chats"] += 1
                yield record(f"chats/{chat_id}.json", _json(chat))
    for section, (name, collect) in TABLES.items():
        if section in sections:
            items = collect(user_id, files)
            counts[name] = len(items)
            yield record(f"{name}.json", _json(items))
    listed, missing = [], []
    try:
        for row in rows:
            alive = state.DB.execute("SELECT rel_path FROM media WHERE id = ? AND user_id = ?",
                                     (row["id"], user_id)).fetchone()
            file = state.CFG.media_dir / alive["rel_path"] if alive is not None else None
            try:
                if file is None:
                    raise FileNotFoundError
                crc, size, digest = await asyncio.to_thread(_sums, file)
            except FileNotFoundError:
                missing.append(row["id"])
                continue
            yield out.entry(files[row["id"]], row["created"], crc, size)
            async for block in _blocks(file, size):
                yield block
            listed.append({"path": files[row["id"]], "media_id": row["id"], "mime": row["mime"], "size": size,
                           "sha256": digest, "download_name": row["download_name"], "created": row["created"]})
    except OSError:
        log.warning("export: хранилище оборвало чтение")
        raise media.StorageError("хранилище оборвало чтение") from None
    counts["files"] = len(listed)
    user = state.DB.execute("SELECT login, name FROM users WHERE id = ?", (user_id,)).fetchone()
    manifest = {"format": "studio-export", "version": 1, "created": now, "schema": db.VERSION,
                "user": {"login": user["login"], "name": user["name"]}, "sections": list(sections), "counts": counts,
                "files": listed, "entries": entries, "missing": missing}
    data = _json(manifest)
    yield out.entry("manifest.json", now, zlib.crc32(data), len(data)) + data
    yield out.end()


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
    log.info("export: %d файлов", len(rows))
    day = datetime.fromtimestamp(clock.NOW()).strftime("%Y-%m-%d")
    return StreamingResponse(_stream(uid, held[1], rows), media_type="application/zip",
                             headers={"Content-Disposition": media.disposition(f"studio-export-{day}.zip"),
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
