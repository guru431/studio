"""Распознавание сканов (спека 2026-10-27-studio-ocr-design, 6.2, 6.3, 6.5, 6.8).

1. Документ — строка `scans`, страницы — `scan_pages` (n с 1): `waiting` ждёт распознавания, `layer` —
   текст из слоя PDF (скан в чате), итог — `done` с текстом, `failed` со словами, `blank` — пустая.
   `pages` — все страницы документа, `done_pages` — сколько из них уже с итогом (`layer` — сразу).
2. Задания пользователя идут по очереди (`RUNNERS` — один раннер на пользователя, `kick`): старейший
   `queued` → `working`; ждущих — не больше QUEUE_MAX. Разные пользователи — параллельно.
3. Страница: PDF — рендер страницы исходника (`ocr_render`, в потоке) и PNG на шару (`scan_pages.media_id`),
   фото — PNG из своего файла (не сохраняется); пустая — без шлюза. Перед первой непустой — release
   моделей картинок и gemma-abl (`speech.free_card`): слот ocr рядом с ними не помещается. Запрос — как
   у cleaner (`_ocr_chat`): PROMPT дословно, картинка первой. Обрезанный, пустой ответ и карта блоков без
   текста — отказ одной страницы (PageFailed), как и страница, которую не нарисовать (ScanError рендера,
   обрезанное фото — UNREADABLE); прочие отказы шлюза — отказ документа (OcrError). 503 —
   ждать и повторять раз в RETRY_SEC до WAIT_MAX_SEC («жду карту · м:сс»).
4. Отмена (`request_cancel`): ждущий — сразу `cancelled`; идущий — `cancelling`: запрос страницы шлюз не
   обрывает (LiteLLM обрыв дальше не передаёт), поэтому раннер дожидается ответа, выбрасывает его и
   освобождает карту. Слот ocr освобождает студия, а не документ (`LEASED`, `_settle`): после каждого
   документа (звал он шлюз или нет) и после отмены ждущего — release шлюзов, чей слот взят, если в студии
   не идёт и не ждёт другое распознавание (иначе следующее заплатит холодный старт).
5. Стирание истории, удаление и отключение учётки (`stop_user`) — ждущие отменены, идущий оборван
   сразу (без release: шлюз ещё считает оборванную страницу); остановка службы (`stop`) — идущий —
   ошибка «прервано», ждущие — их сделает ошибкой `recover` при старте.
6. У документа из чата (`attachment_id`) итог — текст вложения: страницы с пометкой «Страница N»
   (`attachment_text`) или пометка отказа (`attachment_marker`).
7. Журнал — только id, коды и числа: имя файла, текст страниц и картинки туда не пишутся. База — только
   с цикла событий; рендер, чтение и запись файлов — в потоке.
8. API раздела — `router` (`/api/ocr`, вторая половина файла): загрузка, лента, документ со страницами,
   отмена, удаление, выгрузка txt/md/docx (`exports.pages_*`), «В чат» — вложение «<имя>.md».
"""
import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

import chat
import clock
import db
import documents
import exports
import media
import ocr_render
import sections
import speech
import state
import upstream

MODEL = "tencent/HunyuanOCR"
GATEWAY = {"hunyuan-ocr": MODEL}
# Указание — дословно cleaner `_OCR_PROMPT` (из translate prompts/ocr_extract_hunyuan_v1.txt, проверено на корпусе)
PROMPT = (
    "Extract all information from the document image and represent it as Markdown.\n"
    "Tables must be expressed in HTML with <table>/<tr>/<td>; use colspan/rowspan for merged cells.\n"
    "Formulas must use LaTeX inline (e.g. $E=mc^2$).\n"
    "Preserve the original languages — do NOT translate.\n"
    "Follow the natural reading order (top-to-bottom, left-to-right; for multi-column layouts complete one column "
    "before moving to the next).\n"
    "For text visible under stamps or seals, transcribe it normally.\n"
    "Output only the Markdown body — no preamble, no commentary."
)
MAX_TOKENS = 16384               # у HunyuanOCR на srvllm контекст 32768, страница-картинка — несколько тысяч
PAGE_TIMEOUT_SEC = 700           # дольше timeout_s маршрута /pages/ocr (660 с)
RELEASE_TIMEOUT_SEC = 30
RELEASE_RETRIES = 3
RETRY_SEC = 15
WAIT_MAX_SEC = 1800
QUEUE_MAX = 5
RUNNING = ("queued", "working", "cancelling")
TERMINAL = ("done", "error", "cancelled")
QUEUED = "в очереди"
CANCELLING = "отменяю"
CANCELLED = "отменено"
DONE = "готово"
INTERRUPTED = "прервано перезапуском студии"
UNREADABLE = "страница не читается — файл повреждён"

