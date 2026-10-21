"""Картинки (спека 2026-10-19-studio-pwa): генерация, правка кистью по маске, описание gemma-abl и
освобождение карты — через фото-подключение srvllm пользователя; итог — в истории (таблица
`images`, файлы media). Перенос заданий прежнего server.py; поведение отмены и release то же:

1. Одно задание за раз у пользователя: его второе — 409 busy (задания других не мешают).
   Отмена запрос к шлюзу НЕ обрывает: LiteLLM обрыв клиента дальше не передаёт, генерация всё
   равно доходит до конца, а release на идущий через slot-gate запрос отвечает 409 slot_busy
   (ревью 2026-10-19). Поэтому задание «отменяется», ждёт конца своего запроса, выбрасывает
   картинку и сразу зовёт release модели задания — на 409 повторяет с паузой до
   CANCEL_RELEASE_SEC; новое задание до тех пор получает busy.
2. Ответ шлюза или сервиса до сохранения декодируется целиком Pillow (в потоке): только PNG, JPEG
   или WebP не больше RESULT_PIXELS и RESULT_MAX — иначе задание «ошибка» словами, а не «готово» с
   чужими байтами. Готовая картинка — в media со своим типом и расширением (итог кисти — PNG) и с
   превью, строка `images` — запись ленты. Описания и переводы промпта в историю не пишутся: текст
   живёт в памяти, GET отдаёт его и повторно (ответ мог потеряться в пути), пока страница не
   подтвердит получение (DELETE …/<id>) или не пройдёт TEXT_KEEP_SEC от первой выдачи; невыданный —
   до KEEP_DONE_SEC. Перевод — тот же путь, что описание (маршрут шлюза
   `/slot/images/descriptions`, gemma-abl), без картинки.
3. Фото — файл с устройства (сохраняется в media как исходник) или фото истории
   (`source_media_id`); оба нормализуются normalize_image. Маска (PNG) остаётся в студии: снаружи
   выделения результат берётся из исходника, модель получает только фото и инструкцию. Маска с
   устройства ложится в media (`mask_media_id`) — повтор правки идёт с ней же. Фото и маска
   задания в памяти очищаются в finally.
4. Аренда модели картинок держит карту 600 с простоя, и другая модель её не вытесняет: шлюз ждал
   бы карту дольше своего бюджета и отвечал 503. Поэтому перед запросом — release другой модели
   (как перед описанием); занята она (409) — запрос ждёт карту у шлюза, как и без release.
5. База — только с цикла событий: в asyncio.to_thread уходят файлы и Pillow, не SQLite.
6. Журнал — только id задания и модель: промпт, токен, картинки и имена файлов туда не пишутся.
7. Подключение и модель — из раздела «Фото» (sections.resolve). Внешний сервис (`openai`):
   `/images/generations` и `/images/edits` без seed и силы, размеры — EXTERNAL_SIZES, ответ — только
   `b64_json` (по ссылке из ответа сервер не ходит). Кисть, «Описать», «Освободить карту» и release —
   только у шлюза srvllm. Сила пишется в строку только у Lustify (FLUX её не получает).
8. Серия (поле `series`, спека 2026-10-21-studio-photo-presets-compare-series, раздел 7): `count`
   строк сразу — первая `working`, остальные `queued` со своими seed и силой; одна задача
   (run_series) ведёт их по одной тем же run_job, так что серия держит слот пользователя до
   последней (`queued` — не TERMINAL). Ошибка одной не останавливает следующую. Отмена любой строки
   снимает весь остаток: идущую — как одиночную (release_after_cancel), строки в очереди — сразу
   `cancelled`. Строки в очереди при остановке службы — `error` самой остановкой (stop), при старте —
   recover. Связь строк — только в памяти (Job.series).
9. Варианты (`GET /<id>/variants`): свои готовые картинки с тем же исходником и сделанные из этой.
"""
import asyncio
import base64
import binascii
import io
import json
import logging
import math
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

import auth
import chains
import clock
import connections
import db
import forms
import media
import sections
import state
import upstream
from connections import Connection
from image_upload import MAX_BODY, MAX_UPLOAD, UploadError, image_size, prepare_mask, restrict_edit

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/images")

KEEP_DONE_SEC = 600
# выданный, но не подтверждённый текст описания или перевода: страница опрашивает раз в 1,5 с и
# подтверждает сразу, как вставит текст, — две минуты покрывают обрыв мобильной связи на десятки
# повторов, а текст без хозяина не лежит в памяти все KEEP_DONE_SEC
TEXT_KEEP_SEC = 120
# шлюз держит запрос до конца генерации: ожидание карты (budget_s маршрута) плюс n картинок;
# таймаут маршрута LiteLLM — не больше 1260 с (контракт API картинок)
GENERATE_TIMEOUT_SEC = 1300
SHORT_TIMEOUT_SEC = 30
# release после отменённого задания: 409 — slot-gate ещё считает запрос (свой — доли секунды,
# чужой — до конца его картинки); дольше минуты не ждём — аренду снимет её срок
CANCEL_RELEASE_SEC = 60
RELEASE_PAUSE_SEC = 2
TERMINAL = {"done", "error", "cancelled"}
WORKING = "жду карту или генерирую"
#: серия (п. 8 шапки): строк в одном запросе — не больше
SERIES_MAX = 4
#: «Варианты» просмотра: не больше в каждом ряду
VARIANTS_MAX = 24
CANCELLING = "отменяю — карта освободится сразу после текущей картинки (если шлюз ещё ждёт карту — после её получения и картинки)"
INTERRUPTED = "прервано перезапуском студии"
JSON_MAX = 512 * 1024
#: удаление учётки ждёт конца её отменённых заданий: они освобождают карту её подключением
SETTLE_SEC = 90
#: картинка ответа (п. 2 шапки): форматы и пределы — с запасом над размерами моделей и gpt-image-1
RESULT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
RESULT_PIXELS = 4096 * 4096
RESULT_MAX = 50 * 1024 * 1024
NOT_PICTURE = "сервис вернул не картинку (нужен PNG, JPEG или WebP)"

