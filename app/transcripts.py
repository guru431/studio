"""Расшифровка записей (спека 2026-10-20-studio-sections-voice-design, 6.3).

1. Загрузка — тело запроса и есть запись (`Content-Type` — её тип, имя — заголовком `X-Studio-Name`,
   `encodeURIComponent`: строка запроса попала бы в журнал Apache). Тело идёт потоком на шару
   (`media.save_stream`, до UPLOAD_MAX) — не в память: служба живёт в MemoryMax=2G. Тип записи — по
   заголовку, иначе по расширению имени; не звук — 415. Одно идущее задание на пользователя — 409.
2. Задание — в фоне, как картинки: `probe` (длиннее DURATION_MAX — отказ словами), сжатие в mp3
   16 кГц моно во временный файл (audio.py), освобождение моделей картинок и gemma-abl
   (speech.free_card), распознавание подключением раздела «Расшифровка» (WhisperX с собеседниками).
   Карта занята (503) — ждём и повторяем раз в RETRY_SEC до WAIT_MAX_SEC: чужая генерация — не
   отказ. Состояние словами: «готовлю запись», «распознаю · м:сс», «жду карту · м:сс».
3. Итог — JSON реплик в media (`result_media_id`), собеседники — «Собеседник N» (speech.speakers);
   исходная запись остаётся (её слушают в плеере). «Отменить» обрывает своё ожидание и запрос
   (обрыв шлюз дальше не передаёт — WhisperX дослушает запись в пустоту; освободить слот `asr`
   нечем). Перезапуск службы — недоделанное становится ошибкой (`recover`).
4. Журнал — только id и коды: имя файла, звук и текст туда не пишутся. База — только с цикла
   событий; файлы и ffmpeg — в потоке и подпроцессе.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

import audio
import clock
import exports
import forms
import media
import sections
import speech
import state

UPLOAD_MAX = 200 * 1024 * 1024
DURATION_MAX = 60 * 60
NAME_MAX = 255
RETRY_SEC = 15
WAIT_MAX_SEC = 1800
TERMINAL = {"done", "error", "cancelled"}
INTERRUPTED = "прервано перезапуском студии"

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/transcripts")


class JobError(Exception):
    """Отказ задания словами для человека."""


@dataclass
class Job:
    id: str
    user_id: int
    conn: object
    model: str
    source_media_id: str
    stage: str = "готовлю запись"
    stage_at: float = 0.0
    task: "asyncio.Task | None" = None
    by_user: bool = False                    # отмена — человеком (иначе — остановка службы)


JOBS: dict[str, Job] = {}


def _clock(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60}:{s % 60:02d}"


def detail(job: Job) -> str:
    """Состояние идущего задания словами со временем этапа."""
    if job.stage in ("распознаю", "жду карту"):
        return f"{job.stage} · {_clock(clock.NOW() - job.stage_at)}"
    return job.stage


def _row(user_id: int, tid: str):
    row = state.DB.execute("SELECT * FROM transcripts WHERE id = ? AND user_id = ?", (tid, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такой расшифровки нет"})
    return row


def _out(row, segments: "list | None" = None) -> dict:
    job = JOBS.get(row["id"])
    out = {"id": row["id"], "name": row["name"], "state": row["state"],
           "detail": detail(job) if job is not None and row["state"] == "working" else row["detail"],
           "model": row["model"], "duration": row["duration"], "speakers": row["speakers"],
           "source_media_id": row["source_media_id"], "created": row["created"], "finished": row["finished"]}
    if segments is not None:
        out["segments"] = segments
    return out


def _store(job: Job) -> None:
    state.DB.execute("UPDATE transcripts SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _finish(job: Job, state_: str, detail_: str, **extra) -> None:
    cols = "".join(f", {k} = ?" for k in extra)                  # имена — из кода, не из запроса
    state.DB.execute(f"UPDATE transcripts SET state = ?, detail = ?, finished = ?{cols} WHERE id = ?",
                     (state_, detail_, clock.NOW(), *extra.values(), job.id))


def _active(user_id: int) -> "Job | None":
    return next((j for j in JOBS.values() if j.user_id == user_id and j.task is not None and not j.task.done()), None)


def _busy() -> HTTPException:
    return HTTPException(409, {"code": "busy", "message": "идёт другая расшифровка — дождитесь её или отмените"})


async def _recognize(job: Job, path) -> list:
    waited_from = None
    job.stage, job.stage_at = "распознаю", clock.NOW()
    _store(job)
    while True:
        try:
            return await speech.transcribe(job.conn, job.model, path, "audio/mpeg")
        except speech.Busy:
            now = clock.NOW()
            waited_from = now if waited_from is None else waited_from
            if now - waited_from >= WAIT_MAX_SEC:
                raise JobError(f"карта занята дольше {WAIT_MAX_SEC // 60} минут — попробуйте позже") from None
            job.stage, job.stage_at = "жду карту", waited_from
            _store(job)
            await clock.SLEEP(RETRY_SEC)
            job.stage, job.stage_at = "распознаю", clock.NOW()
            _store(job)


async def run(job: Job) -> None:
    tmp = audio.temp(".mp3")
    try:
        try:
            try:
                source = media.path(job.user_id, job.source_media_id)
            except HTTPException:
                raise JobError("запись удалена") from None
            duration = await audio.probe(source)
            state.DB.execute("UPDATE transcripts SET duration = ? WHERE id = ?", (duration, job.id))
            if duration > DURATION_MAX:
                raise JobError(f"запись длиннее {DURATION_MAX // 60} минут — разделите её")
            await audio.compress(source, tmp)
            await speech.free_card(job.conn)
            replicas = speech.speakers(await _recognize(job, tmp))
            if not replicas:
                raise JobError("в записи не распознано ни слова")
            payload = json.dumps({"segments": [asdict(s) for s in replicas]}, ensure_ascii=False).encode("utf-8")
            result = await media.save_async(job.user_id, payload, "application/json", "transcript.json")
            _finish(job, "done", "готово", result_media_id=result,
                    speakers=len({s.speaker for s in replicas if s.speaker}))
            log.info("расшифровка %s: готова", job.id[:8])
        except (JobError, audio.AudioError, speech.SpeechError, media.StorageError) as e:
            _finish(job, "error", str(e))
            log.info("расшифровка %s: ошибка %s", job.id[:8], type(e).__name__)
        except Exception as e:                       # noqa: BLE001 — задание не должно висеть
            _finish(job, "error", f"сбой службы: {type(e).__name__}")
            log.warning("расшифровка %s: сбой %s", job.id[:8], type(e).__name__)
    except asyncio.CancelledError:
        if job.by_user:
            _finish(job, "cancelled", "отменено")
            log.info("расшифровка %s: отменена", job.id[:8])
        else:
            _finish(job, "error", INTERRUPTED)
            raise
    finally:
        await asyncio.to_thread(tmp.unlink, missing_ok=True)


def recover() -> None:
    """После перезапуска идущих заданий нет: их строки — ошибка «прервано перезапуском»."""
    count = state.DB.execute("UPDATE transcripts SET state = 'error', detail = ?, finished = ?"
                             " WHERE state NOT IN ('done', 'error', 'cancelled')", (INTERRUPTED, clock.NOW())).rowcount
    if count:
        log.warning("расшифровки: %d прервано перезапуском", count)


async def _cancel(jobs: list[Job]) -> None:
    for job in jobs:
        job.by_user = True
        job.task.cancel()
    await asyncio.gather(*(j.task for j in jobs), return_exceptions=True)


async def stop_user(user_id: int) -> None:
    """Стирание истории, удаление и отключение учётки: её расшифровки останавливаются."""
    await _cancel([j for j in JOBS.values() if j.user_id == user_id and j.task is not None and not j.task.done()])


async def stop() -> None:
    """Остановка службы: задания записывают «прервано» и кончаются до закрытия базы."""
    tasks = [j.task for j in JOBS.values() if j.task is not None and not j.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- API -------------------------------------------------------------------

def _name(raw: str) -> str:
    name = unquote(raw or "").strip().replace("\x00", "")[:NAME_MAX]
    return name or "запись"


def _int_header(request: Request, key: str) -> "int | None":
    value = request.headers.get(key, "").strip()
    return int(value) if value.isdigit() else None


@router.post("", status_code=202)
async def upload(request: Request):
    uid = request.state.user.id
    conn, model = sections.resolve(uid, "transcribe", _int_header(request, "x-studio-connection"),
                                   request.headers.get("x-studio-model") or None)
    if _active(uid) is not None:
        raise _busy()
    name = _name(request.headers.get("x-studio-name", ""))
    mime = forms.audio_type(request.headers.get("content-type", ""), name)
    if mime is None:
        raise HTTPException(415, {"code": "format", "message": forms.NOT_AUDIO})
    length = _int_header(request, "content-length")
    too_big = {"code": "too_big", "message": f"запись больше {UPLOAD_MAX // (1024 * 1024)} МиБ"}
    if length is not None and length > UPLOAD_MAX:
        raise HTTPException(413, too_big)
    try:
        source = await media.save_stream(uid, request.stream(), mime, name, UPLOAD_MAX)
    except media.TooBig:
        raise HTTPException(413, too_big) from None
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    if _active(uid) is not None or not media.alive(uid, source):    # пока шла загрузка
        await media.delete_async(uid, source)
        raise _busy()
    tid, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO transcripts(id, user_id, connection_id, model, name, source_media_id, state, detail,"
                     " created) VALUES (?, ?, ?, ?, ?, ?, 'working', 'готовлю запись', ?)",
                     (tid, uid, conn.id, model, name, source, now))
    job = Job(id=tid, user_id=uid, conn=conn, model=model, source_media_id=source, stage_at=now)
    JOBS[tid] = job
    job.task = asyncio.create_task(run(job))
    log.info("расшифровка %s: принята", tid[:8])
    return _out(_row(uid, tid))


@router.get("")
async def list_transcripts(request: Request, before: "float | None" = None, limit: int = Query(60, ge=1, le=100)):
    uid = request.state.user.id
    if before is None:
        rows = state.DB.execute("SELECT * FROM transcripts WHERE user_id = ? ORDER BY created DESC, id DESC LIMIT ?",
                                (uid, limit))
    else:
        rows = state.DB.execute("SELECT * FROM transcripts WHERE user_id = ? AND created < ?"
                                " ORDER BY created DESC, id DESC LIMIT ?", (uid, before, limit))
    return {"items": [_out(r) for r in rows]}


async def segments_of(user_id: int, row) -> list:
    """Реплики готовой расшифровки — с шары; файла нет — 404, шара недоступна — 503."""
    try:
        data = await asyncio.to_thread(media.path(user_id, row["result_media_id"]).read_bytes)
    except FileNotFoundError:
        raise HTTPException(404, media.GONE) from None
    except OSError:
        raise HTTPException(503, forms.STORAGE) from None
    return json.loads(data)["segments"]


@router.get("/{tid}")
async def get_transcript(tid: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, tid)
    if row["state"] != "done" or row["result_media_id"] is None:
        return _out(row)
    return _out(row, await segments_of(uid, row))


@router.post("/{tid}/cancel")
async def cancel(tid: str, request: Request):
    uid = request.state.user.id
    _row(uid, tid)
    job = JOBS.get(tid)
    if job is not None and job.task is not None and not job.task.done():
        await _cancel([job])
    return {"state": _row(uid, tid)["state"]}


@router.delete("/{tid}", status_code=204)
async def delete(tid: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, tid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    state.DB.execute("DELETE FROM transcripts WHERE id = ?", (tid,))
    JOBS.pop(tid, None)
    for media_id in {row["source_media_id"], row["result_media_id"]} - {None}:
        if not media.referenced(uid, media_id):
            await media.delete_async(uid, media_id)
    return Response(status_code=204)


@router.get("/{tid}/export")
async def export(tid: str, request: Request, format: str = Query(..., pattern="^(txt|docx|srt)$")):  # noqa: A002
    """Готовая расшифровка файлом: имя — по записи («Звонок.m4a» → «Звонок.docx»)."""
    uid = request.state.user.id
    row = _row(uid, tid)
    if row["state"] != "done" or row["result_media_id"] is None:
        raise HTTPException(409, {"code": "running", "message": "расшифровка ещё не готова"})
    segments = await segments_of(uid, row)
    stem = row["name"].rsplit(".", 1)[0] if "." in row["name"] else row["name"]
    if format == "docx":
        body = exports.docx(segments, stem)
    else:
        body = (exports.txt(segments) if format == "txt" else exports.srt(segments)).encode("utf-8")
    return Response(body, media_type=exports.TYPES[format],
                    headers={"Content-Disposition": media.disposition(f"{stem}.{format}"), "Cache-Control": "no-store"})