log = logging.getLogger("studio")


class OcrError(Exception):
    """Отказ документа словами для человека."""


class Busy(OcrError):
    """Карта занята (503 шлюза): ждать и повторить."""


class PageFailed(OcrError):
    """Страница не прочитана — словами; остальные страницы документа это не останавливает."""


class _Cancelled(Exception):
    """Отмена человеком, замеченная между шагами."""


@dataclass
class Job:
    id: str
    user_id: int
    conn: object
    stage: str = QUEUED
    stage_at: float = 0.0
    cancel: bool = False          # «Отменить»: доделать идущую страницу, выбросить, освободить карту
    stopped: bool = False         # stop_user: раннер оборван, итог — «отменено»
    called: bool = False          # звал шлюз: после документа — release слота ocr


#: документы в очереди и в работе (снимок подключения — для раннера); кончившееся уходит
JOBS: dict[str, Job] = {}
#: раннер пользователя: одно распознавание за раз
RUNNERS: dict[int, asyncio.Task] = {}
#: шлюзы, чей слот ocr взят и ещё не освобождён (адрес → снимок подключения последнего звавшего): освобождает
#: `_settle`, когда в студии нечего распознавать, — кто бы ни кончил последним, даже не звав шлюз
LEASED: dict[str, object] = {}


# ---- шлюз --------------------------------------------------------------------

def _layout_only(text: str) -> bool:
    """Ответ HunyuanOCR — карта блоков с координатами без содержимого, а не текст страницы (cleaner
    `_layout_only`): массив объектов без поля с текстом."""
    text = text.strip()
    if not text.startswith("[") or "layout_type" not in text:
        return False
    try:
        blocks = json.loads(text)
    except ValueError:
        return '"text"' not in text and '"content"' not in text      # оборванный JSON того же вида
    return isinstance(blocks, list) and bool(blocks) and all(
        isinstance(block, dict) and not any(str(block.get(key) or "").strip() for key in ("text", "content", "markdown"))
        for block in blocks)


async def recognize(conn, png: bytes) -> str:
    """Markdown одной страницы."""
    body = {"model": MODEL, "temperature": 0, "max_tokens": MAX_TOKENS, "messages": [
        {"role": "system", "content": ""},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"}},
            {"type": "text", "text": PROMPT}]}]}
    try:
        r = await upstream.request(conn, "POST", f"{conn.base_url}/pages/ocr", timeout=PAGE_TIMEOUT_SEC, json=body)
    except httpx.TimeoutException:
        raise OcrError("распознавание не ответило вовремя") from None
    except httpx.HTTPError as e:
        raise OcrError(f"распознавание недоступно: {type(e).__name__}") from None
    except upstream.UpstreamRefused as e:
        raise OcrError(upstream.refused_words(e, conn)) from None
    if r.status_code == 503:
        raise Busy(upstream.message(r) or "карта занята")
    if r.status_code != 200:
        raise OcrError(upstream.gateway_error(r, conn.name))
    try:
        choice = upstream.json_body(r)["choices"][0]
        content, finish = choice["message"].get("content"), choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise PageFailed("ответ шлюза не разбирается") from None
    if finish == "length":
        raise PageFailed("ответ модели обрезан — страница слишком плотная")
    if not isinstance(content, str) or not content.strip():
        raise PageFailed("на странице не найден текст")
    if _layout_only(content):
        raise PageFailed("модель вернула разметку без текста")
    return content.strip()