#: Модели страницы: короткое имя → id у шлюза (контракт API картинок).
MODELS = {"lustify": "lustify-v10-krea2-turbo", "flux": "flux.2-klein-4b"}
#: Размеры: портрет, квадрат, альбом — около мегапикселя, кратны 16 (обе модели их держат).
SIZES = {"portrait": "832x1216", "square": "1024x1024", "landscape": "1216x832"}
#: те же форматы у внешнего сервиса (размеры gpt-image-1); «original» — без size
EXTERNAL_SIZES = {"832x1216": "1024x1536", "1024x1024": "1024x1024", "1216x832": "1536x1024"}
DESCRIPTION_MODEL = "gemma4-abl"
DESCRIPTION_PROMPT = """Describe this image as an exceptionally detailed, generation-ready English
prompt for recreating a visually similar image. Return only the description, without an
introduction, analysis, or recommendations. Use organized descriptive paragraphs covering:
the image type and artistic/photographic style; every main subject and their count, visible
appearance, facial expression, hair, clothing, accessories, pose, hands and gaze; exact
relative positions, framing, crop, camera angle, perspective, depth and scale; foreground,
background, setting, architecture, objects and their spatial relationships; colors and
palette, materials, textures, patterns and small visible details; light sources, direction,
softness, shadows, highlights, contrast, atmosphere, focus and depth of field. Include any
clearly legible text verbatim. Prioritize the details that make this particular image
recognizable. Be as comprehensive as visible evidence allows; do not invent hidden objects,
identities, exact camera settings, or details you cannot see. Aim for a rich, precise prompt,
not a short caption. Stay within 3000 words."""
TRANSLATION_PROMPT = """Translate the image-generation prompt below into English. Keep every detail,
name, number and the punctuation that carries meaning (commas, parentheses, weights). If a part is
already in English, keep it as is. Return only the translated prompt, without quotes, notes or an
introduction.

Prompt:
"""
TRANSLATION_MAX_TOKENS = 16384
#: задания с текстом вместо картинки: в истории их нет, текст живёт до подтверждения получения (п. 2 шапки)
TEXT_KINDS = {"description", "translation"}
#: слова заданий с текстом: готово, уже выдано (подтверждено или срок вышел), модель ответила пустым
TEXT_WORDS = {"description": ("описание готово", "описание уже выдано", "gemma-abl не вернула описание"),
              "translation": ("перевод готов", "перевод уже выдан", "gemma-abl не вернула перевод")}


@dataclass
class Job:
    id: str
    user_id: int
    conn: Connection
    model: str
    prompt: str
    size: str
    seed: int
    started: float
    state: str = "working"
    detail: str = WORKING
    finished_at: "float | None" = None
    task: "asyncio.Task | None" = None
    image: bytes | None = None
    strength: float = 0.65
    mask: bytes | None = None
    kind: str = "generation"                 # generation | edit | description | translation
    description: str | None = None
    delivered_at: float | None = None        # первая выдача текста: от неё — TEXT_KEEP_SEC
    truncated: bool = False
    description_started: bool = False
    source_media_id: str | None = None
    media_id: str | None = None              # готовая картинка в media (вместо прежнего png в памяти)
    mask_media_id: str | None = None
    series: "list[Job] | None" = None        # все строки серии по порядку (п. 8 шапки)

    @property
    def gateway_model(self) -> str:
        """Id модели в запросе: у шлюза — по короткому имени студии, у внешнего сервиса — как есть."""
        return MODELS.get(self.model, self.model) if self.conn.kind == "srvllm" else self.model


JOBS: dict[str, Job] = {}


class JobError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SeriesIn(BaseModel):
    count: int = Field(ge=2, le=SERIES_MAX)
    vary: Literal["seed", "strength"]
    strength_to: "float | None" = Field(default=None, ge=0.05, le=1)


class JobIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=24000)
    size: Literal["portrait", "square", "landscape", "original"]
    model: "str | None" = Field(default=None, max_length=200)      # короткое имя srvllm или id сервиса
    connection_id: "int | None" = None
    seed: "int | None" = Field(default=None, ge=0, le=2**32 - 1)
    strength: float = Field(default=0.65, ge=0.05, le=1)
    source_media_id: "str | None" = Field(default=None, max_length=64)
    mask_media_id: "str | None" = Field(default=None, max_length=64)    # маска прежней правки (повтор)
    series: "SeriesIn | None" = None


def series_strengths(start: float, end: float, count: int) -> list[float]:
    """`count` сил поровну от start до end включительно, округлённые до 0.05 (целыми двадцатыми — как
    series.js на странице); повтор после округления — ValueError."""
    steps = [math.floor((start + (end - start) * i / (count - 1)) * 20 + 0.5) for i in range(count)]
    if len(set(steps)) != count:
        raise ValueError(count)
    return [n / 20 for n in steps]


