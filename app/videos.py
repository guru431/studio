"""Ролики (спека 2026-10-19-studio-pwa): задания у шлюза srvllm (`/slot/videos`) через
видео-подключение пользователя; строка `videos` — запись ленты. Перенос роликов прежнего server.py,
где студия была прокси списка шлюза; теперь задания свои:

1. Создание: фото с устройства (сохраняется в media с превью) или фото истории (`source_media_id`),
   нормализованное normalize_image, уходит шлюзу формой с именем файла `input.png` — имя с
   устройства дальше студии не идёт. Строка появляется только после ответа шлюза с его id.
   Seed выбирает студия (пусто — случайный): ответ на создание потерялся (таймаут, обрыв, 502/504
   перед шлюзом), а шлюз задание мог принять — студия ищет его в списке шлюза по seed, длительности,
   качеству и времени (`_adopt`) и берёт, только если такое ровно одно и его нет у другой строки.
   Больше список шлюза (`GET /slot/videos`) студия не читает: чужие задания в ленту не попадают.
2. Наблюдатель службы (`start_watcher`) раз в POLL_SEC опрашивает шлюз по всем идущим строкам —
   вкладка не нужна, после перезапуска опрос продолжается. Готовый mp4 (до VIDEO_CONTENT_MAX)
   идёт потоком со шлюза на шару (`media.save_stream`) и снимается у шлюза. Шлюз или шара
   недоступны — строка не меняется до следующего прохода; 401 — `failed` со словами о профиле,
   сессия студии жива.
3. Отмена и удаление идущего ролика сначала снимают его у шлюза. Строки меняются условием
   «ещё идёт»: отмена посреди скачивания выбрасывает скачанное.
4. База — только с цикла событий. Журнал — только id и коды: промпт, токен, картинки, ролики и
   имена файлов туда не пишутся.
"""
import asyncio
import json
import logging
import re
import secrets
import uuid
from typing import AsyncIterator, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

import chains
import clock
import connections
import forms
import media
import sections
import state
import upstream
from connections import Connection
from image_upload import MAX_BODY, MAX_UPLOAD, VIDEO_PIXELS, VIDEO_UPLOAD, UploadError

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/videos")

#: Контракт API видео шлюза (спека 2026-10-19-srvllm-studio-video, «Решения исполнителя»).
VIDEO_MODEL = "wan2.2-remix-i2v-v3"
VIDEO_ID_RE = re.compile(r"video_[0-9a-f]{24}")
# загрузка картинки и ответ `queued` — ролик шлюз не ждёт; чтение mp4 — тот же срок на кусок
VIDEO_TIMEOUT_SEC = 120
# поиск задания, ответ на создание которого потерялся: список шлюза — один короткий запрос
ADOPT_TIMEOUT_SEC = 20
# часы шлюза и студии разные: задание считается созданным этим запросом с таким запасом
ADOPT_SKEW_SEC = 300
VIDEO_CONTENT_MAX = 300 * 1024 * 1024
POLL_SEC = 5
BODY_MAX = VIDEO_UPLOAD + (MAX_BODY - MAX_UPLOAD)
ACTIVE = ("queued", "in_progress")
WHERE_ACTIVE = "state IN ('queued', 'in_progress')"
FORGOTTEN = "шлюз забыл задание"
RETRYING = "не удалось сохранить файл — повторяю"
UNSAVED = "не удалось сохранить файл: хранилище недоступно"


class VideoIn(BaseModel):
    connection_id: "int | None" = None
    model: "str | None" = Field(default=None, max_length=200)
    prompt: str = Field(default="", max_length=4000)
    seconds: int = Field(default=5, ge=1, le=30)
    quality: Literal["fast", "standard", "high"] = "standard"
    smooth: bool = True
    seed: "int | None" = Field(default=None, ge=0, le=2**32 - 1)


