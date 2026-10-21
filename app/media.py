"""Файлы истории (картинки, ролики, документы) на шаре CIFS в `STUDIO_MEDIA_DIR` (спека, раздел 7.6).

- Путь `u{user_id}/{YYYY}/{MM}/{id}{ext}`, превью — `{id}.thumb.webp` рядом. Файл пишется во
  временный рядом и встаёт на место `os.replace`; строка `media` появляется только после файлов.
  Шара недоступна — `StorageError`, ни строки, ни временного файла.
- Путь без строки `media` не теряется: очередь удаления `purge` (без ссылки на учётку) держит пути
  файлов, которые не удалились (шара отказала), и файлов, которые пишутся сейчас (save_async,
  save_stream: отмена или остановка службы посреди записи оставили бы их без строки) — вставка
  строки снимает путь с очереди той же транзакцией. Очередь проходит purge(): при старте службы и
  раз в PURGE_SEC (start_purger), при каждом удалении и при стирании истории. Строка не встала —
  файлы удаляются тем же проходом по одному пути (в потоке).
- Поток записи не остановить: отмена ожидания (и срок SAVE_TIMEOUT_SEC — тогда StorageError «не
  отвечает») его не бросает — путь остаётся в SAVING, пока поток не кончит, и проход очереди
  уберёт записанное после.
- Объём своих файлов (usage) и очередь (waiting) — для «Хранилища» (`GET /api/me/storage`,
  profile.py): только из базы, без обхода шары.
- `GET /api/media/{id}` отдаёт только своё, с диапазонами: Safari на iPhone без `Range` ролик не
  играет. С диска читается только нужный кусок.
- Имена файлов и содержимое в журнал не пишутся: `download_name` — только в `Content-Disposition`.
"""
import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
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
PURGE_SEC = 300
#: Сколько запрос ждёт записи на шару (save_async целиком, у save_stream — каждый шаг в потоке).
#: Зависшая CIFS иначе держала бы запрос до таймаута Apache (ProxyPass timeout=150) и пользователь
#: видел бы 502 вместо слов службы. Правка фото пишет два файла подряд (исходник и маску): 2×45 = 90 с
#: оставляют ~60 с на чтение тела и разбор фото. Сверяет test_deploy.
SAVE_TIMEOUT_SEC = 45
STALLED = "не удалось сохранить файл: хранилище не отвечает"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#: виды файлов «Хранилища» в «Прочем» (usage): по началу mime; документы — ещё PDF, DOCX и JSON итогов расшифровки
KINDS = ("image", "video", "audio", "document", "other")
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
       "video/mp4": ".mp4", "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
       DOCX: ".docx",
       "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav", "audio/x-wav": ".wav",
       "audio/ogg": ".ogg", "audio/webm": ".webm", "audio/flac": ".flac", "audio/aac": ".aac", "video/webm": ".webm",
       "video/quicktime": ".mov", "application/json": ".json"}
GONE = {"code": "gone", "message": "файла нет"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class StorageError(Exception):
    def __init__(self, message: str = "не удалось сохранить файл: хранилище недоступно"):
        super().__init__(message)


class TooBig(StorageError):
    """Поток длиннее предела save_stream: файла нет."""


# ---- запись ----------------------------------------------------------------

def _plan(user_id: int, mime: str) -> tuple[str, str]:
    """(id, rel_path) нового файла; дата каталога — по clock.NOW() в UTC."""
    media_id = uuid.uuid4().hex
    day = datetime.fromtimestamp(clock.NOW(), timezone.utc)
    return media_id, f"u{user_id}/{day:%Y}/{day:%m}/{media_id}{EXT.get(mime, '.bin')}"


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
            download_name: str) -> None:
    """Строка media и снятие её пути с очереди удаления — одной транзакцией. Не встала — путь
    остаётся в очереди (у save — встаёт в неё): файлы удаляет не цикл событий, а проход очереди."""
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (media_id, user_id, rel, mime, size, int(has_thumb), download_name, clock.NOW()))
        state.DB.execute("DELETE FROM purge WHERE media_id = ?", (media_id,))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        queue(user_id, [(media_id, rel)])
        raise


async def _insert_or_purge(media_id: str, user_id: int, rel: str, *row) -> str:
    """_insert с цикла событий; строка не встала — файлы её пути сразу, в потоке (_purge)."""
    try:
        _insert(media_id, user_id, rel, *row)
    except BaseException:
        await _purge([(media_id, rel)])
        raise
    return media_id


