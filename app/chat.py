"""Чат (спека 2026-10-19-studio-pwa, раздел 7.7): диалоги, вложения (документы и фото), сборка
контекста и ход диалога.

1. Ход: сообщение пользователя и его вложения, сборка контекста (`compose`) и проверка предела
   модели — одной транзакцией без await внутри; не помещается — 413 и в базе ничего нет.
2. Подключение из списка администратора (`proxied`) — модель зовёт сервер. Запрос ведёт фоновая
   `StreamTask`: читает модель до конца и сохраняет ответ, даже если клиент ушёл; поток SSE
   ответа только читает её очередь. Пока данных нет, раз в PING_SEC в поток идёт `: ping` —
   холодная модель грузится минутами, а Apache рвёт молчащую линию.
3. Прямое подключение — браузер зовёт модель сам: ход отдаёт ему адрес и сообщения, готовый
   ответ браузер кладёт `PUT .../messages/{id}`. Пока читает, раз в ~15 с шлёт признак жизни
   (`POST .../messages/{id}/alive`, время — в `alive_at`): ответ без признака дольше ALIVE_SEC —
   брошенный (вкладку закрыли) — «прервано»; живой другие вкладки показывают идущим, и новый ход
   ждёт его, как ответ сервера.
4. Вложение, которое так и не отправили, удаляется крестиком (`DELETE /api/attachments/{id}`) или
   уборкой (`sweep_attachments`) через ORPHAN_SEC.
5. База — только с цикла событий: в asyncio.to_thread уходят Pillow, разбор документов и файлы.
6. Журнал — только id и статусы: текст сообщений и документов, имена файлов и токены туда не идут.
"""
import asyncio
import base64
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

import clock
import connections
import documents
import manage
import media
import sections
import state
import upstream
from connections import Connection
from image_upload import MAX_UPLOAD, UploadError, normalize_image

PING_SEC = 15
RESERVE_TOKENS = 4096
MAX_IMAGES = 4
CONTEXT = {"local-gemma-abl": 131072}
TITLE_CHARS = 60

# холодная модель грузится до ~290 с, Apache держит линию /api/chats/ до 600 с
STREAM_READ_SEC = 600
MODELS_TIMEOUT_SEC = 10
SAVE_SEC = 5                     # идущий ответ — в базу не чаще (перезапуск оставит прочитанное)
# признак жизни ответа из браузера: вкладка шлёт его раз в ~15 с, а фоновой вкладке браузер
# разрешает таймер раз в минуту
ALIVE_SEC = 90
ORPHAN_SEC = 86400               # неотправленное вложение живёт сутки
SWEEP_SEC = 3600
TEXT_MAX = 200_000
ANSWER_MAX = 2_000_000
MODEL_MAX = 200
INTERRUPTED = "\n\n[прервано перезапуском студии]"
STORAGE = "хранилище недоступно"
NO_PHOTO = "[фото недоступно: файла нет в хранилище]"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
DOC_MIME = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class ChatIn(BaseModel):
    connection_id: int | None = None
    model: str | None = Field(None, max_length=MODEL_MAX)


class ChatPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    connection_id: int | None = None
    model: str | None = Field(None, max_length=MODEL_MAX)


class TurnIn(BaseModel):
    text: str = Field("", max_length=TEXT_MAX)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)
    regenerate: bool = False


class AnswerIn(BaseModel):
    content: str = Field(max_length=ANSWER_MAX)
    status: Literal["done", "stopped", "error"]


# ---- строки --------------------------------------------------------------------

def _gone(what: str = "такого диалога нет") -> HTTPException:
    return HTTPException(404, {"code": "gone", "message": what})


