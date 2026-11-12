"""Расшифровка записей (спека 2026-10-20-studio-sections-voice-design, 6.3).

1. Загрузка — тело запроса и есть запись (`Content-Type` — её тип, имя — заголовком `X-Studio-Name`,
   `encodeURIComponent`: строка запроса попала бы в журнал Apache). Тело идёт потоком на шару
   (`media.save_stream`, до UPLOAD_MAX) — не в память: служба живёт в MemoryMax=2G. Тип записи — по
   заголовку, иначе по расширению имени; не звук — 415. Задания пользователя — очередью (п. 7).
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
5. JOBS — только идущие задания: кончившееся (и отменённое стиранием истории или удалением учётки)
   уходит из него, убрав временный файл, вместе с записью итога; долговечное — строка `transcripts`.
6. Правка (спека 2026-10-21-studio-transcript-edit-chat-design): тексты реплик и имена собеседников —
   JSON `transcripts.edits` поверх файла итога, который не меняется (`raw` в нём — сегменты до склейки,
   для SRT). Показываемые реплики собирает одна `view` — для GET, выгрузок, «Копировать» и вложения в
   чат (`POST …/attachments`). Последнее сохранение побеждает; текст и имена в журнал не пишутся.
7. Очередь (как у сканов): запись встаёт `queued` («в очереди · N»), один раннер на пользователя
   (`RUNNERS`, `kick`) берёт старейшую в `working`; ждущих — не больше QUEUE_MAX (дальше 409 busy).
   Отмена ждущей — сразу `cancelled`; перезапуск службы: идущая — «прервано», ждущие остаются в очереди
   и продолжаются (`resume`); стирание истории и учётки отменяет и ждущие.
"""
import asyncio
import json
import logging
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import audio
import chat
import clock
import connections
import db
import documents
import exports
import forms
import lock_policy
import media
import operations
import push
import sections
import speech
import state

UPLOAD_MAX = 200 * 1024 * 1024
DURATION_MAX = 60 * 60
NAME_MAX = 255
RETRY_SEC = 15
WAIT_MAX_SEC = 1800
TERMINAL = {"done", "error", "cancelled"}
EDIT_TEXT_MAX = 20_000
NAME_EDIT_MAX = 60
NAME_BANNED = {"Cc", "Zl", "Zp"}             # имя — одной строкой; знаки формата (Cf: ZWJ, мягкий перенос) — можно
INTERRUPTED = "прервано перезапуском студии"
QUEUE_MAX = 5
QUEUED = "в очереди"
#: столбцы строки для ленты, опроса и ответов — без `text` (весь текст записи, нужен только индексу поиска)
COLUMNS = ("id, user_id, connection_id, model, name, source_media_id, result_media_id, duration, speakers, state,"
           " detail, created, finished, edits")

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
#: user_id → раннер его очереди (п. 7)
RUNNERS: dict[int, asyncio.Task] = {}
#: id ждущей записи → эпоха создания (`operations.capture` при постановке; раннер исполняет её под ней)
EPOCHS: dict[str, tuple] = {}


def _clock(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60}:{s % 60:02d}"


def detail(job: Job) -> str:
    """Состояние идущего задания словами со временем этапа."""
    if job.stage in ("распознаю", "жду карту"):
        return f"{job.stage} · {_clock(clock.NOW() - job.stage_at)}"
    return job.stage


def waiting(row) -> str:
    """Слова ждущей записи: место в очереди пользователя (п. 7)."""
    ahead = state.DB.execute("SELECT COUNT(*) FROM transcripts WHERE user_id = ? AND state = 'queued'"
                             " AND (created < ? OR (created = ? AND id < ?))",
                             (row["user_id"], row["created"], row["created"], row["id"])).fetchone()[0]
    return f"{QUEUED} · {ahead + 1}"


