"""Страница студии studio-web — клиент API картинок шлюза (спеки в llm_routers:
docs/superpowers/specs/2026-10-16-srvllm-studio-design.md и
docs/superpowers/specs/2026-10-19-images-api-lustify-design.md, раздел 4).

Служба на процессоре debian, запущена всегда (до 2026-10-19 жила на srvllm). Карту, ComfyUI и
контроллер она не видит: генерирует через шлюз `STUDIO_GATEWAY_URL` (`http://<srvllm>:4000/slot`)
ключом владельца — тем же, что у приложений и пакетного скрипта.

Свойства:

1. Вход — ключ шлюза. Проверка — `GET /slot/models` этим ключом: 200 — вход, 401/403 —
   «неверный токен». Пять неверных токенов с адреса за 15 минут — 429 на 15 минут. Адрес —
   последний элемент X-Forwarded-For: путь к службе один, Apache debian (служба слушает только
   unix-сокет, войти в его каталог может группа www-data).
2. Где живёт ключ. Cookie сессии `v1.<срок>.<ключ base64url>.<HMAC>` — HttpOnly, Secure,
   SameSite=Strict, Path=/studio/, 30 дней; подпись — STUDIO_SESSION_SECRET. Почему так, а не
   хранилище сессий на сервере: ключ лежит только в браузере владельца (куда он его и ввёл) и в
   памяти службы на время запроса — на диске хоста его копии нет (там его прочла бы любая
   служба с доступом к файлу), а сессия переживает перезапуск службы без файла состояния. JavaScript страницы cookie не видит (HttpOnly), в
   журналы она не пишется (`--no-access-log`, журнал — только события). Подпись и срок
   проверяются на каждом запросе: смена STUDIO_SESSION_SECRET гасит все сессии разом, а
   отозванный у шлюза ключ даёт 401 на первом же вызове — и страница снова просит вход.
   Цена: украденная cookie — это сам ключ (его права — только картинки, ключ `owner-images`).
3. Генерация — фоновое задание: `POST /slot/images/generations` (b64_json, таймаут 1300 с —
   шлюз ждёт карту до budget_s маршрута и считает картинку). Через Apache (≤ 120 с) идут только
   короткие опросы; статус — «жду карту или генерирую» с прошедшим временем.
4. Одно задание за раз: второе получает 409 busy. Отмена запрос к шлюзу НЕ обрывает: LiteLLM
   обрыв клиента дальше не передаёт, генерация всё равно доходит до конца, а release на идущий
   через slot-gate запрос отвечает 409 slot_busy (ревью 2026-10-19). Поэтому задание становится
   «отменяется», ждёт конца своего запроса (если шлюз ещё ждёт карту — и её, и картинки),
   выбрасывает картинку и сразу зовёт `POST /slot/images/release` модели задания — на 409
   повторяет с паузой до CANCEL_RELEASE_SEC; новое задание до тех пор получает busy.
   «Освободить карту» — release обеих моделей, итог по каждой.
5. Картинка живёт в памяти до выдачи (один раз) или 600 с. Промпт и ключ в журнал не пишутся.
   Правка результата загружает blob из браузера повторно; size=original сохраняет размер.
   Необязательный PNG mask остаётся в студии: снаружи выделения результат берётся из исходника,
   модель получает только фото и инструкцию. Исходник и маска очищаются в finally задания.
6. Видео (спека 2026-10-19-srvllm-studio-video, раздел 6) — прокси API роликов шлюза
   (`/slot/videos`): задания и очередь живут у переводчика, студия своих заданий не ведёт.
   Картинка — поле `image` страницы (до 20 МБ и 50 Мп, нормализуется в PNG), шлюзу — поле
   `input_reference`. Прямой маршрут шлюза `Range` не пересылает, а Safari на iPhone без него
   ролик не играет: mp4 студия берёт у шлюза целиком и сама отвечает на диапазоны, держа в
   памяти до 3 роликов не дольше 10 минут. На диск ничего не пишется; в журнал — только id и
   параметры, без промпта, картинки и имён файлов.
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Literal
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from image_upload import (UploadError, VIDEO_PIXELS, VIDEO_UPLOAD, image_size, prepare_mask,  # noqa: E402
                          read_image_form, restrict_edit)
log = logging.getLogger("studio")

PREFIX = "/studio"
COOKIE = "studio_session"
SESSION_SEC = 30 * 86400
LOCK_FAILS, LOCK_SEC = 5, 15 * 60
LOGIN_BODY_MAX = 4096        # форма входа — одно поле токена
TOKEN_RE = re.compile(r"[\x21-\x7e]{1,512}")   # печатный ASCII без пробелов: иначе не заголовок
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
REQUIRED = ("STUDIO_SESSION_SECRET", "STUDIO_GATEWAY_URL")

#: Модели страницы: короткое имя → id у шлюза (контракт API картинок).
MODELS = {"lustify": "lustify-v10-krea2-turbo", "flux": "flux.2-klein-4b"}
#: Размеры: портрет, квадрат, альбом — около мегапикселя, кратны 16 (обе модели их держат).
SIZES = {"portrait": "832x1216", "square": "1024x1024", "landscape": "1216x832"}
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
#: Видео — контракт API видео (спека 2026-10-19-srvllm-studio-video, «Решения исполнителя»).
VIDEO_MODEL = "wan2.2-remix-i2v-v3"
VIDEO_ID_RE = re.compile(r"video_[0-9a-f]{24}")
# загрузка картинки и ответ `queued` — ролик шлюз не ждёт; mp4 до VIDEO_CONTENT_MAX — столько же
VIDEO_TIMEOUT_SEC = 120
VIDEO_CONTENT_MAX = 300 * 1024 * 1024
VIDEO_CACHE_ITEMS, VIDEO_CACHE_SEC = 3, 600
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Часы и пауза — через модуль: тесты подменяют их, а не реальное время.
NOW = time.time
SLEEP = asyncio.sleep

CFG: dict = {}
HTTP: "httpx.AsyncClient | None" = None


@dataclass
class Job:
    id: str
    key: str
    model: str
    prompt: str
    size: str
    seed: int
    started: float
    state: str = "working"
    detail: str = "жду карту или генерирую"
    png: "bytes | None" = None
    finished_at: "float | None" = None
    task: "asyncio.Task | None" = None
    image: bytes | None = None
    strength: float = 0.65
    mask: bytes | None = None
    kind: str = "image"
    description: str | None = None
    truncated: bool = False
    description_started: bool = False


JOBS: dict[str, Job] = {}
FAILS: dict[str, list[float]] = {}
#: id ролика → (когда взят, sha256 ключа, mp4); порядок — LRU. Ключ сверяется: чужая или
#: отозванная сессия из кэша ролик не получит.
VIDEO_CACHE: "OrderedDict[str, tuple[float, str, bytes]]" = OrderedDict()
VIDEO_FETCH = asyncio.Lock()        # Safari шлёт несколько Range сразу — ролик качается один раз


class JobError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---- сессия ----------------------------------------------------------------

def _sign(payload: str) -> str:
    return hmac.new(CFG["STUDIO_SESSION_SECRET"].encode("utf-8"), payload.encode("ascii"),
                    hashlib.sha256).hexdigest()


def new_session(key: str) -> str:
    exp = int(NOW()) + SESSION_SEC
    # base64url без `=`: со знаком `=` Starlette ставит значение cookie в кавычки
    packed = base64.urlsafe_b64encode(key.encode("ascii")).decode("ascii").rstrip("=")
    payload = f"v1.{exp}.{packed}"
    return f"{payload}.{_sign(payload)}"


def session_key(value: "str | None") -> "str | None":
    """Ключ из живой подписанной cookie или None."""
    payload, _, sig = (value or "").rpartition(".")
    version, _, rest = payload.partition(".")
    exp, _, packed = rest.partition(".")
    if version != "v1" or not exp.isdigit() or int(exp) <= NOW() or not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        return base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)).decode("ascii")
    except (binascii.Error, ValueError):
        return None


def client_ip(request: Request) -> str:
    """Последний элемент X-Forwarded-For — его дописал Apache; начало мог прислать сам клиент
    (как у панели LLM, llm_routers dashboard/app/auth.py::client_key)."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    return forwarded or (request.client.host if request.client else "?")