class TooBig(Exception):
    """mp4 у шлюза больше VIDEO_CONTENT_MAX."""


# ---- строки ------------------------------------------------------------------

def _row(user_id: int, video_id: str):
    row = state.DB.execute("SELECT * FROM videos WHERE id = ? AND user_id = ?", (video_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого ролика нет"})
    return row


def _out(row, with_chain=True) -> dict:
    """Запись ленты; цепочка промптов — только у одной записи, в ленте — число шагов (как у картинок)."""
    end = row["finished"] if row["finished"] is not None else clock.NOW()
    chain = chains.of_video(row["user_id"], row)
    out = {"id": row["id"], "state": row["state"], "progress": row["progress"], "detail": row["detail"],
           "prompt": row["prompt"], "prompt_source": row["prompt_source"], "seconds": row["seconds"],
           "quality": row["quality"], "smooth": bool(row["smooth"]), "seed": row["seed"],
           "source_media_id": row["source_media_id"], "media_id": row["media_id"], "created": row["created"],
           "elapsed": int(max(0, end - row["created"])), "steps": len(chain)}
    if with_chain:
        out["chain"] = chains.annotate(row["user_id"], chain)
    return out


def _settle(video_id: str, state_: str, detail: str, media_id: "str | None" = None) -> bool:
    """Конец ролика, если он ещё идёт; False — его уже отменили или удалили."""
    progress = ", progress = 100" if state_ == "done" else ""
    return state.DB.execute(f"UPDATE videos SET state = ?, detail = ?, media_id = ?, finished = ?{progress}"
                            f" WHERE id = ? AND {WHERE_ACTIVE}",
                            (state_, detail, media_id, clock.NOW(), video_id)).rowcount == 1


def _int(value, default):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _text(value, default):
    return value if isinstance(value, str) and value.strip() else default


def _connection(row) -> "Connection | None":
    try:
        return connections.get(row["user_id"], row["connection_id"])
    except HTTPException:                         # подключение удалили из профиля
        return None


# ---- шлюз ------------------------------------------------------------------

def _url(conn: Connection, gateway_id: str, tail: str = "") -> str:
    return connections.slot_url(conn, f"/videos/{gateway_id}{tail}")


def _unreachable(e: Exception, conn: Connection) -> HTTPException:
    if isinstance(e, upstream.UpstreamRefused):
        return HTTPException(409, {"code": "refused", "message": upstream.refused_words(e, conn)})
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, {"code": "timeout", "message": f"шлюз не ответил за {VIDEO_TIMEOUT_SEC} с"})
    return HTTPException(502, {"code": "gateway", "message": f"шлюз недоступен: {type(e).__name__}"})


def _refusal(r: httpx.Response, conn: Connection) -> HTTPException:
    """Отказ шлюза — с его кодом и понятными словами; 401 — 502 со словами о профиле: сессия
    студии ни при чём, страница не должна уводить на вход."""
    code = r.status_code
    if code == 429:
        message = "очередь роликов полна — дождитесь готовых: " + (upstream.message(r) or "код 429")
    else:
        message = upstream.gateway_error(r, conn.name)
    return HTTPException(code if code in (400, 409, 429, 503, 504) else 502, {"code": "gateway", "message": message})


async def _gateway_delete(conn: Connection, gateway_id: str) -> httpx.Response:
    return await upstream.request(conn, "DELETE", _url(conn, gateway_id), timeout=VIDEO_TIMEOUT_SEC)


async def _drop(row) -> "str | None":
    """Снять ролик у шлюза: None — снят или снимать нечем, иначе — слова отказа."""
    conn = _connection(row)
    if conn is None or not row["gateway_id"]:
        return None
    try:
        r = await _gateway_delete(conn, row["gateway_id"])
    except upstream.UpstreamRefused:
        return None                               # адрес убран из списка — снять нельзя
    except httpx.HTTPError as e:
        return f"шлюз недоступен: {type(e).__name__}"
    if r.is_success or r.status_code == 404:
        return None
    return upstream.gateway_error(r, conn.name)


async def _drop_quietly(conn: Connection, gateway_id: str, video_id: str) -> None:
    try:
        r = await _gateway_delete(conn, gateway_id)
    except Exception as e:                        # noqa: BLE001 — шлюз сам забудет ролик по сроку
        log.warning("ролик %s: у шлюза не снят (%s)", video_id[:8], type(e).__name__)
        return
    if not (r.is_success or r.status_code == 404):
        log.warning("ролик %s: у шлюза не снят — код %s", video_id[:8], r.status_code)


async def cancel_user(user_id: int) -> None:
    """Все идущие ролики пользователя — снять у шлюза и пометить отменёнными (стирание истории,
    удаление и отключение учётки); отказ шлюза не мешает — забытое он снимет сам по сроку."""
    rows = state.DB.execute(f"SELECT * FROM videos WHERE user_id = ? AND {WHERE_ACTIVE}"
                            " AND gateway_id IS NOT NULL", (user_id,)).fetchall()
    for row in rows:
        conn = _connection(row)
        if conn is not None:
            await _drop_quietly(conn, row["gateway_id"], row["id"])
        _settle(row["id"], "cancelled", "отменено")


# ---- ввод ------------------------------------------------------------------

def _video_connection(user_id: int, connection_id: "int | None" = None, model: "str | None" = None) -> Connection:
    """Подключение раздела «Видео» (sections.resolve): модель — только `wan` шлюза srvllm."""
    conn, _model = sections.resolve(user_id, "video", connection_id, model)
    return conn


async def _adopt(conn: Connection, form: dict, since: float) -> "dict | None":
    """Задание этого запроса в списке шлюза (п. 1 шапки): тот же seed, длительность, качество и
    дорисовка, создано не раньше запроса и не принадлежит другой строке. Не ровно одно — None."""
    try:
        r = await upstream.request(conn, "GET", connections.slot_url(conn, "/videos"), timeout=ADOPT_TIMEOUT_SEC)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return None
    data = upstream.json_body(r).get("data") if r.is_success else None
    found = [v for v in (data if isinstance(data, list) else [])
             if isinstance(v, dict) and isinstance(v.get("id"), str) and VIDEO_ID_RE.fullmatch(v["id"])
             and str(v.get("seed")) == form["seed"] and str(v.get("seconds")) == form["seconds"]
             and v.get("quality") == form["quality"] and v.get("smooth") is (form["smooth"] == "true")
             and _int(v.get("created_at"), 0) >= since - ADOPT_SKEW_SEC
             and state.DB.execute("SELECT 1 FROM videos WHERE gateway_id = ?", (v["id"],)).fetchone() is None]
    return found[0] if len(found) == 1 else None


async def _forget(user_id: int, media_id: str) -> None:
    await media.delete_async(user_id, media_id)


# ---- API -------------------------------------------------------------------

@router.post("", status_code=201)
async def create_video(request: Request):
    uid = request.state.user.id
    _video_connection(uid)                       # раздел без модели — отказ до чтения тела
    try:
        # разбор формы до 20 МиБ — доли секунды: в потоке (базы он не касается)
        fields, upload, _ = await asyncio.to_thread(forms.decode, await forms.read_body(request, BODY_MAX),
                                                    request.headers.get("content-type", ""), max_upload=VIDEO_UPLOAD)
        source_id = fields.pop("source_media_id", None)
        # пустое поле — «по умолчанию»: seed случайный, длительность 5 с
        body = VideoIn.model_validate({k: v for k, v in fields.items() if v != ""})
        conn = _video_connection(uid, body.connection_id, body.model)
        image = await forms.photo(uid, upload, source_id, VIDEO_PIXELS)
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    except ValidationError:
        raise HTTPException(422, {"code": "input", "message": "проверьте промпт (до 4000 знаков), длительность "
                                  "(целые 1–30 с), качество, дорисовку и seed"}) from None
    if upload is not None:
        try:
            source_id = await media.save_async(uid, image, "image/png", "studio-video-source.png", thumb=True)
        except media.StorageError as e:
            raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    seed = body.seed if body.seed is not None else secrets.randbelow(2**32)
    form = {"model": VIDEO_MODEL, "seconds": str(body.seconds), "quality": body.quality,
            "smooth": "true" if body.smooth else "false", "seed": str(seed)}
    if body.prompt.strip():
        form["prompt"] = body.prompt.strip()
    failure, answer, lost, started = None, {}, False, clock.NOW()
    try:
        r = await upstream.request(conn, "POST", connections.slot_url(conn, "/videos"), timeout=VIDEO_TIMEOUT_SEC,
                                   data=form, files={"input_reference": ("input.png", image, "image/png")})
    except (httpx.HTTPError, upstream.UpstreamRefused) as e:
        failure = _unreachable(e, conn)
        # соединения не было — задания у шлюза нет; иначе запрос мог дойти, а ответ — потеряться
        lost = isinstance(e, httpx.HTTPError) and not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout,
                                                                      httpx.PoolTimeout))
    else:
        answer = upstream.json_body(r)
        if not r.is_success:
            failure = _refusal(r, conn)
            lost = r.status_code in (502, 504)          # отказ прокси перед переводчиком
        elif not isinstance(answer.get("id"), str) or not VIDEO_ID_RE.fullmatch(answer["id"]):
            failure = HTTPException(502, {"code": "gateway", "message": "шлюз вернул ролик без id"})
    if lost and (found := await _adopt(conn, form, started)) is not None:
        log.warning("ролик пользователя %d: ответ шлюза потерялся — задание найдено в его списке", uid)
        failure, answer = None, found
    if failure is not None:
        log.warning("ролик пользователя %d не создан: %s", uid, failure.status_code)
        if upload is not None:
            await _forget(uid, source_id)
        raise failure
    status = answer.get("status")
    video_id, now = uuid.uuid4().hex, clock.NOW()
    if not media.alive(uid, source_id):           # фото удалили, пока шлюз принимал ролик
        await _drop_quietly(conn, answer["id"], video_id)
        raise HTTPException(404, media.GONE)
    prompt_source = _text(answer.get("prompt_source"), None)
    chain = chains.of_source(uid, source_id) + [chains.video_step(
        video_id=video_id, prompt=body.prompt.strip(), prompt_source=prompt_source, seconds=body.seconds,
        quality=body.quality, created=now)]
    state.DB.execute(
        "INSERT INTO videos(id, user_id, connection_id, gateway_id, source_media_id, prompt, prompt_source, seconds,"
        " quality, smooth, seed, state, progress, detail, created, chain)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (video_id, uid, conn.id, answer["id"], source_id, body.prompt.strip(), prompt_source,
         body.seconds, body.quality, int(body.smooth), _int(answer.get("seed"), seed),
         status if status in ACTIVE else "queued", min(100, max(0, _int(answer.get("progress"), 0))),
         _text(answer.get("detail"), "в очереди"), now, json.dumps(chain, ensure_ascii=False)))
    log.info("ролик %s: создан, %d с, %s", video_id[:8], body.seconds, body.quality)
    return _out(_row(uid, video_id))