def _row(user_id: int, tid: str):
    row = state.DB.execute(f"SELECT {COLUMNS} FROM transcripts WHERE id = ? AND user_id = ?", (tid, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такой расшифровки нет"})
    return row


def edits_of(row) -> dict:
    """Правки строки: `{"texts": {номер: текст}, "names": {метка: имя}}` (пустые — правок нет)."""
    data = json.loads(row["edits"]) if row["edits"] else {}
    return {"texts": dict(data.get("texts") or {}), "names": dict(data.get("names") or {})}


def _speakers(row, names: dict) -> "int | None":
    """Число разных показываемых имён: метки — «Собеседник 1…N» (speech.turns), файл не нужен."""
    count = row["speakers"]
    if not count or not names:
        return count
    return len({names.get(label, label) for label in (speech.LABEL.format(n) for n in range(1, count + 1))})


def view(row, result: dict) -> list[dict]:
    """Показываемые реплики — распознавание с правками: `{start, end, speaker, label, text, edited}`,
    у правленой ещё `original`. Одна для GET, выгрузок и вложения в чат."""
    edits = edits_of(row)
    out = []
    for n, s in enumerate(result["segments"]):
        label, text = s.get("speaker"), edits["texts"].get(str(n))
        item = {"start": s["start"], "end": s["end"], "speaker": edits["names"].get(label, label) if label else None,
                "label": label, "text": s["text"] if text is None else text, "edited": text is not None}
        if text is not None:
            item["original"] = s["text"]
        out.append(item)
    return out


def text_of(row, result: dict) -> str:
    """Текст для поиска (`transcripts.text`, спека 2026-10-21-studio-history-search): показываемые
    реплики одной строкой."""
    return " ".join(s["text"] for s in view(row, result))


def _out(row, segments: "list | None" = None) -> dict:
    job = JOBS.get(row["id"])
    edits = edits_of(row)
    out = {"id": row["id"], "name": row["name"], "state": row["state"],
           "detail": detail(job) if job is not None and row["state"] == "working"
           else waiting(row) if row["state"] == "queued" else row["detail"],
           "model": row["model"], "duration": row["duration"], "speakers": _speakers(row, edits["names"]),
           "source_media_id": row["source_media_id"], "created": row["created"], "finished": row["finished"],
           "names": edits["names"], "edited": bool(edits["texts"] or edits["names"])}
    if segments is not None:
        out["segments"] = segments
    return out


def _store(job: Job) -> None:
    state.DB.execute("UPDATE transcripts SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _finish(tid: str, state_: str, detail_: str, **extra) -> None:
    cols = "".join(f", {k} = ?" for k in extra)                  # имена — из кода, не из запроса
    changed = state.DB.execute(f"UPDATE transcripts SET state = ?, detail = ?, finished = ?{cols}"
                               " WHERE id = ? AND finished IS NULL",
                               (state_, detail_, clock.NOW(), *extra.values(), tid)).rowcount
    EPOCHS.pop(tid, None)
    if changed:
        push.job_finished("transcripts", tid)


def _full_queue(user_id: int) -> bool:
    return state.DB.execute("SELECT COUNT(*) FROM transcripts WHERE user_id = ? AND state = 'queued'",
                            (user_id,)).fetchone()[0] >= QUEUE_MAX


def _busy() -> HTTPException:
    return HTTPException(409, {"code": "busy", "message": (
        f"в очереди уже {QUEUE_MAX} записей — дождитесь их или отмените")})


def kick(user_id: int) -> None:
    """Раннер очереди пользователя, если его нет (п. 7)."""
    task = RUNNERS.get(user_id)
    if task is None or task.done():
        task = RUNNERS[user_id] = asyncio.create_task(_runner(user_id))
        task.add_done_callback(lambda t: RUNNERS.pop(user_id) if RUNNERS.get(user_id) is t else None)


async def _runner(user_id: int) -> None:
    """Старейшая ждущая запись пользователя — в работу, по одной, пока очередь не пуста. Каждая — под своей
    эпохой (`EPOCHS`), а не под эпохой запроса, запустившего раннер: после очистки истории живой раннер
    берёт и новые записи."""
    lock_policy.RESTRICTED.set(frozenset())          # закрытые разделы запроса, создавшего раннер, ему чужие
    while True:
        row = state.DB.execute(f"SELECT {COLUMNS} FROM transcripts WHERE user_id = ? AND state = 'queued'"
                               " ORDER BY created, id LIMIT 1", (user_id,)).fetchone()
        if row is None:
            return
        epoch = EPOCHS.pop(row["id"], None) or operations.snapshot(user_id)
        now = clock.NOW()
        if not state.DB.execute("UPDATE transcripts SET state = 'working', detail = 'готовлю запись'"
                                " WHERE id = ? AND state = 'queued'", (row["id"],)).rowcount:
            continue
        try:
            conn = connections.get(user_id, row["connection_id"])
        except HTTPException:                        # подключение удалили, пока запись ждала
            conn = None
        job = Job(id=row["id"], user_id=user_id, conn=conn, model=row["model"],
                  source_media_id=row["source_media_id"], stage_at=now)
        JOBS[job.id] = job
        with operations.restore(epoch):
            job.task = asyncio.create_task(run(job))
        await job.task


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
    """Итог — в строку последним, после уборки временного файла (п. 5 шапки): «готово», ошибка и
    отмена значат «и чисто»; задание тогда же уходит из JOBS."""
    tmp = audio.temp(".mp3")
    outcome = ("error", INTERRUPTED, {})              # остановка службы
    try:
        try:
            if job.conn is None:
                raise JobError("подключение раздела удалено из профиля")
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
            replicas, raw = speech.turns(await _recognize(job, tmp))
            if not replicas:
                raise JobError("в записи не распознано ни слова")
            payload = json.dumps({"segments": [asdict(s) for s in replicas], "raw": raw},
                                 ensure_ascii=False).encode("utf-8")
            result = await media.save_async(job.user_id, payload, "application/json", "transcript.json")
            outcome = ("done", "готово", {"result_media_id": result,
                                          "speakers": len({s.speaker for s in replicas if s.speaker}),
                                          "text": " ".join(s.text for s in replicas)})
            log.info("расшифровка %s: готова", job.id[:8])
        except (JobError, audio.AudioError, speech.SpeechError, media.StorageError) as e:
            outcome = ("error", str(e), {})
            log.info("расшифровка %s: ошибка %s", job.id[:8], type(e).__name__)
        except HTTPException as e:                   # барьер истории (`operations.check`): 409 очистка, 401 учётка
            outcome = ("error", e.detail.get("message", "сбой службы") if isinstance(e.detail, dict) else "сбой службы", {})
            log.info("расшифровка %s: отказ %s", job.id[:8], e.status_code)
        except Exception as e:                       # noqa: BLE001 — задание не должно висеть
            outcome = ("error", f"сбой службы: {type(e).__name__}", {})
            log.warning("расшифровка %s: сбой %s", job.id[:8], type(e).__name__)
    except asyncio.CancelledError:
        if not job.by_user:
            raise
        outcome = ("cancelled", "отменено", {})
        log.info("расшифровка %s: отменена", job.id[:8])
    finally:
        cleanup = asyncio.create_task(asyncio.to_thread(tmp.unlink, missing_ok=True))
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass                             # поток удаления ещё идёт: итог ждёт его
            cleanup.result()
        finally:
            state_, detail_, extra = outcome
            _finish(job.id, state_, detail_, **extra)
            JOBS.pop(job.id, None)


def recover() -> None:
    """После перезапуска идущих заданий нет: их строки — ошибка «прервано перезапуском»; ждущие остаются
    в очереди — их продолжит `resume`."""
    count = state.DB.execute("UPDATE transcripts SET state = 'error', detail = ?, finished = ?"
                             " WHERE state NOT IN ('done', 'error', 'cancelled', 'queued')",
                             (INTERRUPTED, clock.NOW())).rowcount
    if count:
        log.warning("расшифровки: %d прервано перезапуском", count)


def resume() -> None:
    """Старт службы (после recover и state.HTTP): очереди с ждущими записями — снова в работу, каждая — с
    эпохой старта (очистка истории после него их сохранение запрещает, как у принятых запросом)."""
    rows = state.DB.execute("SELECT id, user_id FROM transcripts WHERE state = 'queued'").fetchall()
    for row in rows:
        EPOCHS[row["id"]] = operations.snapshot(row["user_id"])
    for uid in {row["user_id"] for row in rows}:
        kick(uid)


async def _cancel(jobs: list[Job]) -> None:
    for job in jobs:
        job.by_user = True
        job.task.cancel()
    await asyncio.gather(*(j.task for j in jobs), return_exceptions=True)


def _drop_waiting(user_id: int, tid: "str | None" = None) -> int:
    """Ждущие записи пользователя (или одна) — `cancelled`; итог — сколько."""
    rows = state.DB.execute("SELECT id FROM transcripts WHERE user_id = ? AND state = 'queued'"
                            + (" AND id = ?" if tid else ""), (user_id, tid) if tid else (user_id,)).fetchall()
    for (wid,) in rows:
        _finish(wid, "cancelled", "отменено")
    return len(rows)


async def stop_user(user_id: int) -> None:
    """Стирание истории, удаление и отключение учётки: её расшифровки — и ждущие — останавливаются."""
    _drop_waiting(user_id)
    await _cancel([j for j in JOBS.values() if j.user_id == user_id and j.task is not None and not j.task.done()])


async def stop() -> None:
    """Остановка службы: задания записывают «прервано» и кончаются до закрытия базы; ждущие остаются."""
    tasks = list(RUNNERS.values()) + [j.task for j in JOBS.values() if j.task is not None and not j.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    EPOCHS.clear()                                   # ждущие остаются в базе; эпоху им даст `resume`


async def backfill_text() -> int:
    """Текст для поиска при старте: готовым расшифровкам без него (до шага схемы 13 и сделанным прежним
    кодом после отката) и правленым — прежний код при откате правит edits, а text не трогает (правленых
    мало). Файл итога — по одному, в потоке. Файла или строки media нет — пропуск; шара недоступна —
    конец, остальное дождётся следующего старта. Итог — сколько текстов записано (верный не переписывается)."""
    filled, last = 0, ""
    while True:
        row = state.DB.execute(f"SELECT {COLUMNS} FROM transcripts WHERE state = 'done' AND result_media_id IS NOT NULL"
                               " AND (text IS NULL OR edits IS NOT NULL) AND id > ? ORDER BY id LIMIT 1",
                               (last,)).fetchone()
        if row is None:
            break
        last = row["id"]
        try:
            data = await asyncio.to_thread(media.path(row["user_id"], row["result_media_id"]).read_bytes)
            result = json.loads(data)
        except (HTTPException, FileNotFoundError, ValueError):
            continue
        except OSError:
            log.warning("расшифровки: текст для поиска — хранилище недоступно, дальше при следующем старте")
            break
        fresh = state.DB.execute(f"SELECT {COLUMNS} FROM transcripts WHERE id = ?", (last,)).fetchone()
        if fresh is not None:                        # без await до UPDATE: свежие правки
            text = text_of(fresh, result)
            filled += state.DB.execute("UPDATE transcripts SET text = ? WHERE id = ? AND text IS NOT ?",
                                       (text, last, text)).rowcount
    if filled:
        log.info("расшифровки: текст для поиска — %d", filled)
    return filled


def start_backfill() -> asyncio.Task:
    """Старт службы: фон не держит старт."""
    return asyncio.create_task(backfill_text())


# ---- API -------------------------------------------------------------------

def _name(raw: str) -> str:
    # NFC: имя идёт в поиск (search_transcripts_*), разложенное «й» из архива macOS иначе не находилось бы
    name = documents.nfc(unquote(raw or "")).strip().replace("\x00", "")[:NAME_MAX]
    return name or "запись"


def _int_header(request: Request, key: str) -> "int | None":
    value = request.headers.get(key, "").strip()
    return int(value) if value.isdigit() else None


@router.post("", status_code=202)
async def upload(request: Request):
    uid = request.state.user.id
    conn, model = sections.resolve(uid, "transcribe", _int_header(request, "x-studio-connection"),
                                   request.headers.get("x-studio-model") or None)
    if _full_queue(uid):
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
    if _full_queue(uid) or not media.alive(uid, source):    # пока шла загрузка
        await media.delete_async(uid, source)
        raise _busy()
    try:
        operations.check(uid)        # историю стёрли, пока шла загрузка: строки нет, файл — в уборку (как speeches)
    except HTTPException:
        await media.delete_async(uid, source)
        raise
    tid, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO transcripts(id, user_id, connection_id, model, name, source_media_id, state, detail,"
                     " created) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                     (tid, uid, conn.id, model, name, source, QUEUED, now))
    EPOCHS[tid] = operations.capture(uid)
    kick(uid)
    log.info("расшифровка %s: принята", tid[:8])
    return _out(_row(uid, tid))


@router.get("")
async def list_transcripts(request: Request, before: "float | None" = None,
                           before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    """Новые сверху; следующая страница — before=<created>&before_id=<id> последней."""
    rows = db.page(state.DB, "transcripts", request.state.user.id, before, before_id, limit, COLUMNS)
    return {"items": [_out(r) for r in rows]}


async def result_of(user_id: int, row) -> dict:
    """Файл итога готовой расшифровки — с шары: `segments` и у новых `raw`; файла нет — 404, шара
    недоступна — 503."""
    try:
        data = await asyncio.to_thread(media.path(user_id, row["result_media_id"]).read_bytes)
    except FileNotFoundError:
        raise HTTPException(404, media.GONE) from None
    except OSError:
        raise HTTPException(503, forms.STORAGE) from None
    return json.loads(data)


def _done(user_id: int, tid: str):
    row = _row(user_id, tid)
    if row["state"] != "done" or row["result_media_id"] is None:
        raise HTTPException(409, {"code": "running", "message": "расшифровка ещё не готова"})
    return row


async def _full(user_id: int, row) -> dict:
    """Ответ GET: готовая — с показываемыми репликами."""
    if row["state"] != "done" or row["result_media_id"] is None:
        return _out(row)
    return _out(row, view(row, await result_of(user_id, row)))


@router.get("/{tid}")
async def get_transcript(tid: str, request: Request):
    uid = request.state.user.id
    return await _full(uid, _row(uid, tid))


class EditIn(BaseModel):
    texts: dict[str, "str | None"] = Field(default_factory=dict)
    names: dict[str, "str | None"] = Field(default_factory=dict)


def _bad(message: str) -> HTTPException:
    return HTTPException(422, {"code": "input", "message": message})


def _checked(body: EditIn, segments: list) -> tuple[dict, dict]:
    """Правки запроса, проверенные по файлу итога; None — снять правку (и значение, равное исходному).
    Текст реплики — одной строкой: управляющие знаки — пробелом, пробелы схлопнуты (пустая строка —
    разделитель реплик TXT и конец субтитра SRT, C0 ломает DOCX); предел — от длины исходной реплики
    (EDIT_TEXT_MAX сверх неё): реплика одного говорящего за 20+ минут длиннее EDIT_TEXT_MAX.
    Имя — без управляющих и разделителей строк; знаки формата (ZWJ эмодзи, мягкий перенос) — можно."""
    texts, names = {}, {}
    for key, value in body.texts.items():
        if not (key.isascii() and key.isdigit()) or int(key) >= len(segments):
            raise _bad("такой реплики нет")
        original = segments[int(key)]["text"]
        if value is not None:
            value = " ".join(exports.CONTROL.sub(" ", value).split())
            limit = len(original) + EDIT_TEXT_MAX
            if not 1 <= len(value) <= limit:
                raise _bad(f"текст реплики — от 1 до {limit} знаков")
        texts[str(int(key))] = None if value == original else value
    labels = {s.get("speaker") for s in segments} - {None}
    for label, value in body.names.items():
        if label not in labels:
            raise _bad("такого собеседника нет")
        if value is not None:
            value = value.strip()
            if not 1 <= len(value) <= NAME_EDIT_MAX or any(unicodedata.category(c) in NAME_BANNED for c in value):
                raise _bad(f"имя — от 1 до {NAME_EDIT_MAX} знаков, одной строкой")
        names[label] = None if value == label else value
    return texts, names


@router.patch("/{tid}")
async def patch_transcript(tid: str, body: EditIn, request: Request):
    """Правка текстов реплик и имён поверх распознавания; последнее сохранение побеждает."""
    uid = request.state.user.id
    result = await result_of(uid, _done(uid, tid))
    texts, names = _checked(body, result["segments"])
    row = _done(uid, tid)                            # после чтения файла: свежие правки, без await до UPDATE
    edits = edits_of(row)
    for part, changes in (("texts", texts), ("names", names)):
        for key, value in changes.items():
            if value is None:
                edits[part].pop(key, None)
            else:
                edits[part][key] = value
    stored = json.dumps(edits, ensure_ascii=False) if edits["texts"] or edits["names"] else None
    state.DB.execute("UPDATE transcripts SET edits = ? WHERE id = ?", (stored, tid))
    log.info("расшифровка %s: правка", tid[:8])
    row = _row(uid, tid)
    state.DB.execute("UPDATE transcripts SET text = ? WHERE id = ?", (text_of(row, result), tid))
    return _out(row, view(row, result))


@router.delete("/{tid}/edits")
async def reset_edits(tid: str, request: Request):
    """«Сбросить правки» — снова исходное распознавание (и текст для поиска — по нему). Файл итога —
    до записи: шара недоступна — 503, а правки и текст прежние; затем то и другое одним UPDATE."""
    uid = request.state.user.id
    row = _row(uid, tid)
    if row["state"] != "done" or row["result_media_id"] is None:
        state.DB.execute("UPDATE transcripts SET edits = NULL WHERE id = ?", (tid,))
        return _out(_row(uid, tid))
    result = await result_of(uid, row)
    _row(uid, tid)                                   # после чтения файла: запись ещё есть, без await до UPDATE
    state.DB.execute("UPDATE transcripts SET edits = NULL, text = ? WHERE id = ?",
                     (text_of({"edits": None}, result), tid))
    log.info("расшифровка %s: правки сброшены", tid[:8])
    row = _row(uid, tid)
    return _out(row, view(row, result))


@router.post("/{tid}/cancel")
async def cancel(tid: str, request: Request):
    uid = request.state.user.id
    _row(uid, tid)
    job = JOBS.get(tid)
    if job is not None and job.task is not None and not job.task.done():
        await _cancel([job])
    elif _drop_waiting(uid, tid):
        log.info("расшифровка %s: снята из очереди", tid[:8])
    return {"state": _row(uid, tid)["state"]}


@router.delete("/{tid}", status_code=204)
async def delete(tid: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, tid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    state.DB.execute("DELETE FROM transcripts WHERE id = ?", (tid,))
    for media_id in {row["source_media_id"], row["result_media_id"]} - {None}:
        if not media.referenced(uid, media_id):
            await media.delete_async(uid, media_id)
    return Response(status_code=204)


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


@router.post("/{tid}/attachments", status_code=201)
async def to_chat(tid: str, request: Request):
    """Расшифровка с правками — неотправленным документом чата («<имя записи>.txt», свой файл): дальше
    это обычное вложение (крестик, уборка, отправка, предел контекста). Ссылка на запись —
    `source_transcript_id`; к нелокальной модели его ход пускает только с `external_ok` (chat.create_turn)."""
    uid = request.state.user.id
    row = _done(uid, tid)
    segments = view(row, await result_of(uid, row))
    head = f"Расшифровка «{row['name']}»" + (f" · {exports.stamp(row['duration'])}" if row["duration"] else "")
    text = f"{head}\n\n{exports.txt(segments)}"
    if len(text) > documents.TEXT_MAX_CHARS:
        raise HTTPException(413, {"code": "too_big", "message": (
            f"расшифровка длиннее {documents.TEXT_MAX_CHARS:,} знаков — в чат её не передать".replace(",", " "))})
    name = f"{_stem(row['name'])}.txt"
    try:
        media_id = await media.save_async(uid, text.encode("utf-8"), "text/plain", name)
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    att_id = uuid.uuid4().hex
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created,"
                     " source_transcript_id) VALUES (?, ?, NULL, 'document', ?, ?, ?, ?, ?, ?)",
                     (att_id, uid, name, media_id, text, len(text), clock.NOW(), tid))
    log.info("вложение %s: из расшифровки %s, %d знаков", att_id[:8], tid[:8], len(text))
    return {"id": att_id, "kind": "document", "name": name, "chars": len(text), "media_id": media_id, "text": text,
            "tokens": chat.estimate_tokens([{"role": "user", "content": text}])}


@router.get("/{tid}/export")
async def export(tid: str, request: Request, format: str = Query(..., pattern="^(txt|docx|srt)$")):  # noqa: A002
    """Готовая расшифровка файлом: имя — по записи («Звонок.m4a» → «Звонок.docx»)."""
    uid = request.state.user.id
    row = _done(uid, tid)
    result = await result_of(uid, row)
    segments = view(row, result)
    stem = _stem(row["name"])
    if format == "docx":
        body = exports.docx(segments, stem)
    else:
        body = (exports.txt(segments) if format == "txt" else exports.srt(segments, result.get("raw"))).encode("utf-8")
    return Response(body, media_type=exports.TYPES[format],
                    headers={"Content-Disposition": media.disposition(f"{stem}.{format}"), "Cache-Control": "no-store"})
