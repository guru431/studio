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
   `/slot/images/descriptions`, gemma-abl), без картинки. Своё идущее или готовое — `GET /text` (страница
   после перезагрузки снова показывает его окно), в «В работе» — строкой с «Отменить» (jobs.py).
3. Фото — файл с устройства (сохраняется в media как исходник) или фото истории
   (`source_media_id`); оба нормализуются normalize_image. Фото с устройства — в пределах картинки
   ролика (VIDEO_UPLOAD, VIDEO_PIXELS: снимок телефона 24–48 Мп): шлюз получает его уже уменьшенным
   до ≤2 Мп, так что предел входа — свой, а не переводчика. Маска (PNG) остаётся в студии: снаружи
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
   только у шлюза srvllm. Сила пишется в строку только у STRENGTH_MODELS (FLUX и FineView её не получают).
8. Серия (поле `series`, спека 2026-10-21-studio-photo-presets-compare-series, раздел 7): `count`
   строк сразу — первая `working`, остальные `queued` со своими seed и силой; одна задача
   (run_series) ведёт их по одной тем же run_job, так что серия держит слот пользователя до
   последней (`queued` — не TERMINAL). Ошибка одной не останавливает следующую. Время строки
   (`elapsed`, `started` в «В работе») — с её перехода в работу (Job.started). Отмена любой строки,
   и только что кончившейся, снимает весь остаток: идущую — как одиночную (release_after_cancel), строки в очереди — сразу
   `cancelled`. Строки в очереди при остановке службы — `error` самой остановкой (stop), при старте —
   recover. Связь строк — только в памяти (Job.series).
9. Варианты (`GET /<id>/variants`): свои готовые картинки с тем же исходником и сделанные из этой.
10. Перезапуск посреди картинки: остановка службы пишет строке ошибку RESTARTING (итог — откат на прежний код
   оставит её удаляемой), обрыв оставляет её в работе или в очереди. При запуске recover ставит такую строку
   снова в очередь (`queued`, RESUMED), resume ведёт её, как серию, с тем же seed, моделью, размером, исходником
   и маской из media — одна попытка: прерванный повтор, отменявшаяся строка, строка в корзине или у
   отключённой учётки, остановка дольше RESUME_SEC — ошибка INTERRUPTED. У обрыва (OOM, `kill -9`) отметки
   остановки нет — срок считается от создания строки (`created`).
