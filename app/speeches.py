"""Озвучка (спека 2026-10-20-studio-sections-voice-design, 6.4): текст до TEXT_MAX → mp3.

1. Подключение и модель — раздел «Озвучка», id `speech` (sections.resolve). srvllm (Qwen3-TTS `/clone`) говорит
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
5. «Озвучить заново» (PATCH) записи со звуком прежнее не стирает: поля KEPT — в `previous` (JSON), звук — в
   `previous_media_id`. Готово — прежний mp3 удаляется (если на него больше никто не ссылается); отмена — запись
   как была; отказ или перезапуск — прежние текст и звук с ошибкой словами (`KEPT_NOTE`). Очередь и «В работе»
   идут по времени постановки `queued` (у строк прежнего кода его нет — `created`), место в чате — по `created`.
"""
import asyncio
import json
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
import lock_policy
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
#: поля записи, которые повторная озвучка заменяет, а отказ и отмена возвращают (п. 5 шапки)
KEPT = ("text", "connection_id", "model", "voice", "voice_name", "duration", "state", "detail", "finished")
KEPT_NOTE = "прежняя озвучка сохранена"
#: порядок очереди: время постановки, у строк прежнего кода — создания
QUEUE_KEY = "COALESCE(queued, created)"

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/speeches")


class JobError(Exception):
    """Отказ задания словами для человека."""


class SpeechIn(BaseModel):
    text: str = Field(max_length=TEXT_MAX * 2)
    voice: str = Field(max_length=VOICE_NAME_MAX)
    connection_id: "int | None" = None
    model: "str | None" = Field(default=None, max_length=200)
    chat_id: "str | None" = Field(default=None, min_length=1, max_length=64)


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
    ready: int = 0                                    # частей, уже записанных во временные файлы (слушать до склейки)


JOBS: dict[str, Job] = {}
#: user_id → раннер его очереди
RUNNERS: dict[int, asyncio.Task] = {}
#: id ждущей озвучки → эпоха создания (`operations.capture` при постановке; раннер исполняет её под ней)
EPOCHS: dict[str, tuple] = {}


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
    key = queued_at(row)
    ahead = state.DB.execute(f"SELECT COUNT(*) FROM speeches WHERE user_id = ? AND state = 'queued'"
                             f" AND ({QUEUE_KEY} < ? OR ({QUEUE_KEY} = ? AND id < ?))",
                             (row["user_id"], key, key, row["id"])).fetchone()[0]
    return f"{QUEUED} · {ahead + 1}"


def queued_at(row) -> float:
    """Время постановки в очередь (повторная озвучка — заново); строка прежнего кода — время создания."""
    return row["created"] if row["queued"] is None else row["queued"]


def _out(row) -> dict:
    job = JOBS.get(row["id"])
    return {"id": row["id"], "chat_id": row["chat_id"], "voice_name": row["voice_name"],
            "connection_id": row["connection_id"],
            "text": row["text"], "voice": row["voice"], "model": row["model"], "state": row["state"],
            "detail": job.stage if job is not None and row["state"] == "working"
            else waiting(row) if row["state"] == "queued" else row["detail"],
            "media_id": row["media_id"], "duration": row["duration"], "created": row["created"],
            "finished": row["finished"], "parts": job.ready if job is not None and row["state"] == "working" else 0}


