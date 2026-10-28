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
6. Файл (спека 6.4) — PDF, DOCX, TXT, MD: текст `documents.extract`, файл — в `media`. PDF со страницами без слоя
   при подключении «Сканов» через сервер (`ocr.plan_pages`) — обычный документ «Сканов» (`origin` section, тот же
   файл) в очереди распознавания пользователя, запись — без исходника, со `scan_id`. Её задание (`_await_scan`)
   ждёт скан раз в POLL_SEC (этап — страницы), берёт его текст (`ocr.attachment_text`) в исходник и куски и
   зовёт модель только после `ocr.idle`: карту освобождает раннер «Сканов» (`free_card`, `_settle`), и модель
   перевода иначе ждала бы её под арендой слота ocr. Скан удалён, с ошибкой, отменён или без единой страницы с
   текстом — ошибка записи «скан не распознан: …»; «Остановить» во время ожидания отменяет и скан. PDF со
   страницами-сканами без такого подключения — отказ загрузки, и когда сканы — только часть страниц.
7. Снимок (спека 6.5, 6.6) — PNG, JPEG, WebP при подключении «Сканов» через сервер: на шару — только PNG после
   поворота по EXIF (`ocr_render.photo_png`), его же байты уходят в распознавание и показываются — рамки лягут на
   свои места. Задание (`_spot`) распознаёт его одним запросом `/pages/ocr` с указанием `translate_spot.PROMPT`
   под `ocr.borrowed` (free_card до, release слота ocr после, учёт в `ocr.EXTERNAL`), строки собирает в блоки
   (`translate_spot.group`) — куски с рамкой; текста нет — запись «готово» со словами, без модели; текст без
   координат — ошибка NO_BOXES (повтор может помочь). Модель — после
   `ocr.idle`. Блоки переводятся пачками соседних до CHUNK_CHARS одним нумерованным запросом (`_blocks`); номера
   вернулись не все — каждый блок пачки отдельно.
8. Повтор (спека 6.2) — новая запись из прежней: исходник известен — он же (у снимка — блоки с рамками), без
   нового распознавания; нет, но скан готов — его текст; иначе файл с шары проходит путь загрузки заново.
   Выгрузка — txt и docx перевода (docx через `md_plain` — только у перевода скана: прочее — не Markdown).
9. Журнал — только id, коды и числа: текст, имя файла, языки исходника туда не пишутся. База — только с цикла
   событий.
10. API раздела — `router` (`/api/translate`, вторая половина файла): языки, перевод текста и файла, лента,
   запись с кусками (`?from=` — только новые), остановка, «Продолжить», повтор, выгрузка, удаление.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import chat
import clock
import connections
import db
import documents
import exports
import images
import media
import ocr
import ocr_render
import sections
import state
import translate_spot
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
NO_CONNECTION = "подключение перевода удалено — переведите заново"
SCAN_FAILED = "скан не распознан"
PHOTO_FAILED = "снимок не распознан"
NO_TEXT = "текст на снимке не найден"
NO_BOXES = "распознавание вернуло текст без координат — попробуйте ещё раз"
NAME_CHARS = 60                 # имя текстовой записи — начало первой строки исходника
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


def _chunks(source: str) -> list[tuple]:
    """Строки кусков исходника: (текст, разделитель после, рамки нет)."""
    return [(c, sep, None) for c, sep in translate_text.split(source, translate_text.CHUNK_CHARS)]


def create(user_id: int, conn, model: str, *, kind: str, name: str, source_lang: str, target_lang: str,
           source: "str | None", source_media_id: "str | None" = None, scan_id: "str | None" = None,
           blocks: "list[dict] | None" = None, ocr_conn=None) -> str:
    """Запись в очередь своей полосы (раннер — `kick`). Куски — `blocks` (`{"source", "box"}`) или нарезка
    `source` (уже `normalize`); исходник ещё не известен (скан) — кусков нет. Ждущих уже QUEUE_MAX — 409 busy."""
    _check_queue(user_id, lane_of(kind))
    if blocks is not None:
        rows = [(b["source"], "\n", json.dumps(b["box"])) for b in blocks]
    else:
        rows = _chunks(source) if source else []
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


