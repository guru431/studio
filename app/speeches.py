"""Озвучка (спека 2026-10-20-studio-sections-voice-design, 6.4): текст до TEXT_MAX → mp3.

1. Подключение и модель — раздел «Речь» (sections.resolve). srvllm (Qwen3-TTS `/clone`) говорит
   голосом по образцу пользователя (voices.py) фразами по 3–12 с: текст делится на фразы
   (`phrases`, до PHRASE_MAX знаков), фразы озвучиваются по одной. Внешний сервис — голос по имени,
   фразами до EXTERNAL_MAX (у OpenAI предел ввода 4096) — обычно одним запросом.
2. Задание — в фоне, как расшифровка: перед первой фразой srvllm освобождает модели картинок и
   gemma-abl (speech.free_card); 503 (карта занята, Qwen говорит другое) — ждём и повторяем раз в
   RETRY_SEC до WAIT_MAX_SEC. Части — во временные файлы, склейка и mp3 — ffmpeg (audio.mp3_of),
   итог — media `audio/mpeg`. Задания пользователя — очередью, как у transcripts.py (п. 7 её шапки:
   `queued` «в очереди · N», один раннер, до QUEUE_MAX ждущих, ждущие переживают перезапуск); отмена,
   перезапуск — как там.
3. Журнал — только id и коды: текст и звук туда не пишутся.
4. JOBS — только идущие задания, как у transcripts.py: кончившееся уходит из него, убрав временные
   файлы, вместе с записью итога; долговечное — строка `speeches`.
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
import connections
import db
import documents
import media
import operations
import push
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
QUEUE_MAX = 5
QUEUED = "в очереди"

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
#: user_id → раннер его очереди
RUNNERS: dict[int, asyncio.Task] = {}


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


def waiting(row) -> str:
    """Слова ждущей озвучки: место в очереди пользователя."""
    ahead = state.DB.execute("SELECT COUNT(*) FROM speeches WHERE user_id = ? AND state = 'queued'"
                             " AND (created < ? OR (created = ? AND id < ?))",
                             (row["user_id"], row["created"], row["created"], row["id"])).fetchone()[0]
    return f"{QUEUED} · {ahead + 1}"


def _out(row) -> dict:
    job = JOBS.get(row["id"])
    return {"id": row["id"], "text": row["text"], "voice": row["voice"], "model": row["model"], "state": row["state"],
            "detail": job.stage if job is not None and row["state"] == "working"
            else waiting(row) if row["state"] == "queued" else row["detail"],
            "media_id": row["media_id"], "duration": row["duration"], "created": row["created"],
            "finished": row["finished"]}


def _store(job: Job) -> None:
    state.DB.execute("UPDATE speeches SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _finish(sid: str, state_: str, detail: str, **extra) -> None:
    cols = "".join(f", {k} = ?" for k in extra)                  # имена — из кода, не из запроса
    changed = state.DB.execute(f"UPDATE speeches SET state = ?, detail = ?, finished = ?{cols}"
                               " WHERE id = ? AND finished IS NULL",
                               (state_, detail, clock.NOW(), *extra.values(), sid)).rowcount
    if changed:
        push.job_finished("speeches", sid)


def _full_queue(user_id: int) -> bool:
    return state.DB.execute("SELECT COUNT(*) FROM speeches WHERE user_id = ? AND state = 'queued'",
                            (user_id,)).fetchone()[0] >= QUEUE_MAX


def kick(user_id: int) -> None:
    """Раннер очереди пользователя, если его нет."""
    task = RUNNERS.get(user_id)
    if task is None or task.done():
        task = RUNNERS[user_id] = asyncio.create_task(_runner(user_id))
        task.add_done_callback(lambda t: RUNNERS.pop(user_id) if RUNNERS.get(user_id) is t else None)


async def _runner(user_id: int) -> None:
    """Старейшая ждущая озвучка пользователя — в работу, по одной, пока очередь не пуста."""
    while True:
        row = state.DB.execute("SELECT * FROM speeches WHERE user_id = ? AND state = 'queued' ORDER BY created, id"
                               " LIMIT 1", (user_id,)).fetchone()
        if row is None:
            return
        if not state.DB.execute("UPDATE speeches SET state = 'working', detail = 'готовлю' WHERE id = ? AND state = 'queued'",
                                (row["id"],)).rowcount:
            continue
        try:
            conn = connections.get(user_id, row["connection_id"])
        except HTTPException:                        # подключение удалили, пока озвучка ждала
            conn = None
        job = Job(id=row["id"], user_id=user_id, conn=conn, model=row["model"], voice=row["voice"], text=row["text"])
        JOBS[job.id] = job
        job.task = asyncio.create_task(run(job))
        await job.task


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
    """Итог — в строку последним, после уборки временных файлов (п. 4 шапки)."""
    outcome = ("error", INTERRUPTED, {})              # остановка службы
    try:
        try:
            if job.conn is None:
                raise JobError("подключение раздела удалено из профиля")
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
            outcome = ("done", "готово", {"media_id": media_id, "duration": duration})
            log.info("озвучка %s: готова", job.id[:8])
        except (JobError, audio.AudioError, speech.SpeechError, media.StorageError) as e:
            outcome = ("error", str(e), {})
            log.info("озвучка %s: ошибка %s", job.id[:8], type(e).__name__)
        except Exception as e:                       # noqa: BLE001 — задание не должно висеть
            outcome = ("error", f"сбой службы: {type(e).__name__}", {})
            log.warning("озвучка %s: сбой %s", job.id[:8], type(e).__name__)
    except asyncio.CancelledError:
        if not job.by_user:
            raise
        outcome = ("cancelled", "отменено", {})
    finally:
        job.text = ""
        try:
            for f in job.files:
                await asyncio.to_thread(f.unlink, missing_ok=True)
        finally:                                     # и вторая отмена посреди уборки итог не теряет
            state_, detail, extra = outcome
            _finish(job.id, state_, detail, **extra)
            JOBS.pop(job.id, None)


def recover() -> None:
    """Идущие — «прервано перезапуском»; ждущие остаются в очереди — их продолжит `resume`."""
    count = state.DB.execute("UPDATE speeches SET state = 'error', detail = ?, finished = ?"
                             " WHERE state NOT IN ('done', 'error', 'cancelled', 'queued')",
                             (INTERRUPTED, clock.NOW())).rowcount
    if count:
        log.warning("озвучки: %d прервано перезапуском", count)


def resume() -> None:
    """Старт службы (после recover и state.HTTP): очереди с ждущими озвучками — снова в работу."""
    for (uid,) in state.DB.execute("SELECT DISTINCT user_id FROM speeches WHERE state = 'queued'").fetchall():
        kick(uid)


async def _cancel(jobs: list[Job]) -> None:
    for job in jobs:
        job.by_user = True
        job.task.cancel()
    await asyncio.gather(*(j.task for j in jobs), return_exceptions=True)


def _drop_waiting(user_id: int, sid: "str | None" = None) -> int:
    """Ждущие озвучки пользователя (или одна) — `cancelled`; итог — сколько."""
    rows = state.DB.execute("SELECT id FROM speeches WHERE user_id = ? AND state = 'queued'"
                            + (" AND id = ?" if sid else ""), (user_id, sid) if sid else (user_id,)).fetchall()
    for (wid,) in rows:
        _finish(wid, "cancelled", "отменено")
    return len(rows)


async def stop_user(user_id: int) -> None:
    _drop_waiting(user_id)
    await _cancel([j for j in JOBS.values() if j.user_id == user_id and j.task is not None and not j.task.done()])


async def stop() -> None:
    tasks = list(RUNNERS.values()) + [j.task for j in JOBS.values() if j.task is not None and not j.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- API -----------------------------------------------------------------------------

@router.post("", status_code=202)
async def create(body: SpeechIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    text = documents.nfc(body.text).strip()           # текст идёт в поиск (search_speeches_*), как у документов
    if not text or len(text) > TEXT_MAX:
        raise HTTPException(422, {"code": "text", "message": f"текст — от 1 до {TEXT_MAX} знаков"})
    conn, model = sections.resolve(uid, "speech", body.connection_id, body.model)
    voice = body.voice.strip()
    if conn.kind == "srvllm":
        voices._row(uid, voice)                                   # чужой и неизвестный голос — 404 gone
    elif not voice:
        raise HTTPException(422, {"code": "voice", "message": "назовите голос сервиса"})
    if _full_queue(uid):
        raise HTTPException(409, {"code": "busy", "message": (
            f"в очереди уже {QUEUE_MAX} озвучек — дождитесь их или отмените")})
    sid, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO speeches(id, user_id, connection_id, model, voice, text, state, detail, created)"
                     " VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)", (sid, uid, conn.id, model, voice, text, QUEUED, now))
    kick(uid)
    log.info("озвучка %s: принята", sid[:8])
    return _out(_row(uid, sid))


@router.get("")
async def list_speeches(request: Request, before: "float | None" = None,
                        before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    """Новые сверху; следующая страница — before=<created>&before_id=<id> последней."""
    rows = db.page(state.DB, "speeches", request.state.user.id, before, before_id, limit)
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
    elif _drop_waiting(uid, sid):
        log.info("озвучка %s: снята из очереди", sid[:8])
    return {"state": _row(uid, sid)["state"]}


@router.delete("/{sid}", status_code=204)
async def delete(sid: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, sid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    state.DB.execute("DELETE FROM speeches WHERE id = ?", (sid,))
    if row["media_id"] and not media.referenced(uid, row["media_id"]):
        await media.delete_async(uid, row["media_id"])
    return Response(status_code=204)