def _seeds(seed: "int | None", count: int, vary: str) -> list[int]:
    """seed, seed+1, … (по модулю 2³²) серии по seed; пусто — случайные разные. Серия по силе — один seed."""
    if vary != "seed":
        one = seed if seed is not None else secrets.randbelow(2**32)
        return [one] * count
    if seed is not None:
        return [(seed + i) % 2**32 for i in range(count)]
    out: list[int] = []
    while len(out) < count:
        if (s := secrets.randbelow(2**32)) not in out:
            out.append(s)
    return out


def _series_refused(message: str) -> HTTPException:
    return HTTPException(422, {"code": "series", "message": message})


class TranslationIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=24000)


# ---- задания в памяти и строки истории -----------------------------------------

def _prune() -> None:
    now = clock.NOW()
    for job_id in [j.id for j in JOBS.values() if j.finished_at is not None and now - j.finished_at > KEEP_DONE_SEC]:
        JOBS.pop(job_id, None)
    for job in JOBS.values():
        if job.delivered_at is not None and now - job.delivered_at > TEXT_KEEP_SEC:
            job.description = None


def _active(user_id: int) -> "Job | None":
    return next((j for j in JOBS.values() if j.user_id == user_id and j.state not in TERMINAL), None)


def ensure_idle(user_id: int) -> None:
    _prune()
    active = _active(user_id)
    if active is not None:
        message = ("предыдущее задание отменяется — дождитесь освобождения карты"
                   if active.state == "cancelling" else "предыдущее задание ещё выполняется")
        raise HTTPException(409, {"code": "busy", "message": message,
                                  "id": active.id})


def _srvllm_photo(user_id: int) -> Connection:
    """Шлюз srvllm раздела «Фото» — для «Описать» и «Освободить карту» (их нет у внешних сервисов)."""
    conn = next((l.connection for l in sections.links(user_id, "photo")
                 if l.connection.kind == "srvllm" and l.connection.proxied), None)
    if conn is None:
        raise HTTPException(409, {"code": "no_gateway", "message": "добавьте шлюз srvllm в настройках раздела «Фото»"})
    return conn


def _store(job: Job) -> None:
    """Состояние задания — в его строку истории (описаний и переводов в истории нет)."""
    if job.kind in TEXT_KINDS:
        return
    state.DB.execute("UPDATE images SET state = ?, detail = ?, seed = ?, media_id = ?, finished = ? WHERE id = ?",
                     (job.state, job.detail, job.seed, job.media_id, job.finished_at, job.id))