@router.get("")
async def list_videos(request: Request, before: "float | None" = None, limit: int = Query(60, ge=1, le=100)):
    """Лента: новые сверху, вместе с идущими; следующая страница — before=<created последней>."""
    uid = request.state.user.id
    if before is None:
        rows = state.DB.execute("SELECT * FROM videos WHERE user_id = ? ORDER BY created DESC, id DESC LIMIT ?",
                                (uid, limit))
    else:
        rows = state.DB.execute("SELECT * FROM videos WHERE user_id = ? AND created < ?"
                                " ORDER BY created DESC, id DESC LIMIT ?", (uid, before, limit))
    return {"items": [_out(r, with_chain=False) for r in rows]}


@router.get("/{video_id}")
async def video_status(video_id: str, request: Request):
    return _out(_row(request.state.user.id, video_id))


@router.post("/{video_id}/cancel")
async def cancel_video(video_id: str, request: Request):
    """Снять у шлюза и пометить отменённым; шлюз недоступен — 502, ролик идёт дальше."""
    uid = request.state.user.id
    row = _row(uid, video_id)
    if row["state"] in ACTIVE:
        words = await _drop(row)
        if words is not None:
            raise HTTPException(502, {"code": "gateway", "message": words})
        if _settle(video_id, "cancelled", "отменено"):
            log.info("ролик %s: отменён", video_id[:8])
    return _out(_row(uid, video_id))