async def _busy(job: Job, waited_from: "float | None") -> float:
    """503 шлюза: пауза RETRY_SEC с этапом «жду карту · м:сс» от первого отказа; дольше WAIT_MAX_SEC — ошибка,
    остановка — сразу после паузы. Итог — начало ожидания."""
    now = clock.NOW()
    waited_from = now if waited_from is None else waited_from
    if now - waited_from >= WAIT_MAX_SEC:
        raise translate_text.TranslateError(f"карта занята дольше {WAIT_MAX_SEC // 60} минут — попробуйте позже")
    stage = job.stage
    job.stage, job.stage_at = "жду карту", waited_from
    _store(job)
    await clock.SLEEP(RETRY_SEC)
    _check(job)
    job.stage = stage
    _store(job)
    return waited_from


async def _attempt(job: Job, text: str, origin: str, lang: str, blocks: bool = False) -> str:
    """Перевод текста одним запросом; 503 — ждать и повторять (остановка между попытками — сразу).
    Дельты дописываются к `job.live` после уже готового (половинка обрезанного куска); у нумерованного
    списка блоков (`blocks`) — нет: «[1] …» — не перевод для показа."""
    head, waited_from = job.live, None

    def on_delta(piece: str) -> None:
        _check(job)                                   # «Остановить» — поток бросается на следующей дельте
        if not blocks:
            job.live += piece

    while True:
        job.live = head
        try:
            return await translate_text.chunk(job.conn, job.model, text, origin, lang, on_delta, blocks=blocks)
        except translate_text.Busy:
            waited_from = await _busy(job, waited_from)


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


async def _cancel_scan(user_id: int, scan_id: str) -> None:
    try:
        await ocr.request_cancel(user_id, scan_id)
    except HTTPException:                        # документ уже удалён
        pass


async def _await_scan(job: Job, row):
    """Скан записи-файла: ждать документ «Сканов» раз в POLL_SEC (этап — страница в работе), готовый — его текст
    в исходник и куски; затем ждать `ocr.idle` (раннер «Сканов» освобождает слот ocr). Итог — строка записи заново."""
    sid = row["scan_id"]
    while True:
        if job.cancel:                           # «Остановить» — и скан: его ждал только перевод
            await _cancel_scan(job.user_id, sid)
            raise _Cancelled
        scan = state.DB.execute("SELECT state, detail FROM scans WHERE id = ? AND user_id = ?",
                                (sid, job.user_id)).fetchone()
        if scan is None:
            raise translate_text.TranslateError(f"{SCAN_FAILED}: документ удалён")
        if scan["state"] in ("error", "cancelled"):
            raise translate_text.TranslateError(f"{SCAN_FAILED}: {scan['detail']}")
        if scan["state"] == "done":
            break
        done, pages = ocr.progress([sid])
        stage = f"распознаю · стр. {min(done + 1, pages)} из {pages}"
        if stage != job.stage:
            job.stage = stage
            _store(job)
        await clock.SLEEP(POLL_SEC)
    unread = _unread(sid)
    if unread is not None:
        raise translate_text.TranslateError(f"{SCAN_FAILED}: {unread}")
    source = translate_text.normalize(ocr.attachment_text(sid))
    if len(source) > translate_text.TEXT_MAX:
        raise translate_text.TranslateError(TOO_LONG)
    rows = _chunks(source)
    _set_source(job, source, rows)
    log.info("перевод %s: скан готов, %d знаков, %d кусков", job.id[:8], len(source), len(rows))
    return await _await_idle(job)


def _unread(sid: str) -> "str | None":
    """Готовый скан без единой страницы с текстом (распознанной или слоя) — слова (отказ первой нераспознанной
    или «текст не найден»): переводить нечего, кроме пометок. Есть текст — None."""
    pages = state.DB.execute("SELECT state, detail FROM scan_pages WHERE scan_id = ? ORDER BY n", (sid,)).fetchall()
    if any(p["state"] in ("done", "layer") for p in pages):
        return None
    failed = next((p for p in pages if p["state"] == "failed"), None)
    return failed["detail"] if failed is not None and failed["detail"] else "текст не найден"