def _chat_row(user_id: int, chat_id: str):
    row = state.DB.execute("SELECT * FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
    if row is None:
        raise _gone()
    return row


def _chat_out(row) -> dict:
    return {"id": row["id"], "title": row["title"], "connection_id": row["connection_id"], "model": row["model"],
            "updated": row["updated"]}


def _chat_pair(user_id: int, conn_id: "int | None", model: "str | None") -> tuple[Connection, str]:
    """Подключение и модель диалога из раздела «Чат» (sections.resolve): чужое — 404 gone, не
    отмеченная модель — 422, нет умолчания — 409 no_model."""
    return sections.resolve(user_id, "chat", conn_id, (model or "").strip() or None)


def _attachments(chat_id: str) -> dict[str, list]:
    """Вложения сообщений диалога: id сообщения → строки по порядку загрузки."""
    rows = state.DB.execute("SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id"
                            " WHERE m.chat_id = ? ORDER BY a.created, a.rowid", (chat_id,))
    out: dict[str, list] = {}
    for row in rows:
        out.setdefault(row["message_id"], []).append(row)
    return out


def _messages(chat_id: str):
    return state.DB.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY created, rowid", (chat_id,)).fetchall()


def _alive(row) -> bool:
    """Ответ из браузера, который вкладка ещё читает (признак жизни свежее ALIVE_SEC)."""
    return (row["via"] == "browser" and row["status"] == "streaming" and row["alive_at"] is not None
            and clock.NOW() - row["alive_at"] < ALIVE_SEC)


def _message_out(row, atts: list) -> dict:
    live = STREAMS.get(row["id"])
    return {"id": row["id"], "role": row["role"], "content": live.content if live else row["content"],
            "status": row["status"], "via": row["via"], "model": row["model"], "created": row["created"],
            "live": _alive(row),
            "attachments": [{"id": a["id"], "kind": a["kind"], "name": a["name"], "media_id": a["media_id"],
                             "chars": a["chars"]} for a in atts]}


def _finish(message_id: str, chat_id: str, content: str, status: str) -> bool:
    """Итог идущего ответа; False — он уже сохранён (или удалён)."""
    done = state.DB.execute("UPDATE messages SET content = ?, status = ? WHERE id = ? AND status = 'streaming'",
                            (content, status, message_id)).rowcount == 1
    if done:
        state.DB.execute("UPDATE chats SET updated = ? WHERE id = ?", (clock.NOW(), chat_id))
    return done


def recover() -> None:
    """После перезапуска потоков сервера нет: их ответы — ошибка с пометкой (ответы браузера идут дальше)."""
    count = state.DB.execute("UPDATE messages SET status = 'error', content = content || ?"
                             " WHERE status = 'streaming' AND via = 'server'", (INTERRUPTED,)).rowcount
    if count:
        log.warning("чат: %d ответов прервано перезапуском", count)


# ---- контекст ------------------------------------------------------------------

def _content(text: str, atts: list):
    """Текст пользователя с документами перед ним; с картинками — части OpenAI (не больше MAX_IMAGES):
    картинка — пока только id media, данные ставит fill_photos."""
    docs = "".join(f'<document name="{a["name"]}">\n{a["text"]}\n</document>\n\n'
                   for a in atts if a["kind"] == "document")
    pics = [a for a in atts if a["kind"] == "image"][:MAX_IMAGES]
    if not pics:
        return docs + text
    return [{"type": "text", "text": docs + text}] + [{"type": "image_url", "media_id": a["media_id"]} for a in pics]


def _merge(first, second):
    """Два сообщения пользователя подряд (ответ между ними — ошибка или пустой): одно — тексты через
    пустую строку, картинки — последние MAX_IMAGES. Шаблоны Gemma требуют чередования ролей."""
    if isinstance(first, str) and isinstance(second, str):
        return "\n\n".join(t for t in (first, second) if t)
    parts = [p for c in (first, second) for p in ([{"type": "text", "text": c}] if isinstance(c, str) else c)]
    text = "\n\n".join(p["text"] for p in parts if p["type"] == "text" and p["text"])
    return [{"type": "text", "text": text}] + [p for p in parts if p["type"] != "text"][-MAX_IMAGES:]


def compose(chat_id: str, upto: str) -> list[dict]:
    """Сообщения OpenAI диалога до `upto` включительно: только `done` и `stopped`; сообщения
    пользователя подряд сливаются. Синхронно и без файлов (идёт в транзакции хода): картинки —
    частями с id media, данные — fill_photos после COMMIT."""
    atts = _attachments(chat_id)
    out = []
    for row in _messages(chat_id):
        if row["status"] in ("done", "stopped"):
            if row["role"] == "user":
                content = _content(row["content"], atts.get(row["id"], []))
                if out and out[-1]["role"] == "user":
                    out[-1]["content"] = _merge(out[-1]["content"], content)
                else:
                    out.append({"role": "user", "content": content})
            elif row["content"]:
                out.append({"role": "assistant", "content": row["content"]})
        if row["id"] == upto:
            break
    return out


def _read_photos(paths: dict) -> dict:
    """id media → data: URL; файла (или строки) нет — None. Без базы — для asyncio.to_thread;
    прочая OSError (шара недоступна) — наружу."""
    urls = {}
    for media_id, path in paths.items():
        try:
            data = path.read_bytes() if path is not None else None
        except FileNotFoundError:
            data = None
        urls[media_id] = None if data is None else "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    return urls


async def fill_photos(chat_id: str, user_id: int, messages: list[dict]) -> list[dict]:
    """Картинки сообщений compose — data: URL; файлы читаются в потоке. Фото, которого нет на
    шаре, — короткая пометка текстом: иначе каждый следующий ход диалога падал бы."""
    ids = {p["media_id"] for m in messages if isinstance(m["content"], list) for p in m["content"] if "media_id" in p}
    if not ids:
        return messages
    paths = {}
    for media_id in ids:
        try:
            paths[media_id] = media.path(user_id, media_id)
        except HTTPException:                        # строки media нет
            paths[media_id] = None
    urls = await asyncio.to_thread(_read_photos, paths)
    for media_id in sorted(m for m in ids if urls[m] is None):
        log.warning("чат %s: фото %s нет в хранилище — вместо него пометка", chat_id[:8], media_id)

    def part(p):
        if "media_id" not in p:
            return p
        url = urls[p["media_id"]]
        return {"type": "image_url", "image_url": {"url": url}} if url else {"type": "text", "text": NO_PHOTO}

    return [{**m, "content": [part(p) for p in m["content"]]} if isinstance(m["content"], list) else m
            for m in messages]


def estimate_tokens(messages: list[dict]) -> int:
    chars, pics = 0, 0
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            chars += len(content)
            continue
        for part in content:
            if part.get("type") == "text":
                chars += len(part["text"])
            elif part.get("type") == "image_url":
                pics += 1
    return math.ceil(chars / 3) + 1000 * pics


async def _limit(conn: Connection, model: str) -> int | None:
    """Предел контекста модели: CONTEXT, иначе `context_length` списка моделей подключения через
    сервер; не узнать — None (тогда без проверки)."""
    if model in CONTEXT:
        return CONTEXT[model]
    if not conn.proxied:
        return None
    try:
        r = await upstream.request(conn, "GET", connections.models_url(conn), timeout=MODELS_TIMEOUT_SEC)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return None
    data = upstream.json_body(r).get("data") if r.is_success else None
    for m in data if isinstance(data, list) else []:
        if isinstance(m, dict) and m.get("id") == model:
            length = m.get("context_length")
            return length if isinstance(length, int) and not isinstance(length, bool) and length > 0 else None
    return None


def _too_big(tokens: int, limit: int) -> HTTPException:
    return HTTPException(413, {"code": "context", "message": (
        f"Не помещается в контекст модели: ~{round(tokens / 1000)} тыс. токенов при пределе {limit // 1000} тыс. "
        "Уберите документ или начните новый диалог.")})


# ---- поток через сервер --------------------------------------------------------

@dataclass(eq=False)
class StreamTask:
    """Запрос к модели ответа `message_id`; очередь — события для потока SSE: ("ping", None),
    ("delta", текст), ("done", статус) или ("error", слова)."""
    user_id: int
    chat_id: str
    message_id: str
    conn: Connection
    model: str
    messages: list
    content: str = ""
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    fresh: bool = False          # данные с прошлого маркера пингов
    saved: float = 0.0
    shutdown: bool = False

    async def _pings(self) -> None:
        while True:
            self.fresh = False
            await clock.SLEEP(PING_SEC)
            if not self.fresh:
                self.queue.put_nowait(("ping", None))

    def _delta(self, text: str) -> None:
        self.content += text
        self.fresh = True
        self.queue.put_nowait(("delta", text))
        now = clock.NOW()
        if now - self.saved >= SAVE_SEC:
            self.saved = now
            state.DB.execute("UPDATE messages SET content = ? WHERE id = ? AND status = 'streaming'",
                             (self.content, self.message_id))

    async def _read(self) -> str | None:
        """Ответ модели до конца; итог — слова ошибки или None."""
        payload, self.messages = {"model": self.model, "messages": self.messages, "stream": True}, []
        try:
            async with upstream.stream(self.conn, "POST", connections.chat_url(self.conn), json=payload,
                                       timeout=httpx.Timeout(STREAM_READ_SEC, connect=30)) as r:
                if r.status_code != 200:
                    await r.aread()
                    return upstream.gateway_error(r, self.conn.name)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        error = chunk["error"]
                        words = error.get("message") if isinstance(error, dict) else error
                        return f"модель прервала ответ: {str(words)[:300]}"
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") if isinstance(choice, dict) else None
                        text = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(text, str) and text:
                            self._delta(text)
        except upstream.UpstreamRefused:
            return f"адрес подключения «{self.conn.name}» убран из списка администратора"
        except httpx.TimeoutException:
            return f"шлюз не ответил за {STREAM_READ_SEC} с"
        except httpx.HTTPError as e:
            return f"шлюз недоступен: {type(e).__name__}"
        return None

    async def run(self) -> None:
        pings = asyncio.create_task(self._pings())
        error = None
        try:
            error = await self._read()
            status = "done" if error is None else "error"
        except asyncio.CancelledError:               # кнопка «стоп» или остановка службы
            status = "error" if self.shutdown else "stopped"
        except Exception as e:                       # noqa: BLE001 — ответ не должен висеть
            status, error = "error", f"сбой службы: {type(e).__name__}"
        finally:
            pings.cancel()
        _finish(self.message_id, self.chat_id, self.content + (INTERRUPTED if self.shutdown else ""), status)
        if STREAMS.get(self.message_id) is self:
            del STREAMS[self.message_id]
        log.info("чат %s: ответ %s — %s", self.chat_id[:8], self.message_id[:8], status)
        self.queue.put_nowait(("error", error) if error is not None else ("done", status))


STREAMS: dict[str, StreamTask] = {}


def _chat_streams(chat_id: str) -> list[StreamTask]:
    return [st for st in STREAMS.values() if st.chat_id == chat_id]


async def _cancel(streams: list[StreamTask]) -> None:
    """Отменить и дождаться записи итога; задача, отменённая до старта, итог не пишет — его пишет здесь."""
    for st in streams:
        st.task.cancel()
    await asyncio.gather(*(st.task for st in streams), return_exceptions=True)
    for st in streams:
        status = "error" if st.shutdown else "stopped"
        if _finish(st.message_id, st.chat_id, st.content + (INTERRUPTED if st.shutdown else ""), status):
            st.queue.put_nowait(("done", status))
        if STREAMS.get(st.message_id) is st:
            del STREAMS[st.message_id]


def _stop_unstarted(chat_ids: set[str]) -> None:
    """Ответы через сервер, чей поток ещё не запущен (ход читает фото), — сразу «остановлен»:
    дочитав фото, ход модель уже не зовёт (см. create_turn)."""
    for chat_id in chat_ids:
        for row in state.DB.execute("SELECT id FROM messages WHERE chat_id = ? AND via = 'server'"
                                    " AND status = 'streaming'", (chat_id,)).fetchall():
            if row["id"] not in STREAMS:
                _finish(row["id"], chat_id, "", "stopped")


async def stop_user(user_id: int) -> None:
    """Стирание истории, удаление или отключение учётки: идущие ответы пользователя останавливаются —
    модель не держит карту ради ответа, который уже некуда сохранить. Потоки — по user_id, а не по
    диалогам: поток диалога, который уже удалён, тоже его."""
    chats = {r["id"] for r in state.DB.execute("SELECT id FROM chats WHERE user_id = ?", (user_id,))}
    _stop_unstarted(chats)
    await _cancel([st for st in STREAMS.values() if st.user_id == user_id])


async def stop_all() -> None:
    """Остановка службы: идущие ответы сохраняются с пометкой о перезапуске."""
    streams = list(STREAMS.values())
    for st in streams:
        st.shutdown = True
    await _cancel(streams)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _events(st: StreamTask, user_message_id: str):
    yield _sse("turn", {"user_message_id": user_message_id, "assistant_message_id": st.message_id})
    while True:
        kind, data = await st.queue.get()
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "delta":
            yield _sse("delta", {"text": data})
        elif kind == "done":
            yield _sse("done", {"status": data})
            return
        else:
            yield _sse("error", {"message": data})
            return


async def _ended(user_message_id: str, answer_id: str, status: str):
    """Поток SSE хода, чей ответ кончился до запроса к модели (остановлен, пока ход читал фото)."""
    yield _sse("turn", {"user_message_id": user_message_id, "assistant_message_id": answer_id})
    yield _sse("done", {"status": status})


# ---- API: диалоги --------------------------------------------------------------

@router.get("/chats")
async def list_chats(request: Request):
    rows = state.DB.execute("SELECT * FROM chats WHERE user_id = ? ORDER BY updated DESC, rowid DESC",
                            (request.state.user.id,))
    return {"items": [_chat_out(r) for r in rows]}


@router.post("/chats", status_code=201)
async def create_chat(body: ChatIn, request: Request):
    user = request.state.user
    conn, model = _chat_pair(user.id, body.connection_id, body.model)
    chat_id, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO chats(id, user_id, title, connection_id, model, created, updated)"
                     " VALUES (?, ?, NULL, ?, ?, ?, ?)", (chat_id, user.id, conn.id, model, now, now))
    log.info("чат %s: создан", chat_id[:8])
    return _chat_out(_chat_row(user.id, chat_id))


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: str, request: Request):
    row = _chat_row(request.state.user.id, chat_id)
    atts = _attachments(chat_id)
    return {"chat": _chat_out(row), "messages": [_message_out(m, atts.get(m["id"], [])) for m in _messages(chat_id)]}


