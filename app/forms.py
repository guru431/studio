"""Формы разделов «Фото» и «Видео»: тело запроса с пределом, разбор формы с фото — файлом с
устройства или своим фото истории (`source_media_id`) — и чтение своих файлов истории.

Базы не касается ничего, кроме `history_file` (строка media — с цикла событий); Pillow и файлы —
в asyncio.to_thread."""
import asyncio
import re
from pathlib import Path

from fastapi import HTTPException, Request

import media
from image_upload import IMAGE_FIELDS, MAX_PIXELS, MAX_UPLOAD, UploadError, decode_form, normalize_image

MEDIA_ID_RE = re.compile(r"[0-9a-f]{32}")
#: отказ decode_form форме без файла фото — тогда фото может прийти полем source_media_id
NO_FILE = f"нужен файл {IMAGE_FIELDS[0]} (PNG, JPEG или WebP)"
STORAGE = {"code": "storage", "message": "хранилище недоступно"}


async def read_body(request: Request, limit: int) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, {"code": "too_big", "message": "слишком большое тело запроса"})
    return bytes(raw)


def decode(raw: bytes, content_type: str, *, with_mask: bool = False,
           max_upload: int = MAX_UPLOAD) -> tuple[dict, "bytes | None", "bytes | None"]:
    """(поля, фото, маска) формы. Фото — файл `image` или поле `source_media_id` (фото истории):
    форме без файла decode_form отказывает, и тогда она разбирается ещё раз этим полем вместо файла."""
    extra = {} if with_mask else None
    try:
        fields, image = decode_form(raw, content_type, extra_files=extra, max_upload=max_upload)
    except UploadError as e:
        if str(e) != NO_FILE:
            raise
        extra = {} if with_mask else None
        try:
            fields, source = decode_form(raw, content_type, extra_files=extra, file_fields=("source_media_id",))
        except UploadError:
            raise e from None                   # ни файла, ни фото истории
        fields["source_media_id"], image = source.decode("utf-8", "replace"), None
    else:
        if "source_media_id" in fields:
            raise UploadError("нужно одно фото: файл или фото из истории")
    return fields, image, (extra or {}).get("mask")


def _load(file: Path) -> bytes:
    if file.stat().st_size > MAX_UPLOAD:
        raise UploadError(f"фото должно быть непустым и не больше {MAX_UPLOAD // (1024 * 1024)} МиБ", 413)
    return file.read_bytes()


async def history_file(user_id: int, media_id: str) -> bytes:
    """Свой файл истории (фото или маска) до MAX_UPLOAD; чужой, неизвестный и пропавший с шары — 404 gone."""
    if not MEDIA_ID_RE.fullmatch(media_id):
        raise HTTPException(404, media.GONE)
    file = media.path(user_id, media_id)
    try:
        return await asyncio.to_thread(_load, file)
    except FileNotFoundError:
        raise HTTPException(404, media.GONE) from None
    except OSError:
        raise HTTPException(503, STORAGE) from None


async def photo(user_id: int, upload: "bytes | None", source_id: "str | None",
                max_pixels: int = MAX_PIXELS) -> "bytes | None":
    """Нормализованное фото задания: файл с устройства или своё фото истории; нет ни того, ни другого — None."""
    if upload is None and source_id is None:
        return None
    if upload is None:
        upload = await history_file(user_id, source_id)
    return await asyncio.to_thread(normalize_image, upload, max_pixels)