def _set_source(job: Job, source: str, rows: list[tuple]) -> None:
    """Распознанный исходник и его куски — одной транзакцией."""
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("UPDATE translations SET source = ?, parts = ? WHERE id = ?", (source, len(rows), job.id))
        state.DB.executemany("INSERT INTO translation_parts(translation_id, n, source, sep, box, state)"
                             " VALUES (?, ?, ?, ?, ?, 'waiting')", [(job.id, n, *r) for n, r in enumerate(rows, 1)])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise


async def _await_idle(job: Job):
    """Модель — только после `ocr.idle`: иначе локальная ждала бы карту под арендой слота ocr. Итог — строка
    записи заново."""
    job.stage = "готовлю"
    _store(job)
    while not ocr.idle(job.user_id):
        _check(job)
        await clock.SLEEP(POLL_SEC)
    return state.DB.execute("SELECT * FROM translations WHERE id = ?", (job.id,)).fetchone()


def _read_source(job: Job, media_id: str) -> bytes:
    """Файл записи — в потоке; нет или шара не отвечает — отказ словами."""
    try:
        return _read_media(job.user_id, media_id)
    except HTTPException as e:
        raise translate_text.TranslateError(e.detail["message"]) from None


async def _spotted(job: Job, png: bytes) -> str:
    """Ответ распознавания снимка с координатами; 503 — ждать и повторять, как кусок."""
    waited_from = None
    while True:
        try:
            return await ocr.recognize(job.ocr_conn, png, translate_spot.PROMPT)
        except ocr.Busy:
            waited_from = await _busy(job, waited_from)


async def _spot(job: Job, row):
    """Снимок без исходника: распознать под учётом карты (`ocr.borrowed`), блоки — в исходник и куски с рамкой.
    Текста нет — None (запись «готово» без модели); иначе — строка записи после `ocr.idle`."""
    job.stage = "распознаю снимок"
    _store(job)
    png = await asyncio.to_thread(_read_source, job, row["source_media_id"])
    _check(job)
    try:
        async with ocr.borrowed(job.id, job.user_id, job.ocr_conn):
            content = await _spotted(job, png)
            _check(job)                              # ответ остановленного — выбросить
    except ocr.PageFailed as e:
        if str(e) != ocr.EMPTY:
            raise translate_text.TranslateError(f"{PHOTO_FAILED}: {e}") from None
        content = ""
    except ocr.OcrError as e:
        raise translate_text.TranslateError(f"{PHOTO_FAILED}: {e}") from None
    if job.ocr_conn.kind == "srvllm" and job.ocr_conn.base_url == job.conn.base_url:
        job.freed = True                             # free_card того же шлюза уже отпустил модели картинок
    blocks = translate_spot.group(translate_spot.parse(content))
    log.info("перевод %s: снимок, %d блоков", job.id[:8], len(blocks))
    if not blocks:
        if content.strip():                          # текст есть, координат нет — повтор может помочь
            raise translate_text.TranslateError(NO_BOXES)
        return None
    source = "\n".join(b["source"] for b in blocks)
    _set_source(job, source, [(b["source"], "\n", json.dumps(b["box"])) for b in blocks])
    return await _await_idle(job)


def _done(job: Job, n: int, text: str) -> None:
    state.DB.execute("UPDATE translation_parts SET state = 'done', text = ?, detail = NULL"
                     " WHERE translation_id = ? AND n = ?", (text, job.id, n))
    state.DB.execute("UPDATE translations SET done_parts = done_parts + 1 WHERE id = ?", (job.id,))


def _failed(job: Job, n: int, parts: int, e: Exception) -> translate_text.TranslateError:
    state.DB.execute("UPDATE translation_parts SET state = 'failed', detail = ? WHERE translation_id = ? AND n = ?",
                     (str(e), job.id, n))
    return translate_text.TranslateError(f"кусок {n} из {parts}: {e}")