def _release_when_done(media_id: str, task: asyncio.Future) -> None:
    """Снять путь с SAVING, когда запись в потоке кончится (сейчас или после отмены и срока)."""
    if task.done():
        SAVING.discard(media_id)
    else:
        task.add_done_callback(lambda _: SAVING.discard(media_id))


def _storage_failed(user_id: int, e: OSError) -> StorageError:
    log.warning("media пользователя %d: запись не удалась (%s)", user_id, type(e).__name__)
    return StorageError()


@dataclass(frozen=True)
class Written:
    """Файлы на шаре без строки media: итог write(), строку ставит insert()."""
    id: str
    user_id: int
    rel: str
    mime: str
    size: int
    has_thumb: bool
    download_name: str


def write(user_id: int, data: bytes, mime: str, download_name: str, *, thumb: bool = False,
          plan: tuple[str, str] | None = None) -> Written:
    """Файл и превью (Pillow и запись на шару) без базы — можно в asyncio.to_thread. База — нет:
    одно соединение SQLite общее, вставка из потока попала бы в чужую транзакцию запроса.
    plan — (id, rel_path) из _plan, уже поставленный в очередь удаления (save_async)."""
    media_id, rel = plan or _plan(user_id, mime)
    full = state.CFG.media_dir / rel
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
    return Written(media_id, user_id, rel, mime, len(data), small is not None, download_name)


def insert(written: Written) -> str:
    """Строка media записанных файлов — с цикла событий (или из синхронного кода), не из потока."""
    w = written
    _insert(w.id, w.user_id, w.rel, w.mime, w.size, w.has_thumb, w.download_name)
    return w.id


def save(user_id: int, data: bytes, mime: str, download_name: str, *, thumb: bool = False) -> str:
    """write и insert подряд — для синхронного кода; из async — save_async."""
    return insert(write(user_id, data, mime, download_name, thumb=thumb))


async def save_async(user_id: int, data: bytes, mime: str, download_name: str, *, thumb: bool = False) -> str:
    """Как save: файлы — в потоке, строка — здесь, на цикле событий. Отмена ожидания и срок
    SAVE_TIMEOUT_SEC поток не останавливают, и он допишет файлы, до строки которых дело уже не
    дойдёт: поэтому путь заранее стоит в очереди удаления (снимает его insert), а проход purge его
    не трогает, пока поток пишет."""
    plan = _plan(user_id, mime)
    queue(user_id, [plan])
    SAVING.add(plan[0])
    task = asyncio.ensure_future(asyncio.to_thread(write, user_id, data, mime, download_name, thumb=thumb, plan=plan))
    try:
        written = await asyncio.wait_for(asyncio.shield(task), SAVE_TIMEOUT_SEC)
    except TimeoutError:
        log.warning("media пользователя %d: хранилище не ответило за %d с", user_id, SAVE_TIMEOUT_SEC)
        raise StorageError(STALLED) from None
    finally:
        _release_when_done(plan[0], task)
    w = written
    return await _insert_or_purge(w.id, w.user_id, w.rel, w.mime, w.size, w.has_thumb, w.download_name)


def _drop(out, *paths: Path) -> None:
    """Закрыть недописанный файл и убрать его — в потоке: close ждёт идущую запись."""
    if out is not None:
        try:
            out.close()
        except OSError:
            pass
    _discard(*paths)


async def _stream(full: Path, chunks: AsyncIterator[bytes], max_bytes: int, stalled: asyncio.Future) -> int:
    """Тело save_stream — отдельной задачей: поток во временный файл и на место; отказ или отмена —
    ни временного, ни итогового файла. Шаг в потоке ждётся до конца и при отмене (отмена выходит
    наружу после него: иначе уборка обогнала бы перенос файла на место); шаг дольше
    SAVE_TIMEOUT_SEC отмечает stalled — save_stream ответит, не дожидаясь."""
    async def step(fn, *args, **kwargs):
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        try:
            done, _ = await asyncio.wait({fut}, timeout=SAVE_TIMEOUT_SEC)
            if not done:
                if not stalled.done():
                    stalled.set_result(None)
                await asyncio.wait({fut})
        except asyncio.CancelledError:
            await asyncio.wait({fut})
            raise
        return fut.result()

    tmp, size, out = _tmp(full), 0, None

    def opened():
        nonlocal out
        out = open(tmp, "wb")                    # в потоке: и отменённый шаг оставит, что закрыть

    try:
        await step(full.parent.mkdir, parents=True, exist_ok=True)
        await step(opened)
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise TooBig("файл больше предела")
            await step(out.write, chunk)
        await step(out.close)
        await step(os.replace, tmp, full)
    except BaseException:
        await step(_drop, out, tmp, full)
        raise
    return size