"""
import asyncio
import base64
import binascii
import io
import json
import logging
import math
import re
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
import card
import clock
import connections
import db
import forms
import media
import operations
import push
import sections
import state
import upstream
from connections import Connection
from image_upload import (MAX_BODY, MAX_UPLOAD, VIDEO_PIXELS, VIDEO_UPLOAD, UploadError, image_size, prepare_mask,
                          restrict_edit)

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
# release после готового описания или перевода: свой запрос кончился, 409 — миг, пока slot-gate ещё
# его считает, или чужой запрос к gemma-abl; ждать чужой ради release — держать готовый текст и слот
DONE_RELEASE_SEC = 4
RELEASE_PAUSE_SEC = 2
TERMINAL = {"done", "error", "cancelled"}
WORKING = "жду карту или генерирую"
#: серия (п. 8 шапки): строк в одном запросе — не больше
SERIES_MAX = 4
#: «Варианты» просмотра: не больше в каждом ряду
VARIANTS_MAX = 24
CANCELLING = "отменяю — карта освободится сразу после текущей картинки (если шлюз ещё ждёт карту — после её получения и картинки)"
INTERRUPTED = "прервано перезапуском студии"
#: остановка службы посреди картинки (п. 10 шапки): строка ждёт повтора при запуске
RESTARTING = "прервано перезапуском студии — повторю после запуска"
#: повтор после перезапуска: слова строки в очереди и в работе; второй перезапуск — уже INTERRUPTED
RESUMED = "повторяю после перезапуска студии"
#: остановка дольше часа (или откат на прежний код и возврат) — не повод генерировать давнее
RESUME_SEC = 3600
JSON_MAX = 512 * 1024
#: удаление учётки ждёт конца её отменённых заданий: они освобождают карту её подключением
SETTLE_SEC = 90
#: картинка ответа (п. 2 шапки): форматы и пределы — с запасом над размерами моделей и gpt-image-1
RESULT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
RESULT_PIXELS = 4096 * 4096
RESULT_MAX = 50 * 1024 * 1024
NOT_PICTURE = "сервис вернул не картинку (нужен PNG, JPEG или WebP)"

#: Модели страницы: короткое имя → id у шлюза (контракт API картинок).
MODELS = {"snofs": "snofs-krea2-v1.6", "flux": "flux-3-image-klein",
          "fineview": "fineview-qw21", "jib-moss": "jib-moss-v2"}
STRENGTH_MODELS = {"snofs", "jib-moss"}
COMFY_MODELS = {value for name, value in MODELS.items() if name != "flux"}
RELEASE_MODELS = {name: MODELS[name] for name in ("snofs", "flux")}
UPSCALE_MODES = {"seedvr2-3b-int8": "restore", "realesrgan-x4plus": "fast"}
MEDIA_TIMEOUT_SEC = 1900
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
    resumed: bool = False                    # повтор после перезапуска (п. 10 шапки)
    upscale_scale: float | None = None

    @property
    def gateway_model(self) -> str:
        """Id модели в запросе: у шлюза — по короткому имени студии, у внешнего сервиса — как есть."""
        if self.upscale_scale is not None:    # release работает по модели, привязанной к слоту comfy
            return RELEASE_MODELS["snofs"]
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


class UpscaleIn(BaseModel):
    mode: Literal["restore", "fast"] = "restore"
    scale: float = Field(default=2, ge=1, le=4)
    source_media_id: str | None = Field(default=None, max_length=64)


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
    operations.check(user_id)
    _prune()
    active = _active(user_id)
    if active is not None:
        message = ("предыдущее задание отменяется — дождитесь освобождения карты"
                   if active.state == "cancelling" else "предыдущее задание ещё выполняется")
        # kind: описание или перевод в ленте не видны — страница по нему показывает их строкой с «Отменить»
        raise HTTPException(409, {"code": "busy", "message": message,
                                  "id": active.id, "kind": active.kind})


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
    changed = state.DB.execute("UPDATE images SET state = ?, detail = ?, seed = ?, media_id = ?, finished = ?"
                               " WHERE id = ? AND finished IS NULL",
                               (job.state, job.detail, job.seed, job.media_id, job.finished_at, job.id)).rowcount
    if changed and job.finished_at is not None and job.detail != RESTARTING:   # повтор впереди — не итог
        push.job_finished("images", job.id)


def _row(user_id: int, image_id: str):
    """Своя картинка не из корзины (`deleted`, trash.py), иначе 404."""
    row = state.DB.execute("SELECT * FROM images WHERE id = ? AND user_id = ? AND deleted IS NULL",
                           (image_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такой картинки нет"})
    return row


def started(row) -> float:
    """Начало работы строки: у строки серии — переход из очереди в работу (Job.started в памяти),
    а не создание серии; задания в памяти нет — время создания."""
    job = JOBS.get(row["id"])
    return job.started if job is not None else row["created"]


def _out(row, with_chain=True) -> dict:
    """Запись ленты; цепочка промптов — только у одной записи (просмотр): в ленте — число шагов,
    иначе длинные цепочки раздули бы страницу и добавили запросов к базе на каждый шаг."""
    end = row["finished"] if row["finished"] is not None else clock.NOW()
    chain = chains.of_image(row["user_id"], row)
    out = {"id": row["id"], "kind": row["kind"], "state": row["state"], "detail": row["detail"],
           "elapsed": int(max(0, end - started(row))), "model": row["model"], "prompt": row["prompt"],
           "size": row["size"], "seed": row["seed"], "strength": row["strength"],
           "source_media_id": row["source_media_id"], "mask_media_id": row["mask_media_id"],
           "media_id": row["media_id"], "created": row["created"], "steps": len(chain)}
    if with_chain:
        out["chain"] = chains.annotate(row["user_id"], chain)
    return card.mark(out, "photo", row)


def _status(job: Job) -> dict:
    end = job.finished_at if job.finished_at is not None else clock.NOW()
    return card.mark({"id": job.id, "state": job.state, "detail": job.detail, "model": job.model, "kind": job.kind,
                      "elapsed": int(max(0, end - job.started))}, "photo", {"id": job.id, "finished": job.finished_at})


def recover() -> None:
    """После перезапуска идущих заданий нет (п. 10 шапки): картинка, прерванная впервые, — снова в очередь
    (`queued`, RESUMED), её поведёт resume; прочие прерванные — ошибка INTERRUPTED."""
    now = clock.NOW()
    count = state.DB.execute(
        "UPDATE images SET state = 'error', detail = ?, finished = COALESCE(finished, ?)"
        " WHERE (state IN ('working', 'cancelling', 'queued') OR (state = 'error' AND detail = ?))"
        " AND (state = 'cancelling' OR detail = ? OR deleted IS NOT NULL OR (state = 'error' AND finished < ?)"
        " OR (state IN ('working', 'queued') AND created < ?)"
        " OR user_id NOT IN (SELECT id FROM users WHERE disabled = 0))",
        (INTERRUPTED, now, RESTARTING, RESUMED, now - RESUME_SEC, now - RESUME_SEC)).rowcount
    again = state.DB.execute("UPDATE images SET state = 'queued', detail = ?, finished = NULL"
                             " WHERE state IN ('working', 'queued') OR (state = 'error' AND detail = ?)",
                             (RESUMED, RESTARTING)).rowcount
    if count or again:
        log.warning("картинки: прервано перезапуском — %d, повторю — %d", count, again)


def resume() -> None:
    """Старт службы (после recover и state.HTTP): строки, поставленные в повтор, — в память сразу (слот учётки
    занят, «Отменить» работает) и задачей на учётку по порядку ленты, как серия (п. 10 шапки). Модели больше нет
    среди отмеченных в разделе — ошибка INTERRUPTED."""
    rows = state.DB.execute("SELECT * FROM images WHERE state = 'queued' AND detail = ?"
                            " ORDER BY user_id, created DESC, id", (RESUMED,)).fetchall()
    queues: dict[int, list[Job]] = {}
    for row in rows:
        upscale_scale = None
        try:
            if row["model"] in UPSCALE_MODES:
                conn, model = _srvllm_photo(row["user_id"]), row["model"]
                upscale_scale = chains.of_image(row["user_id"], row)[-1].get("scale", 2)
            else:
                conn, model = sections.resolve(row["user_id"], "photo", None, row["model"])
        except HTTPException:
            conn = None
        if conn is None or (row["mask_media_id"] is not None and conn.kind != "srvllm"):
            state.DB.execute("UPDATE images SET state = 'error', detail = ?, finished = ? WHERE id = ?",
                             (INTERRUPTED, clock.NOW(), row["id"]))
            continue
        job = Job(id=row["id"], user_id=row["user_id"], conn=conn, model=model, prompt=row["prompt"], size=row["size"],
                  seed=row["seed"], started=row["created"], state="queued", detail=RESUMED, kind=row["kind"],
                  strength=row["strength"] if row["strength"] is not None else 0.65,
                  source_media_id=row["source_media_id"], mask_media_id=row["mask_media_id"], resumed=True,
                  upscale_scale=upscale_scale)
        JOBS[job.id] = job
        queues.setdefault(job.user_id, []).append(job)
    for uid, jobs in queues.items():
        with operations.bind(uid):                   # очистка истории после старта запретит их сохранение
            task = asyncio.create_task(run_series(jobs))
        for job in jobs:
            job.series = jobs if len(jobs) > 1 else None
            job.task = task
        log.info("картинки: повтор после перезапуска — %d", len(jobs))


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
    о `queued` не знает, и строки остались бы неудаляемыми. Итог — RESTARTING (п. 10 шапки), у повтора — INTERRUPTED."""
    tasks = {j.task for j in JOBS.values() if j.task and not j.task.done()}   # у серии задача одна на все строки
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for job in JOBS.values():
        if job.state == "queued":
            _end_queued(job, "error", INTERRUPTED if job.resumed else RESTARTING)