async def release(conn) -> None:
    """Освобождение слота ocr: `POST /pages/ocr-release {}`. 409 — slot-gate ещё считает страницу
    (свою или чужую): повтор через RETRY_SEC до RELEASE_RETRIES раз. Ошибки — в журнал, не наружу."""
    if conn.kind != "srvllm":
        return
    for attempt in range(RELEASE_RETRIES + 1):
        try:
            r = await upstream.request(conn, "POST", f"{conn.base_url}/pages/ocr-release",
                                       timeout=RELEASE_TIMEOUT_SEC, json={})
        except (httpx.HTTPError, upstream.UpstreamRefused) as e:
            log.warning("распознавание: release не прошёл (%s)", type(e).__name__)
            return
        if r.status_code == 200:
            log.info("распознавание: карта освобождена")
            return
        if r.status_code != 409 or attempt == RELEASE_RETRIES:
            log.warning("распознавание: release ответил %s", r.status_code)
            return
        await clock.SLEEP(RETRY_SEC)


# ---- документы ---------------------------------------------------------------

def attachment_marker(detail: str) -> str:
    return f"[скан не распознан: {detail}]"


def attachment_text(scan_id: str) -> str:
    """Текст вложения чата: страницы с итогом — «Страница N» и текст, нераспознанные — пометкой,
    пустые пропущены. Не длиннее documents.TEXT_MAX_CHARS (спека 6.5): дальше — обрезка с пометкой, а не
    отказ — распознанное уже стоило минут карты."""
    parts = []
    for p in state.DB.execute("SELECT n, state, text, detail FROM scan_pages WHERE scan_id = ? ORDER BY n", (scan_id,)):
        if p["state"] in ("done", "layer"):
            parts.append(f"Страница {p['n']}\n\n{p['text'] or ''}")
        elif p["state"] == "failed":
            parts.append(f"[страница {p['n']} не распознана: {p['detail']}]")
    text, limit = "\n\n".join(parts), documents.TEXT_MAX_CHARS
    if len(text) > limit:
        mark = f"\n\n[текст скана обрезан: длиннее {limit:,} знаков]".replace(",", " ")
        text = text[:limit - len(mark)] + mark
    return text


def _attachment(attachment_id: "str | None", text: str) -> None:
    if attachment_id is not None:
        state.DB.execute("UPDATE attachments SET text = ?, chars = ? WHERE id = ?", (text, len(text), attachment_id))


# ---- скан в чате (chat.py) -----------------------------------------------------

def pending(user_id: int, attachment_ids: list[str]) -> list[str]:
    """Идущие и ждущие документы (RUNNING) этих вложений чата."""
    if not attachment_ids:
        return []
    marks = ", ".join("?" * len(attachment_ids))
    return [r["id"] for r in state.DB.execute(
        f"SELECT id FROM scans WHERE user_id = ? AND state IN {RUNNING} AND attachment_id IN ({marks})"
        " ORDER BY created, rowid", (user_id, *attachment_ids))]


def progress(scan_ids: list[str]) -> tuple[int, int]:
    """Ход документов для плашки чата: (страниц с итогом, всего) — без страниц текстового слоя."""
    marks = ", ".join("?" * len(scan_ids))
    row = state.DB.execute(f"SELECT count(*), coalesce(sum(state != 'waiting'), 0) FROM scan_pages"
                           f" WHERE scan_id IN ({marks}) AND state != 'layer'", tuple(scan_ids)).fetchone()
    return row[1], row[0]


def idle(user_id: int) -> bool:
    """Раннер пользователя не дописывает свой последний документ: после итога он ещё освобождает карту
    (release), и ход чата, позвавший модель раньше, ждал бы её под арендой слота ocr. Идут другие его
    документы — карту никто не освободит (`run`), ждать нечего."""
    task = RUNNERS.get(user_id)
    return task is None or task.done() or state.DB.execute(
        f"SELECT 1 FROM scans WHERE user_id = ? AND state IN {RUNNING} LIMIT 1", (user_id,)).fetchone() is not None