@router.patch("/chats/{chat_id}")
async def patch_chat(chat_id: str, body: ChatPatch, request: Request):
    uid = request.state.user.id
    _chat_row(uid, chat_id)
    sent, changes = body.model_fields_set, {}
    if "title" in sent:
        changes["title"] = (body.title or "").strip() or None
    if ("connection_id" in sent and body.connection_id is not None) or ("model" in sent and (body.model or "").strip()):
        row = _chat_row(uid, chat_id)
        conn_id = body.connection_id if body.connection_id is not None else row["connection_id"]
        conn, model = _chat_pair(uid, conn_id, body.model if (body.model or "").strip() else row["model"])
        changes["connection_id"], changes["model"] = conn.id, model
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE chats SET {cols} WHERE id = ?", (*changes.values(), chat_id))
    return _chat_out(_chat_row(uid, chat_id))


@router.delete("/chats/{chat_id}", status_code=204)
async def delete_chat(chat_id: str, request: Request):
    """Диалог, его сообщения и вложения; файл, на который ссылается другая запись истории, остаётся."""
    uid = request.state.user.id
    _chat_row(uid, chat_id)
    await _cancel(_chat_streams(chat_id))
    media_ids = {r["media_id"] for r in state.DB.execute(
        "SELECT a.media_id FROM attachments a JOIN messages m ON m.id = a.message_id WHERE m.chat_id = ?", (chat_id,))}
    state.DB.execute("DELETE FROM chats WHERE id = ?", (chat_id,))   # сообщения и вложения — каскадом
    for media_id in media_ids:
        if not media.referenced(uid, media_id):
            await media.delete_async(uid, media_id)
    log.info("чат %s: удалён", chat_id[:8])
    return Response(status_code=204)