async def _part(job: Job, n: int, source: str, parts: int, origin: str, lang: str) -> None:
    """Один кусок (или блок снимка) — своим запросом; отказ — кусок `failed`, ошибка записи с его номером."""
    _check(job)
    job.stage, job.live = f"перевожу · {n} из {parts}", ""
    _store(job)
    try:
        text = await _chunk(job, source, origin, lang)
    except translate_text.TranslateError as e:
        raise _failed(job, n, parts, e) from e
    _check(job)                                  # ответ остановленного — выбросить
    _done(job, n, text)
    job.live = ""


def _batches(waiting: list) -> list[list]:
    """Соседние блоки пачками суммой не длиннее CHUNK_CHARS (длинный блок — пачка сам)."""
    out: list[list] = []
    for part in waiting:
        if out and out[-1][-1]["n"] + 1 == part["n"] and sum(len(p["source"]) for p in out[-1]) + len(
                part["source"]) <= translate_text.CHUNK_CHARS:
            out[-1].append(part)
        else:
            out.append([part])
    return out


async def _blocks(job: Job, waiting: list, parts: int, origin: str, lang: str) -> None:
    """Блоки снимка: пачка — одним нумерованным запросом; номера вернулись не все или ответ обрезан — каждый
    блок пачки отдельно. Каждый блок — свой кусок `done`."""
    for batch in _batches(waiting):
        if len(batch) == 1:
            await _part(job, batch[0]["n"], batch[0]["source"], parts, origin, lang)
            continue
        _check(job)
        first, last = batch[0]["n"], batch[-1]["n"]
        job.stage, job.live = f"перевожу · {first}–{last} из {parts}", ""
        _store(job)
        try:
            answer = await _attempt(job, translate_text.numbered([p["source"] for p in batch]), origin, lang,
                                    blocks=True)
            texts = translate_text.parse_numbered(answer, len(batch))
        except translate_text.Cut:
            texts = None
        except translate_text.TranslateError as e:
            raise _failed(job, first, parts, e) from e
        _check(job)                              # ответ остановленного — выбросить
        if texts is None:
            log.info("перевод %s: номера блоков %d–%d не сошлись — по одному", job.id[:8], first, last)
            for p in batch:
                await _part(job, p["n"], p["source"], parts, origin, lang)
            continue
        for p, text in zip(batch, texts):
            _done(job, p["n"], text)


async def _translate(job: Job, row) -> "str | None":
    """Куски `waiting` по порядку; отказ куска — ошибка записи с его номером. Итог — слова «готово» иные, чем
    DONE (снимок без текста), или None."""
    if row["source"] is None:
        row = await (_await_scan(job, row) if row["scan_id"] is not None else _spot(job, row))
        if row is None:
            return NO_TEXT
    if sections.is_local(job.conn, job.model) and not job.freed:
        job.freed = True
        await _free(job.conn)                    # и до определения языка: оно — запрос к той же модели
    _check(job)                                  # «Остановить» до определения языка — без запроса к модели
    origin, lang = await _languages(job, row)
    waiting = state.DB.execute("SELECT n, source FROM translation_parts WHERE translation_id = ? AND state = 'waiting'"
                               " ORDER BY n", (job.id,)).fetchall()
    if row["kind"] == "photo":
        await _blocks(job, waiting, row["parts"], origin, lang)
        return None
    for part in waiting:
        await _part(job, part["n"], part["source"], row["parts"], origin, lang)
    return None


async def run(job: Job) -> None:
    """Одна запись. Итог — в строку последним: перевод готовых кусков остаётся при любом исходе."""
    row = state.DB.execute("SELECT * FROM translations WHERE id = ?", (job.id,)).fetchone()
    outcome = ("error", INTERRUPTED)
    try:
        try:
            job.stage, job.stage_at = "готовлю", clock.NOW()
            state.DB.execute("UPDATE translations SET state = 'working', detail = ? WHERE id = ?", (job.stage, job.id))
            outcome = ("done", await _translate(job, row) or DONE)
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
        if row["scan_id"] is not None and not row["has_source"]:
            await _cancel_scan(user_id, row["scan_id"])
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
    try:
        connections.get(user_id, row["connection_id"])
    except HTTPException:                        # 404 gone — «такого перевода нет» для страницы, а запись есть
        raise HTTPException(409, {"code": "no_connection", "message": NO_CONNECTION}) from None
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
        ocr.settle_later()                       # оборванный снимок карту не освободил (`ocr.borrowed`)


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
FORMAT = "нужен PDF, DOCX, TXT, MD или снимок PNG, JPEG, WebP"
SOME_SCANS = "страниц-сканов в PDF — {} из {}, без распознавания перевод вышел бы без них"
FILE_GONE = "исходный файл удалён"
NO_OCR_PHOTO = "чтобы переводить снимки, подключите «Распознавание» в настройках"
#: имя выгрузки текстовой записи: «Перевод.ru.txt»
TEXT_STEM = "Перевод"