def create(user_id: int, conn, model: str, name: str, *, origin: str, source_media_id: "str | None",
           pages: list[dict], attachment_id: "str | None" = None) -> str:
    """Документ в очередь пользователя (раннер — `kick`). `pages`: `{"n", "state": "waiting"|"layer",
    "media_id", "text"}`. Ждущих уже QUEUE_MAX — 409 busy."""
    waiting = state.DB.execute("SELECT count(*) FROM scans WHERE user_id = ? AND state = 'queued'",
                               (user_id,)).fetchone()[0]
    if waiting >= QUEUE_MAX:
        raise HTTPException(409, {"code": "busy", "message": (
            f"в очереди уже {QUEUE_MAX} документов — дождитесь их или отмените")})
    sid, now = uuid.uuid4().hex, clock.NOW()
    ready = sum(p["state"] != "waiting" for p in pages)
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("INSERT INTO scans(id, user_id, connection_id, model, name, source_media_id, origin,"
                         " attachment_id, pages, done_pages, state, detail, created)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                         (sid, user_id, conn.id, model, name, source_media_id, origin, attachment_id, len(pages),
                          ready, QUEUED, now))
        state.DB.executemany("INSERT INTO scan_pages(scan_id, user_id, n, media_id, state, text) VALUES (?, ?, ?, ?, ?, ?)",
                             [(sid, user_id, p["n"], p.get("media_id"), p["state"], p.get("text")) for p in pages])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    JOBS[sid] = Job(id=sid, user_id=user_id, conn=conn, stage_at=now)
    log.info("распознавание %s: в очереди, %d стр.", sid[:8], len(pages))
    return sid


def kick(user_id: int) -> None:
    """Раннер пользователя, если его нет (с цикла событий)."""
    task = RUNNERS.get(user_id)
    if task is None or task.done():
        RUNNERS[user_id] = asyncio.create_task(_runner(user_id))


def _clock(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60}:{s % 60:02d}"


def detail(job: Job) -> str:
    """Состояние идущего документа словами: ожидание карты — со временем."""
    if job.stage == "жду карту":
        return f"{job.stage} · {_clock(clock.NOW() - job.stage_at)}"
    return job.stage


def _store(job: Job) -> None:
    state.DB.execute("UPDATE scans SET detail = ? WHERE id = ? AND state = 'working'", (job.stage, job.id))


