"""Картинки (спека 2026-10-19-studio-pwa): генерация, правка кистью по маске, описание gemma-abl и
освобождение карты — через фото-подключение srvllm пользователя; итог — в истории (таблица
`images`, файлы media). Перенос заданий прежнего server.py; поведение отмены и release то же:

1. Одно задание за раз у пользователя: его второе — 409 busy (задания других не мешают).
   Отмена запрос к шлюзу НЕ обрывает: LiteLLM обрыв клиента дальше не передаёт, генерация всё
   равно доходит до конца, а release на идущий через slot-gate запрос отвечает 409 slot_busy
   (ревью 2026-10-19). Поэтому задание «отменяется», ждёт конца своего запроса, выбрасывает
   картинку и сразу зовёт release модели задания — на 409 повторяет с паузой до
   CANCEL_RELEASE_SEC; новое задание до тех пор получает busy.
2. Готовая PNG — в media с превью, строка `images` — запись ленты. Описания в историю не
   пишутся: текст живёт в памяти до выдачи (один раз) или KEEP_DONE_SEC.
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
   только у шлюза srvllm.
"""
import asyncio
import base64
import binascii
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

import auth
import chains
import clock
import connections
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
# шлюз держит запрос до конца генерации: ожидание карты (budget_s маршрута) плюс n картинок;
# таймаут маршрута LiteLLM — не больше 1260 с (контракт API картинок)
GENERATE_TIMEOUT_SEC = 1300
SHORT_TIMEOUT_SEC = 30
# release после отменённого задания: 409 — slot-gate ещё считает запрос (свой — доли секунды,
# чужой — до конца его картинки); дольше минуты не ждём — аренду снимет её срок
CANCEL_RELEASE_SEC = 60
RELEASE_PAUSE_SEC = 2
TERMINAL = {"done", "error", "cancelled"}
CANCELLING = "отменяю — карта освободится сразу после текущей картинки (если шлюз ещё ждёт карту — после её получения и картинки)"
INTERRUPTED = "прервано перезапуском студии"
JSON_MAX = 512 * 1024
#: удаление учётки ждёт конца её отменённых заданий: они освобождают карту её подключением
SETTLE_SEC = 90

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
    detail: str = "жду карту или генерирую"
    finished_at: "float | None" = None
    task: "asyncio.Task | None" = None
    image: bytes | None = None
    strength: float = 0.65
    mask: bytes | None = None
    kind: str = "generation"                 # generation | edit | description
    description: str | None = None
    truncated: bool = False
    description_started: bool = False
    source_media_id: str | None = None
    media_id: str | None = None              # готовая картинка в media (вместо прежнего png в памяти)
    mask_media_id: str | None = None

    @property
    def gateway_model(self) -> str:
        """Id модели в запросе: у шлюза — по короткому имени студии, у внешнего сервиса — как есть."""
        return MODELS.get(self.model, self.model) if self.conn.kind == "srvllm" else self.model


JOBS: dict[str, Job] = {}


class JobError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JobIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=24000)
    size: Literal["portrait", "square", "landscape", "original"]
    model: "str | None" = Field(default=None, max_length=200)      # короткое имя srvllm или id сервиса
    connection_id: "int | None" = None
    seed: "int | None" = Field(default=None, ge=0, le=2**32 - 1)
    strength: float = Field(default=0.65, ge=0.05, le=1)
    source_media_id: "str | None" = Field(default=None, max_length=64)
    mask_media_id: "str | None" = Field(default=None, max_length=64)    # маска прежней правки (повтор)


# ---- задания в памяти и строки истории -----------------------------------------

def _prune() -> None:
    now = clock.NOW()
    for job_id in [j.id for j in JOBS.values() if j.finished_at is not None and now - j.finished_at > KEEP_DONE_SEC]:
        JOBS.pop(job_id, None)


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
    """Состояние задания — в его строку истории (описаний в истории нет)."""
    if job.kind == "description":
        return
    state.DB.execute("UPDATE images SET state = ?, detail = ?, seed = ?, media_id = ?, finished = ? WHERE id = ?",
                     (job.state, job.detail, job.seed, job.media_id, job.finished_at, job.id))


