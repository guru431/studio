"""Перевод (спека 2026-10-28-studio-translator-design, 6.3, 6.7, 6.9): записи, задания, API раздела.

1. Запись — строка `translations` (вид `text`, `file`, `photo`), куски — `translation_parts` (n с 1): `waiting`
   ждёт перевода, `done` — с переводом, `failed` — со словами. Куски — `translate_text.split` исходника, у
   снимка — блоки с рамкой (`box`, разделитель `\\n`). `parts` — число кусков, `done_parts` — готовых;
   `text` — склейка готовых кусков подряд (`joined`), пишется итогом задания — по готовности, по остановке и
   по ошибке: переведённое до обрыва видно и ищется.
2. Задания пользователя — двумя полосами (`lane_of`): `text` — текст из поля, `file` — файлы и снимки. В
   полосе одно задание идёт (`RUNNERS` — раннер на пару пользователь и полоса, `kick`), ждущих — не больше
   QUEUE_MAX; полосы друг друга не ждут, разные пользователи — параллельно.
3. Задание (`run`): язык исходника — выбранный, определённый раньше или ответом модели (`translate_text.detect`),
   язык перевода — `redirect` (совпал с целевым — на английский, английский — на русский); у «Продолжить» оба
   уже в строке. Локальная модель — сначала release моделей картинок (иначе ждала бы карту под арендой
   Lustify), один раз на задание. Затем куски `waiting` по порядку потоком (`job.live` — перевод идущего куска
   для страницы). 503 — ждать и повторять раз в RETRY_SEC до WAIT_MAX_SEC («жду карту · м:сс»); обрезанный
   ответ — кусок пополам, половинки по очереди (их обрезка — ошибка); прочий отказ — кусок `failed`, запись —
   ошибка «кусок n из N: …», дальше не идёт.
4. «Остановить» (`request_cancel`): ждущий — сразу «остановлено»; идущий — `cancelling`: поток куска бросается
   на следующей дельте (обрыв шлюз дальше не передаёт, карту он не держит — модель дочитает сама), готовые
   куски остаются. Перезапуск службы — недоделанное и ждущее становится ошибкой «прервано» (`recover`).
5. «Продолжить» (`resume`) — та же модель и тот же язык, только непереведённые куски: `failed` снова `waiting`.
6. Журнал — только id, коды и числа: текст, имя файла, языки исходника туда не пишутся. База — только с цикла
   событий.
7. API раздела — `router` (`/api/translate`, вторая половина файла): языки, перевод текста, лента, запись с
   кусками (`?from=` — только новые), остановка, «Продолжить», удаление.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import clock
import db
import images
import media
import sections
import state
import translate_text
import upstream

QUEUE_MAX = 5
RUNNING = ("queued", "working", "cancelling")
TERMINAL = ("done", "error", "cancelled")
RETRY_SEC = 15
WAIT_MAX_SEC = 1800
POLL_SEC = 1
QUEUED = "в очереди"
CANCELLING = "останавливаю"
CANCELLED = "остановлено"
DONE = "готово"
INTERRUPTED = "прервано перезапуском студии"
NAME_CHARS = 60                  # имя текстовой записи — начало первой строки исходника
HEAD_CHARS = 200                 # начало исходника и перевода в ленте
#: полоса → виды записей в ней
LANES = {"text": ("text",), "file": ("file", "photo")}

log = logging.getLogger("studio")


class _Cancelled(Exception):
    """Остановка человеком, замеченная между шагами или на дельте потока."""


@dataclass
class Job:
    id: str
    user_id: int
    conn: object
    model: str
    ocr_conn: object = None       # подключение «Сканов» — распознавание скана и снимка
    stage: str = QUEUED
    stage_at: float = 0.0
    live: str = ""                # перевод идущего куска по мере генерации
    cancel: bool = False          # «Остановить»: бросить идущий кусок, готовые оставить
    stopped: bool = False         # stop_user: раннер оборван, итог — «остановлено»
    freed: bool = False           # модели картинок отпущены перед локальной моделью


#: записи в очереди и в работе (снимок подключения — для раннера); кончившееся уходит
JOBS: dict[str, Job] = {}
#: раннер полосы пользователя: (пользователь, полоса) → задача
RUNNERS: dict[tuple[int, str], asyncio.Task] = {}


def lane_of(kind: str) -> str:
    return "text" if kind == "text" else "file"


def _lane_kinds(lane: str) -> str:
    return "(" + ", ".join(f"'{k}'" for k in LANES[lane]) + ")"


# ---- записи ----------------------------------------------------------------------

def _check_queue(user_id: int, lane: str) -> None:
    waiting = state.DB.execute(f"SELECT count(*) FROM translations WHERE user_id = ? AND state = 'queued'"
                               f" AND kind IN {_lane_kinds(lane)}", (user_id,)).fetchone()[0]
    if waiting >= QUEUE_MAX:
        raise HTTPException(409, {"code": "busy", "message": (
            f"в очереди уже {QUEUE_MAX} переводов — дождитесь их или остановите")})


def create(user_id: int, conn, model: str, *, kind: str, name: str, source_lang: str, target_lang: str,
           source: "str | None", source_media_id: "str | None" = None, scan_id: "str | None" = None,
           blocks: "list[dict] | None" = None, ocr_conn=None) -> str:
    """Запись в очередь своей полосы (раннер — `kick`). Куски — `blocks` (`{"source", "box"}`) или нарезка
    `source` (уже `normalize`); исходник ещё не известен (скан) — кусков нет. Ждущих уже QUEUE_MAX — 409 busy."""
    _check_queue(user_id, lane_of(kind))
    if blocks is not None:
        rows = [(b["source"], "\n", json.dumps(b["box"])) for b in blocks]
    else:
        rows = [(c, sep, None) for c, sep in translate_text.split(source, translate_text.CHUNK_CHARS)] if source else []
    tid, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("INSERT INTO translations(id, user_id, kind, connection_id, model, source_lang, target_lang,"
                         " name, source_media_id, scan_id, source, parts, state, detail, created)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                         (tid, user_id, kind, conn.id, model, source_lang, target_lang, name, source_media_id,
                          scan_id, source, len(rows), QUEUED, now))
        state.DB.executemany("INSERT INTO translation_parts(translation_id, n, source, sep, box, state)"
                             " VALUES (?, ?, ?, ?, ?, 'waiting')",
                             [(tid, n, *row) for n, row in enumerate(rows, 1)])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    JOBS[tid] = Job(id=tid, user_id=user_id, conn=conn, model=model, ocr_conn=ocr_conn, stage_at=now)
    log.info("перевод %s: в очереди, %d кусков", tid[:8], len(rows))
    return tid


def kick(user_id: int, lane: str) -> None:
    """Раннер полосы пользователя, если его нет (с цикла событий)."""
    task = RUNNERS.get((user_id, lane))
    if task is None or task.done():
        RUNNERS[(user_id, lane)] = asyncio.create_task(_runner(user_id, lane))


def _clock(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60}:{s % 60:02d}"


def detail(job: Job) -> str:
    """Состояние идущей записи словами: ожидание карты — со временем."""
    if job.stage == "жду карту":
        return f"{job.stage} · {_clock(clock.NOW() - job.stage_at)}"
    return job.stage


def joined(tid: str) -> str:
    """Перевод готовых кусков по порядку до первого неготового, с разделителями между ними."""
    out = []
    for p in state.DB.execute("SELECT state, text, sep FROM translation_parts WHERE translation_id = ? ORDER BY n",
                              (tid,)):
        if p["state"] != "done":
            break
        out += [p["text"], p["sep"]]
    return "".join(out[:-1])


def _store(job: Job) -> None:
    state.DB.execute("UPDATE translations SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _finish(tid: str, state_: str, detail_: str) -> None:
    """Итог записи одним UPDATE — с переводом готовых кусков (поиск видит и недоделанное)."""
    state.DB.execute("UPDATE translations SET state = ?, detail = ?, finished = ?, text = ? WHERE id = ?",
                     (state_, detail_, clock.NOW(), joined(tid) or None, tid))


def _check(job: Job) -> None:
    if job.cancel:
        raise _Cancelled


# ---- задание ---------------------------------------------------------------------

async def _free(conn) -> None:
    """Release моделей картинок перед локальной моделью; итог не важен (занятую дождётся шлюз)."""
    try:
        for model in images.MODELS.values():
            await images.release(conn, model)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return


async def _attempt(job: Job, text: str, origin: str, lang: str) -> str:
    """Перевод текста одним запросом; 503 — ждать и повторять (остановка между попытками — сразу).
    Дельты дописываются к `job.live` после уже готового (половинка обрезанного куска)."""
    head, waited_from = job.live, None

    def on_delta(piece: str) -> None:
        _check(job)                                   # «Остановить» — поток бросается на следующей дельте
        job.live += piece

    while True:
        job.live = head
        try:
            return await translate_text.chunk(job.conn, job.model, text, origin, lang, on_delta)
        except translate_text.Busy:
            now = clock.NOW()
            waited_from = now if waited_from is None else waited_from
            if now - waited_from >= WAIT_MAX_SEC:
                raise translate_text.TranslateError(
                    f"карта занята дольше {WAIT_MAX_SEC // 60} минут — попробуйте позже") from None
            stage = job.stage
            job.stage, job.stage_at = "жду карту", waited_from
            _store(job)
            await clock.SLEEP(RETRY_SEC)
            _check(job)
            job.stage = stage
            _store(job)


async def _chunk(job: Job, text: str, origin: str, lang: str) -> str:
    """Кусок; обрезанный ответ — пополам, половинки по очереди (их обрезка — ошибка `Cut`)."""
    try:
        return await _attempt(job, text, origin, lang)
    except translate_text.Cut:
        out = job.live = ""
        for half, sep in translate_text.split(text, max(1, len(text) // 2)):
            out += await _attempt(job, half, origin, lang) + sep
            job.live = out
        return out


async def _languages(job: Job, row) -> tuple[str, str]:
    """(язык исходника для указания, язык перевода): у «Продолжить» оба уже в строке — без нового запроса."""
    origin = row["source_lang"]
    if origin == translate_text.AUTO:
        origin = row["detected_lang"]
        if origin is None and row["result_lang"] is None:
            origin = await translate_text.detect(job.conn, job.model, row["source"])
            state.DB.execute("UPDATE translations SET detected_lang = ? WHERE id = ?", (origin, job.id))
        origin = origin or translate_text.AUTO
    lang = row["result_lang"]
    if lang is None:
        lang = (row["target_lang"] if origin == translate_text.AUTO
                else translate_text.redirect(origin, row["target_lang"]))
        state.DB.execute("UPDATE translations SET result_lang = ? WHERE id = ?", (lang, job.id))
    return origin, lang


async def _translate(job: Job, row) -> None:
    """Куски `waiting` по порядку; отказ куска — ошибка записи с его номером."""
    if row["source"] is None:                    # скан и снимок — исходник ещё распознаётся (задачи 4–5 плана)
        raise translate_text.TranslateError("исходник не готов")
    if sections.is_local(job.conn, job.model) and not job.freed:
        job.freed = True
        await _free(job.conn)                    # и до определения языка: оно — запрос к той же модели
        _check(job)
    origin, lang = await _languages(job, row)
    waiting = state.DB.execute("SELECT n, source FROM translation_parts WHERE translation_id = ? AND state = 'waiting'"
                               " ORDER BY n", (job.id,)).fetchall()
    for part in waiting:
        _check(job)
        n = part["n"]
        job.stage, job.live = f"перевожу · {n} из {row['parts']}", ""
        _store(job)
        try:
            text = await _chunk(job, part["source"], origin, lang)
        except translate_text.TranslateError as e:
            state.DB.execute("UPDATE translation_parts SET state = 'failed', detail = ? WHERE translation_id = ? AND n = ?",
                             (str(e), job.id, n))
            raise translate_text.TranslateError(f"кусок {n} из {row['parts']}: {e}") from e
        _check(job)                              # ответ остановленного — выбросить
        state.DB.execute("UPDATE translation_parts SET state = 'done', text = ?, detail = NULL"
                         " WHERE translation_id = ? AND n = ?", (text, job.id, n))
        state.DB.execute("UPDATE translations SET done_parts = done_parts + 1 WHERE id = ?", (job.id,))
        job.live = ""


async def run(job: Job) -> None:
    """Одна запись. Итог — в строку последним: перевод готовых кусков остаётся при любом исходе."""
    row = state.DB.execute("SELECT * FROM translations WHERE id = ?", (job.id,)).fetchone()
    outcome = ("error", INTERRUPTED)
    try:
        try:
            job.stage, job.stage_at = "готовлю", clock.NOW()
            state.DB.execute("UPDATE translations SET state = 'working', detail = ? WHERE id = ?", (job.stage, job.id))
            await _translate(job, row)
            outcome = ("done", DONE)
            log.info("перевод %s: готово", job.id[:8])
        except _Cancelled:
            outcome = ("cancelled", CANCELLED)
        except translate_text.TranslateError as e:
            outcome = ("error", str(e))
            log.info("перевод %s: ошибка %s", job.id[:8], type(e.__cause__ or e).__name__)
        except Exception as e:                       # noqa: BLE001 — запись не должна висеть
            outcome = ("error", f"сбой службы: {type(e).__name__}")
            log.warning("перевод %s: ошибка %s", job.id[:8], type(e).__name__)
    except asyncio.CancelledError:
        if job.stopped:
            outcome = ("cancelled", CANCELLED)
        raise
    finally:
        _finish(job.id, *outcome)
        JOBS.pop(job.id, None)


async def _runner(user_id: int, lane: str) -> None:
    try:
        while True:
            row = state.DB.execute(f"SELECT id FROM translations WHERE user_id = ? AND state = 'queued'"
                                   f" AND kind IN {_lane_kinds(lane)} ORDER BY created, rowid LIMIT 1",
                                   (user_id,)).fetchone()
            if row is None:
                return
            job = JOBS.get(row["id"])
            if job is None:                          # снимка подключения нет — такого быть не должно
                _finish(row["id"], "error", INTERRUPTED)
                continue
            await run(job)
    finally:
        if RUNNERS.get((user_id, lane)) is asyncio.current_task():
            del RUNNERS[(user_id, lane)]


async def request_cancel(user_id: int, tid: str) -> str:
    """«Остановить»: ждущая — сразу `cancelled`; идущая — `cancelling` (раннер бросит кусок). Итог — state."""
    row = _row(user_id, tid)
    if row["state"] == "queued":
        _finish(tid, "cancelled", CANCELLED)
        JOBS.pop(tid, None)
        return "cancelled"
    job = JOBS.get(tid)
    if row["state"] == "working" and job is not None:
        job.cancel = True
        state.DB.execute("UPDATE translations SET state = 'cancelling', detail = ? WHERE id = ?", (CANCELLING, tid))
        return "cancelling"
    return row["state"]


def _can_continue(row) -> bool:
    return row["state"] in ("error", "cancelled") and bool(row["has_source"]) and row["done_parts"] < row["parts"]


async def resume(user_id: int, tid: str) -> None:
    """«Продолжить»: той же моделью и на тот же язык — только непереведённые куски. Модели больше нет среди
    отмеченных, подключение удалено или убрано из списка — отказ `sections.resolve` словами (повтор — новая запись)."""
    row = _row(user_id, tid)
    if not _can_continue(row):
        raise HTTPException(409, {"code": "nothing", "message": "нечего продолжать — переведите заново"})
    conn, model = sections.resolve(user_id, "translate", row["connection_id"], row["model"])
    _check_queue(user_id, lane_of(row["kind"]))
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("UPDATE translation_parts SET state = 'waiting', detail = NULL"
                         " WHERE translation_id = ? AND state = 'failed'", (tid,))
        state.DB.execute("UPDATE translations SET state = 'queued', detail = ?, finished = NULL WHERE id = ?",
                         (QUEUED, tid))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    JOBS[tid] = Job(id=tid, user_id=user_id, conn=conn, model=model, stage_at=clock.NOW())
    kick(user_id, lane_of(row["kind"]))


def recover() -> None:
    """После перезапуска раннеров нет: ждущие и идущие — ошибка «прервано» (готовые куски — в тексте)."""
    rows = state.DB.execute(f"SELECT id FROM translations WHERE state IN {RUNNING}").fetchall()
    for row in rows:
        _finish(row["id"], "error", INTERRUPTED)
    if rows:
        log.warning("переводы: %d прервано перезапуском", len(rows))


async def stop_user(user_id: int) -> None:
    """Стирание истории, удаление и отключение учётки: ждущие остановлены, идущие обеих полос оборваны сразу."""
    for row in state.DB.execute("SELECT id FROM translations WHERE user_id = ? AND state = 'queued'",
                                (user_id,)).fetchall():
        _finish(row["id"], "cancelled", CANCELLED)
        JOBS.pop(row["id"], None)
    tasks = [t for (uid, _), t in RUNNERS.items() if uid == user_id and not t.done()]
    if tasks:
        for job in JOBS.values():
            if job.user_id == user_id:
                job.stopped = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def stop() -> None:
    """Остановка службы: идущие пишут «прервано» и кончаются до закрытия базы."""
    tasks = [t for t in RUNNERS.values() if not t.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- API раздела (спека 6.2, 6.3) --------------------------------------------------
# Ответы — «item» (`_out`); у GET одной записи ещё куски (`?from=k` — только n > k: страница опрашивает идущую
# и дописывает новое) и, при k = 0, весь исходник.

router = APIRouter(prefix="/api/translate")
#: столбцы ленты и ответов — без исходника и перевода целиком (до 300 000 знаков): их начала и признак исходника
COLUMNS = ("id, user_id, kind, connection_id, model, source_lang, target_lang, detected_lang, result_lang, name,"
           " source_media_id, scan_id, parts, done_parts, state, detail, created, finished,"
           f" source IS NOT NULL AS has_source, substr(source, 1, {HEAD_CHARS}) AS source_head,"
           f" substr(text, 1, {HEAD_CHARS}) AS text_head")
TOO_LONG = f"текст длиннее {translate_text.TEXT_MAX:,} знаков — разделите его".replace(",", " ")


class TextIn(BaseModel):
    # до нормализации: переносы Windows (`\r\n`) — два знака на один, предел TEXT_MAX сверяется после
    text: str = Field(max_length=2 * translate_text.TEXT_MAX)
    source_lang: str = Field(max_length=8)
    target_lang: str = Field(max_length=8)
    connection_id: "int | None" = None
    model: "str | None" = Field(None, max_length=sections.MODEL_MAX)


def _row(user_id: int, tid: str):
    row = state.DB.execute(f"SELECT {COLUMNS} FROM translations WHERE id = ? AND user_id = ?",
                           (tid, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого перевода нет"})
    return row


def _out(row) -> dict:
    job = JOBS.get(row["id"])
    return {"id": row["id"], "kind": row["kind"], "name": row["name"], "state": row["state"],
            "detail": detail(job) if job is not None and row["state"] == "working" else row["detail"],
            "model": row["model"], "source_lang": row["source_lang"], "target_lang": row["target_lang"],
            "detected_lang": row["detected_lang"], "result_lang": row["result_lang"], "parts": row["parts"],
            "done_parts": row["done_parts"], "created": row["created"], "finished": row["finished"],
            "source_head": row["source_head"], "text_head": row["text_head"], "can_continue": _can_continue(row)}


def _name(text: str) -> str:
    first = text.split("\n", 1)[0].strip()
    return first if len(first) <= NAME_CHARS else first[:NAME_CHARS].rstrip() + "…"


def _langs(source: str, target: str) -> None:
    if source != translate_text.AUTO and source not in translate_text.LANGS or target not in translate_text.LANGS:
        raise HTTPException(422, {"code": "lang", "message": "такого языка нет в списке"})


@router.get("/languages")
async def get_languages():
    return {"items": translate_text.languages(), "auto": translate_text.AUTO,
            "default_target": translate_text.DEFAULT_TARGET}


@router.post("", status_code=202)
async def translate_field(body: TextIn, request: Request):
    """Текст из поля — запись в полосу `text`. Отказы — до записи: пустой, длиннее TEXT_MAX, язык, модель."""
    uid = request.state.user.id
    text = translate_text.normalize(body.text)
    if not text:
        raise HTTPException(422, {"code": "empty", "message": "введите текст"})
    if len(text) > translate_text.TEXT_MAX:
        raise HTTPException(422, {"code": "too_long", "message": TOO_LONG})
    _langs(body.source_lang, body.target_lang)
    conn, model = sections.resolve(uid, "translate", body.connection_id, body.model)
    tid = create(uid, conn, model, kind="text", name=_name(text), source_lang=body.source_lang,
                 target_lang=body.target_lang, source=text)
    kick(uid, "text")
    return _out(_row(uid, tid))


@router.get("")
async def list_translations(request: Request, before: "float | None" = None,
                            before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    """Новые сверху; следующая страница — before=<created>&before_id=<id> последней."""
    rows = db.page(state.DB, "translations", request.state.user.id, before, before_id, limit, COLUMNS)
    return {"items": [_out(r) for r in rows]}


@router.get("/{tid}")
async def get_translation(tid: str, request: Request, start: int = Query(0, alias="from", ge=0)):
    """Запись и её куски с n > `from`; при `from=0` — ещё весь исходник. `live` — перевод идущего куска."""
    row = _row(request.state.user.id, tid)
    job = JOBS.get(tid)
    parts = [{"n": p["n"], "source": p["source"], "sep": p["sep"], "box": json.loads(p["box"]) if p["box"] else None,
              "text": p["text"], "state": p["state"], "detail": p["detail"]}
             for p in state.DB.execute("SELECT n, source, sep, box, text, state, detail FROM translation_parts"
                                       " WHERE translation_id = ? AND n > ? ORDER BY n", (tid, start))]
    out = _out(row) | {"connection_id": row["connection_id"], "media_id": row["source_media_id"],
                       "scan_id": row["scan_id"], "live": job.live if job is not None and row["state"] == "working" else "",
                       "parts": parts}
    if start == 0:
        out["source"] = state.DB.execute("SELECT source FROM translations WHERE id = ?", (tid,)).fetchone()[0]
    return out


@router.post("/{tid}/cancel")
async def cancel(tid: str, request: Request):
    return {"state": await request_cancel(request.state.user.id, tid)}


@router.post("/{tid}/continue")
async def continue_(tid: str, request: Request):
    uid = request.state.user.id
    await resume(uid, tid)
    return _out(_row(uid, tid))


@router.delete("/{tid}", status_code=204)
async def delete(tid: str, request: Request):
    """Строка (куски — каскадом), затем исходный файл, если на него больше ничто не ссылается."""
    uid = request.state.user.id
    row = _row(uid, tid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала остановите"})
    state.DB.execute("DELETE FROM translations WHERE id = ?", (tid,))
    media_id = row["source_media_id"]
    if media_id is not None and not media.referenced(uid, media_id):
        await media.delete_async(uid, media_id)
    return Response(status_code=204)