def _finish(scan_id: str, state_: str, detail_: str, text: "str | None" = None) -> None:
    """Итог документа и, у документа из чата, текст вложения."""
    state.DB.execute("UPDATE scans SET state = ?, detail = ?, finished = ?, text = ? WHERE id = ?",
                     (state_, detail_, clock.NOW(), text, scan_id))
    row = state.DB.execute("SELECT attachment_id FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if row is not None:
        _attachment(row["attachment_id"], attachment_text(scan_id) if state_ == "done" else attachment_marker(detail_))


def _check(job: Job) -> None:
    if job.cancel:
        raise _Cancelled


async def _page_text(job: Job, png: bytes) -> str:
    """Страница через шлюз; 503 — ждать и повторять (отмена между попытками — сразу)."""
    waited_from = None
    while True:
        try:
            return await recognize(job.conn, png)
        except Busy:
            now = clock.NOW()
            waited_from = now if waited_from is None else waited_from
            if now - waited_from >= WAIT_MAX_SEC:
                raise OcrError(f"карта занята дольше {WAIT_MAX_SEC // 60} минут — попробуйте позже") from None
            stage = job.stage
            job.stage, job.stage_at = "жду карту", waited_from
            _store(job)
            await clock.SLEEP(RETRY_SEC)
            _check(job)
            job.stage = stage
            _store(job)


def _read(user_id: int, media_id: str) -> bytes:
    """Файл документа — в потоке; файла нет — OcrError."""
    try:
        return media.path(user_id, media_id).read_bytes()
    except (HTTPException, FileNotFoundError):
        raise OcrError("файл документа удалён") from None
    except OSError:
        raise OcrError("хранилище недоступно") from None


async def _recognize_pages(job: Job, row) -> str:
    """Страницы `waiting` по порядку; итог — текст документа."""
    stem = row["name"].rsplit(".", 1)[0] if "." in row["name"] else row["name"]
    source = await asyncio.to_thread(_read, job.user_id, row["source_media_id"]) if row["source_media_id"] else None
    waiting = state.DB.execute("SELECT n, media_id FROM scan_pages WHERE scan_id = ? AND state = 'waiting' ORDER BY n",
                               (job.id,)).fetchall()
    for page in waiting:
        _check(job)
        n = page["n"]
        try:
            if source is not None:
                png = await asyncio.to_thread(ocr_render.render_page, source, n - 1)
            else:
                png = await asyncio.to_thread(ocr_render.photo_png,
                                              await asyncio.to_thread(_read, job.user_id, page["media_id"]))
        except ocr_render.ScanError:                      # битая страница, обрезанное фото — отказ страницы
            png = None
        if png is not None and source is not None:
            media_id = await media.save_async(job.user_id, png, "image/png", f"{stem}-{n}.png")
            state.DB.execute("UPDATE scan_pages SET media_id = ? WHERE scan_id = ? AND n = ?", (media_id, job.id, n))
        _check(job)
        if png is None:
            page_state, text, why = "failed", None, UNREADABLE
        elif await asyncio.to_thread(ocr_render.is_blank, png):
            page_state, text, why = "blank", None, None
        else:
            if not job.called:
                job.called = True
                await speech.free_card(job.conn)
                _check(job)                               # отменили, пока освобождалась карта
            LEASED[job.conn.base_url] = job.conn
            job.stage = f"распознаю · стр. {n} из {row['pages']}"
            _store(job)
            try:
                page_state, text, why = "done", await _page_text(job, png), None
            except PageFailed as e:
                page_state, text, why = "failed", None, str(e)
            _check(job)                                   # ответ отменённого — выбросить
        state.DB.execute("UPDATE scan_pages SET state = ?, text = ?, detail = ? WHERE scan_id = ? AND n = ?",
                         (page_state, text, why, job.id, n))
        state.DB.execute("UPDATE scans SET done_pages = done_pages + 1 WHERE id = ?", (job.id,))
    texts = [p["text"] for p in state.DB.execute(
        "SELECT text FROM scan_pages WHERE scan_id = ? AND state IN ('done', 'layer') ORDER BY n", (job.id,))]
    return "\n\n".join(t for t in texts if t)


async def _settle() -> None:
    """Release слота ocr каждого шлюза из LEASED, если в студии нечего распознавать (ни идущего, ни ждущего
    документа): следующий за ним заплатил бы холодный старт. Решает студия, а не документ: последний
    кончившийся мог шлюз и не звать (пустые страницы, отмена до первой страницы, отказ до шлюза)."""
    if not LEASED or state.DB.execute(f"SELECT 1 FROM scans WHERE state IN {RUNNING} LIMIT 1").fetchone() is not None:
        return
    leased = list(LEASED.values())
    LEASED.clear()
    for conn in leased:
        await release(conn)


async def run(job: Job) -> None:
    """Один документ. Итог — в строку последним; затем — `_settle` (release, если в студии больше нечего
    распознавать), звал этот документ шлюз или нет. Оборванный (stop_user, остановка службы) карту не
    освобождает."""
    row = state.DB.execute("SELECT * FROM scans WHERE id = ?", (job.id,)).fetchone()
    outcome = ("error", INTERRUPTED, None)
    try:
        try:
            job.stage, job.stage_at = "готовлю страницы", clock.NOW()
            state.DB.execute("UPDATE scans SET state = 'working', detail = ? WHERE id = ?", (job.stage, job.id))
            text = await _recognize_pages(job, row)
            outcome = ("done", DONE, text)
            log.info("распознавание %s: готово, %d стр.", job.id[:8], row["pages"])
        except _Cancelled:
            outcome = ("cancelled", CANCELLED, None)
            log.info("распознавание %s: отменено", job.id[:8])
        except (OcrError, ocr_render.ScanError, media.StorageError) as e:
            outcome = ("error", str(e), None)
            log.info("распознавание %s: ошибка %s", job.id[:8], type(e).__name__)
        except Exception as e:                       # noqa: BLE001 — документ не должен висеть
            outcome = ("error", f"сбой службы: {type(e).__name__}", None)
            log.warning("распознавание %s: сбой %s", job.id[:8], type(e).__name__)
    except asyncio.CancelledError:
        if job.stopped:
            outcome = ("cancelled", CANCELLED, None)
            log.info("распознавание %s: остановлено", job.id[:8])
        raise
    finally:
        _finish(job.id, *outcome)
        JOBS.pop(job.id, None)
    await _settle()


async def _runner(user_id: int) -> None:
    try:
        while True:
            row = state.DB.execute("SELECT id FROM scans WHERE user_id = ? AND state = 'queued'"
                                   " ORDER BY created, rowid LIMIT 1", (user_id,)).fetchone()
            if row is None:
                return
            job = JOBS.get(row["id"])
            if job is None:                          # снимка подключения нет — такого быть не должно
                _finish(row["id"], "error", INTERRUPTED)
                continue
            await run(job)
    finally:
        if RUNNERS.get(user_id) is asyncio.current_task():
            del RUNNERS[user_id]


async def request_cancel(user_id: int, scan_id: str) -> str:
    """«Отменить»: ждущий — сразу `cancelled`; идущий — `cancelling` (раннер дождётся страницы). Итог — state."""
    row = state.DB.execute("SELECT state FROM scans WHERE id = ? AND user_id = ?", (scan_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого документа нет"})
    if row["state"] == "queued":
        _finish(scan_id, "cancelled", CANCELLED)
        JOBS.pop(scan_id, None)
        log.info("распознавание %s: отменено в очереди", scan_id[:8])
        await _settle()
        return "cancelled"
    job = JOBS.get(scan_id)
    if row["state"] == "working" and job is not None:
        job.cancel = True
        state.DB.execute("UPDATE scans SET state = 'cancelling', detail = ? WHERE id = ?", (CANCELLING, scan_id))
        return "cancelling"
    return row["state"]


def recover() -> None:
    """После перезапуска раннеров нет: ждущие и идущие — ошибка «прервано перезапуском», у
    документов из чата — пометка во вложении."""
    rows = state.DB.execute(f"SELECT id FROM scans WHERE state IN {RUNNING}").fetchall()
    for row in rows:
        _finish(row["id"], "error", INTERRUPTED)
    if rows:
        log.warning("распознавания: %d прервано перезапуском", len(rows))


async def stop_user(user_id: int) -> None:
    """Стирание истории, удаление и отключение учётки: ждущие отменены, идущий оборван."""
    for row in state.DB.execute("SELECT id FROM scans WHERE user_id = ? AND state = 'queued'", (user_id,)).fetchall():
        _finish(row["id"], "cancelled", CANCELLED)
        JOBS.pop(row["id"], None)
    await _settle()                    # ждали отменённые — слот держали для них
    task = RUNNERS.get(user_id)
    if task is not None and not task.done():
        for job in JOBS.values():
            if job.user_id == user_id:
                job.stopped = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def stop() -> None:
    """Остановка службы: идущие пишут «прервано» и кончаются до закрытия базы."""
    tasks = [t for t in RUNNERS.values() if not t.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- API раздела (спека 6.4, 6.6) --------------------------------------------------
# Загрузка — multipart, части `file`: один PDF (все страницы ждут распознавания, слой не читается) или
# 1…PAGES_MAX фото (каждое — как есть, `media_id` своей страницы). Отказы — до записи на шару. Ответы —
# «item» (`_out`), у GET одного документа `pages` — список страниц, а не их число.

router = APIRouter(prefix="/api/ocr")
#: столбцы ленты и ответов — без `text` (весь текст документа нужен только поиску)
COLUMNS = ("id, user_id, connection_id, model, name, source_media_id, origin, attachment_id, pages, done_pages,"
           " state, detail, created, finished")
FORMAT = "нужен один PDF или фото PNG, JPEG, WebP"
TOO_MANY_PAGES = f"больше {ocr_render.PAGES_MAX} страниц — разделите документ"
PHOTO_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _row(user_id: int, sid: str):
    row = state.DB.execute(f"SELECT {COLUMNS} FROM scans WHERE id = ? AND user_id = ?", (sid, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого документа нет"})
    return row


def _out(row) -> dict:
    job = JOBS.get(row["id"])
    return {"id": row["id"], "name": row["name"], "state": row["state"],
            "detail": detail(job) if job is not None and row["state"] == "working" else row["detail"],
            "model": row["model"], "pages": row["pages"], "done_pages": row["done_pages"], "origin": row["origin"],
            "created": row["created"], "finished": row["finished"]}


def _page_list(sid: str) -> list[dict]:
    return [dict(r) for r in state.DB.execute(
        "SELECT n, state, text, detail, media_id FROM scan_pages WHERE scan_id = ? ORDER BY n", (sid,))]


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _inspect(files: list[tuple[str, bytes]]) -> tuple[int, list[str]]:
    """Состав загрузки (в потоке): один PDF — (страниц, []), только фото — (фото, их форматы Pillow).
    Иначе — ScanError словами."""
    if any(data[:5] == b"%PDF-" or name.lower().endswith(".pdf") for name, data in files):
        if len(files) != 1:
            raise ocr_render.ScanError(FORMAT, 415)
        pages = ocr_render.pdf_page_count(files[0][1])
        if pages > ocr_render.PAGES_MAX:
            raise ocr_render.ScanError(TOO_MANY_PAGES)
        if pages == 0:
            raise ocr_render.ScanError(ocr_render.BROKEN)
        return pages, []
    formats = []
    for _, data in files:
        try:
            formats.append(ocr_render.check_photo(data))
        except ocr_render.ScanError as e:
            if e.status == 415:
                raise ocr_render.ScanError(FORMAT, 415) from None
            raise
    return len(files), formats


async def _drop(user_id: int, media_ids: list[str]) -> None:
    for media_id in media_ids:
        await media.delete_async(user_id, media_id)


def _int_header(request: Request, key: str) -> "int | None":
    value = request.headers.get(key, "").strip()
    return int(value) if value.isdigit() else None


@router.post("", status_code=202)
async def upload(request: Request):
    uid = request.state.user.id
    conn, model = sections.resolve(uid, "ocr", _int_header(request, "x-studio-connection"),
                                   request.headers.get("x-studio-model") or None)
    try:
        files = await documents.read_uploads(request, ocr_render.PAGES_MAX)
        count, formats = await asyncio.to_thread(_inspect, files)
    except documents.DocumentError as e:
        raise HTTPException(e.status, {"code": "upload", "message": str(e)}) from None
    except ocr_render.ScanError as e:
        raise HTTPException(e.status, {"code": "format" if e.status == 415 else "scan", "message": str(e)}) from None
    saved: list[str] = []
    try:
        if formats:
            for (name, data), fmt in zip(files, formats):
                saved.append(await media.save_async(uid, data, PHOTO_MIME[fmt], name))
        else:
            saved.append(await media.save_async(uid, files[0][1], "application/pdf", files[0][0]))
    except media.StorageError as e:
        await _drop(uid, saved)
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    if not all(media.alive(uid, m) for m in saved):          # история стёрта, пока шла запись
        await _drop(uid, saved)
        raise HTTPException(409, {"code": "gone", "message": "файл удалён во время загрузки"})
    if formats:
        pages = [{"n": n, "state": "waiting", "media_id": m, "text": None} for n, m in enumerate(saved, 1)]
    else:
        pages = [{"n": n, "state": "waiting", "media_id": None, "text": None} for n in range(1, count + 1)]
    try:
        sid = create(uid, conn, model, files[0][0], origin="section",
                     source_media_id=None if formats else saved[0], pages=pages)
    except HTTPException:                                    # очередь полна
        await _drop(uid, saved)
        raise
    kick(uid)
    log.info("ocr %s: принят, %d стр.", sid[:8], len(pages))
    return _out(_row(uid, sid))


@router.get("")
async def list_scans(request: Request, before: "float | None" = None,
                     before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    """Новые сверху; следующая страница — before=<created>&before_id=<id> последней."""
    rows = db.page(state.DB, "scans", request.state.user.id, before, before_id, limit, COLUMNS)
    return {"items": [_out(r) for r in rows]}


@router.get("/{sid}")
async def get_scan(sid: str, request: Request):
    """Документ и его страницы: `pages` — список `{n, state, text, detail, media_id}`."""
    row = _row(request.state.user.id, sid)
    return _out(row) | {"pages": _page_list(sid)}


@router.post("/{sid}/cancel")
async def cancel(sid: str, request: Request):
    return {"state": await request_cancel(request.state.user.id, sid)}


@router.delete("/{sid}", status_code=204)
async def delete(sid: str, request: Request):
    """Строка (страницы — каскадом), затем файлы, на которые больше ничто не ссылается."""
    uid = request.state.user.id
    row = _row(uid, sid)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    files = {row["source_media_id"], *(p["media_id"] for p in _page_list(sid))} - {None}
    state.DB.execute("DELETE FROM scans WHERE id = ?", (sid,))
    for media_id in files:
        if not media.referenced(uid, media_id):
            await media.delete_async(uid, media_id)
    log.info("ocr %s: удалён", sid[:8])
    return Response(status_code=204)


def _done(user_id: int, sid: str):
    row = _row(user_id, sid)
    if row["state"] != "done":
        raise HTTPException(409, {"code": "running", "message": "документ ещё не распознан"})
    return row


@router.get("/{sid}/export")
async def export(sid: str, request: Request, format: str = Query(..., pattern="^(txt|md|docx)$")):  # noqa: A002
    """Готовый документ файлом: имя — по документу («Договор.pdf» → «Договор.docx»)."""
    row = _done(request.state.user.id, sid)
    pages, stem = _page_list(sid), _stem(row["name"])
    if format == "docx":
        body = exports.pages_docx(pages, stem)
    else:
        body = (exports.pages_txt(pages) if format == "txt" else exports.pages_md(pages)).encode("utf-8")
    return Response(body, media_type=exports.TYPES[format],
                    headers={"Content-Disposition": media.disposition(f"{stem}.{format}"), "Cache-Control": "no-store"})


@router.post("/{sid}/attachments", status_code=201)
async def to_chat(sid: str, request: Request):
    """Распознанный текст — неотправленным документом чата «<имя>.md» (свой файл): дальше это обычное
    вложение. Подтверждения для внешней модели нет (спека 6.4): документ — свой."""
    uid = request.state.user.id
    row = _done(uid, sid)
    text = exports.pages_md(_page_list(sid))
    if len(text) > documents.TEXT_MAX_CHARS:
        raise HTTPException(413, {"code": "too_big", "message": (
            f"текст длиннее {documents.TEXT_MAX_CHARS:,} знаков — в чат его не передать".replace(",", " "))})
    name = f"{_stem(row['name'])}.md"
    try:
        media_id = await media.save_async(uid, text.encode("utf-8"), "text/markdown", name)
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    att_id = uuid.uuid4().hex
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
                     " VALUES (?, ?, NULL, 'document', ?, ?, ?, ?, ?)",
                     (att_id, uid, name, media_id, text, len(text), clock.NOW()))
    log.info("вложение %s: из распознавания %s, %d знаков", att_id[:8], sid[:8], len(text))
    return {"id": att_id, "kind": "document", "name": name, "chars": len(text), "media_id": media_id, "text": text,
            "tokens": chat.estimate_tokens([{"role": "user", "content": text}])}