async def save_stream(user_id: int, chunks: AsyncIterator[bytes], mime: str, download_name: str,
                      max_bytes: int) -> str:
    """Как save, но потоком и без превью (ролики); больше max_bytes — StorageError, файла нет.
    Путь — в очереди удаления, пока строки нет, и в SAVING, пока запись не кончилась (как у
    save_async): запись — одной задачей (_stream) под защитой от отмены. Отмена ожидания и шаг
    дольше SAVE_TIMEOUT_SEC отменяют задачу: она доделывает шаг в потоке, убирает свои файлы и
    только тогда снимает путь с SAVING."""
    media_id, rel = _plan(user_id, mime)
    queue(user_id, [(media_id, rel)])
    SAVING.add(media_id)
    stalled = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(_stream(state.CFG.media_dir / rel, chunks, max_bytes, stalled))
    try:
        await asyncio.wait({task, stalled}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            log.warning("media пользователя %d: хранилище не ответило за %d с", user_id, SAVE_TIMEOUT_SEC)
            raise StorageError(STALLED)
        size = task.result()
    except OSError as e:
        raise _storage_failed(user_id, e) from None
    finally:
        task.cancel()                            # уже кончилась — ничего; иначе — остановится после шага
        _release_when_done(media_id, task)
    return await _insert_or_purge(media_id, user_id, rel, mime, size, False, download_name)


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


def _remove(media_id: str, rel_path: str) -> None:
    """Файл и превью, и их временные (запись оборвалась); нет файла — не ошибка, прочая OSError —
    наружу. Без базы — для asyncio.to_thread."""
    full = state.CFG.media_dir / rel_path
    thumb = _thumb_of(full, media_id)
    for p in (full, thumb, _tmp(full), _tmp(thumb)):
        p.unlink(missing_ok=True)


def remove_files(rows) -> list:
    """Файлы многих строк media `(id, rel_path)` одним вызовом в asyncio.to_thread (стирание
    истории, очередь удаления); базы не касается. Возвращает строки, чьи файлы не удалились."""
    failed = []
    for media_id, rel_path in rows:
        try:
            _remove(media_id, rel_path)
        except OSError:
            failed.append((media_id, rel_path))
    return failed


# ---- очередь удаления ------------------------------------------------------

#: id media, чьи файлы сейчас пишет поток save_async / save_stream: путь уже в очереди, но проход
#: purge его не трогает, пока запись не кончилась (строку вставят — путь уйдёт из очереди)
SAVING: set[str] = set()


def queue(user_id: int, rows) -> None:
    """Пути `(id, rel_path)` — в очередь удаления; с цикла событий (внутри транзакции зовущего — тоже)."""
    state.DB.executemany("INSERT OR IGNORE INTO purge(media_id, user_id, rel_path, queued) VALUES (?, ?, ?, ?)",
                         [(media_id, user_id, rel, clock.NOW()) for media_id, rel in rows])


async def _purge(rows: list) -> None:
    """Файлы путей очереди — в потоке; удалённые снимаются с очереди, прочие ждут следующего прохода."""
    failed = {media_id for media_id, _ in await asyncio.to_thread(remove_files, rows)}
    state.DB.executemany("DELETE FROM purge WHERE media_id = ?", [(m,) for m, _ in rows if m not in failed])
    state.DB.executemany("UPDATE purge SET tries = tries + 1 WHERE media_id = ?", [(m,) for m in failed])
    if failed:
        log.warning("очередь удаления: не удалилось файлов — %d", len(failed))


async def purge(user_id: int | None = None) -> None:
    """Проход очереди удаления — вся (start_purger) или одного пользователя (стирание его истории);
    пути, которые ещё пишутся (SAVING), пропускаются."""
    where, args = ("WHERE user_id = ?", (user_id,)) if user_id is not None else ("", ())
    rows = [(r["media_id"], r["rel_path"]) for r in
            state.DB.execute(f"SELECT media_id, rel_path FROM purge {where}", args) if r["media_id"] not in SAVING]
    if rows:
        await _purge(rows)


def waiting(user_id: int | None = None) -> tuple[int, float | None]:
    """Очередь удаления пользователя или вся (панель — только числом): сколько путей ждут и когда
    встал самый старый; пути, которые пишутся сейчас (SAVING), не в счёт."""
    where, args = ("WHERE user_id = ?", (user_id,)) if user_id is not None else ("", ())
    queued = [r["queued"] for r in state.DB.execute(f"SELECT media_id, queued FROM purge {where}", args)
              if r["media_id"] not in SAVING]
    return len(queued), min(queued, default=None)


def pending(user_id: int) -> int:
    """Сколько файлов пользователя ждут в очереди удаления (кроме тех, что пишутся сейчас)."""
    return waiting(user_id)[0]


def usage(user_id: int) -> dict:
    """Свои файлы по видам KINDS — из строк media (размер оригинала, превью не в счёт), шару не трогает."""
    found = {r["kind"]: (r["files"], r["bytes"]) for r in state.DB.execute(
        "SELECT CASE WHEN mime LIKE 'image/%' THEN 'image' WHEN mime LIKE 'video/%' THEN 'video'"
        " WHEN mime LIKE 'audio/%' THEN 'audio'"
        " WHEN mime LIKE 'text/%' OR mime IN ('application/pdf', 'application/json', ?) THEN 'document'"
        " ELSE 'other' END AS kind, COUNT(*) AS files, SUM(size) AS bytes FROM media WHERE user_id = ?"
        " GROUP BY kind", (DOCX, user_id))}
    kinds = [{"kind": k, "files": found.get(k, (0, 0))[0], "bytes": found.get(k, (0, 0))[1]} for k in KINDS]
    return {"kinds": kinds, "total": {"files": sum(k["files"] for k in kinds), "bytes": sum(k["bytes"] for k in kinds)}}


async def _purge_loop() -> None:
    while True:
        try:
            await purge()
        except Exception as e:                    # noqa: BLE001 — очередь не должна вставать
            log.warning("очередь удаления: сбой прохода (%s)", type(e).__name__)
        await clock.SLEEP(PURGE_SEC)


def start_purger() -> asyncio.Task:
    """Первый проход — сразу: при старте службы доудаляется оставшееся от прошлой (в том числе
    недописанное до остановки)."""
    return asyncio.create_task(_purge_loop())


def alive(user_id: int, media_id: str) -> bool:
    """Строка media есть (удаление снимает её до того, как ждать шару). С цикла событий — сразу перед
    INSERT строки, которая на неё сошлётся, без await между ними."""
    return state.DB.execute(
        "SELECT 1 FROM media WHERE id = ? AND user_id = ?", (media_id, user_id)).fetchone() is not None


def referenced(user_id: int, media_id: str) -> bool:
    """На файл ссылается запись истории — картинка (итог, исходник, маска), ролик, вложение чата,
    расшифровка (запись, итог), голос или озвучка: удаление другой записи его не трогает."""
    return state.DB.execute(
        "SELECT 1 FROM images WHERE user_id = ? AND ? IN (media_id, source_media_id, mask_media_id)"
        " UNION ALL SELECT 1 FROM videos WHERE user_id = ? AND ? IN (media_id, source_media_id)"
        " UNION ALL SELECT 1 FROM attachments WHERE user_id = ? AND media_id = ?"
        " UNION ALL SELECT 1 FROM transcripts WHERE user_id = ? AND ? IN (source_media_id, result_media_id)"
        " UNION ALL SELECT 1 FROM voices WHERE user_id = ? AND media_id = ?"
        " UNION ALL SELECT 1 FROM speeches WHERE user_id = ? AND media_id = ? LIMIT 1",
        (user_id, media_id) * 6).fetchone() is not None


async def delete_async(user_id: int, media_id: str) -> None:
    """Строка уходит сразу, её путь — в очередь удаления (одной транзакцией, до всякого await):
    новое задание исходником файл уже не возьмёт (alive), а путь не потеряется. Файлы — в потоке
    (зависшая шара не останавливает службу); не удалились — доудалит проход очереди, не ошибка.
    Нет файла или строки — тоже не ошибка."""
    row = state.DB.execute("SELECT id, rel_path FROM media WHERE id = ? AND user_id = ?",
                           (media_id, user_id)).fetchone()
    if row is None:
        return
    rows = [(row["id"], row["rel_path"])]
    state.DB.execute("BEGIN")
    try:
        queue(user_id, rows)
        state.DB.execute("DELETE FROM media WHERE id = ? AND user_id = ?", (media_id, user_id))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    await _purge(rows)


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


def disposition(name: str) -> str:
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
        headers["Content-Disposition"] = disposition(row["download_name"])
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