# ---- ввод: форма, фото, маска --------------------------------------------------

def _multipart(request: Request) -> bool:
    return request.headers.get("content-type", "").lower().startswith("multipart/form-data")


async def _discard(user_id: int, media_ids: list[str]) -> None:
    """Файлы, записанные запросом, который потом отказал."""
    for media_id in media_ids:
        await media.delete_async(user_id, media_id)


def _fit_mask(mask: bytes, image: "bytes | None") -> bytes:
    """Маску редактора — к размеру исходника задания (для asyncio.to_thread). Редактор рисует её по полному
    размеру картинки ленты (до RESULT_PIXELS), а normalize_image уменьшает исходник до ≤2 Мп и ≤2048:
    PNG того же соотношения сторон (с точностью до округления сторон) уменьшается NEAREST. Прочее — как
    есть, отказ скажет prepare_mask: её пределы — пределы копии модуля переводчика, они не меняются."""
    if image is None:
        return mask
    target = image_size(image)
    try:
        with Image.open(io.BytesIO(mask)) as picture:
            width, height = picture.size
            if (picture.format != "PNG" or (width, height) == target or width < target[0]
                    or width * height > RESULT_PIXELS
                    or abs(width * target[1] - height * target[0]) > width + height):
                return mask
            fitted = picture.convert("L").resize(target, Image.Resampling.NEAREST)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return mask
    out = io.BytesIO()
    fitted.save(out, format="PNG")
    return out.getvalue()


# ---- API -------------------------------------------------------------------