def _row(user_id: int, image_id: str):
    row = state.DB.execute("SELECT * FROM images WHERE id = ? AND user_id = ?", (image_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такой картинки нет"})
    return row


def _out(row, with_chain=True) -> dict:
    """Запись ленты; цепочка промптов — только у одной записи (просмотр): в ленте — число шагов,
    иначе длинные цепочки раздули бы страницу и добавили запросов к базе на каждый шаг."""
    end = row["finished"] if row["finished"] is not None else clock.NOW()
    chain = chains.of_image(row["user_id"], row)
    out = {"id": row["id"], "kind": row["kind"], "state": row["state"], "detail": row["detail"],
           "elapsed": int(max(0, end - row["created"])), "model": row["model"], "prompt": row["prompt"],
           "size": row["size"], "seed": row["seed"], "strength": row["strength"],
           "source_media_id": row["source_media_id"], "mask_media_id": row["mask_media_id"],
           "media_id": row["media_id"], "created": row["created"], "steps": len(chain)}
    if with_chain:
        out["chain"] = chains.annotate(row["user_id"], chain)
    return out


def _status(job: Job) -> dict:
    end = job.finished_at if job.finished_at is not None else clock.NOW()
    return {"id": job.id, "state": job.state, "detail": job.detail, "model": job.model, "kind": job.kind,
            "elapsed": int(max(0, end - job.started))}


def recover() -> None:
    """После перезапуска идущих заданий нет: их строки — ошибка «прервано перезапуском»."""
    count = state.DB.execute("UPDATE images SET state = 'error', detail = ?, finished = ?"
                             " WHERE state IN ('working', 'cancelling', 'queued')", (INTERRUPTED, clock.NOW())).rowcount
    if count:
        log.warning("картинки: %d заданий прервано перезапуском", count)


def _end_queued(job: Job, state_: str, detail: str) -> None:
    """Строка серии в очереди кончается, не начавшись (отмена, остановка службы)."""
    job.state, job.detail, job.finished_at = state_, detail, clock.NOW()
    job.image = job.mask = None
    job.prompt = ""
    _store(job)


def _cancel(job: Job) -> None:
    """Идущее — «отменяется» (run_job дождётся запроса и освободит карту), в очереди — сразу «отменено»."""
    if job.state == "working":
        job.state, job.detail = "cancelling", CANCELLING
        _store(job)
        log.info("задание %s: отменяется", job.id[:8])
    elif job.state == "queued":
        _end_queued(job, "cancelled", "отменено")


def forget_user(user_id: int) -> None:
    """Стирание истории, удаление или отключение учётки: идущие задания пользователя отменяются —
    картинку, пришедшую после, путь отмены run_job выбросит и освободит карту; строки серии в
    очереди не начнутся; невыданные описания забываются."""
    for job in JOBS.values():
        if job.user_id != user_id:
            continue
        job.description = None
        _cancel(job)


async def settle_user(user_id: int) -> bool:
    """Ждать конца заданий пользователя (после forget_user они сами освобождают карту) не дольше
    SETTLE_SEC; True — все кончились."""
    deadline = clock.NOW() + SETTLE_SEC
    while any(j.user_id == user_id and j.task is not None and not j.task.done() for j in JOBS.values()):
        if clock.NOW() >= deadline:
            return False
        await clock.SLEEP(1)
    return True


async def stop() -> None:
    """Остановка службы: идущие задания отменяются и успевают записать итог до закрытия базы; строки
    серии в очереди — «прервано перезапуском» здесь же: откат install.sh вернул бы код, чей recover()
    о `queued` не знает, и строки остались бы неудаляемыми."""
    tasks = {j.task for j in JOBS.values() if j.task and not j.task.done()}   # у серии задача одна на все строки
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for job in JOBS.values():
        if job.state == "queued":
            _end_queued(job, "error", INTERRUPTED)


# ---- ввод: форма, фото, маска --------------------------------------------------

def _multipart(request: Request) -> bool:
    return request.headers.get("content-type", "").lower().startswith("multipart/form-data")


async def _discard(user_id: int, media_ids: list[str]) -> None:
    """Файлы, записанные запросом, который потом отказал."""
    for media_id in media_ids:
        await media.delete_async(user_id, media_id)


# ---- API -------------------------------------------------------------------

@router.post("", status_code=202)
async def create_job(request: Request):
    uid = request.state.user.id
    sections.resolve(uid, "photo", None, None)  # раздел без модели — отказ до чтения тела
    ensure_idle(uid)
    upload, mask = None, None
    try:
        if _multipart(request):
            # разбор формы до 20 МиБ — доли секунды: в потоке (базы он не касается)
            fields, upload, mask = await asyncio.to_thread(
                forms.decode, await forms.read_body(request, MAX_BODY + MAX_UPLOAD), request.headers["content-type"],
                with_mask=True)
            if fields.get("seed") == "":
                fields.pop("seed")
            if "series" in fields:                   # серия в форме — строкой JSON
                fields["series"] = json.loads(fields["series"])
            body = JobIn.model_validate(fields)
        else:
            body = JobIn.model_validate_json(await forms.read_body(request, JSON_MAX))
        if body.series is not None and (mask is not None or body.mask_media_id is not None):
            raise _series_refused("серия — без кисти: правка по маске — только одиночная")
        image = await forms.photo(uid, upload, body.source_media_id)
        if body.mask_media_id is not None:
            if mask is not None:
                raise UploadError("нужна одна маска: файл или маска прежней правки")
            mask = await forms.history_file(uid, body.mask_media_id)
        if mask is not None:
            mask = await asyncio.to_thread(prepare_mask, mask, image)
        if body.size == "original" and image is None:
            raise HTTPException(422, {"code": "image", "message": "для исходного размера нужно фото"})
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    except (ValidationError, json.JSONDecodeError):
        raise HTTPException(422, {"code": "input", "message": "проверьте промпт, модель, размер, seed и силу изменения"}) from None
    conn, model = sections.resolve(uid, "photo", body.connection_id, body.model)
    if mask is not None and conn.kind != "srvllm":
        raise HTTPException(422, {"code": "brush", "message": "правка кистью — только через шлюз srvllm"})
    lustify_edit = image is not None and conn.kind == "srvllm" and model == "lustify"
    count, vary = (body.series.count, body.series.vary) if body.series else (1, "seed")
    strengths = [body.strength if lustify_edit else None] * count
    if vary == "strength":
        if not lustify_edit:
            raise _series_refused("серия по силе — только Lustify с исходным фото")
        if body.series.strength_to is None:
            raise _series_refused("для серии по силе нужна сила «до»")
        try:
            strengths = series_strengths(body.strength, body.series.strength_to, count)
        except ValueError:
            raise _series_refused(f"слишком узкий диапазон для {count} вариантов") from None
    # Parsing uploads yields to other requests: recheck before inserting the job.
    ensure_idle(uid)
    seeds = _seeds(body.seed, count, vary)
    seed = seeds[0]
    size = "{}x{}".format(*image_size(image)) if body.size == "original" else SIZES[body.size]
    source_id, mask_id = body.source_media_id, body.mask_media_id
    saved = []                                   # файлы этого запроса: отказ ниже их удаляет
    try:
        if upload is not None:
            source_id = await media.save_async(uid, image, "image/png", f"studio-source-{seed}.png", thumb=True)
            saved.append(source_id)
        if mask is not None and mask_id is None:
            mask_id = await media.save_async(uid, mask, "image/png", f"studio-mask-{seed}.png")
            saved.append(mask_id)
        if saved:
            ensure_idle(uid)                     # запись на шару тоже уступала другим запросам
        if any(m is not None and not media.alive(uid, m) for m in (source_id, mask_id)):
            raise HTTPException(404, media.GONE)  # исходник или маску удалили, пока задание их готовило
    except media.StorageError as e:
        await _discard(uid, saved)
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    except HTTPException:
        await _discard(uid, saved)
        raise
    now = clock.NOW()
    before = chains.of_source(uid, source_id)
    jobs = []
    for i in range(count):
        # строки серии — по порядку ленты: первая сверху (created на миллисекунду новее следующей)
        created = now - i * 0.001
        job = Job(id=uuid.uuid4().hex, user_id=uid, conn=conn, model=model, prompt=body.prompt, size=size,
                  seed=seeds[i], started=created, kind="generation" if image is None else "edit", image=image,
                  strength=strengths[i] if strengths[i] is not None else body.strength, mask=mask,
                  source_media_id=source_id, mask_media_id=mask_id)
        if i:
            job.state, job.detail = "queued", f"в очереди серии · {i + 1} из {count}"
        chain = before + [chains.image_step(
            image_id=job.id, kind="brush" if mask_id else job.kind, model=job.model, prompt=job.prompt, size=size,
            seed=job.seed, strength=strengths[i], created=created)]
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, strength, source_media_id,"
                         " mask_media_id, state, detail, created, chain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (job.id, uid, job.kind, job.model, job.prompt, size, job.seed, strengths[i], source_id, mask_id,
                          job.state, job.detail, created, json.dumps(chain, ensure_ascii=False)))
        JOBS[job.id] = job
        jobs.append(job)
    task = asyncio.create_task(run_series(jobs) if count > 1 else run_job(jobs[0]))
    for job in jobs:
        job.series = jobs if count > 1 else None
        job.task = task
    log.info("задание %s: %s, строк — %d", jobs[0].id[:8], size, count)
    return {"id": jobs[0].id, "seed": seed, "ids": [j.id for j in jobs]}


