"""Файлы истории (картинки, ролики, документы) на шаре CIFS в `STUDIO_MEDIA_DIR` (спека, раздел 7.6).

- Путь `u{user_id}/{YYYY}/{MM}/{id}{ext}`, превью — `{id}.thumb.webp` рядом. Файл пишется во
  временный рядом и встаёт на место `os.replace`; строка `media` появляется только после файлов.
  Шара недоступна — `StorageError`, ни строки, ни временного файла.
- `GET /api/media/{id}` отдаёт только своё, с диапазонами: Safari на iPhone без `Range` ролик не
  играет. С диска читается только нужный кусок.
- Имена файлов и содержимое в журнал не пишутся: `download_name` — только в `Content-Disposition`.
"""
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from PIL import Image

import clock
import state

THUMB_SIDE = 384
CHUNK = 64 << 10
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
       "video/mp4": ".mp4", "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
       "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}
GONE = {"code": "gone", "message": "файла нет"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class StorageError(Exception):
    def __init__(self, message: str = "не удалось сохранить файл: хранилище недоступно"):
        super().__init__(message)


# ---- запись ----------------------------------------------------------------

def _target(user_id: int, media_id: str, mime: str) -> tuple[str, Path]:
    """(rel_path, полный путь) нового файла; дата каталога — по clock.NOW() в UTC."""
    day = datetime.fromtimestamp(clock.NOW(), timezone.utc)
    rel = f"u{user_id}/{day:%Y}/{day:%m}/{media_id}{EXT.get(mime, '.bin')}"
    return rel, state.CFG.media_dir / rel


def _thumb_of(full: Path, media_id: str) -> Path:
    return full.with_name(f"{media_id}.thumb.webp")


def _tmp(target: Path) -> Path:
    return target.with_name(f".{target.name}.tmp")


def _discard(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _thumbnail(data: bytes) -> bytes | None:
    """WebP с длинной стороной ≤ THUMB_SIDE; не картинка — None (файл сохранится без превью)."""
    try:
        with Image.open(BytesIO(data)) as img:
            img.thumbnail((THUMB_SIDE, THUMB_SIDE))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
            out = BytesIO()
            img.save(out, "WEBP", quality=80)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _insert(media_id: str, user_id: int, rel: str, mime: str, size: int, has_thumb: bool,
            download_name: str, written: tuple[Path, ...]) -> None:
    try:
        state.DB.execute("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (media_id, user_id, rel, mime, size, int(has_thumb), download_name, clock.NOW()))
    except BaseException:
        _discard(*written)           # строки нет — файлы без строки никому не нужны
        raise


def _storage_failed(user_id: int, e: OSError) -> StorageError:
    log.warning("media пользователя %d: запись не удалась (%s)", user_id, type(e).__name__)
    return StorageError()


def save(user_id: int, data: bytes, mime: str, download_name: str, *, thumb: bool = False) -> str:
    """Синхронно (Pillow и запись на шару): вызывающий запускает в asyncio.to_thread."""
    media_id = uuid.uuid4().hex
    rel, full = _target(user_id, media_id, mime)
    small = _thumbnail(data) if thumb else None
    parts = [(full, data)] + ([(_thumb_of(full, media_id), small)] if small is not None else [])
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        for target, body in parts:
            _tmp(target).write_bytes(body)
            os.replace(_tmp(target), target)
    except OSError as e:
        _discard(*(p for target, _ in parts for p in (_tmp(target), target)))
        raise _storage_failed(user_id, e) from None
    _insert(media_id, user_id, rel, mime, len(data), small is not None, download_name,
            tuple(target for target, _ in parts))
    return media_id


async def save_stream(user_id: int, chunks: AsyncIterator[bytes], mime: str, download_name: str,
                      max_bytes: int) -> str:
    """Как save, но потоком и без превью (ролики); больше max_bytes — StorageError, файла нет."""
    media_id = uuid.uuid4().hex
    rel, full = _target(user_id, media_id, mime)
    tmp, size, out = _tmp(full), 0, None
    try:
        await asyncio.to_thread(full.parent.mkdir, parents=True, exist_ok=True)
        out = await asyncio.to_thread(open, tmp, "wb")
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise StorageError("файл больше предела")
            await asyncio.to_thread(out.write, chunk)
        await asyncio.to_thread(out.close)
        await asyncio.to_thread(os.replace, tmp, full)
    except BaseException as e:
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        await asyncio.to_thread(_discard, tmp, full)
        if isinstance(e, OSError):
            raise _storage_failed(user_id, e) from None
        raise
    _insert(media_id, user_id, rel, mime, size, False, download_name, (full,))
    return media_id


# ---- чтение и удаление -----------------------------------------------------

def _row(user_id: int, media_id: str):
    row = state.DB.execute("SELECT id, rel_path, mime, has_thumb, download_name FROM media"
                           " WHERE id = ? AND user_id = ?", (media_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, GONE)
    return row


def _file(row, thumb: bool) -> Path:
    full = state.CFG.media_dir / row["rel_path"]
    return _thumb_of(full, row["id"]) if thumb and row["has_thumb"] else full


def path(user_id: int, media_id: str, thumb: bool = False) -> Path:
    """Путь своего файла (превью — если есть, иначе оригинал); чужой или неизвестный — 404 gone."""
    return _file(_row(user_id, media_id), thumb)


def delete(user_id: int, media_id: str) -> None:
    """Файлы и строка; нет файла или строки — не ошибка. Прочая OSError — наружу, строка остаётся."""
    row = state.DB.execute("SELECT id, rel_path FROM media WHERE id = ? AND user_id = ?",
                           (media_id, user_id)).fetchone()
    if row is None:
        return
    full = state.CFG.media_dir / row["rel_path"]
    full.unlink(missing_ok=True)
    _thumb_of(full, row["id"]).unlink(missing_ok=True)
    state.DB.execute("DELETE FROM media WHERE id = ?", (media_id,))


def byte_range(header: str, size: int) -> "tuple[int, int] | None":
    """Один диапазон `bytes=a-b`, `a-` или `-n` → (начало, конец включительно); None — 416."""
    match = RANGE_RE.fullmatch(header.strip())
    if match is None or match.groups() == ("", ""):
        return None
    first, last = match.groups()
    if first == "":
        if int(last) == 0:
            return None
        return max(0, size - int(last)), size - 1
    start, end = int(first), int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _disposition(name: str) -> str:
    """ASCII-замена в `filename` (без кавычек, `\\` и управляющих), полное имя — в `filename*` (RFC 5987)."""
    plain = "".join(ch if " " <= ch <= "~" and ch not in '"\\' else "_" for ch in name) or "file"
    header = f'attachment; filename="{plain}"'
    if plain != name:
        header += f"; filename*=UTF-8''{quote(name, safe='')}"
    return header


def _read(file: Path, start: int, length: int):
    """Кусок файла блоками CHUNK; Starlette крутит обычный генератор в пуле потоков."""
    with open(file, "rb") as f:
        f.seek(start)
        while length > 0:
            block = f.read(min(CHUNK, length))
            if not block:
                return
            length -= len(block)
            yield block


@router.get("/media/{media_id}")
async def get_media(media_id: str, request: Request, thumb: str = "", download: str = ""):
    row = _row(request.state.user.id, media_id)
    file = _file(row, thumb == "1")
    try:
        size = (await asyncio.to_thread(file.stat)).st_size
    except FileNotFoundError:
        raise HTTPException(404, GONE) from None
    except OSError:
        log.warning("media %s: хранилище недоступно", row["id"])
        raise HTTPException(503, {"code": "storage", "message": "хранилище недоступно"}) from None
    mime = "image/webp" if file.name.endswith(".thumb.webp") else row["mime"]
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=86400",
               "X-Content-Type-Options": "nosniff"}
    if download == "1":
        headers["Content-Disposition"] = _disposition(row["download_name"])
    spec = request.headers.get("range")
    if spec is None:
        return StreamingResponse(_read(file, 0, size), media_type=mime,
                                 headers=headers | {"Content-Length": str(size)})
    span = byte_range(spec, size)
    if span is None:
        return Response(status_code=416, headers=headers | {"Content-Range": f"bytes */{size}"})
    start, end = span
    return StreamingResponse(_read(file, start, end - start + 1), status_code=206, media_type=mime,
                             headers=headers | {"Content-Range": f"bytes {start}-{end}/{size}",
                                                "Content-Length": str(end - start + 1)})