@router.post("", status_code=202)
async def create_job(request: Request):
    uid = request.state.user.id
    sections.resolve(uid, "photo", None, None)  # раздел без модели — отказ до чтения тела
    ensure_idle(uid)
    upload, mask = None, None
    try:
        if _multipart(request):
            # разбор формы до 20 МиБ — доли секунды: в потоке (базы он не касается); тело — до фото ролика
            # с полями (предел Apache), маска страницы идёт с фото истории, а не с файлом устройства
            fields, upload, mask = await asyncio.to_thread(
                forms.decode, await forms.read_body(request, MAX_BODY + MAX_UPLOAD), request.headers["content-type"],
                with_mask=True, max_upload=VIDEO_UPLOAD)
            if fields.get("seed") == "":
                fields.pop("seed")
            if "series" in fields:                   # серия в форме — строкой JSON
                try:
                    fields["series"] = json.loads(fields["series"])
                except RecursionError:               # глубокая вложенность: не 500
                    raise _series_refused("поле серии не разобрать") from None
            body = JobIn.model_validate(fields)
        else:
            body = JobIn.model_validate_json(await forms.read_body(request, JSON_MAX))
        if body.series is not None and (mask is not None or body.mask_media_id is not None):
            raise _series_refused("серия — без кисти: правка по маске — только одиночная")
        image = await forms.photo(uid, upload, body.source_media_id, VIDEO_PIXELS)
        if body.mask_media_id is not None:
            if mask is not None:
                raise UploadError("нужна одна маска: файл или маска прежней правки")
            mask = await forms.history_file(uid, body.mask_media_id)
        if mask is not None:
            mask = await asyncio.to_thread(lambda m=mask: prepare_mask(_fit_mask(m, image), image))
        if body.size == "original" and image is None:
            raise HTTPException(422, {"code": "image", "message": "для исходного размера нужно фото"})
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    except (ValidationError, json.JSONDecodeError):
        raise HTTPException(422, {"code": "input", "message": "проверьте промпт, модель, размер, seed и силу изменения"}) from None
    conn, model = sections.resolve(uid, "photo", body.connection_id, body.model)
    if mask is not None and conn.kind != "srvllm":
        raise HTTPException(422, {"code": "brush", "message": "правка кистью — только через шлюз srvllm"})
    lustify_edit = image is not None and conn.kind == "srvllm" and model in STRENGTH_MODELS
    count, vary = (body.series.count, body.series.vary) if body.series else (1, "seed")
    strengths = [body.strength if lustify_edit else None] * count
    if vary == "strength":
        if not lustify_edit:
            raise _series_refused("серия по силе — только модели с силой изменения и с исходным фото")
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
        _insert(job, chain, strengths[i])
        jobs.append(job)
    task = asyncio.create_task(run_series(jobs) if count > 1 else run_job(jobs[0]))
    for job in jobs:
        job.series = jobs if count > 1 else None
        job.task = task
    log.info("задание %s: %s, строк — %d", jobs[0].id[:8], size, count)
    return {"id": jobs[0].id, "seed": seed, "ids": [j.id for j in jobs]}


def _insert(job: Job, chain: list[dict], strength: float | None = None) -> None:
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, strength, source_media_id,"
                     " mask_media_id, state, detail, created, chain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (job.id, job.user_id, job.kind, job.model, job.prompt, job.size, job.seed, strength,
                      job.source_media_id, job.mask_media_id, job.state, job.detail, job.started,
                      json.dumps(chain, ensure_ascii=False)))
    JOBS[job.id] = job


def _upscale_source(image: bytes, body: UpscaleIn) -> tuple[str, str]:
    mime = _picture(image)
    with Image.open(io.BytesIO(image)) as photo:
        width, height = photo.size
        if photo.getexif().get(274) in (5, 6, 7, 8):
            width, height = height, width
    size = (round(width * body.scale), round(height * body.scale))
    if max(size) > 4096:
        raise UploadError("Результат должен быть не больше 4096 пикселей по длинной стороне")
    if body.mode == "fast" and width * height * 16 > 36_000_000:
        raise UploadError("Для быстрого режима нужен исходник до 2,25 Мп")
    return mime, "{}x{}".format(*size)


@router.post("/upscales", status_code=202)
async def create_upscale(request: Request):
    uid = request.state.user.id
    conn = _srvllm_photo(uid)
    ensure_idle(uid)
    try:
        fields, upload, _ = await asyncio.to_thread(forms.decode, await forms.read_body(request, MAX_BODY),
                                                    request.headers.get("content-type", ""))
        body = UpscaleIn.model_validate(fields)
        source_id = body.source_media_id
        image = upload if upload is not None else await forms.history_file(uid, source_id)
        mime, size = await asyncio.to_thread(_upscale_source, image, body)
    except ValidationError:
        raise HTTPException(422, {"code": "input", "message": "Выберите режим и масштаб от 1 до 4"}) from None
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    except JobError:
        raise HTTPException(400, {"code": "image", "message": "Нужен PNG, JPEG или WebP до 16 Мп"}) from None
    saved = []
    try:
        ensure_idle(uid)
        if upload is not None:
            source_id = await media.save_async(uid, image, mime, f"studio-upscale-source{media.EXT[mime]}", thumb=True)
            saved.append(source_id)
            ensure_idle(uid)
        if not media.alive(uid, source_id):
            raise HTTPException(404, media.GONE)
        model = next(model for model, mode in UPSCALE_MODES.items() if mode == body.mode)
        job = Job(id=uuid.uuid4().hex, user_id=uid, conn=conn, model=model, prompt="", size=size, seed=0,
                  started=clock.NOW(), kind="edit", image=image, source_media_id=source_id, upscale_scale=body.scale)
        step = chains.image_step(image_id=job.id, kind="upscale", model=model, prompt="", size=size, seed=0,
                                 strength=None, created=job.started)
        _insert(job, chains.of_source(uid, source_id) + [{**step, "scale": body.scale}])
    except media.StorageError as e:
        await _discard(uid, saved)
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    except Exception:
        await _discard(uid, saved)
        raise
    job.task = asyncio.create_task(run_job(job))
    return {"id": job.id}