@router.get("")
async def list_images(request: Request, before: "float | None" = None,
                      before_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100)):
    """Лента: новые сверху, вместе с идущими; следующая страница — before=<created>&before_id=<id> последней."""
    rows = db.page(state.DB, "images", request.state.user.id, before, before_id, limit)
    return {"items": [_out(r, with_chain=False) for r in rows]}


@router.post("/descriptions", status_code=202)
async def create_description(request: Request):
    uid = request.state.user.id
    conn = _srvllm_photo(uid)
    ensure_idle(uid)
    try:
        fields, upload, _ = await asyncio.to_thread(forms.decode, await forms.read_body(request, MAX_BODY),
                                                    request.headers.get("content-type", ""))
        image = await forms.photo(uid, upload, fields.get("source_media_id"))
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    ensure_idle(uid)
    job = Job(id=uuid.uuid4().hex, user_id=uid, conn=conn, model="gemma-abl", prompt="", size="", seed=0,
              started=clock.NOW(), kind="description", image=image, detail="жду карту или описываю фото")
    JOBS[job.id] = job
    job.task = asyncio.create_task(run_job(job))
    log.info("задание %s: описание gemma-abl", job.id[:8])
    return {"id": job.id}


def _text_status(job_id: str, request: Request, kind: str):
    """Статус описания или перевода; готовый текст — в этом же ответе и в повторах, пока страница не
    подтвердит получение (_text_received) или не пройдёт TEXT_KEEP_SEC от первой выдачи."""
    _prune()
    job = JOBS.get(job_id)
    if job is None or job.user_id != request.state.user.id or job.kind != kind:
        raise HTTPException(404, {"code": "gone", "message": "задание забыто или не существовало"})
    if job.state != "done":
        return _status(job)
    if job.description is None:
        raise HTTPException(404, {"code": "gone", "message": TEXT_WORDS[kind][1]})
    if job.delivered_at is None:
        job.delivered_at = clock.NOW()
    return JSONResponse({"state": "done", "text": job.description, "truncated": job.truncated},
                        headers={"Cache-Control": "no-store"})


def _text_received(job_id: str, request: Request, kind: str) -> Response:
    """Страница вставила текст в поле — он стирается. Повтор и чужое или забытое задание — тоже 204."""
    job = JOBS.get(job_id)
    if job is not None and job.user_id == request.state.user.id and job.kind == kind and job.state == "done":
        job.description = None
    return Response(status_code=204)


@router.get("/descriptions/{job_id}")
async def description_status(job_id: str, request: Request):
    return _text_status(job_id, request, "description")


@router.delete("/descriptions/{job_id}", status_code=204)
async def description_received(job_id: str, request: Request):
    return _text_received(job_id, request, "description")


@router.post("/translations", status_code=202)
async def create_translation(request: Request):
    """Промпт на английский (gemma-abl, как «Описать»): поле фото заменит ответ, без генерации."""
    uid = request.state.user.id
    conn = _srvllm_photo(uid)
    ensure_idle(uid)
    try:
        body = TranslationIn.model_validate_json(await forms.read_body(request, JSON_MAX))
    except ValidationError:
        raise HTTPException(422, {"code": "input", "message": "нужен промпт до 24000 знаков"}) from None
    ensure_idle(uid)
    job = Job(id=uuid.uuid4().hex, user_id=uid, conn=conn, model="gemma-abl", prompt=body.prompt, size="", seed=0,
              started=clock.NOW(), kind="translation", detail="жду карту или перевожу")
    JOBS[job.id] = job
    job.task = asyncio.create_task(run_job(job))
    log.info("задание %s: перевод gemma-abl", job.id[:8])
    return {"id": job.id}


@router.get("/translations/{job_id}")
async def translation_status(job_id: str, request: Request):
    return _text_status(job_id, request, "translation")


@router.delete("/translations/{job_id}", status_code=204)
async def translation_received(job_id: str, request: Request):
    return _text_received(job_id, request, "translation")


