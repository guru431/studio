"""Озвучка (спека 2026-10-20-studio-sections-voice-design, 6.4): текст до TEXT_MAX → mp3.

1. Подключение и модель — раздел «Озвучка» (sections.resolve). srvllm (Qwen3-TTS `/clone`) говорит
   голосом по образцу пользователя (voices.py) фразами по 3–12 с: текст делится на фразы
   (`phrases`, до PHRASE_MAX знаков), фразы озвучиваются по одной. Внешний сервис — голос по имени,
   фразами до EXTERNAL_MAX (у OpenAI предел ввода 4096) — обычно одним запросом.
2. Задание — в фоне, как расшифровка: перед первой фразой srvllm освобождает модели картинок и
   gemma-abl (speech.free_card); 503 (карта занята, Qwen говорит другое) — ждём и повторяем раз в
   RETRY_SEC до WAIT_MAX_SEC. Части — во временные файлы, склейка и mp3 — ffmpeg (audio.mp3_of),
   итог — media `audio/mpeg`. Одно идущее задание на пользователя; отмена, перезапуск — как у
   transcripts.py.
3. Журнал — только id и коды: текст и звук туда не пишутся.
"""
import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import audio
import clock
import media
import sections
import speech
import state
import voices

TEXT_MAX = 2000
PHRASE_MAX = 220
EXTERNAL_MAX = 4000
VOICE_NAME_MAX = 64
RETRY_SEC = 15
WAIT_MAX_SEC = 1800
TERMINAL = {"done", "error", "cancelled"}
INTERRUPTED = "прервано перезапуском студии"

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/speeches")


class JobError(Exception):
    """Отказ задания словами для человека."""


class SpeechIn(BaseModel):
    text: str = Field(max_length=TEXT_MAX * 2)
    voice: str = Field(max_length=VOICE_NAME_MAX)
    connection_id: "int | None" = None
    model: "str | None" = Field(default=None, max_length=200)


@dataclass
class Job:
    id: str
    user_id: int
    conn: object
    model: str
    voice: str
    text: str
    stage: str = "готовлю"
    task: "asyncio.Task | None" = None
    by_user: bool = False
    files: list = field(default_factory=list)


JOBS: dict[str, Job] = {}


# ---- фразы ---------------------------------------------------------------------------

def _pieces(sentence: str, limit: int) -> list[str]:
    """Предложение длиннее limit — по запятым и точкам с запятой, затем по пробелам, затем — режем."""
    if len(sentence) <= limit:
        return [sentence]
    out = []
    for chunk in re.split(r"(?<=[,;:])\s+", sentence):
        words = chunk.split(" ") if len(chunk) > limit else [chunk]
        for word in words:
            while len(word) > limit:
                out.append(word[:limit])
                word = word[limit:]
            if out and len(out[-1]) + 1 + len(word) <= limit:
                out[-1] = f"{out[-1]} {word}"
            elif word:
                out.append(word)
    return out