# MCP files already belong to section history. Project them into the photo feed without copying them.
MCP_PHOTOS = """
SELECT COALESCE(m.id, j.id) AS id, j.user_id, j.id AS mcp_job_id, j.server AS mcp_server,
       j.tool, j.status, j.error, j.model, j.prompt, j.created, m.id AS media_id, j.server_id, json_extract(j.result, '$.photo') AS photo,
       substr(json_extract(j.result, '$.text'), 1, 100) AS report
FROM mcp_section_jobs j
LEFT JOIN mcp_section_files f ON f.job_id = j.id AND f.user_id = j.user_id
  AND f.media_id IS NOT json_extract(j.result, '$.photo.source_media_id')
LEFT JOIN media m ON m.id = f.media_id AND m.user_id = j.user_id AND m.mime LIKE 'image/%'
WHERE j.section = 'photo' AND j.tool IN ('image_generate','image_edit','generate_image','edit_image','upscale_image')
  AND (m.id IS NOT NULL OR (f.media_id IS NULL AND j.status <> 'completed'))
  AND NOT EXISTS (SELECT 1 FROM mcp_gallery_trash g WHERE g.id = COALESCE(m.id, j.id) AND g.user_id = j.user_id)
"""


def _mcp_photo_out(row):
    metadata = mcp_photo_metadata({"photo": json.loads(row["photo"]) if row["photo"] else {}, "text": row["report"] or ""})
    size, seed = metadata.get("size", ""), metadata.get("seed")
    kind = {"image_edit": "edit", "edit_image": "edit", "upscale_image": "upscale"}.get(row["tool"], "generation")
    step = {"image_id": row["id"], "kind": kind,
            "model": row["model"], "server": row["mcp_server"], "prompt": row["prompt"], "size": size,
            "seed": seed, "mcp_job_id": row["mcp_job_id"], "created": row["created"], "media_id": row["media_id"]}
    status = row["status"]
    progress = {"queued": "В очереди", "waiting_gpu": "Ожидает GPU", "running": "Генерирую", "saving": "Сохраняю"}
    shown = "done" if row["media_id"] else "queued" if status in progress else "cancelled" if status == "cancelled" else "error"
    return {**step, "id": row["id"], "mcp_server": row["mcp_server"], "mcp_server_id": row["server_id"],
            "mcp_tool": row["tool"], "state": shown, "detail": row["error"] or progress.get(status, ""),
            "elapsed": 0, "strength": metadata.get("strength"), "source_media_id": metadata.get("source_media_id"), "mask_media_id": None,
            "steps": 1, "chain": [step]}


def mcp_photo_metadata(result):
    """Поля сохранённой операции и метаданные формата Spark из прежних записей."""
    metadata = dict(result.get("photo") or {})
    match = re.match(r"^([1-9][0-9]{0,4})[×x]([1-9][0-9]{0,4}), ([0-9]{1,3}) шт\., seed ([0-9]{1,16})\.", result.get("text", ""))
    if match:
        width, height, count, seed = match.groups()
        metadata["size"] = f"{width}x{height}"
        if count == "1" and int(seed) <= 2**53 - 1:
            metadata["seed"] = int(seed)
    return metadata


def _mcp_photo_row(uid, image_id, *, status=False):
    query = " AND j.user_id = ? AND COALESCE(m.id, j.id) = ?"
    args = (uid, image_id)
    if status:  # карточка опрашивает ID задания, пока полученные файлы ещё не появились в ленте
        query = " AND j.user_id = ? AND (COALESCE(m.id, j.id) = ? OR j.id = ?) ORDER BY m.id LIMIT 1"
        args += (image_id,)
    return state.DB.execute(MCP_PHOTOS + query, args).fetchone()


def share_row(uid, image_id):
    remote = _mcp_photo_row(uid, image_id)
    return _mcp_photo_out(remote) if remote is not None else _row(uid, image_id)


@router.get("")
async def list_images(request: Request, before: "float | None" = None,
                      before_id: "str | None" = Query(None, max_length=64), after: "float | None" = None,
                      after_id: "str | None" = Query(None, max_length=64), limit: int = Query(60, ge=1, le=100),
                      model: "str | None" = Query(None, max_length=200)):
    """Лента: новые сверху, вместе с идущими; следующая страница — before=<created>&before_id=<id> последней;
    after=<created>&after_id=<id> записи — ближайшие более новые (просмотр записи вне загруженной ленты);
    model — только записи этой модели (чип модели в шапке ленты)."""
    if (after is None) != (after_id is None):
        raise HTTPException(422, {"code": "after", "message": "after и after_id — только вместе"})
    extra, args = (" AND deleted IS NULL AND model = ?", (model,)) if model else (" AND deleted IS NULL", ())
    uid = request.state.user.id
    cursors = (before, before_id, limit)
    newer = None if after is None else (after, after_id)
    native = db.page(state.DB, "images", uid, *cursors, extra=extra, extra_args=args, after=newer)
    remote = db.page(state.DB, "(" + MCP_PHOTOS + ")", uid, *cursors,
                     extra=" AND model = ?" if model else "", extra_args=args, after=newer)
    rows = sorted([*native, *remote], key=lambda row: (row["created"], row["id"]), reverse=True)
    rows = rows[-limit:] if newer is not None else rows[:limit]
    return {"items": [_mcp_photo_out(r) if "mcp_job_id" in r.keys() else _out(r, with_chain=False) for r in rows]}