# ---- API: вложения -------------------------------------------------------------

@router.post("/attachments", status_code=201)
async def upload_attachment(request: Request):
    """Картинка (PNG, JPEG, WebP — нормализуется до PNG) или документ (текст — сразу); файл — в media.
    К сообщению вложение привязывает ход."""
    uid = request.state.user.id
    text = None
    try:
        name, data = await documents.read_upload(request)
        suffix = PurePosixPath(name.lower()).suffix
        if suffix in IMAGE_SUFFIXES:
            if len(data) > MAX_UPLOAD:
                raise UploadError(f"фото должно быть не больше {MAX_UPLOAD >> 20} МиБ", 413)
            kind, mime, body = "image", "image/png", await asyncio.to_thread(normalize_image, data)
            download = PurePosixPath(name).stem + ".png"
        else:
            text = await asyncio.to_thread(documents.extract, name, data)
            kind, mime, body, download = "document", DOC_MIME[suffix], data, name
    except documents.DocumentError as e:
        raise HTTPException(e.status, {"code": "document", "message": str(e)}) from None
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    try:
        media_id = await media.save_async(uid, body, mime, download, thumb=kind == "image")
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    att_id, chars = uuid.uuid4().hex, len(text or "")
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
                     " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)", (att_id, uid, kind, name, media_id, text, chars, clock.NOW()))
    log.info("вложение %s: %s, %d знаков", att_id[:8], kind, chars)
    return {"id": att_id, "kind": kind, "name": name, "chars": chars, "media_id": media_id}