def locked(ip: str) -> bool:
    """Заодно забывает адреса без свежих неудач: иначе каждый, кто хоть раз открыл вход,
    оставался бы в FAILS навсегда."""
    now = NOW()
    for key in list(FAILS):
        FAILS[key] = [t for t in FAILS[key] if now - t < LOCK_SEC]
        if not FAILS[key]:
            del FAILS[key]
    return len(FAILS.get(ip, [])) >= LOCK_FAILS


# ---- шлюз ------------------------------------------------------------------

def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _message(response: httpx.Response) -> "str | None":
    """Сообщение отказа шлюза: OpenAI `error.message`, LiteLLM `detail` или None."""
    body = _json(response)
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    if not message and isinstance(body.get("detail"), dict):
        message = body["detail"].get("message") or body["detail"].get("error")
    message = message or body.get("detail")
    return str(message)[:300] if message else None


def _reason(response: httpx.Response) -> str:
    """Причина отказа шлюза: его сообщение или код."""
    return _message(response) or f"код {response.status_code}"


def gateway_error(response: httpx.Response) -> str:
    code = response.status_code
    if code == 401:
        return "шлюз не принял ключ — войдите заново"
    if code == 403:
        return "ключу закрыт этот маршрут шлюза"
    if code == 400:
        return f"шлюз отклонил запрос: {_reason(response)}"
    if code == 409:
        # slot-gate и переводчик говорят, чем занята карта («идут ролики: 2»)
        return _message(response) or "карта занята генерацией"
    if code == 503:
        return f"карта занята или недоступна: {_reason(response)}"
    if code in (502, 504):
        return f"шлюз не дождался службы: {_reason(response)}"
    return f"шлюз ответил {code}: {_reason(response)}"