def phrases(text: str, limit: int = PHRASE_MAX) -> list[str]:
    """Фразы для озвучки: по концам предложений и переводам строк, соседние короткие — вместе до limit."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+|\n+", text) if s.strip()]
    out: list[str] = []
    for sentence in sentences:
        for piece in _pieces(" ".join(sentence.split()), limit):
            if out and len(out[-1]) + 1 + len(piece) <= limit:
                out[-1] = f"{out[-1]} {piece}"
            else:
                out.append(piece)
    return out


# ---- задание -------------------------------------------------------------------------

def _row(user_id: int, sid: str):
    row = state.DB.execute("SELECT * FROM speeches WHERE id = ? AND user_id = ?", (sid, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такой озвучки нет"})
    return row


def _out(row) -> dict:
    job = JOBS.get(row["id"])
    return {"id": row["id"], "text": row["text"], "voice": row["voice"], "model": row["model"], "state": row["state"],
            "detail": job.stage if job is not None and row["state"] == "working" else row["detail"],
            "media_id": row["media_id"], "duration": row["duration"], "created": row["created"],
            "finished": row["finished"]}


def _store(job: Job) -> None:
    state.DB.execute("UPDATE speeches SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _finish(job: Job, state_: str, detail: str, **extra) -> None:
    cols = "".join(f", {k} = ?" for k in extra)                  # имена — из кода, не из запроса
    state.DB.execute(f"UPDATE speeches SET state = ?, detail = ?, finished = ?{cols} WHERE id = ?",
                     (state_, detail, clock.NOW(), *extra.values(), job.id))


def _active(user_id: int) -> "Job | None":
    return next((j for j in JOBS.values() if j.user_id == user_id and j.task is not None and not j.task.done()), None)


async def _say(job: Job, text: str, voice, label: str) -> tuple[bytes, str]:
    waited_from = None
    while True:
        try:
            return await speech.say(job.conn, job.model, text, voice)
        except speech.Busy:
            now = clock.NOW()
            waited_from = now if waited_from is None else waited_from
            if now - waited_from >= WAIT_MAX_SEC:
                raise JobError(f"карта занята дольше {WAIT_MAX_SEC // 60} минут — попробуйте позже") from None
            job.stage = f"жду карту · {label}"
            _store(job)
            await clock.SLEEP(RETRY_SEC)
            job.stage = label
            _store(job)


async def run(job: Job) -> None:
    try:
        try:
            srvllm = job.conn.kind == "srvllm"
            pieces = phrases(job.text, PHRASE_MAX if srvllm else EXTERNAL_MAX)
            if srvllm:
                try:
                    voice = await voices.reference(job.user_id, job.voice)
                except HTTPException:
                    raise JobError("голос удалён") from None
                await speech.free_card(job.conn)
            else:
                voice = job.voice
            for n, piece in enumerate(pieces, 1):
                label = f"озвучиваю {n} из {len(pieces)}"
                job.stage = label
                _store(job)
                data, mime = await _say(job, piece, voice, label)
                part = audio.temp(".wav" if mime == "audio/wav" else ".mp3")
                job.files.append(part)
                await asyncio.to_thread(part.write_bytes, data)
            out = audio.temp(".mp3")
            job.files.append(out)
            await audio.mp3_of(job.files[:-1], out)
            duration = await audio.probe(out)
            data = await asyncio.to_thread(out.read_bytes)
            media_id = await media.save_async(job.user_id, data, "audio/mpeg", f"speech-{job.id[:8]}.mp3")
            _finish(job, "done", "готово", media_id=media_id, duration=duration)
            log.info("озвучка %s: готова", job.id[:8])
        except (JobError, audio.AudioError, speech.SpeechError, media.StorageError) as e:
            _finish(job, "error", str(e))
            log.info("озвучка %s: ошибка %s", job.id[:8], type(e).__name__)
        except Exception as e:                       # noqa: BLE001 — задание не должно висеть
            _finish(job, "error", f"сбой службы: {type(e).__name__}")
            log.warning("озвучка %s: сбой %s", job.id[:8], type(e).__name__)
    except asyncio.CancelledError:
        if job.by_user:
            _finish(job, "cancelled", "отменено")
        else:
            _finish(job, "error", INTERRUPTED)
            raise
    finally:
        for f in job.files:
            await asyncio.to_thread(f.unlink, missing_ok=True)
        job.text = ""


def recover() -> None:
    count = state.DB.execute("UPDATE speeches SET state = 'error', detail = ?, finished = ?"
                             " WHERE state NOT IN ('done', 'error', 'cancelled')", (INTERRUPTED, clock.NOW())).rowcount
    if count:
        log.warning("озвучки: %d прервано перезапуском", count)


async def _cancel(jobs: list[Job]) -> None:
    for job in jobs:
        job.by_user = True
        job.task.cancel()
    await asyncio.gather(*(j.task for j in jobs), return_exceptions=True)


async def stop_user(user_id: int) -> None:
    await _cancel([j for j in JOBS.values() if j.user_id == user_id and j.task is not None and not j.task.done()])


async def stop() -> None:
    tasks = [j.task for j in JOBS.values() if j.task is not None and not j.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- API -----------------------------------------------------------------------------

@router.post("", status_code=202)
async def create(body: SpeechIn, request: Request):
    uid = request.state.user.id
    text = body.text.strip()
    if not text or len(text) > TEXT_MAX:
        raise HTTPException(422, {"code": "text", "message": f"текст — от 1 до {TEXT_MAX} знаков"})
    conn, model = sections.resolve(uid, "speech", body.connection_id, body.model)
    voice = body.voice.strip()
    if conn.kind == "srvllm":
        voices._row(uid, voice)                                   # чужой и неизвестный голос — 404 gone
    elif not voice:
        raise HTTPException(422, {"code": "voice", "message": "назовите голос сервиса"})
    if _active(uid) is not None:
        raise HTTPException(409, {"code": "busy", "message": "идёт другая озвучка — дождитесь её или отмените"})
    sid, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO speeches(id, user_id, connection_id, model, voice, text, state, detail, created)"
                     " VALUES (?, ?, ?, ?, ?, ?, 'working', 'готовлю', ?)", (sid, uid, conn.id, model, voice, text, now))
    job = Job(id=sid, user_id=uid, conn=conn, model=model, voice=voice, text=text)
    JOBS[sid] = job
    job.task = asyncio.create_task(run(job))
    log.info("озвучка %s: принята", sid[:8])
    return _out(_row(uid, sid))


@router.get("")
async def list_speeches(request: Request, before: "float | None" = None, limit: int = Query(60, ge=1, le=100)):
    uid = request.state.user.id
    if before is None:
        rows = state.DB.execute("SELECT * FROM speeches WHERE user_id = ? ORDER BY created DESC, id DESC LIMIT ?",
                                (uid, limit))
    else:
        rows = state.DB.execute("SELECT * FROM speeches WHERE user_id = ? AND created < ?"
                                " ORDER BY created DESC, id DESC LIMIT ?", (uid, before, limit))
    return {"items": [_out(r) for r in rows]}


@router.get("/{sid}")
async def get_speech(sid: str, request: Request):
    return _out(_row(request.state.user.id, sid))


@router.post("/{sid}/cancel")
async def cancel(sid: str, request: Request):
    uid = request.state.user.id
    _row(uid, sid)
    job = JOBS.get(sid)
    if job is not None and job.task is not None and not job.task.done():
        await _cancel([job])
    return {"state": _row(uid, sid)["state"]}


@router.delete("/{sid}", status_code=204)
async def delete(sid: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, sid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    state.DB.execute("DELETE FROM speeches WHERE id = ?", (sid,))
    JOBS.pop(sid, None)
    if row["media_id"] and not media.referenced(uid, row["media_id"]):
        await media.delete_async(uid, row["media_id"])
    return Response(status_code=204)