@router.post("/descriptions", status_code=202)
async def create_description(request: Request):
    uid = request.state.user.id
    conn = _srvllm_photo(uid)
    ensure_idle(uid)
    try:
        # тело — как у create_job (фото до VIDEO_UPLOAD с полями, предел Apache); больший файл страница
        # отклоняет до отправки, называя предел
        fields, upload, _ = await asyncio.to_thread(forms.decode, await forms.read_body(request, MAX_BODY + MAX_UPLOAD),
                                                    request.headers.get("content-type", ""), max_upload=VIDEO_UPLOAD)
        image = await forms.photo(uid, upload, fields.get("source_media_id"), VIDEO_PIXELS)
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    ensure_idle(uid)
    # фото истории — его миниатюра в окне, подхваченном после перезагрузки (current_text)
    job = Job(id=uuid.uuid4().hex, user_id=uid, conn=conn, model="gemma-abl", prompt="", size="", seed=0,
              started=clock.NOW(), kind="description", image=image, detail="жду карту или описываю фото",
              source_media_id=fields.get("source_media_id") if upload is None else None)
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


def text_job(user_id: int) -> "Job | None":
    """Своё описание или перевод (п. 2 шапки), идущее или готовое с ещё не стёртым текстом; последнее начатое."""
    _prune()
    mine = [j for j in JOBS.values() if j.user_id == user_id and j.kind in TEXT_KINDS
            and (j.state not in TERMINAL or (j.state == "done" and j.description is not None))]
    return max(mine, key=lambda j: j.started, default=None)


@router.get("/text")
async def current_text(request: Request):
    """Раздел «Фото» при открытии (перезагрузка, PWA выгружена, «Открыть» в «В работе») — своё идущее или готовое
    текстовое задание, чтобы показать его окно; сам текст — обычным опросом …/descriptions|translations/<id>.
    Нет — `{}`."""
    job = text_job(request.state.user.id)
    return {} if job is None else _status(job) | {"source_media_id": job.source_media_id}


@router.post("/release")
async def release_card(request: Request):
    """Обе модели, gemma-abl и слот ocr (HunyuanOCR, `ocr.release_now`) — release фото-подключения
    администратора: итог по каждой."""
    import ocr                                   # ocr → speech → images: не при загрузке модуля
    conn = _srvllm_photo(auth.require_admin(request).id)
    results = {}
    for name, model in (RELEASE_MODELS | {"gemma-abl": DESCRIPTION_MODEL}).items():
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
            results[name] = {"state": "error", "message": upstream.gateway_error(r, conn)}
    results["hunyuan-ocr"] = await ocr.release_now(conn)
    log.info("освобождение карты кнопкой: %s", ", ".join(f"{k} {v['state']}" for k, v in results.items()))
    return {"results": results}


@router.get("/{image_id}")
async def image_status(image_id: str, request: Request):
    uid = request.state.user.id
    remote = _mcp_photo_row(uid, image_id, status=True)
    return _mcp_photo_out(remote) if remote else _out(_row(uid, image_id))


@router.get("/{image_id}/variants")
async def variants(image_id: str, request: Request):
    """«Варианты» просмотра (п. 9 шапки): свои готовые с файлом — из того же исходника (кроме этой) и
    сделанные из этой; новые сверху, не больше VARIANTS_MAX в каждом ряду."""
    uid = request.state.user.id
    remote = _mcp_photo_row(uid, image_id)
    row = remote if remote else _row(uid, image_id)

    def made_from(media_id: "str | None") -> list[dict]:
        if media_id is None:
            return []
        rows = state.DB.execute("SELECT * FROM images WHERE user_id = ? AND source_media_id = ? AND id != ?"
                                " AND state = 'done' AND media_id IS NOT NULL AND deleted IS NULL"
                                " ORDER BY created DESC, id DESC LIMIT ?",
                                (uid, media_id, image_id, VARIANTS_MAX))
        return [_out(r, with_chain=False) for r in rows]

    return {"siblings": [] if remote else made_from(row["source_media_id"]), "children": made_from(row["media_id"])}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    """Запрос к шлюзу не обрывается (обрыв до slot-gate не доходит): задание ждёт его конца,
    выбрасывает картинку и освобождает карту само — см. run_job. Описание отменяется так же."""
    uid = request.state.user.id
    if remote := _mcp_photo_row(uid, job_id, status=True):
        from mcp_sections import cancel_gallery
        await cancel_gallery(uid, "photo", remote["mcp_job_id"])
        return {"state": _mcp_photo_out(_mcp_photo_row(uid, job_id, status=True))["state"]}
    _prune()
    job = JOBS.get(job_id)
    if job is None or job.user_id != uid:
        return {"state": _row(uid, job_id)["state"]}          # закончилось до перезапуска или забыто
    # серия — весь её остаток (п. 8 шапки), даже если нажатая строка уже кончилась: кончившиеся
    # строки _cancel не трогает
    for j in job.series or [job]:
        _cancel(j)
    return {"state": job.state}