async def _drop_attachments(rows) -> int:
    """Строки неотправленных вложений и их файлы (кроме тех, на что ссылается другая запись);
    вложение, которое за это время ушло в ход, остаётся. Итог — сколько удалено."""
    dropped = 0
    for row in rows:
        if state.DB.execute("DELETE FROM attachments WHERE id = ? AND message_id IS NULL", (row["id"],)).rowcount != 1:
            continue
        dropped += 1
        if not media.referenced(row["user_id"], row["media_id"]):
            await media.delete_async(row["user_id"], row["media_id"])
    return dropped


@router.delete("/attachments/{att_id}", status_code=204)
async def delete_attachment(att_id: str, request: Request):
    """Крестик на вложении, которое ещё не отправили; отправленное — часть сообщения (409)."""
    row = state.DB.execute("SELECT * FROM attachments WHERE id = ? AND user_id = ?",
                           (att_id, request.state.user.id)).fetchone()
    if row is None:
        raise _gone("такого вложения нет")
    if row["message_id"] is not None:
        raise HTTPException(409, {"code": "sent", "message": "вложение уже отправлено"})
    await _drop_attachments([row])
    return Response(status_code=204)


async def sweep_attachments() -> None:
    """Неотправленные вложения старше ORPHAN_SEC (вкладку закрыли с прикреплённым файлом) — прочь."""
    rows = state.DB.execute("SELECT * FROM attachments WHERE message_id IS NULL AND created < ?",
                            (clock.NOW() - ORPHAN_SEC,)).fetchall()
    if rows and (count := await _drop_attachments(rows)):
        log.info("уборка: удалено неотправленных вложений — %d", count)