class TextIn(BaseModel):
    # до нормализации: переносы Windows (`\r\n`) — два знака на один, предел TEXT_MAX сверяется после
    text: str = Field(max_length=2 * translate_text.TEXT_MAX)
    source_lang: str = Field(max_length=8)
    target_lang: str = Field(max_length=8)
    connection_id: "int | None" = None
    model: "str | None" = Field(None, max_length=sections.MODEL_MAX)


class RepeatIn(BaseModel):
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


def _int_header(request: Request, key: str) -> "int | None":
    value = request.headers.get(key, "").strip()
    return int(value) if value.isdigit() else None


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


async def _read_file(user_id: int, name: str, data: bytes) -> "tuple[str | None, list[dict] | None, object]":
    """(текст, None, _) документа или (None, страницы, (подключение, модель) «Сканов») скана — до записи на шару.
    Скан — PDF со страницами без слоя (`ocr.plan_pages`), только при подключении «Сканов» через сервер; без него —
    отказ `scan` словами, и когда сканы — только часть страниц. Отказы — HTTPException."""
    suffix = PurePosixPath(name.lower()).suffix
    if suffix not in documents.SUFFIXES:
        raise HTTPException(415, {"code": "format", "message": FORMAT})
    reader = sections.default(user_id, "ocr") if suffix == ".pdf" else None
    try:
        if suffix == ".pdf":
            scanned = await asyncio.to_thread(ocr_render.pdf_scan_pages, data)
            wanted = sum(w for _, w in scanned)
            if wanted and (reader is None or not reader[0].proxied):
                # и часть страниц-сканов — отказ: иначе перевод молча вышел бы без них
                if wanted == len(scanned):
                    words = chat.NO_OCR if reader is None else sections.unlisted(reader[0])
                else:
                    words = SOME_SCANS.format(wanted, len(scanned)) + (
                        " — подключите «Распознавание» в настройках" if reader is None
                        else f"; {sections.unlisted(reader[0])}")
                raise HTTPException(422, {"code": "scan", "message": words})
            text, pages = ocr.plan_pages(scanned)
        else:
            text, pages = await asyncio.to_thread(documents.extract, name, data), None
    except documents.DocumentError as e:
        if str(e) == documents.TOO_LONG:             # предел перевода меньше, его слова — свои
            raise HTTPException(422, {"code": "too_long", "message": TOO_LONG}) from None
        raise HTTPException(e.status, {"code": "document", "message": str(e)}) from None
    if pages is not None:
        return None, pages, reader
    text = translate_text.normalize(text)
    if not text:
        raise HTTPException(422, {"code": "empty", "message": "в файле нет текста"})
    if len(text) > translate_text.TEXT_MAX:
        raise HTTPException(422, {"code": "too_long", "message": TOO_LONG})
    return text, None, reader


def _is_photo(data: bytes) -> bool:
    """Снимок PNG, JPEG, WebP (в потоке: заголовок Pillow); слишком большой — отказ 413, не снимок — False."""
    try:
        ocr_render.check_photo(data)
    except ocr_render.ScanError as e:
        if e.status == 415:
            return False
        raise HTTPException(e.status, {"code": "photo", "message": str(e)}) from None
    return True


def _photo_reader(user_id: int):
    """(подключение, модель) «Сканов» для снимка: только через сервер, иначе 409 no_ocr словами."""
    reader = sections.default(user_id, "ocr")
    if reader is None or not reader[0].proxied:
        raise HTTPException(409, {"code": "no_ocr", "message": (
            NO_OCR_PHOTO if reader is None else sections.unlisted(reader[0]))})
    return reader