@router.post("/release")
async def release_card(request: Request):
    """Обе модели и gemma-abl — тремя release фото-подключения администратора: итог по каждой."""
    conn = _srvllm_photo(auth.require_admin(request).id)
    results = {}
    for name, model in (MODELS | {"gemma-abl": DESCRIPTION_MODEL}).items():
        try:
            r = await release_description(conn) if name == "gemma-abl" else await release(conn, model)
        except (httpx.HTTPError, upstream.UpstreamRefused) as e:
            results[name] = {"state": "error", "message": f"шлюз недоступен: {type(e).__name__}"}
            continue
        if r.status_code == 200:
            results[name] = {"state": "released", "message": "освобождена"}
        elif r.status_code == 409:
            # Lustify и видео — слот comfy: его освобождение решает переводчик («идут ролики: N»)
            results[name] = {"state": "busy", "message": upstream.message(r) or "занята генерацией"}
        else:
            # и 401: ключ подключения не принят — слова о профиле, сессия студии жива
            results[name] = {"state": "error", "message": upstream.gateway_error(r, conn.name)}
    log.info("освобождение карты кнопкой: %s", ", ".join(f"{k} {v['state']}" for k, v in results.items()))
    return {"results": results}


@router.get("/{image_id}")
async def image_status(image_id: str, request: Request):
    return _out(_row(request.state.user.id, image_id))


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    """Запрос к шлюзу не обрывается (обрыв до slot-gate не доходит): задание ждёт его конца,
    выбрасывает картинку и освобождает карту само — см. run_job. Описание отменяется так же."""
    uid = request.state.user.id
    _prune()
    job = JOBS.get(job_id)
    if job is None or job.user_id != uid:
        return {"state": _row(uid, job_id)["state"]}          # закончилось до перезапуска или забыто
    if job.state in ("working", "queued"):
        for j in job.series or [job]:                         # серия — весь её остаток (п. 8 шапки)
            _cancel(j)
    return {"state": job.state}


@router.delete("/{image_id}", status_code=204)
async def delete_image(image_id: str, request: Request):
    """Строка и её файлы; файл, на который ссылается другая запись истории, остаётся."""
    uid = request.state.user.id
    row = _row(uid, image_id)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    state.DB.execute("DELETE FROM images WHERE id = ?", (image_id,))
    JOBS.pop(image_id, None)
    for media_id in {row["media_id"], row["source_media_id"], row["mask_media_id"]} - {None}:
        if not media.referenced(uid, media_id):
            await media.delete_async(uid, media_id)
    return Response(status_code=204)


# ---- шлюз ------------------------------------------------------------------

async def _slot(conn: Connection, path: str, timeout: float, **kw) -> httpx.Response:
    return await upstream.request(conn, "POST", connections.slot_url(conn, path), timeout=timeout, **kw)


async def release(conn: Connection, model: str) -> httpx.Response:
    return await _slot(conn, "/images/release", SHORT_TIMEOUT_SEC, json={"model": model})


async def release_description(conn: Connection) -> httpx.Response:
    return await _slot(conn, "/images/descriptions/release", SHORT_TIMEOUT_SEC, json={"model": DESCRIPTION_MODEL})


def _refused(conn: Connection, e: upstream.UpstreamRefused) -> JobError:
    return JobError("connection" if isinstance(e, upstream.ConnectionChanged) else "refused",
                    upstream.refused_words(e, conn))


def _png(body: dict) -> "tuple[bytes, int | None]":
    data = body.get("data")
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    try:
        png = base64.b64decode(item["b64_json"], validate=True)
    except (KeyError, TypeError, binascii.Error, ValueError):
        raise JobError("no_image", "шлюз не вернул картинку")
    seed = item.get("seed")
    return png, seed if isinstance(seed, int) and not isinstance(seed, bool) else None


def _picture(data: bytes) -> str:
    """MIME картинки ответа после полного декодирования (п. 2 шапки) — для asyncio.to_thread.
    Не картинка, чужой формат, обрезанный файл или больше пределов — JobError."""
    too_big = JobError("too_big", f"сервис вернул картинку больше предела ({RESULT_PIXELS // 1_000_000} Мп,"
                                  f" {RESULT_MAX // (1024 * 1024)} МиБ)")
    if len(data) > RESULT_MAX:
        raise too_big
    # без warnings.catch_warnings: фильтр предупреждений общий на процесс, а здесь поток — параллельные
    # задания перепутали бы его. Бомбу отсекает предел пикселей по заголовку; больше двух
    # MAX_IMAGE_PIXELS Pillow отказывает сам ещё в Image.open
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in RESULT_TYPES:
                raise JobError("not_image", NOT_PICTURE)
            if image.width * image.height > RESULT_PIXELS:   # до декодирования: размер — из заголовка
                raise too_big
            image.load()
            return RESULT_TYPES[image.format]
    except Image.DecompressionBombError:
        raise too_big from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise JobError("not_image", NOT_PICTURE) from None


async def _release_others(job: Job) -> None:
    """Release другой модели картинок перед запросом (п. 4 шапки): итог не важен — занятую (409)
    запрос дождётся у шлюза, а недоступный шлюз и убранный адрес назовёт сам запрос."""
    for model in MODELS.values():
        if model == job.gateway_model:
            continue
        try:
            r = await release(job.conn, model)
        except (httpx.HTTPError, upstream.UpstreamRefused):
            return
        if r.status_code not in (200, 409):
            log.info("задание %s: release другой модели — код %s", job.id[:8], r.status_code)