async def _sweep() -> None:
    while True:
        try:
            await sweep_attachments()
        except Exception as e:                       # noqa: BLE001 — уборка не должна вставать
            log.warning("уборка вложений: сбой (%s)", type(e).__name__)
        await clock.SLEEP(SWEEP_SEC)


def start_sweeper() -> asyncio.Task:
    return asyncio.create_task(_sweep())


# ---- API: ход ------------------------------------------------------------------

def _free_attachments(user_id: int, ids: list[str]) -> list:
    rows = []
    for att_id in dict.fromkeys(ids):
        row = state.DB.execute("SELECT * FROM attachments WHERE id = ? AND user_id = ? AND message_id IS NULL",
                               (att_id, user_id)).fetchone()
        if row is None:
            raise _gone("вложения нет или оно уже отправлено")
        rows.append(row)
    if sum(r["kind"] == "image" for r in rows) > MAX_IMAGES:
        raise HTTPException(422, {"code": "images", "message": f"не больше {MAX_IMAGES} картинок в сообщении"})
    return rows


def _busy(chat_id: str) -> None:
    """Идёт ответ через сервер — его поток или строка `streaming` (поток ещё не запущен: ход читает
    фото) — или ответ из браузера, который вкладка ещё читает."""
    if _chat_streams(chat_id) or state.DB.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND status = 'streaming' AND via = 'server' LIMIT 1",
            (chat_id,)).fetchone():
        raise HTTPException(409, {"code": "busy", "message": "ответ ещё идёт — дождитесь или остановите"})
    if any(_alive(r) for r in state.DB.execute(
            "SELECT via, status, alive_at FROM messages WHERE chat_id = ? AND status = 'streaming'", (chat_id,))):
        raise HTTPException(409, {"code": "busy", "message": "ответ ещё идёт в другой вкладке — дождитесь его"})