def _row(user_id: int, image_id: str):
    row = state.DB.execute("SELECT * FROM images WHERE id = ? AND user_id = ?", (image_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такой картинки нет"})
    return row


def _out(row) -> dict:
    end = row["finished"] if row["finished"] is not None else clock.NOW()
    return {"id": row["id"], "kind": row["kind"], "state": row["state"], "detail": row["detail"],
            "elapsed": int(max(0, end - row["created"])), "model": row["model"], "prompt": row["prompt"],
            "size": row["size"], "seed": row["seed"], "strength": row["strength"],
            "source_media_id": row["source_media_id"], "mask_media_id": row["mask_media_id"],
            "media_id": row["media_id"], "created": row["created"],
            "chain": chains.annotate(row["user_id"], chains.of_image(row["user_id"], row))}


def _status(job: Job) -> dict:
    end = job.finished_at if job.finished_at is not None else clock.NOW()
    return {"id": job.id, "state": job.state, "detail": job.detail, "model": job.model, "kind": job.kind,
            "elapsed": int(max(0, end - job.started))}


def recover() -> None:
    """После перезапуска идущих заданий нет: их строки — ошибка «прервано перезапуском»."""
    count = state.DB.execute("UPDATE images SET state = 'error', detail = ?, finished = ?"
                             " WHERE state IN ('working', 'cancelling')", (INTERRUPTED, clock.NOW())).rowcount
    if count:
        log.warning("картинки: %d заданий прервано перезапуском", count)


def forget_user(user_id: int) -> None:
    """Стирание истории, удаление или отключение учётки: идущие задания пользователя отменяются —
    картинку, пришедшую после, путь отмены run_job выбросит и освободит карту; невыданные описания
    забываются."""
    for job in JOBS.values():
        if job.user_id != user_id:
            continue
        job.description = None
        if job.state == "working":
            job.state, job.detail = "cancelling", CANCELLING
            _store(job)


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
    """Остановка службы: идущие задания отменяются и успевают записать итог до закрытия базы."""
    tasks = [j.task for j in JOBS.values() if j.task and not j.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- ввод: форма, фото, маска --------------------------------------------------

def _multipart(request: Request) -> bool:
    return request.headers.get("content-type", "").lower().startswith("multipart/form-data")


async def _discard(user_id: int, media_ids: list[str]) -> None:
    """Файлы, записанные запросом, который потом отказал."""
    for media_id in media_ids:
        try:
            await media.delete_async(user_id, media_id)
        except OSError:
            log.warning("исходник или маска пользователя %d: файл не удалился", user_id)


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
            body = JobIn.model_validate(fields)
        else:
            body = JobIn.model_validate_json(await forms.read_body(request, JSON_MAX))
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
    except ValidationError:
        raise HTTPException(422, {"code": "input", "message": "проверьте промпт, модель, размер, seed и силу изменения"}) from None
    conn, model = sections.resolve(uid, "photo", body.connection_id, body.model)
    if mask is not None and conn.kind != "srvllm":
        raise HTTPException(422, {"code": "brush", "message": "правка кистью — только через шлюз srvllm"})
    # Parsing uploads yields to other requests: recheck before inserting the job.
    ensure_idle(uid)
    seed = body.seed if body.seed is not None else secrets.randbelow(2**32)
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
    job = Job(id=uuid.uuid4().hex, user_id=uid, conn=conn, model=model, prompt=body.prompt, size=size,
              seed=seed, started=now, kind="generation" if image is None else "edit", image=image,
              strength=body.strength, mask=mask, source_media_id=source_id, mask_media_id=mask_id)
    strength = body.strength if image is not None and conn.kind == "srvllm" else None
    chain = chains.of_source(uid, source_id) + [chains.image_step(
        image_id=job.id, kind="brush" if mask_id else job.kind, model=job.model, prompt=job.prompt, size=size,
        seed=seed, strength=strength, created=now)]
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, strength, source_media_id,"
                     " mask_media_id, state, detail, created, chain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (job.id, uid, job.kind, job.model, job.prompt, size, seed, strength, source_id, mask_id,
                      job.state, job.detail, now, json.dumps(chain, ensure_ascii=False)))
    JOBS[job.id] = job
    job.task = asyncio.create_task(run_job(job))
    log.info("задание %s: %s", job.id[:8], job.size)
    return {"id": job.id, "seed": seed}


@router.get("")
async def list_images(request: Request, before: "float | None" = None, limit: int = Query(60, ge=1, le=100)):
    """Лента: новые сверху, вместе с идущими; следующая страница — before=<created последней>."""
    uid = request.state.user.id
    if before is None:
        rows = state.DB.execute("SELECT * FROM images WHERE user_id = ? ORDER BY created DESC, id DESC LIMIT ?",
                                (uid, limit))
    else:
        rows = state.DB.execute("SELECT * FROM images WHERE user_id = ? AND created < ?"
                                " ORDER BY created DESC, id DESC LIMIT ?", (uid, before, limit))
    return {"items": [_out(r) for r in rows]}


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


@router.get("/descriptions/{job_id}")
async def description_status(job_id: str, request: Request):
    """Статус описания; готовый текст — в этом же ответе, один раз."""
    _prune()
    job = JOBS.get(job_id)
    if job is None or job.user_id != request.state.user.id or job.kind != "description":
        raise HTTPException(404, {"code": "gone", "message": "задание забыто или не существовало"})
    if job.state != "done":
        return _status(job)
    if job.description is None:
        raise HTTPException(404, {"code": "gone", "message": "описание уже выдано"})
    text, job.description = job.description, None
    return JSONResponse({"state": "done", "text": text, "truncated": job.truncated},
                        headers={"Cache-Control": "no-store"})


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
    if job.state == "working":
        job.state, job.detail = "cancelling", CANCELLING
        _store(job)
        log.info("задание %s: отменяется", job.id[:8])
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
            try:
                await media.delete_async(uid, media_id)
            except OSError:
                log.warning("картинка %s: файл media не удалился", image_id[:8])
    return Response(status_code=204)


# ---- шлюз ------------------------------------------------------------------

async def _slot(conn: Connection, path: str, timeout: float, **kw) -> httpx.Response:
    return await upstream.request(conn, "POST", connections.slot_url(conn, path), timeout=timeout, **kw)


async def release(conn: Connection, model: str) -> httpx.Response:
    return await _slot(conn, "/images/release", SHORT_TIMEOUT_SEC, json={"model": model})


async def release_description(conn: Connection) -> httpx.Response:
    return await _slot(conn, "/images/descriptions/release", SHORT_TIMEOUT_SEC, json={"model": DESCRIPTION_MODEL})


def _refused(conn: Connection) -> JobError:
    return JobError("refused", f"адрес подключения «{conn.name}» убран из списка администратора")


def _png(body: dict) -> "tuple[bytes, int | None]":
    data = body.get("data")
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    try:
        png = base64.b64decode(item["b64_json"], validate=True)
    except (KeyError, TypeError, binascii.Error, ValueError):
        raise JobError("no_image", "шлюз не вернул картинку")
    seed = item.get("seed")
    return png, seed if isinstance(seed, int) and not isinstance(seed, bool) else None


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


async def _generate_external(job: Job) -> "tuple[bytes, int | None]":
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
    except upstream.UpstreamRefused:
        raise _refused(job.conn) from None
    if r.status_code != 200:
        raise JobError(f"http_{r.status_code}", upstream.gateway_error(r, job.conn.name))
    body = upstream.json_body(r)
    data = body.get("data")
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if "b64_json" not in first and "url" in first:
        raise JobError("url", "сервис вернул ссылку, а не картинку")
    png, _seed = _png(body)
    return png, None


async def generate(job: Job) -> "tuple[bytes, int | None]":
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
    except upstream.UpstreamRefused:
        raise _refused(job.conn) from None
    if r.status_code != 200:
        raise JobError(f"http_{r.status_code}", upstream.gateway_error(r, job.conn.name))
    png, seed = _png(upstream.json_body(r))
    if job.mask is not None:
        png = await asyncio.to_thread(restrict_edit, job.image, png, job.mask)
    return png, seed


async def describe(job: Job):
    # Image slots hold leases for 600 s; release only idle image requests before switching.
    for model in MODELS.values():
        try:
            response = await release(job.conn, model)
        except httpx.HTTPError as e:
            raise JobError("gateway", f"шлюз недоступен: {type(e).__name__}") from None
        except upstream.UpstreamRefused:
            raise _refused(job.conn) from None
        except Exception:
            if state.DB.execute("SELECT 1 FROM connections WHERE id = ?", (job.conn.id,)).fetchone() is not None:
                raise
            raise JobError("connection", f"подключение «{job.conn.name}» удалено из профиля") from None
        if response.status_code != 200:
            raise JobError("release", "не удалось подготовить карту: " + upstream.gateway_error(response, job.conn.name))
    image = "data:image/png;base64," + base64.b64encode(job.image).decode("ascii")
    payload = {"model": DESCRIPTION_MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": DESCRIPTION_PROMPT},
        {"type": "image_url", "image_url": {"url": image}}]}],
        "temperature": 0.2, "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False}}
    job.description_started = True
    try:
        response = await _slot(job.conn, "/images/descriptions", GENERATE_TIMEOUT_SEC, json=payload)
    except httpx.TimeoutException:
        raise JobError("timeout", "шлюз не дождался описания") from None
    except httpx.HTTPError:
        raise JobError("gateway", "шлюз недоступен") from None
    except upstream.UpstreamRefused:
        raise _refused(job.conn) from None
    if response.status_code != 200:
        raise JobError(f"http_{response.status_code}", upstream.gateway_error(response, job.conn.name))
    choices = upstream.json_body(response).get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    text = (choice.get("message") or {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise JobError("no_description", "gemma-abl не вернула описание")
    job.description = text.strip()
    job.truncated = choice.get("finish_reason") == "length"


async def release_after_cancel(job: Job, *, cancelled=True) -> str:
    """Release модели отменённого задания сразу после конца его запроса. 409 — slot-gate ещё
    считает запрос (свой или чужой): повтор с паузой до CANCEL_RELEASE_SEC."""
    deadline = clock.NOW() + CANCEL_RELEASE_SEC
    prefix = "отменено" if cancelled else "описание готово"
    if job.conn.kind != "srvllm":                 # у внешнего сервиса карты студии нет
        return prefix
    while True:
        try:
            r = await release_description(job.conn) if job.kind == "description" else await release(job.conn, job.gateway_model)
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


async def _save(job: Job, png: bytes, seed: int) -> str:
    return await media.save_async(job.user_id, png, "image/png", f"studio-{job.model}-{seed}.png", thumb=True)


async def run_job(job: Job) -> None:
    outcome = None
    try:
        try:
            if job.kind == "description":
                await describe(job)
                outcome = ("done", "описание готово", None, None)
            else:
                png, seed = await generate(job)
                outcome = ("done", "готово", png, seed)
        except JobError as e:
            outcome = ("error", str(e), None, None)
            log.warning("задание %s: ошибка %s", job.id[:8], e.code)
        except Exception as e:                        # noqa: BLE001 — задание не должно висеть
            outcome = ("error", f"сбой службы: {type(e).__name__}", None, None)
            log.warning("задание %s: сбой %s", job.id[:8], type(e).__name__)
        description_cleanup = None
        if job.kind == "description" and job.description_started:
            description_cleanup = await release_after_cancel(job, cancelled=job.state == "cancelling")
            if outcome[0] == "done":
                outcome = ("done", description_cleanup, None, None)
        state_, detail, png, seed = outcome
        if png is not None and job.state != "cancelling":
            try:
                job.media_id = await _save(job, png, seed if seed is not None else job.seed)
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
                try:
                    await media.delete_async(job.user_id, mid)
                except OSError:
                    log.warning("задание %s: файл отменённой картинки не удалился", job.id[:8])
            # запрос кончился (картинкой или ошибкой) — картинку не выдаём, карту освобождаем
            job.detail = "запрос завершён — освобождаю карту"
            _store(job)
            if job.kind == "description":
                job.state, job.detail = "cancelled", (description_cleanup or "отменено").replace("описание готово", "отменено")
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
        if job.kind == "description":
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