@router.delete("/{video_id}", status_code=204)
async def delete_video(video_id: str, request: Request):
    """Идущий — сначала снять у шлюза; затем строка и её файлы (кроме тех, на что ссылается другая запись)."""
    uid = request.state.user.id
    row = _row(uid, video_id)
    if row["state"] in ACTIVE:
        words = await _drop(row)
        if words is not None:
            raise HTTPException(502, {"code": "gateway", "message": words})
    row = _row(uid, video_id)                     # наблюдатель мог успеть положить ролик
    state.DB.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    for media_id in {row["media_id"], row["source_media_id"]} - {None}:
        if not media.referenced(uid, media_id):
            await media.delete_async(uid, media_id)
    return Response(status_code=204)


# ---- наблюдатель -------------------------------------------------------------

async def _limited(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    size = 0
    async for chunk in chunks:
        size += len(chunk)
        if size > VIDEO_CONTENT_MAX:
            raise TooBig()
        yield chunk


async def _fail(row, detail: str, conn: "Connection | None" = None) -> None:
    """Ролик не получился; шлюз, который его ещё помнит (conn), снимает его — не держит в очереди."""
    if _settle(row["id"], "failed", detail):
        log.info("ролик %s: не получился", row["id"][:8])
    if conn is not None:
        await _drop_quietly(conn, row["gateway_id"], row["id"])


def _update(row, body: dict) -> "int | None":
    """Ход ролика по ответу шлюза; пустые поля прежних значений не стирают. Итог — seed."""
    status = body.get("status")
    seed = _int(body.get("seed"), row["seed"])
    state.DB.execute(f"UPDATE videos SET state = ?, progress = ?, detail = ?, prompt = ?, prompt_source = ?, seed = ?"
                     f" WHERE id = ? AND {WHERE_ACTIVE}",
                     (status if status in ACTIVE else row["state"],
                      min(100, max(0, _int(body.get("progress"), row["progress"]))),
                      _text(body.get("detail"), row["detail"]), _text(body.get("prompt"), row["prompt"]),
                      _text(body.get("prompt_source"), row["prompt_source"]), seed, row["id"]))
    return seed


def _failure_words(body: dict) -> str:
    error = body.get("error")
    text = error.get("message") if isinstance(error, dict) else error
    text = text or body.get("detail")
    return f"ролик не получился: {str(text)[:300]}" if text else "ролик не получился"


def _forgotten(row) -> str:
    """Шлюз ролика не знает: забыл сам — или готовый ролик не лёг на шару, пока шлюз его держал."""
    return UNSAVED if row["detail"] == RETRYING else FORGOTTEN


async def _fetch(row, conn: Connection, seed: "int | None") -> None:
    """Готовый mp4 — потоком со шлюза на шару, затем снять у шлюза и пометить готовым."""
    uid, gateway_id = row["user_id"], row["gateway_id"]
    name = f"studio-video-{seed if seed is not None else gateway_id}.mp4"
    try:
        async with upstream.stream(conn, "GET", _url(conn, gateway_id, "/content"),
                                   timeout=VIDEO_TIMEOUT_SEC) as r:
            if r.status_code != 200:
                await r.aread()
                if r.status_code == 404:
                    await _fail(row, _forgotten(row))
                elif r.status_code in (401, 403):
                    await _fail(row, upstream.gateway_error(r, conn.name))
                else:
                    log.warning("ролик %s: mp4 не отдан — код %s", row["id"][:8], r.status_code)
                return
            media_id = await media.save_stream(uid, _limited(r.aiter_bytes()), "video/mp4", name,
                                               max_bytes=VIDEO_CONTENT_MAX)
    except TooBig:
        log.warning("ролик %s: mp4 больше %d МиБ", row["id"][:8], VIDEO_CONTENT_MAX >> 20)
        await _fail(row, f"ролик больше {VIDEO_CONTENT_MAX >> 20} МиБ — в историю не взят", conn)
        return
    except media.StorageError:
        log.warning("ролик %s: шара недоступна — следующий проход", row["id"][:8])
        state.DB.execute(f"UPDATE videos SET detail = ? WHERE id = ? AND {WHERE_ACTIVE}", (RETRYING, row["id"]))
        return
    except httpx.HTTPError as e:
        log.info("ролик %s: mp4 не скачан (%s) — следующий проход", row["id"][:8], type(e).__name__)
        return
    # сначала «готово» в базе, потом снять у шлюза: обрыв между ними не теряет скачанный ролик
    if not _settle(row["id"], "done", "готово", media_id):
        await _forget(uid, media_id)       # пока качали, ролик отменили или удалили
    else:
        log.info("ролик %s: готов, в истории", row["id"][:8])
    await _drop_quietly(conn, gateway_id, row["id"])


async def _poll(row) -> None:
    conn = _connection(row)
    if conn is None:
        await _fail(row, "подключение шлюза удалено из профиля")
        return
    try:
        r = await upstream.request(conn, "GET", _url(conn, row["gateway_id"]), timeout=VIDEO_TIMEOUT_SEC)
    except upstream.UpstreamRefused as e:
        await _fail(row, upstream.refused_words(e, conn))
        return
    except httpx.HTTPError as e:
        log.info("ролик %s: шлюз недоступен (%s) — следующий проход", row["id"][:8], type(e).__name__)
        return
    if r.status_code == 404:
        await _fail(row, _forgotten(row))
        return
    if r.status_code in (401, 403):
        await _fail(row, upstream.gateway_error(r, conn.name))
        return
    if not r.is_success:
        log.warning("ролик %s: статус не получен — код %s", row["id"][:8], r.status_code)
        return
    body = upstream.json_body(r)
    seed = _update(row, body)
    if body.get("status") == "completed":
        await _fetch(row, conn, seed)
    elif body.get("status") == "failed":
        await _fail(row, _failure_words(body), conn)


async def poll_once() -> None:
    """Один проход по всем идущим роликам; сбой одного не мешает остальным."""
    rows = state.DB.execute(f"SELECT * FROM videos WHERE {WHERE_ACTIVE} AND gateway_id IS NOT NULL"
                            " ORDER BY created").fetchall()
    for row in rows:
        try:
            await _poll(row)
        except Exception as e:                    # noqa: BLE001 — наблюдатель не должен вставать
            log.warning("ролик %s: сбой прохода (%s)", row["id"][:8], type(e).__name__)


async def _watch() -> None:
    while True:
        try:
            await poll_once()
        except Exception as e:                    # noqa: BLE001 — наблюдатель не должен вставать
            log.warning("наблюдатель роликов: сбой прохода (%s)", type(e).__name__)
        await clock.SLEEP(POLL_SEC)


def start_watcher() -> asyncio.Task:
    return asyncio.create_task(_watch())