@router.delete("/{image_id}", status_code=204)
async def delete_image(image_id: str, request: Request):
    """Кончившаяся картинка — в корзину (`deleted`, trash.py); строка и файлы — до `purge`."""
    uid = request.state.user.id
    remote = _mcp_photo_row(uid, image_id)
    if remote is not None:
        from mcp_sections import trash_gallery
        trash_gallery(remote, "photo", source_id=_mcp_photo_out(remote)["source_media_id"])
        return Response(status_code=204)
    row = _row(uid, image_id)
    if row["state"] not in TERMINAL:
        raise HTTPException(409, {"code": "running", "message": "сначала отмените"})
    state.DB.execute("UPDATE images SET deleted = ? WHERE id = ?", (clock.NOW(), image_id))
    JOBS.pop(image_id, None)
    log.info("картинка %s: в корзине", image_id[:8])
    return Response(status_code=204)



async def purge(user_id: int, item_id: str) -> None:
    """Окончательно: строка и её файлы; файл, на который ссылается другая запись истории, остаётся. Строки уже
    нет — ничего."""
    row = state.DB.execute("SELECT * FROM images WHERE id = ? AND user_id = ?", (item_id, user_id)).fetchone()
    if row is None:
        return
    state.DB.execute("DELETE FROM images WHERE id = ?", (item_id,))
    JOBS.pop(item_id, None)
    for media_id in {row["media_id"], row["source_media_id"], row["mask_media_id"]} - {None}:
        if not media.referenced(user_id, media_id):
            await media.delete_async(user_id, media_id)
    log.info("картинка %s: удалена", item_id[:8])


# ---- шлюз ------------------------------------------------------------------

async def _slot(conn: Connection, path: str, timeout: float, **kw) -> httpx.Response:
    return await upstream.request(conn, "POST", connections.slot_url(conn, path), timeout=timeout, **kw)


async def release(conn: Connection, model: str) -> httpx.Response:
    return await _slot(conn, "/images/release", SHORT_TIMEOUT_SEC, json={"model": model})


async def free_models(conn: Connection) -> bool:
    """Release всех моделей картинок (Lustify, FLUX держат карту арендой) перед локальной моделью. Итог не важен:
    идущую генерацию release не оборвёт (409), занятую карту дождётся шлюз. False — шлюз отказал или не ответил."""
    try:
        for model in RELEASE_MODELS.values():
            await release(conn, model)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return False
    return True


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
    # MAX_IMAGE_PIXELS Pillow отказывает сам ещё в Image.open. Предупреждение — тоже предел: фильтр «ошибка»
    # может остаться на процесс от перекрывшихся catch_warnings копии image_upload (находка 2026-11-09)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in RESULT_TYPES:
                raise JobError("not_image", NOT_PICTURE)
            if image.width * image.height > RESULT_PIXELS:   # до декодирования: размер — из заголовка
                raise too_big
            image.load()
            return RESULT_TYPES[image.format]
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise too_big from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise JobError("not_image", NOT_PICTURE) from None


async def _release_others(job: Job) -> None:
    """Release другой модели картинок перед запросом (п. 4 шапки): итог не важен — занятую (409)
    запрос дождётся у шлюза, а недоступный шлюз и убранный адрес назовёт сам запрос."""
    for model in RELEASE_MODELS.values():
        if model == job.gateway_model or (model == MODELS["snofs"] and job.gateway_model in COMFY_MODELS):
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
        raise JobError(f"http_{r.status_code}", upstream.gateway_error(r, job.conn))
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
    timeout = MEDIA_TIMEOUT_SEC if job.model in MODELS and job.model != "flux" else GENERATE_TIMEOUT_SEC
    try:
        if job.upscale_scale is not None:
            r = await _slot(job.conn, "/images/upscales", timeout,
                            data={"mode": UPSCALE_MODES[job.model], "scale": str(job.upscale_scale)},
                            files={"image": ("input", job.image, "application/octet-stream")})
        elif job.image is not None:
            if job.model in STRENGTH_MODELS:
                payload["strength"] = job.strength
            r = await _slot(job.conn, "/images/edits", timeout,
                            data={k: str(v) for k, v in payload.items()},
                            files={"image": ("input.png", job.image, "image/png")})
        else:
            r = await _slot(job.conn, "/images/generations", timeout, json=payload)
    except httpx.TimeoutException:
        raise JobError("timeout", f"шлюз не ответил за {timeout} с")
    except httpx.HTTPError as e:
        raise JobError("gateway", f"шлюз недоступен: {type(e).__name__}")
    except upstream.UpstreamRefused as e:
        raise _refused(job.conn, e) from None
    if r.status_code != 200:
        raise JobError(f"http_{r.status_code}", upstream.gateway_error(r, job.conn))
    png, seed = _png(upstream.json_body(r))
    mime = await asyncio.to_thread(_picture, png)       # до слияния: битое там было бы «сбоем службы»
    if job.mask is not None:
        png, mime = await asyncio.to_thread(restrict_edit, job.image, png, job.mask), "image/png"
    return png, seed, mime