def _start_file(user_id: int, conn, model: str, *, name: str, source_lang: str, target_lang: str, media_id: str,
                text: "str | None", pages: "list[dict] | None", reader, kind: str = "file") -> str:
    """Запись-файл или снимок (`kind`, исходник распознает задание) в полосу `file`; скан — сначала документ
    «Сканов» с тем же файлом. Без await: между проверкой очереди и записью она не изменится, отказ (409 busy любой
    из двух очередей) не оставит половины."""
    scan_id = None
    if pages is not None:
        _check_queue(user_id, "file")
        scan_id = ocr.create(user_id, reader[0], reader[1], name, origin="section", source_media_id=media_id,
                             pages=pages)
    tid = create(user_id, conn, model, kind=kind, name=name, source_lang=source_lang, target_lang=target_lang,
                 source=text, source_media_id=media_id, scan_id=scan_id,
                 ocr_conn=reader[0] if kind == "photo" else None)
    if scan_id is not None:
        ocr.kick(user_id)
        log.info("перевод %s: скан %s, %d стр. на распознавание", tid[:8], scan_id[:8],
                 sum(p["state"] == "waiting" for p in pages))
    kick(user_id, "file")
    return tid


@router.post("/files", status_code=202)
async def translate_file(request: Request):
    """Файл — multipart, одно поле `file` (имя — из части, не из строки запроса: её пишет журнал Apache); языки,
    подключение и модель — заголовками `X-Studio-*`. Отказы — до записи на шару; после неё — файл удаляется.
    Снимок (`ocr_render.check_photo` его принял) — нужно подключение «Сканов» через сервер; на шару — только PNG
    после поворота по EXIF: его байты и распознаются, и показываются."""
    uid = request.state.user.id
    source_lang = request.headers.get("x-studio-source-lang", "")
    target_lang = request.headers.get("x-studio-target-lang", "")
    _langs(source_lang, target_lang)
    conn, model = sections.resolve(uid, "translate", _int_header(request, "x-studio-connection"),
                                   request.headers.get("x-studio-model") or None)
    try:
        name, data = await documents.read_upload(request)
    except documents.DocumentError as e:
        raise HTTPException(e.status, {"code": "upload", "message": str(e)}) from None
    if await asyncio.to_thread(_is_photo, data):
        kind, text, pages, reader = "photo", None, None, _photo_reader(uid)
        try:
            data = await asyncio.to_thread(ocr_render.photo_png, data)
        except ocr_render.ScanError as e:
            raise HTTPException(e.status, {"code": "format", "message": str(e)}) from None
        mime, stored = "image/png", f"{_stem(name)}.png"
    else:
        kind, (text, pages, reader) = "file", await _read_file(uid, name, data)
        mime, stored = chat.DOC_MIME[PurePosixPath(name.lower()).suffix], name
    _check_queue(uid, "file")                        # полная полоса — до записи на шару
    try:
        media_id = await media.save_async(uid, data, mime, stored)
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    if not media.alive(uid, media_id):               # история стёрта, пока шла запись
        await media.delete_async(uid, media_id)
        raise HTTPException(409, {"code": "gone", "message": "файл удалён во время загрузки"})
    try:
        tid = _start_file(uid, conn, model, name=name, source_lang=source_lang, target_lang=target_lang,
                          media_id=media_id, text=text, pages=pages, reader=reader, kind=kind)
    except HTTPException:                            # очередь заполнилась, пока шла запись
        await media.delete_async(uid, media_id)
        raise
    return _out(_row(uid, tid))


def _read_media(user_id: int, media_id: str) -> bytes:
    """Исходный файл записи — в потоке; нет — 409 gone, шара не отвечает — 503."""
    try:
        return media.path(user_id, media_id).read_bytes()
    except (HTTPException, FileNotFoundError):
        raise HTTPException(409, {"code": "gone", "message": FILE_GONE}) from None
    except OSError:
        raise HTTPException(503, {"code": "storage", "message": "хранилище недоступно"}) from None


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