async def _generate_external(job: Job) -> "tuple[bytes, int | None, str]":
    """Внешний сервис OpenAI-стиля (п. 7 шапки)."""
    payload = {"model": job.gateway_model, "prompt": job.prompt, "n": 1, "response_format": "b64_json"}
    if job.size in EXTERNAL_SIZES:               # «original» — размер фото, у сервиса его нет
        payload["size"] = EXTERNAL_SIZES[job.size]
    url = f"{job.conn.base_url}/images/{'edits' if job.image is not None else 'generations'}"
    try:
        if job.image is not None:
            r = await upstream.request(job.conn, "POST", url, timeout=GENERATE_TIMEOUT_SEC,
                                       data={k: str(v) for k, v in payload.items()},
                                       files={"image": ("input.png", job.image, "image/png")})
        else:
            r = await upstream.request(job.conn, "POST", url, timeout=GENERATE_TIMEOUT_SEC, json=payload)
    except httpx.TimeoutException:
        raise JobError("timeout", f"сервис не ответил за {GENERATE_TIMEOUT_SEC} с")
    except httpx.HTTPError as e:
        raise JobError("gateway", f"сервис недоступен: {type(e).__name__}")
    except upstream.UpstreamRefused as e:
        raise _refused(job.conn, e) from None
    if r.status_code != 200:
        raise JobError(f"http_{r.status_code}", upstream.gateway_error(r, job.conn.name))
    body = upstream.json_body(r)
    data = body.get("data")
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if "b64_json" not in first and "url" in first:
        raise JobError("url", "сервис вернул ссылку, а не картинку")
    png, _seed = _png(body)
    return png, None, await asyncio.to_thread(_picture, png)


async def generate(job: Job) -> "tuple[bytes, int | None, str]":
    """Картинка ответа, seed шлюза (или None) и её MIME."""
    if job.conn.kind != "srvllm":
        return await _generate_external(job)
    payload = {"model": job.gateway_model, "prompt": job.prompt, "n": 1, "size": job.size,
               "seed": job.seed, "response_format": "b64_json"}
    await _release_others(job)
    try:
        if job.image is not None:
            if job.model == "lustify":
                payload["strength"] = job.strength
            r = await _slot(job.conn, "/images/edits", GENERATE_TIMEOUT_SEC,
                            data={k: str(v) for k, v in payload.items()},
                            files={"image": ("input.png", job.image, "image/png")})
        else:
            r = await _slot(job.conn, "/images/generations", GENERATE_TIMEOUT_SEC, json=payload)
    except httpx.TimeoutException:
        raise JobError("timeout", f"шлюз не ответил за {GENERATE_TIMEOUT_SEC} с")
    except httpx.HTTPError as e:
        raise JobError("gateway", f"шлюз недоступен: {type(e).__name__}")
    except upstream.UpstreamRefused as e:
        raise _refused(job.conn, e) from None
    if r.status_code != 200:
        raise JobError(f"http_{r.status_code}", upstream.gateway_error(r, job.conn.name))
    png, seed = _png(upstream.json_body(r))
    mime = await asyncio.to_thread(_picture, png)       # до слияния: битое там было бы «сбоем службы»
    if job.mask is not None:
        png, mime = await asyncio.to_thread(restrict_edit, job.image, png, job.mask), "image/png"
    return png, seed, mime