async def describe(job: Job):
    # Image slots hold leases for 600 s; release only idle image requests before switching.
    for model in RELEASE_MODELS.values():
        try:
            response = await release(job.conn, model)
        except httpx.HTTPError as e:
            raise JobError("gateway", f"шлюз недоступен: {type(e).__name__}") from None
        except upstream.UpstreamRefused as e:     # и подключение удалили или сменили (ConnectionChanged)
            raise _refused(job.conn, e) from None
        if response.status_code != 200:
            raise JobError("release", "не удалось подготовить карту: " + upstream.gateway_error(response, job.conn))
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
        raise JobError(f"http_{response.status_code}", upstream.gateway_error(response, job.conn))
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


async def release_after_cancel(job: Job, *, cancelled=True, wait=CANCEL_RELEASE_SEC) -> str:
    """Release модели отменённого задания сразу после конца его запроса. 409 — slot-gate ещё
    считает запрос (свой или чужой): повтор с паузой до `wait` секунд."""
    deadline = clock.NOW() + wait
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
            return f"{prefix}; карту освободить не удалось — {upstream.gateway_error(r, job.conn)}"
        if clock.NOW() >= deadline:
            log.warning("задание %s: release — 409 дольше %d с", job.id[:8], wait)
            return prefix + "; карта занята другим запросом — освободится сама по окончании аренды"
        await clock.SLEEP(RELEASE_PAUSE_SEC)


async def _save(job: Job, picture: "tuple[bytes, str]", seed: int) -> str:
    data, mime = picture
    return await media.save_async(job.user_id, data, mime, f"studio-{job.model}-{seed}{media.EXT[mime]}", thumb=True)


async def run_series(jobs: list[Job]) -> None:
    """Строки серии по одной (п. 8 шапки): следующая — после конца предыдущей и без await между ними,
    так что слот пользователя не пустеет ни на миг. Отменённые и снятые строки пропускаются. Повтор после
    перезапуска (resume) — та же очередь с первой строки; в работе его слова — RESUMED (п. 10 шапки)."""
    for i, job in enumerate(jobs):
        if job.state == "queued":
            job.state, job.started = "working", clock.NOW()        # время строки — с этого мига
            job.detail = RESUMED if job.resumed else WORKING
            _store(job)
        elif i or job.state not in ("working", "cancelling"):
            continue
        await run_job(job)


async def _reload(job: Job) -> None:
    """Фото и маска повтора после перезапуска — снова из media, тем же путём, что у запроса (create_job)."""
    try:
        if job.upscale_scale is not None:
            job.image = await forms.history_file(job.user_id, job.source_media_id)
        else:
            job.image = await forms.photo(job.user_id, None, job.source_media_id, VIDEO_PIXELS)
        if job.mask_media_id is not None:
            mask = await forms.history_file(job.user_id, job.mask_media_id)
            job.mask = await asyncio.to_thread(lambda: prepare_mask(_fit_mask(mask, job.image), job.image))
    except UploadError as e:
        raise JobError("image", str(e)) from None
    except HTTPException as e:                        # исходник или маску удалили, шара недоступна
        raise JobError("image", e.detail["message"]) from None
    if job.state == "cancelling":                     # отменили, пока читали файлы: запрос не нужен
        raise JobError("cancelled", "отменено")


async def run_job(job: Job) -> None:
    outcome = None
    try:
        try:
            if job.kind in TEXT_KINDS:
                await describe(job)
                outcome = ("done", TEXT_WORDS[job.kind][0], None, None)
            else:
                if job.resumed and (job.source_media_id or job.mask_media_id) and job.image is None:
                    await _reload(job)
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
            cancelled = job.state == "cancelling"
            # готовый текст не ждёт чужой запрос к gemma-abl; отменённое — ждёт свой, шлюз его дорабатывает
            wait = DONE_RELEASE_SEC if outcome[0] == "done" and not cancelled else CANCEL_RELEASE_SEC
            description_cleanup = await release_after_cancel(job, cancelled=cancelled, wait=wait)
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
        else:                                         # отменявшаяся и сам повтор — без нового повтора (п. 10 шапки)
            job.state, job.detail = "error", INTERRUPTED if job.resumed or job.state == "cancelling" else RESTARTING
        raise
    finally:
        job.image = None
        job.mask = None
        job.prompt = ""
        job.finished_at = clock.NOW()
        _store(job)