@router.post("/{tid}/repeat", status_code=202)
async def repeat(tid: str, body: RepeatIn, request: Request):
    """Новая запись из прежней — тот же вид, имя и файл, свои языки и модель; прежняя не меняется. Исходник
    известен — он же (у снимка — блоки с рамками), без нового распознавания; нет, но скан записи готов — его
    текст, тоже без распознавания; иначе (скан не распознан) — файл с шары проходит путь загрузки, скан — новым
    документом «Сканов». Файла нет — 409 gone."""
    uid = request.state.user.id
    row = _row(uid, tid)
    _langs(body.source_lang, body.target_lang)
    conn, model = sections.resolve(uid, "translate", body.connection_id, body.model)
    langs = {"source_lang": body.source_lang, "target_lang": body.target_lang}
    media_id = row["source_media_id"]
    if row["has_source"]:
        source = state.DB.execute("SELECT source FROM translations WHERE id = ?", (tid,)).fetchone()[0]
        blocks = [{"source": p["source"], "box": json.loads(p["box"])} for p in state.DB.execute(
            "SELECT source, box FROM translation_parts WHERE translation_id = ? AND box IS NOT NULL ORDER BY n", (tid,))]
        new = create(uid, conn, model, kind=row["kind"], name=row["name"], source=source, source_media_id=media_id,
                     scan_id=row["scan_id"], blocks=blocks or None, **langs)
        kick(uid, lane_of(row["kind"]))
    elif _scan_done(uid, row["scan_id"]):            # скан распознан, перевод не дошёл до кусков — без нового
        source = translate_text.normalize(ocr.attachment_text(row["scan_id"]))
        if len(source) > translate_text.TEXT_MAX:
            raise HTTPException(422, {"code": "too_long", "message": TOO_LONG})
        new = create(uid, conn, model, kind=row["kind"], name=row["name"], source=source, source_media_id=media_id,
                     scan_id=row["scan_id"], **langs)
        kick(uid, lane_of(row["kind"]))
    else:
        if media_id is None or not media.alive(uid, media_id):
            raise HTTPException(409, {"code": "gone", "message": FILE_GONE})
        if row["kind"] == "photo":                   # PNG на шаре уже повёрнут — распознает задание
            text, pages, reader = None, None, _photo_reader(uid)
        else:
            data = await asyncio.to_thread(_read_media, uid, media_id)
            text, pages, reader = await _read_file(uid, row["name"], data)
            if not media.alive(uid, media_id):       # удалён, пока читали
                raise HTTPException(409, {"code": "gone", "message": FILE_GONE})
        new = _start_file(uid, conn, model, name=row["name"], media_id=media_id, text=text, pages=pages,
                          reader=reader, kind=row["kind"], **langs)
    log.info("перевод %s: повтор %s", new[:8], tid[:8])
    return _out(_row(uid, new))


def _scan_done(user_id: int, sid: "str | None") -> bool:
    """Скан записи готов и есть что переводить (`_unread`) — повтор берёт его текст, не распознавая заново."""
    if sid is None:
        return False
    scan = state.DB.execute("SELECT state FROM scans WHERE id = ? AND user_id = ?", (sid, user_id)).fetchone()
    return scan is not None and scan["state"] == "done" and _unread(sid) is None


@router.get("/{tid}/export")
async def export(tid: str, request: Request, format: str = Query(..., pattern="^(txt|docx)$")):  # noqa: A002
    """Перевод файлом — у кончившейся записи с переведённым: «<имя без расширения>.<язык перевода>.<формат>»,
    у текста — «Перевод.ru.txt»."""
    row = _row(request.state.user.id, tid)
    text = joined(tid) if row["state"] in TERMINAL else ""
    if not text:
        raise HTTPException(409, {"code": "running", "message": "перевода ещё нет"})
    stem = TEXT_STEM if row["kind"] == "text" else _stem(row["name"])
    # Markdown — только у перевода распознанного скана; текст поля и документа — как есть
    body = (exports.text_docx(text, stem, markdown=row["scan_id"] is not None) if format == "docx"
            else text.encode("utf-8"))
    name = f"{stem}.{row['result_lang'] or row['target_lang']}.{format}"
    return Response(body, media_type=exports.TYPES[format],
                    headers={"Content-Disposition": media.disposition(name), "Cache-Control": "no-store"})


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