def _drop_last_answer(chat_id: str) -> str:
    """Перед повтором: последний ответ ассистента удаляется; итог — id последнего сообщения пользователя."""
    last = state.DB.execute("SELECT id, role FROM messages WHERE chat_id = ? ORDER BY created DESC, rowid DESC"
                            " LIMIT 1", (chat_id,)).fetchone()
    if last is not None and last["role"] == "assistant":
        state.DB.execute("DELETE FROM messages WHERE id = ?", (last["id"],))
        last = state.DB.execute("SELECT id, role FROM messages WHERE chat_id = ? ORDER BY created DESC, rowid DESC"
                                " LIMIT 1", (chat_id,)).fetchone()
    if last is None or last["role"] != "user":
        raise HTTPException(409, {"code": "nothing", "message": "нечего переспрашивать"})
    return last["id"]


def _title(text: str) -> str | None:
    lines = text.strip().splitlines()
    if not lines:
        return None
    return lines[0].strip()[:TITLE_CHARS].rstrip() or None


@router.post("/chats/{chat_id}/turns")
async def create_turn(chat_id: str, body: TurnIn, request: Request):
    uid = request.state.user.id
    row = _chat_row(uid, chat_id)
    _busy(chat_id)
    if row["connection_id"] is None:
        raise HTTPException(409, {"code": "no_connection", "message": "выберите подключение диалога"})
    conn = connections.get(uid, row["connection_id"])
    model = row["model"] or manage.DEFAULT_MODEL
    atts = [] if body.regenerate else _free_attachments(uid, body.attachment_ids)
    if not body.regenerate and not body.text.strip() and not atts:
        raise HTTPException(422, {"code": "empty", "message": "пустое сообщение"})
    limit = await _limit(conn, model)
    _busy(chat_id)                                   # пока ждали список моделей, мог начаться другой ход
    via = "server" if conn.proxied else "browser"
    now, answer_id = clock.NOW(), uuid.uuid4().hex
    # без await до COMMIT: другой запрос не вклинится, отказ откатывает всё
    state.DB.execute("BEGIN")
    try:
        # ответ прямого подключения, который так и не сохранили (вкладку закрыли), — не вечная загрузка;
        # живой ответ сюда не доходит: его ждёт _busy
        state.DB.execute("UPDATE messages SET status = 'error' WHERE chat_id = ? AND via = 'browser'"
                         " AND status = 'streaming'", (chat_id,))
        if body.regenerate:
            user_mid = _drop_last_answer(chat_id)
        else:
            user_mid = uuid.uuid4().hex
            state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                             " VALUES (?, ?, 'user', ?, 'done', ?, ?, ?)", (user_mid, chat_id, body.text, via, model, now))
            for a in atts:
                if state.DB.execute("UPDATE attachments SET message_id = ? WHERE id = ? AND message_id IS NULL",
                                    (user_mid, a["id"])).rowcount != 1:
                    raise _gone("вложения нет или оно уже отправлено")
        messages = compose(chat_id, user_mid)
        tokens = estimate_tokens(messages)
        if limit is not None and tokens > limit - RESERVE_TOKENS:
            log.info("чат %s: ход не помещается в контекст (~%d токенов)", chat_id[:8], tokens)
            raise _too_big(tokens, limit)
        state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, alive_at)"
                         " VALUES (?, ?, 'assistant', '', 'streaming', ?, ?, ?, ?)",
                         (answer_id, chat_id, via, model, now, now))
        title = row["title"] or (None if body.regenerate else _title(body.text))
        state.DB.execute("UPDATE chats SET title = ?, updated = ? WHERE id = ?", (title, now, chat_id))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    try:
        messages = await fill_photos(chat_id, uid, messages)    # файлы — после COMMIT и в потоке
    except BaseException as e:
        # строка ответа уже в базе: без итога диалог считался бы занятым (см. _busy)
        _finish(answer_id, chat_id, STORAGE if isinstance(e, OSError) else "", "error")
        if isinstance(e, OSError):                   # картинка вложения не прочиталась с шары
            log.warning("чат %s: вложение не прочиталось (%s)", chat_id[:8], type(e).__name__)
            raise HTTPException(503, {"code": "storage", "message": STORAGE}) from None
        raise
    # пока читались фото, ответ могли остановить, а диалог — удалить (и стиранием истории)
    answer = state.DB.execute("SELECT status FROM messages WHERE id = ?", (answer_id,)).fetchone()
    if answer is None:
        raise _gone()
    if answer["status"] != "streaming":
        log.info("чат %s: ход %s остановлен до запроса к модели", chat_id[:8], answer_id[:8])
        return StreamingResponse(_ended(user_mid, answer_id, answer["status"]), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    log.info("чат %s: ход %s, %s", chat_id[:8], answer_id[:8], via)
    if not conn.proxied:
        return {"user_message_id": user_mid, "assistant_message_id": answer_id,
                "url": connections.chat_url(conn), "model": model, "messages": messages}
    st = StreamTask(user_id=uid, chat_id=chat_id, message_id=answer_id, conn=conn, model=model, messages=messages)
    STREAMS[answer_id] = st
    st.task = asyncio.create_task(st.run())
    return StreamingResponse(_events(st, user_mid), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.put("/chats/{chat_id}/messages/{message_id}")
async def put_answer(chat_id: str, message_id: str, body: AnswerIn, request: Request):
    """Ответ прямого подключения: браузер прочитал модель сам и кладёт итог (один раз)."""
    _chat_row(request.state.user.id, chat_id)
    row = state.DB.execute("SELECT role, status, via FROM messages WHERE id = ? AND chat_id = ?",
                           (message_id, chat_id)).fetchone()
    if row is None:
        raise _gone("такого сообщения нет")
    if (row["role"], row["status"], row["via"]) != ("assistant", "streaming", "browser") \
            or not _finish(message_id, chat_id, body.content, body.status):
        raise HTTPException(409, {"code": "state", "message": "этот ответ уже сохранён"})
    log.info("чат %s: ответ %s от браузера — %s", chat_id[:8], message_id[:8], body.status)
    return {"id": message_id, "status": body.status}


@router.post("/chats/{chat_id}/messages/{message_id}/alive", status_code=204)
async def answer_alive(chat_id: str, message_id: str, request: Request):
    """Признак жизни ответа, который читает браузер; ответ уже не идёт — 409, его вкладка перестаёт слать."""
    _chat_row(request.state.user.id, chat_id)
    if state.DB.execute("UPDATE messages SET alive_at = ? WHERE id = ? AND chat_id = ? AND role = 'assistant'"
                        " AND via = 'browser' AND status = 'streaming'",
                        (clock.NOW(), message_id, chat_id)).rowcount != 1:
        raise HTTPException(409, {"code": "state", "message": "этот ответ уже не идёт"})
    return Response(status_code=204)


@router.post("/chats/{chat_id}/stop", status_code=204)
async def stop_turn(chat_id: str, request: Request):
    _chat_row(request.state.user.id, chat_id)
    _stop_unstarted({chat_id})
    await _cancel(_chat_streams(chat_id))
    return Response(status_code=204)