async def release(key: str, model: str) -> httpx.Response:
    return await HTTP.post(CFG["STUDIO_GATEWAY_URL"] + "/images/release", json={"model": model},
                           headers=_auth(key), timeout=SHORT_TIMEOUT_SEC)


# ---- запуск ----------------------------------------------------------------

def load_config() -> dict:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"нет переменных окружения: {', '.join(missing)}")
    cfg = {k: os.environ[k] for k in REQUIRED}
    cfg["STUDIO_GATEWAY_URL"] = cfg["STUDIO_GATEWAY_URL"].rstrip("/")
    return cfg


def make_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(SHORT_TIMEOUT_SEC))


@asynccontextmanager
async def lifespan(_app):
    global HTTP
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    CFG.update(load_config())
    HTTP = make_http()
    try:
        yield
    finally:
        for job in JOBS.values():
            if job.task and not job.task.done():
                job.task.cancel()
        await HTTP.aclose()


app = FastAPI(title="studio",lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Все /studio/api/* — только с живой сессией; страница сама показывает вход."""
    if request.url.path.startswith(PREFIX + "/api/"):
        key = session_key(request.cookies.get(COOKIE))
        if key is None:
            return JSONResponse({"detail": {"code": "login", "message": "нужен вход"}}, status_code=401)
        request.state.key = key
    return await call_next(request)


def page(name: str, error: str = "") -> str:
    return (HERE / name).read_text(encoding="utf-8").replace("{{error}}", html.escape(error))


# ---- страница и вход -------------------------------------------------------

@app.get(PREFIX + "/health")
async def health():
    return {"status": "ok"}


@app.get(PREFIX)
async def bare():
    return RedirectResponse(PREFIX + "/", status_code=308)


@app.get(PREFIX + "/", response_class=HTMLResponse)
async def index(request: Request):
    name = "index.html" if session_key(request.cookies.get(COOKIE)) else "login.html"
    return HTMLResponse(page(name), headers={"Cache-Control": "no-store"})


async def small_body(request: Request, limit: int) -> bytes | None:
    """Тело не длиннее `limit` или None. Читается потоком: вход открыт без сессии, и ни
    Content-Length, ни chunked не заставят держать в памяти больше предела."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


def _login_page(error: str, status: int) -> HTMLResponse:
    return HTMLResponse(page("login.html", error), status_code=status)


@app.post(PREFIX + "/login")
async def login(request: Request):
    ip = client_ip(request)
    body = await small_body(request, LOGIN_BODY_MAX)
    if body is None:
        log.warning("вход с %s: тело длиннее %d байт", ip, LOGIN_BODY_MAX)
        return _login_page("Слишком длинный запрос", 413)
    token = (parse_qs(body.decode("utf-8", "replace")).get("token") or [""])[0].strip()
    if locked(ip):
        log.warning("вход с %s: заблокирован после %d неудач", ip, LOCK_FAILS)
        return _login_page("Слишком много попыток — подождите 15 минут", 429)
    # неудача записывается ДО вызова шлюза и снимается при успехе: пачка параллельных попыток
    # не проходит проверку блокировки разом, пока шлюз отвечает
    attempt = NOW()
    FAILS.setdefault(ip, []).append(attempt)
    code = None
    if TOKEN_RE.fullmatch(token):
        try:
            r = await HTTP.get(CFG["STUDIO_GATEWAY_URL"] + "/models", headers=_auth(token),
                               timeout=SHORT_TIMEOUT_SEC)
            code = r.status_code
        except httpx.HTTPError as e:
            code = type(e).__name__
    if code == 200:
        FAILS.pop(ip, None)
        log.info("вход с %s: успешно", ip)
        response = RedirectResponse(PREFIX + "/", status_code=303)
        response.set_cookie(COOKIE, new_session(token), max_age=SESSION_SEC, path=PREFIX + "/",
                            secure=True, httponly=True, samesite="strict")
        return response
    if code is None or code in (401, 403):
        log.warning("вход с %s: неверный токен", ip)
        return _login_page("Неверный токен", 401)
    # шлюз недоступен — не вина токена: попытка не считается
    if attempt in FAILS.get(ip, []):
        FAILS[ip].remove(attempt)
        if not FAILS[ip]:
            del FAILS[ip]
    log.warning("вход с %s: шлюз не ответил (%s)", ip, code)
    return _login_page("Шлюз недоступен — попробуйте позже", 502)


@app.post(PREFIX + "/logout")
async def logout():
    response = RedirectResponse(PREFIX + "/", status_code=303)
    response.delete_cookie(COOKIE, path=PREFIX + "/", secure=True, httponly=True, samesite="strict")
    return response


# ---- задания ---------------------------------------------------------------

class JobIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=24000)
    size: Literal["portrait", "square", "landscape", "original"]
    model: Literal["lustify", "flux"] = "lustify"
    seed: "int | None" = Field(default=None, ge=0, le=2**32 - 1)
    strength: float = Field(default=0.65, ge=0.05, le=1)


def _prune() -> None:
    now = NOW()
    for job_id in [j.id for j in JOBS.values() if j.finished_at is not None and now - j.finished_at > KEEP_DONE_SEC]:
        JOBS.pop(job_id, None)


def _job(job_id: str) -> Job:
    _prune()
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, {"code": "gone", "message": "задание забыто или не существовало"})
    return job


def _active() -> "Job | None":
    return next((j for j in JOBS.values() if j.state not in TERMINAL), None)


def _status(job: Job) -> dict:
    end = job.finished_at if job.finished_at is not None else NOW()
    return {"id": job.id, "state": job.state, "detail": job.detail, "seed": job.seed,
            "model": job.model, "kind": job.kind, "elapsed": int(max(0, end - job.started))}


def ensure_idle():
    _prune()
    active = _active()
    if active is not None:
        message = ("предыдущее задание отменяется — дождитесь освобождения карты"
                   if active.state == "cancelling" else "предыдущее задание ещё выполняется")
        raise HTTPException(409, {"code": "busy", "message": message,
                                  "id": active.id})


@app.post(PREFIX + "/api/jobs", status_code=202)
async def create_job(request: Request):
    ensure_idle()
    image, mask = None, None
    try:
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            extra_files = {}
            fields, image = await read_image_form(request, extra_files=extra_files)
            if "mask" in extra_files:
                mask = await asyncio.to_thread(prepare_mask, extra_files["mask"], image)
            if fields.get("seed") == "":
                fields.pop("seed")
            body = JobIn.model_validate(fields)
        else:
            raw = await small_body(request, 512 * 1024)
            if raw is None:
                raise HTTPException(413, "слишком большое тело запроса")
            body = JobIn.model_validate_json(raw)
        if body.size == "original" and image is None:
            raise HTTPException(422, {"code": "image", "message": "для исходного размера нужно фото"})
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    except ValidationError:
        raise HTTPException(422, {"code": "input", "message": "проверьте промпт, модель, размер, seed и силу изменения"}) from None
    # Parsing uploads yields to other requests: recheck before inserting the job.
    ensure_idle()
    seed = body.seed if body.seed is not None else secrets.randbelow(2**32)
    size = "{}x{}".format(*image_size(image)) if body.size == "original" else SIZES[body.size]
    job = Job(uuid.uuid4().hex, request.state.key, body.model, body.prompt, size, seed, NOW())
    job.image, job.strength = image, body.strength
    job.mask = mask
    JOBS[job.id] = job
    job.task = asyncio.create_task(run_job(job))
    log.info("задание %s: %s %s", job.id[:8], body.model, job.size)
    return {"id": job.id, "seed": seed}


@app.post(PREFIX + "/api/descriptions", status_code=202)
async def create_description(request: Request):
    ensure_idle()
    try:
        _, image = await read_image_form(request)
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    ensure_idle()
    job = Job(uuid.uuid4().hex, request.state.key, "gemma-abl", "", "", 0, NOW(),
              kind="description", image=image, detail="жду карту или описываю фото")
    JOBS[job.id] = job
    job.task = asyncio.create_task(run_job(job))
    log.info("задание %s: описание gemma-abl", job.id[:8])
    return {"id": job.id, "kind": "description"}


@app.get(PREFIX + "/api/jobs/current")
async def current_job():
    """Идущее задание — для страницы после перезагрузки вкладки (объявлено до `{job_id}`)."""
    _prune()
    job = _active()
    if job is None:
        raise HTTPException(404, {"code": "none", "message": "задания нет"})
    return _status(job)


@app.get(PREFIX + "/api/jobs/{job_id}")
async def job_status(job_id: str):
    return _status(_job(job_id))


@app.get(PREFIX + "/api/jobs/{job_id}/image")
async def job_image(job_id: str):
    job = _job(job_id)
    if job.png is None:
        raise HTTPException(404, {"code": "gone", "message": "картинка уже выдана или ещё не готова"})
    data, job.png = job.png, None
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store", "X-Seed": str(job.seed)})


@app.delete(PREFIX + "/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Запрос к шлюзу не обрывается (обрыв до slot-gate не доходит): задание ждёт его конца,
    выбрасывает картинку и освобождает карту само — см. run_job."""
    job = _job(job_id)
    if job.state == "working":
        job.state, job.detail = "cancelling", CANCELLING
        log.info("задание %s: отменяется", job.id[:8])
    return {"state": job.state}


@app.get(PREFIX + "/api/jobs/{job_id}/description")
async def job_description(job_id: str):
    job = _job(job_id)
    if job.description is None:
        raise HTTPException(404, {"code": "gone", "message": "описание уже выдано или ещё не готово"})
    text, job.description = job.description, None
    return JSONResponse({"text": text, "truncated": job.truncated, "model": "gemma-abl"},
                        headers={"Cache-Control": "no-store"})


@app.post(PREFIX + "/api/release")
async def release_card(request: Request):
    """Обе модели — двумя release: итог по каждой (освобождена / занята генерацией / ошибка)."""
    results = {}
    for name, model in (MODELS | {"gemma-abl": DESCRIPTION_MODEL}).items():
        try:
            r = await release_description(request.state.key) if name == "gemma-abl" else await release(request.state.key, model)
        except httpx.HTTPError as e:
            results[name] = {"state": "error", "message": f"шлюз недоступен: {type(e).__name__}"}
            continue
        if r.status_code == 401:
            raise HTTPException(401, {"code": "login", "message": gateway_error(r)})
        if r.status_code == 200:
            results[name] = {"state": "released", "message": "освобождена"}
        elif r.status_code == 409:
            # Lustify и видео — слот comfy: его освобождение решает переводчик («идут ролики: N»)
            results[name] = {"state": "busy", "message": _message(r) or "занята генерацией"}
        else:
            results[name] = {"state": "error", "message": gateway_error(r)}
    log.info("освобождение карты кнопкой: %s", ", ".join(f"{k} {v['state']}" for k, v in results.items()))
    return {"results": results}


# ---- видео -----------------------------------------------------------------

class VideoIn(BaseModel):
    prompt: str = Field(default="", max_length=4000)
    seconds: int = Field(default=5, ge=1, le=30)
    quality: Literal["fast", "standard", "high"] = "standard"
    smooth: bool = True
    seed: "int | None" = Field(default=None, ge=0, le=2**32 - 1)


def _video_id(video_id: str) -> str:
    """Чужое в путь шлюза не уходит: id — только формы контракта."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(404, {"code": "gone", "message": "нет такого ролика"})
    return video_id


def _unreachable(error: httpx.HTTPError) -> HTTPException:
    if isinstance(error, httpx.TimeoutException):
        return HTTPException(504, {"code": "timeout", "message": f"шлюз не ответил за {VIDEO_TIMEOUT_SEC} с"})
    return HTTPException(502, {"code": "gateway", "message": f"шлюз недоступен: {type(error).__name__}"})


def video_failure(response: httpx.Response) -> JSONResponse:
    """Отказ шлюза — с его кодом и понятными словами. 401 гасит сессию: иначе опрос списка
    роликов с отозванным ключом перезагружал бы страницу без конца."""
    code = response.status_code
    if code == 429:
        message = "очередь роликов полна — дождитесь готовых: " + _reason(response)
    elif code == 404:
        message = _message(response) or "ролик забыт или не существовал"
    else:
        message = gateway_error(response)
    failure = JSONResponse({"detail": {"code": "login" if code == 401 else "gateway", "message": message}},
                           status_code=code if 400 <= code < 600 else 502)
    if code == 401:
        failure.delete_cookie(COOKIE, path=PREFIX + "/", secure=True, httponly=True, samesite="strict")
    return failure


async def _video_call(method: str, path: str, key: str, **kwargs) -> httpx.Response:
    try:
        return await HTTP.request(method, CFG["STUDIO_GATEWAY_URL"] + "/videos" + path, headers=_auth(key),
                                  timeout=VIDEO_TIMEOUT_SEC, **kwargs)
    except httpx.HTTPError as e:
        raise _unreachable(e) from None


def _video_json(response: httpx.Response) -> Response:
    if not response.is_success:
        return video_failure(response)
    body = _json(response)
    if not body:
        return JSONResponse({"detail": {"code": "gateway", "message": "шлюз вернул не JSON"}}, status_code=502)
    return JSONResponse(body, status_code=response.status_code, headers={"Cache-Control": "no-store"})


@app.post(PREFIX + "/api/videos")
async def create_video(request: Request):
    try:
        fields, image = await read_image_form(request, max_upload=VIDEO_UPLOAD, max_pixels=VIDEO_PIXELS)
        # пустое поле — «по умолчанию»: seed случайный, длительность 5 с
        body = VideoIn.model_validate({k: v for k, v in fields.items() if v != ""})
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    except ValidationError:
        raise HTTPException(422, {"code": "input", "message": "проверьте промпт (до 4000 знаков), длительность "
                                  "(целые 1–30 с), качество, дорисовку и seed"}) from None
    form = {"model": VIDEO_MODEL, "seconds": str(body.seconds), "quality": body.quality,
            "smooth": "true" if body.smooth else "false"}
    if body.prompt.strip():
        form["prompt"] = body.prompt.strip()
    if body.seed is not None:
        form["seed"] = str(body.seed)
    # имя файла своё: имя с устройства владельца дальше студии не уходит
    r = await _video_call("POST", "", request.state.key, data=form,
                          files={"input_reference": ("input.png", image, "image/png")})
    if r.is_success:
        log.info("ролик %s: создан, %d с, %s", str(_json(r).get("id"))[:14], body.seconds, body.quality)
    else:
        log.warning("ролик не создан: шлюз ответил %s", r.status_code)
    return _video_json(r)


@app.get(PREFIX + "/api/videos")
async def list_videos(request: Request):
    return _video_json(await _video_call("GET", "", request.state.key))


@app.get(PREFIX + "/api/videos/{video_id}")
async def video_status(video_id: str, request: Request):
    return _video_json(await _video_call("GET", "/" + _video_id(video_id), request.state.key))


@app.delete(PREFIX + "/api/videos/{video_id}")
async def delete_video(video_id: str, request: Request):
    video_id = _video_id(video_id)
    VIDEO_CACHE.pop(video_id, None)
    r = await _video_call("DELETE", "/" + video_id, request.state.key)
    log.info("ролик %s: удаление — шлюз ответил %s", video_id[:14], r.status_code)
    return _video_json(r)


def _cached_video(video_id: str, owner: str) -> "bytes | None":
    now = NOW()
    for stale in [v for v, (taken, _, _) in VIDEO_CACHE.items() if now - taken > VIDEO_CACHE_SEC]:
        del VIDEO_CACHE[stale]
    hit = VIDEO_CACHE.get(video_id)
    if hit is None or not hmac.compare_digest(hit[1], owner):
        return None
    VIDEO_CACHE.move_to_end(video_id)
    return hit[2]


async def _video_mp4(key: str, video_id: str) -> "bytes | Response":
    """mp4 ролика из кэша или целиком у шлюза (он `Range` не пересылает)."""
    owner = hashlib.sha256(key.encode("ascii")).hexdigest()
    data = _cached_video(video_id, owner)
    if data is not None:
        return data
    async with VIDEO_FETCH:
        data = _cached_video(video_id, owner)
        if data is not None:
            return data
        body = bytearray()
        try:
            async with HTTP.stream("GET", f"{CFG['STUDIO_GATEWAY_URL']}/videos/{video_id}/content",
                                   headers=_auth(key), timeout=VIDEO_TIMEOUT_SEC) as r:
                if r.status_code != 200:
                    await r.aread()
                    return video_failure(r)
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > VIDEO_CONTENT_MAX:
                        log.warning("ролик %s: mp4 больше %d МиБ", video_id[:14], VIDEO_CONTENT_MAX >> 20)
                        raise HTTPException(502, {"code": "too_big",
                                                  "message": f"ролик больше {VIDEO_CONTENT_MAX >> 20} МиБ"})
        except httpx.HTTPError as e:
            raise _unreachable(e) from None
        data = bytes(body)
        VIDEO_CACHE[video_id] = (NOW(), owner, data)
        while len(VIDEO_CACHE) > VIDEO_CACHE_ITEMS:
            VIDEO_CACHE.popitem(last=False)
    log.info("ролик %s: mp4 %d КиБ взят у шлюза", video_id[:14], len(data) >> 10)
    return data


def byte_range(header: str, size: int) -> "tuple[int, int] | None":
    """Один диапазон `bytes=a-b`, `a-` или `-n` → (начало, конец включительно); None — 416."""
    match = RANGE_RE.fullmatch(header.strip())
    if match is None or match.groups() == ("", ""):
        return None
    first, last = match.groups()
    if first == "":
        if int(last) == 0:
            return None
        return max(0, size - int(last)), size - 1
    start, end = int(first), int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


async def _video_seed(key: str, video_id: str) -> str:
    """Seed ролика для имени файла; не узнали — id."""
    try:
        r = await _video_call("GET", "/" + video_id, key)
    except HTTPException:
        return video_id
    seed = _json(r).get("seed") if r.is_success else None
    return str(seed) if isinstance(seed, int) and not isinstance(seed, bool) else video_id


@app.get(PREFIX + "/api/videos/{video_id}/content")
async def video_content(video_id: str, request: Request, download: str = ""):
    """Safari на iPhone без `Range` ролик не играет: диапазоны — здесь, из mp4 в памяти."""
    video_id = _video_id(video_id)
    data = await _video_mp4(request.state.key, video_id)
    if isinstance(data, Response):
        return data
    size = len(data)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if download == "1":
        name = await _video_seed(request.state.key, video_id)
        headers["Content-Disposition"] = f'attachment; filename="studio-video-{name}.mp4"'
    spec = request.headers.get("range")
    if spec is None:
        return Response(data, media_type="video/mp4", headers=headers)
    span = byte_range(spec, size)
    if span is None:
        return Response(status_code=416, headers=headers | {"Content-Range": f"bytes */{size}"})
    start, end = span
    return Response(data[start:end + 1], status_code=206, media_type="video/mp4",
                    headers=headers | {"Content-Range": f"bytes {start}-{end}/{size}"})


def _png(body: dict) -> "tuple[bytes, int | None]":
    data = body.get("data")
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    try:
        png = base64.b64decode(item["b64_json"], validate=True)
    except (KeyError, TypeError, binascii.Error, ValueError):
        raise JobError("no_image", "шлюз не вернул картинку")
    seed = item.get("seed")
    return png, seed if isinstance(seed, int) and not isinstance(seed, bool) else None


async def generate(job: Job) -> "tuple[bytes, int | None]":
    payload = {"model": MODELS[job.model], "prompt": job.prompt, "n": 1, "size": job.size,
               "seed": job.seed, "response_format": "b64_json"}
    try:
        if job.image is not None:
            if job.model == "lustify":
                payload["strength"] = job.strength
            r = await HTTP.post(CFG["STUDIO_GATEWAY_URL"] + "/images/edits",
                                data={k: str(v) for k, v in payload.items()},
                                files={"image": ("input.png", job.image, "image/png")},
                                headers=_auth(job.key), timeout=GENERATE_TIMEOUT_SEC)
        else:
            r = await HTTP.post(CFG["STUDIO_GATEWAY_URL"] + "/images/generations", json=payload,
                                headers=_auth(job.key), timeout=GENERATE_TIMEOUT_SEC)
    except httpx.TimeoutException:
        raise JobError("timeout", f"шлюз не ответил за {GENERATE_TIMEOUT_SEC} с")
    except httpx.HTTPError as e:
        raise JobError("gateway", f"шлюз недоступен: {type(e).__name__}")
    if r.status_code != 200:
        raise JobError(f"http_{r.status_code}", gateway_error(r))
    png, seed = _png(_json(r))
    if job.mask is not None:
        png = await asyncio.to_thread(restrict_edit, job.image, png, job.mask)
    return png, seed


async def release_description(key):
    return await HTTP.post(CFG["STUDIO_GATEWAY_URL"] + "/images/descriptions/release",
                           json={"model": DESCRIPTION_MODEL}, headers=_auth(key), timeout=SHORT_TIMEOUT_SEC)


async def describe(job: Job):
    # Image slots hold leases for 600 s; release only idle image requests before switching.
    for model in MODELS.values():
        response = await release(job.key, model)
        if response.status_code != 200:
            raise JobError("release", "не удалось подготовить карту: " + gateway_error(response))
    image = "data:image/png;base64," + base64.b64encode(job.image).decode("ascii")
    payload = {"model": DESCRIPTION_MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": DESCRIPTION_PROMPT},
        {"type": "image_url", "image_url": {"url": image}}]}],
        "temperature": 0.2, "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False}}
    job.description_started = True
    try:
        response = await HTTP.post(CFG["STUDIO_GATEWAY_URL"] + "/images/descriptions", json=payload,
                                   headers=_auth(job.key), timeout=GENERATE_TIMEOUT_SEC)
    except httpx.TimeoutException:
        raise JobError("timeout", "шлюз не дождался описания") from None
    except httpx.HTTPError:
        raise JobError("gateway", "шлюз недоступен") from None
    if response.status_code != 200:
        raise JobError(f"http_{response.status_code}", gateway_error(response))
    choices = _json(response).get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    text = (choice.get("message") or {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise JobError("no_description", "gemma-abl не вернула описание")
    job.description = text.strip()
    job.truncated = choice.get("finish_reason") == "length"


async def release_after_cancel(job: Job, *, cancelled=True) -> str:
    """Release модели отменённого задания сразу после конца его запроса. 409 — slot-gate ещё
    считает запрос (свой или чужой): повтор с паузой до CANCEL_RELEASE_SEC."""
    deadline = NOW() + CANCEL_RELEASE_SEC
    prefix = "отменено" if cancelled else "описание готово"
    while True:
        try:
            r = await release_description(job.key) if job.kind == "description" else await release(job.key, MODELS[job.model])
        except httpx.HTTPError as e:
            log.warning("задание %s: release не прошёл (%s)", job.id[:8], type(e).__name__)
            return f"{prefix}; карту освободить не удалось — шлюз недоступен: {type(e).__name__}"
        if r.status_code == 200:
            log.info("задание %s: %s, карта освобождена", job.id[:8], prefix)
            return prefix + " — карта освобождена"
        if r.status_code != 409:
            log.warning("задание %s: release ответил %s", job.id[:8], r.status_code)
            return f"{prefix}; карту освободить не удалось — {gateway_error(r)}"
        if NOW() >= deadline:
            log.warning("задание %s: release — 409 дольше %d с", job.id[:8], CANCEL_RELEASE_SEC)
            return prefix + "; карта занята другим запросом — освободится сама по окончании аренды"
        await SLEEP(RELEASE_PAUSE_SEC)


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
        if job.state == "cancelling":
            job.description = None
            # запрос кончился (картинкой или ошибкой) — картинку не выдаём, карту освобождаем
            job.detail = "запрос завершён — освобождаю карту"
            if job.kind == "description":
                job.state, job.detail = "cancelled", (description_cleanup or "отменено").replace("описание готово", "отменено")
            else:
                job.state, job.detail = "cancelled", await release_after_cancel(job)
            return
        state, job.detail, job.png, seed = outcome
        if seed is not None:
            job.seed = seed
        job.state = state                             # последним: страница берёт картинку по «done»
        if state == "done":
            log.info("задание %s: готово", job.id[:8])
    except asyncio.CancelledError:                    # остановка службы
        job.description = None
        job.state, job.detail, job.png = "cancelled", "отменено остановкой студии", None
        raise
    finally:
        job.image = None
        job.mask = None
        job.prompt = job.key = ""
        job.finished_at = NOW()