async def describe(job: Job):
    # Image slots hold leases for 600 s; release only idle image requests before switching.
    for model in MODELS.values():
        try:
            response = await release(job.conn, model)
        except httpx.HTTPError as e:
            raise JobError("gateway", f"шлюз недоступен: {type(e).__name__}") from None
        except upstream.UpstreamRefused as e:     # и подключение удалили или сменили (ConnectionChanged)
            raise _refused(job.conn, e) from None
        if response.status_code != 200:
            raise JobError("release", "не удалось подготовить карту: " + upstream.gateway_error(response, job.conn.name))
    if job.kind == "translation":
        content, max_tokens = TRANSLATION_PROMPT + job.prompt, TRANSLATION_MAX_TOKENS
    else:
        image = "data:image/png;base64," + base64.b64encode(job.image).decode("ascii")
        content = [{"type": "text", "text": DESCRIPTION_PROMPT}, {"type": "image_url", "image_url": {"url": image}}]
        max_tokens = 4096
    payload = {"model": DESCRIPTION_MODEL, "messages": [{"role": "user", "content": content}],
               "temperature": 0.2, "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False}}
    job.description_started = True
    try:
        response = await _slot(job.conn, "/images/descriptions", GENERATE_TIMEOUT_SEC, json=payload)
    except httpx.TimeoutException:
        raise JobError("timeout", "шлюз не дождался описания") from None
    except httpx.HTTPError:
        raise JobError("gateway", "шлюз недоступен") from None
    except upstream.UpstreamRefused as e:
        raise _refused(job.conn, e) from None
    if response.status_code != 200:
        raise JobError(f"http_{response.status_code}", upstream.gateway_error(response, job.conn.name))
    choices = upstream.json_body(response).get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    text = (choice.get("message") or {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise JobError("no_description", TEXT_WORDS[job.kind][2])
    job.truncated = choice.get("finish_reason") == "length"
    if job.truncated and job.kind == "translation":
        # обрезанный перевод заменил бы промпт его половиной
        raise JobError("length", "перевод не уместился в ответ модели — сократите промпт")
    job.description = text.strip()


async def release_after_cancel(job: Job, *, cancelled=True) -> str:
    """Release модели отменённого задания сразу после конца его запроса. 409 — slot-gate ещё
    считает запрос (свой или чужой): повтор с паузой до CANCEL_RELEASE_SEC."""
    deadline = clock.NOW() + CANCEL_RELEASE_SEC
    prefix = "отменено" if cancelled else TEXT_WORDS[job.kind][0]
    if job.conn.kind != "srvllm":                 # у внешнего сервиса карты студии нет
        return prefix
    while True:
        try:
            r = await release_description(job.conn) if job.kind in TEXT_KINDS else await release(job.conn, job.gateway_model)
        except httpx.HTTPError as e:
            log.warning("задание %s: release не прошёл (%s)", job.id[:8], type(e).__name__)
            return f"{prefix}; карту освободить не удалось — шлюз недоступен: {type(e).__name__}"
        except Exception as e:                        # noqa: BLE001 — подключение убрали из списка или удалили
            log.warning("задание %s: release не прошёл (%s)", job.id[:8], type(e).__name__)
            return f"{prefix}; карту освободить не удалось — подключение недоступно"
        if r.status_code == 200:
            log.info("задание %s: %s, карта освобождена", job.id[:8], prefix)
            return prefix + " — карта освобождена"
        if r.status_code != 409:
            log.warning("задание %s: release ответил %s", job.id[:8], r.status_code)
            return f"{prefix}; карту освободить не удалось — {upstream.gateway_error(r, job.conn.name)}"
        if clock.NOW() >= deadline:
            log.warning("задание %s: release — 409 дольше %d с", job.id[:8], CANCEL_RELEASE_SEC)
            return prefix + "; карта занята другим запросом — освободится сама по окончании аренды"
        await clock.SLEEP(RELEASE_PAUSE_SEC)


async def _save(job: Job, picture: "tuple[bytes, str]", seed: int) -> str:
    data, mime = picture
    return await media.save_async(job.user_id, data, mime, f"studio-{job.model}-{seed}{media.EXT[mime]}", thumb=True)


async def run_series(jobs: list[Job]) -> None:
    """Строки серии по одной (п. 8 шапки): следующая — после конца предыдущей и без await между ними,
    так что слот пользователя не пустеет ни на миг. Отменённые и снятые строки пропускаются."""
    for i, job in enumerate(jobs):
        if i:
            if job.state != "queued":
                continue
            job.state, job.detail = "working", WORKING
            _store(job)
        await run_job(job)


async def run_job(job: Job) -> None:
    outcome = None
    try:
        try:
            if job.kind in TEXT_KINDS:
                await describe(job)
                outcome = ("done", TEXT_WORDS[job.kind][0], None, None)
            else:
                data, seed, mime = await generate(job)
                outcome = ("done", "готово", (data, mime), seed)
        except JobError as e:
            outcome = ("error", str(e), None, None)
            log.warning("задание %s: ошибка %s", job.id[:8], e.code)
        except Exception as e:                        # noqa: BLE001 — задание не должно висеть
            outcome = ("error", f"сбой службы: {type(e).__name__}", None, None)
            log.warning("задание %s: сбой %s", job.id[:8], type(e).__name__)
        description_cleanup = None
        if job.kind in TEXT_KINDS and job.description_started:
            description_cleanup = await release_after_cancel(job, cancelled=job.state == "cancelling")
            if outcome[0] == "done":
                outcome = ("done", description_cleanup, None, None)
        state_, detail, picture, seed = outcome
        if picture is not None and job.state != "cancelling":
            try:
                job.media_id = await _save(job, picture, seed if seed is not None else job.seed)
            except media.StorageError as e:
                state_, detail = "error", str(e)
                log.warning("задание %s: картинка не сохранена", job.id[:8])
            except Exception as e:                    # noqa: BLE001 — и учётку удалили посреди записи: не висеть
                state_, detail = "error", f"сбой службы: {type(e).__name__}"
                log.warning("задание %s: картинка не сохранена — сбой %s", job.id[:8], type(e).__name__)
        if job.state == "cancelling":                 # и отмена во время записи картинки
            job.description = None
            if job.media_id is not None:
                # ссылка снимается до await: остановка службы посреди удаления не оставит строку
                # images, указывающую на файл, которого уже нет
                mid, job.media_id = job.media_id, None
                await media.delete_async(job.user_id, mid)
            # запрос кончился (картинкой или ошибкой) — картинку не выдаём, карту освобождаем
            job.detail = "запрос завершён — освобождаю карту"
            _store(job)
            if job.kind in TEXT_KINDS:
                job.state, job.detail = "cancelled", (description_cleanup or "отменено").replace(TEXT_WORDS[job.kind][0], "отменено")
            else:
                job.state, job.detail = "cancelled", await release_after_cancel(job)
            return
        job.detail = detail
        if seed is not None:
            job.seed = seed
        job.state = state_                            # последним: страница берёт картинку по «done»
        if state_ == "done":
            log.info("задание %s: готово", job.id[:8])
    except asyncio.CancelledError:                    # остановка службы — для истории это перезапуск
        job.description = None
        if job.kind in TEXT_KINDS:
            job.state, job.detail = "cancelled", "отменено остановкой студии"
        else:
            job.state, job.detail = "error", INTERRUPTED
        raise
    finally:
        job.image = None
        job.mask = None
        job.prompt = ""
        job.finished_at = clock.NOW()
        _store(job)