def _store(job: Job) -> None:
    state.DB.execute("UPDATE speeches SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _settle(sid: str, state_: str, detail: str, extra: dict) -> "tuple[bool, str | None]":
    """Итог в строку, если она ещё не кончилась; повторная озвучка с прежним звуком — по п. 5 шапки.
    Возвращает, записан ли итог, и id прежнего mp3, который больше не нужен."""
    now = clock.NOW()
    cols, dropped = {"state": state_, "detail": detail, "finished": now, **extra}, None
    row = state.DB.execute("SELECT previous, previous_media_id FROM speeches WHERE id = ?", (sid,)).fetchone()
    if row is not None and row["previous"] is not None:
        if state_ == "done":
            dropped = row["previous_media_id"]
        else:
            cols = json.loads(row["previous"]) | {"media_id": row["previous_media_id"]}
            if state_ == "error":
                cols |= {"state": "error", "detail": f"{detail} — {KEPT_NOTE}", "finished": now}
        cols |= {"previous": None, "previous_media_id": None}
    sets = ", ".join(f"{k} = ?" for k in cols)                    # имена — из кода, не из запроса
    changed = state.DB.execute(f"UPDATE speeches SET {sets} WHERE id = ? AND finished IS NULL",
                               (*cols.values(), sid)).rowcount
    return bool(changed), dropped if changed else None


def _finish(sid: str, state_: str, detail: str, **extra) -> "str | None":
    """Итог задания; уведомление — о готовом и об ошибке (отмена молчит, даже вернув прежнее «готово»).
    Возвращает id прежнего mp3, который зовущий удаляет (`_drop`)."""
    changed, dropped = _settle(sid, state_, detail, extra)
    EPOCHS.pop(sid, None)
    if changed and state_ != "cancelled":
        push.job_finished("speeches", sid)
    return dropped


async def _drop(user_id: int, media_id: "str | None") -> None:
    """Прежний mp3 повторно озвученной записи — если на него больше никто не ссылается."""
    if media_id and not media.referenced(user_id, media_id):
        await media.delete_async(user_id, media_id)


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
    """Старейшая ждущая озвучка пользователя — в работу, по одной, пока очередь не пуста. Каждая — под своей
    эпохой (`EPOCHS`), а не под эпохой запроса, запустившего раннер: после очистки истории живой раннер
    берёт и новые озвучки."""
    lock_policy.RESTRICTED.set(frozenset())          # закрытые разделы запроса, создавшего раннер, ему чужие
    while True:
        row = state.DB.execute(f"SELECT * FROM speeches WHERE user_id = ? AND state = 'queued'"
                               f" ORDER BY {QUEUE_KEY}, id LIMIT 1", (user_id,)).fetchone()
        if row is None:
            return
        epoch = EPOCHS.pop(row["id"], None) or operations.snapshot(user_id)
        if not state.DB.execute("UPDATE speeches SET state = 'working', detail = 'готовлю' WHERE id = ? AND state = 'queued'",
                                (row["id"],)).rowcount:
            continue
        try:
            conn = connections.get(user_id, row["connection_id"])
        except HTTPException:                        # подключение удалили, пока озвучка ждала
            conn = None
        job = Job(id=row["id"], user_id=user_id, conn=conn, model=row["model"], voice=row["voice"], text=row["text"])
        JOBS[job.id] = job
        with operations.restore(epoch):
            job.task = asyncio.create_task(run(job))
        job.task.add_done_callback(lambda _, item=job: _finish_unstarted(item))
        await asyncio.gather(asyncio.shield(job.task), return_exceptions=True)


def _finish_unstarted(job: Job) -> None:
    if JOBS.get(job.id) is job:
        state_, detail = ("cancelled", "отменено") if job.by_user else ("error", INTERRUPTED)
        _finish(job.id, state_, detail)
        JOBS.pop(job.id)


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
            if job.model == "qwen3-tts":             # кончившуюся генерацию картинок снять, не ждать конца аренды
                await speech.free_card(job.conn)
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
                if job.model == "qwen3-tts":
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
                await audio.file_io(part.write_bytes, data)
                job.ready = n
            out = audio.temp(".mp3")
            job.files.append(out)
            await audio.mp3_of(job.files[:-1], out)
            duration = await audio.probe(out)
            data = await audio.file_io(out.read_bytes)
            media_id = await media.save_async(job.user_id, data, "audio/mpeg", f"speech-{job.id[:8]}.mp3")
            outcome = ("done", "готово", {"media_id": media_id, "duration": duration})
            log.info("озвучка %s: готова", job.id[:8])
        except (JobError, audio.AudioError, speech.SpeechError, media.StorageError) as e:
            outcome = ("error", str(e), {})
            log.info("озвучка %s: ошибка %s", job.id[:8], type(e).__name__)
        except HTTPException as e:                   # барьер истории (`operations.check`): 409 очистка, 401 учётка
            outcome = ("error", e.detail.get("message", "сбой службы") if isinstance(e.detail, dict) else "сбой службы", {})
            log.info("озвучка %s: отказ %s", job.id[:8], e.status_code)
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
            await audio.remove_temp(*job.files)
        except asyncio.CancelledError:
            pass
        finally:
            state_, detail, extra = outcome
            dropped = _finish(job.id, state_, detail, **extra)
            JOBS.pop(job.id, None)
            await _drop(job.user_id, dropped)        # отмена посреди удаления файл не теряет: путь уже в очереди удаления


def recover() -> None:
    """Идущие — «прервано перезапуском» (повторная озвучка — с прежним звуком); ждущие остаются в очереди —
    их продолжит `resume`."""
    rows = state.DB.execute("SELECT id FROM speeches WHERE state NOT IN ('done', 'error', 'cancelled', 'queued')"
                            " AND finished IS NULL").fetchall()
    count = sum(_settle(row["id"], "error", INTERRUPTED, {})[0] for row in rows)
    if count:
        log.warning("озвучки: %d прервано перезапуском", count)


def resume() -> None:
    """Старт службы (после recover и state.HTTP): очереди с ждущими озвучками — снова в работу, каждая — с
    эпохой старта (очистка истории после него их сохранение запрещает, как у принятых запросом)."""
    rows = state.DB.execute("SELECT id, user_id FROM speeches WHERE state = 'queued'").fetchall()
    for row in rows:
        EPOCHS[row["id"]] = operations.snapshot(row["user_id"])
    for uid in {row["user_id"] for row in rows}:
        kick(uid)


async def _cancel(jobs: list[Job]) -> None:
    for job in jobs:
        if not job.by_user:
            job.by_user = True
            job.task.cancel()
    await asyncio.gather(*(asyncio.shield(j.task) for j in jobs), return_exceptions=True)


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
    runners = list(RUNNERS.values())
    jobs = [j for j in JOBS.values() if j.task is not None and not j.task.done()]
    for task in runners:
        task.cancel()
    for job in jobs:
        if not job.by_user:
            job.task.cancel()
    await asyncio.gather(*runners, *(j.task for j in jobs), return_exceptions=True)
    EPOCHS.clear()                                   # ждущие остаются в базе; эпоху им даст `resume`


# ---- API -----------------------------------------------------------------------------

def _input(uid: int, body: SpeechIn):
    """Одинаковые проверки при новой записи и при её повторной озвучке."""
    text = documents.nfc(body.text).strip()           # текст идёт в поиск (search_speeches_*), как у документов
    if not text or len(text) > TEXT_MAX:
        raise HTTPException(422, {"code": "text", "message": f"текст — от 1 до {TEXT_MAX} знаков"})
    conn, model = sections.resolve(uid, "speech", body.connection_id, body.model)
    voice = body.voice.strip()
    if conn.kind == "srvllm":
        name = voices._row(uid, voice)["name"]                    # чужой и неизвестный голос — 404 gone
    elif not voice:
        raise HTTPException(422, {"code": "voice", "message": "назовите голос сервиса"})
    else:
        name = voice
    if _full_queue(uid):
        raise HTTPException(409, {"code": "busy", "message": (
            f"в очереди уже {QUEUE_MAX} озвучек — дождитесь их или отмените")})
    return text, conn, model, voice, name


def _chat_rows(uid: int, cid: str):
    rows = state.DB.execute("SELECT * FROM speeches WHERE user_id = ? AND chat_id = ? ORDER BY created, rowid",
                            (uid, cid)).fetchall()
    if not rows:
        raise HTTPException(404, {"code": "gone", "message": "такого чата озвучки нет"})
    return rows


def _chat_out(rows, *, full=False):
    items = [_out(r) for r in rows]
    current = next((i for i in items if i["state"] not in TERMINAL), items[-1])
    summary = {"id": rows[0]["chat_id"], "created": rows[0]["chat_created"], "text": items[0]["text"],
               "state": current["state"], "detail": current["detail"],
               "revision": [(i["id"], i["state"], i["detail"], i["media_id"], i["parts"], i["finished"]) for i in items]}
    if full:
        summary["items"] = items
    return summary


@router.post("", status_code=202)
async def create(body: SpeechIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    chat = _chat_rows(uid, body.chat_id)[0] if body.chat_id else None
    text, conn, model, voice, name = _input(uid, body)
    sid, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO speeches(id, user_id, connection_id, model, voice, voice_name, text, state, detail,"
                     " created, queued, chat_id, chat_created) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                     (sid, uid, conn.id, model, voice, name, text, QUEUED, now, now,
                      chat["chat_id"] if chat else sid, chat["chat_created"] if chat else now))
    EPOCHS[sid] = operations.capture(uid)
    kick(uid)
    log.info("озвучка %s: принята", sid[:8])
    return _out(_row(uid, sid))


@router.get("")
async def list_speeches(request: Request, before: "float | None" = None,
                        before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    """Новые сверху; следующая страница — before=<created>&before_id=<id> последней."""
    rows = db.page(state.DB, "speeches", request.state.user.id, before, before_id, limit)
    return {"items": [_out(r) for r in rows]}


@router.get("/chats")
async def list_chats(request: Request, before: "float | None" = None,
                     before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    uid = request.state.user.id
    where, args = "", [uid]
    if before is not None:
        where = " AND (chat_created < ? OR (chat_created = ? AND chat_id < ?))" if before_id else " AND chat_created < ?"
        args.extend((before, before, before_id) if before_id else (before,))
    chats = state.DB.execute("SELECT chat_id FROM speeches WHERE user_id = ?" + where
                             + " GROUP BY chat_id ORDER BY chat_created DESC, chat_id DESC LIMIT ?", (*args, limit)).fetchall()
    return {"items": [_chat_out(_chat_rows(uid, r["chat_id"])) for r in chats]}


@router.get("/chats/{cid}")
async def get_chat(cid: str, request: Request):
    return _chat_out(_chat_rows(request.state.user.id, cid), full=True)


@router.patch("/{sid}", status_code=202)
async def edit(sid: str, body: SpeechIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    row = _row(uid, sid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "дождитесь озвучки или отмените её"})
    if body.chat_id is not None and body.chat_id != row["chat_id"]:
        raise HTTPException(422, {"code": "chat", "message": "нельзя перенести запись в другой чат"})
    text, conn, model, voice, name = _input(uid, body)
    # прежние поля и звук — до итога новой озвучки (п. 5 шапки); записи без звука возвращать нечего
    previous = json.dumps({k: row[k] for k in KEPT}, ensure_ascii=False) if row["media_id"] else None
    state.DB.execute("UPDATE speeches SET text = ?, connection_id = ?, model = ?, voice = ?, voice_name = ?,"
                     " state = 'queued', detail = ?, media_id = NULL, duration = NULL, finished = NULL, queued = ?,"
                     " previous = ?, previous_media_id = ? WHERE id = ?",
                     (text, conn.id, model, voice, name, QUEUED, clock.NOW(), previous, row["media_id"], sid))
    EPOCHS[sid] = operations.capture(uid)
    kick(uid)
    return _out(_row(uid, sid))


@router.get("/{sid}")
async def get_speech(sid: str, request: Request):
    return _out(_row(request.state.user.id, sid))


@router.get("/{sid}/parts/{n}")
async def get_part(sid: str, n: int, request: Request):
    """Готовая часть идущей озвучки (n с 0) — слушать до склейки; итоговый mp3 от этого не меняется. Части
    живут во временных файлах задания: кончилось (склейка, уборка) — 404, дальше слушают готовую озвучку.
    Диапазоны — как у `/api/media` (`media.byte_range`: Safari на iPhone без 206 звук не играет); часть —
    одна фраза, читается целиком: файл не держится открытым, пока задание его убирает."""
    _row(request.state.user.id, sid)
    job = JOBS.get(sid)
    if job is None or not 0 <= n < job.ready:
        raise HTTPException(404, {"code": "gone", "message": "этой части нет — слушайте готовую озвучку"})
    part = job.files[n]
    try:
        data = await asyncio.to_thread(part.read_bytes)
    except FileNotFoundError:
        raise HTTPException(404, {"code": "gone", "message": "этой части нет — слушайте готовую озвучку"}) from None
    mime = "audio/wav" if part.suffix == ".wav" else "audio/mpeg"
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
    spec = request.headers.get("range")
    span = None if spec is None else media.byte_range(spec, len(data))
    if span is None:
        return Response(data, media_type=mime, headers=headers)
    if span == media.UNSATISFIABLE:
        return Response(status_code=416, headers=headers | {"Content-Range": f"bytes */{len(data)}"})
    start, end = span
    return Response(data[start:end + 1], status_code=206, media_type=mime,
                    headers=headers | {"Content-Range": f"bytes {start}-{end}/{len(data)}"})


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
