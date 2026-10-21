"""Чат (спека 2026-10-19-studio-pwa, раздел 7.7): диалоги, вложения (документы и фото), сборка
контекста и ход диалога.

1. Ход: сообщение пользователя и его вложения, сборка контекста (`compose`, картинок — не больше
   MAX_IMAGES на весь запрос) и проверка предела модели — одной транзакцией без await внутри; не
   помещается — 413 и в базе ничего нет.
2. Подключение из списка администратора (`proxied`) — модель зовёт сервер. Запрос ведёт фоновая
   `StreamTask`: читает модель до конца и сохраняет ответ, даже если клиент ушёл; поток SSE
   ответа только читает её очередь (клиент ушёл — очередь не пополняется). Пока данных нет, раз в
   PING_SEC в поток идёт `: ping` — холодная модель грузится минутами, а Apache рвёт молчащую
   линию. Ответ — до ANSWER_MAX знаков и STREAM_TOTAL_SEC, конец — `[DONE]` или `finish_reason`
   (без них — ошибка), у пользователя сразу — не больше USER_STREAMS таких ответов (429).
   С «Интернетом» (`chats.web`, спека 2026-10-21-studio-chat-internet) запрос несёт tools, и ответ идёт
   кругами: вызовы модели исполняет web.call, шаги — в `messages.tools` по одному (событие SSE `tool`).
3. Прямое подключение — браузер зовёт модель сам: ход отдаёт ему адрес и сообщения, готовый
   ответ браузер кладёт `PUT .../messages/{id}`. Пока читает, раз в ~15 с шлёт признак жизни
   (`POST .../messages/{id}/alive`, время — в `alive_at`): ответ без признака дольше ALIVE_SEC —
   брошенный (вкладку закрыли) — «прервано»; живой другие вкладки показывают идущим, и новый ход
   ждёт его, как ответ сервера.
4. Вложение, которое так и не отправили, удаляется крестиком (`DELETE /api/attachments/{id}`) или
   уборкой (`sweep_attachments`) через ORPHAN_SEC.
5. Вложение из расшифровки (`source_transcript_id`) в истории диалога — ход и повтор к нелокальной
   модели (sections.is_local) только с `external_ok`, иначе 409 `external`; подтверждённым считается
   диалог, где после последнего такого вложения уже есть ответ этой модели тем же путём (`_needs_consent`).
   Ход, которому «Интернет» даёт Exa, с такой расшифровкой новее `chats.web_consent` — только с `web_ok`,
   иначе 409 `external_web` (после `external`); `web_ok` ставит `web_consent` (`_needs_web_consent`).
6. База — только с цикла событий: в asyncio.to_thread уходят Pillow, разбор документов и файлы.
7. Журнал — только id и статусы: текст сообщений и документов, имена файлов и токены туда не идут.
"""
import asyncio
import base64
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
import web
from connections import Connection
from image_upload import MAX_UPLOAD, UploadError, normalize_image

PING_SEC = 15
RESERVE_TOKENS = 4096
MAX_IMAGES = 4
CONTEXT = {"local-gemma-abl": 131072}
TITLE_CHARS = 60

# холодная модель грузится до ~290 с, Apache держит линию /api/chats/ до 600 с
STREAM_READ_SEC = 600
# весь ответ через сервер: долгий ответ gemma-abl — минуты (~30 ток/с — десятки тысяч токенов за
# полчаса), а модель, что шлёт данные без конца, read timeout не ловит — держала бы карту и память
# службы вечно. Пинги держат линию Apache и дольше 600 с, поток SSE живёт и после ухода клиента.
STREAM_TOTAL_SEC = 1800
# ответов через сервер у одного пользователя сразу: каждый держит в памяти до ANSWER_MAX знаков
# (и очередь событий), а MemoryMax службы — 2 ГБ на всех
USER_STREAMS = 3
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
# пометки обрезанного ответа: он — ошибка, в контекст следующих ходов не идёт (compose)
CUT_LENGTH = "\n\n[ответ обрезан пределом длины модели]"
CUT_SIZE = "\n\n[ответ обрезан: слишком длинный для студии]"
CUT_TIME = f"\n\n[ответ обрезан: шёл дольше {STREAM_TOTAL_SEC // 60} минут]"
# «Интернет» (спека 2026-10-21-studio-chat-internet, 6.3): кругов с инструментами на ответ, затем последний
# запрос без tools — модель отвечает по найденному
TOOL_ROUNDS = 5
# вызовов в одном круге: каждый — до 30 с Exa или 2×60 с yt-dlp и деньги ключа Exa; лишние — шаг-ошибка
CALLS_PER_ROUND = 4
TOO_MANY = f"не больше {CALLS_PER_ROUND} вызовов за раз"
WEB_OFF = "Интернет выключен"
TIME_WORDS = "ответ шёл слишком долго — обрезан"
NO_ANSWER = "модель не ответила по найденному — переспросите"
CUT_CONTEXT = "\n[обрезано: не помещается в контекст модели]"
NO_ROOM = "не помещается в контекст модели"
NO_TOOLS = "эта модель не умеет пользоваться интернетом — выключите «Интернет» в этом диалоге"
NO_EXA_KEY = "сначала задайте ключ Exa в Настройках → Прочее → Интернет"   # PATCH {web: true} без ключа — 409
# к системному сообщению даты, пока запрос несёт tools: без неё gemma-abl на «найди в интернете» отвечала по
# памяти (проба 2026-10-21, спека раздел 8). Последняя фраза — против «я заглянула в интернет» без вызова: на
# диалоге, где модель уже так ответила, вызов поиска на «какой курс доллара» — 10 из 12 вместо 5 из 12 (2026-10-22)
WEB_NOTE = ("У тебя есть доступ к интернету через инструменты: {tools}. Если пользователь просит найти что-то в "
            "интернете, прочитать ссылку или ролик YouTube или нужны свежие данные — вызови инструмент, а не "
            "отвечай по памяти. Говори, что нашёл что-то в интернете, только если инструмент в этом ответе вернул "
            "результат; если вернул ошибку — так и скажи и не выдумывай данные.")
STORAGE = "хранилище недоступно"
NO_PHOTO = "[фото недоступно: файла нет в хранилище]"
DROPPED = f"[фото убрано из контекста: модели — не больше {MAX_IMAGES} картинок на запрос]"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
DOC_MIME = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MSK = timezone(timedelta(hours=3))               # Москва без перехода на летнее время
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class ChatIn(BaseModel):
    connection_id: int | None = None
    model: str | None = Field(None, max_length=MODEL_MAX)


class ChatPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    connection_id: int | None = None
    model: str | None = Field(None, max_length=MODEL_MAX)
    web: bool | None = None              # кнопка «Интернет»; null — не трогать


class TurnIn(BaseModel):
    text: str = Field("", max_length=TEXT_MAX)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)
    regenerate: bool = False
    external_ok: bool = False        # расшифровку — и нелокальной модели (человек подтвердил)
    web_ok: bool = False             # расшифровка в диалоге, а «Интернет» даёт Exa (человек подтвердил)


class AnswerIn(BaseModel):
    # как у ответа через сервер: до ANSWER_MAX знаков и пометка обрезки (длиннейшая — CUT_SIZE)
    content: str = Field(max_length=ANSWER_MAX + len(CUT_SIZE))
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


def _needs_consent(chat_id: str, model: str, via: str) -> bool:
    """В истории диалога (её ход уносит модели целиком) есть вложение из расшифровки, а после
    последнего такого сообщения нет ответа ассистента этой модели тем же путём (`via`). Такой ответ и
    есть след подтверждения: ход с расшифровкой к нелокальной модели без `external_ok` не проходит.
    Подключение сообщение не хранит (схема прежняя) — пара сводится к модели и пути: та же модель
    другого подключения того же пути считается подтверждённой."""
    last = state.DB.execute(
        "SELECT m.created, m.rowid FROM messages m JOIN attachments a ON a.message_id = m.id"
        " WHERE m.chat_id = ? AND a.source_transcript_id IS NOT NULL ORDER BY m.created DESC, m.rowid DESC LIMIT 1",
        (chat_id,)).fetchone()
    return last is not None and state.DB.execute(
        "SELECT 1 FROM messages WHERE chat_id = ? AND role = 'assistant' AND model = ? AND via = ?"
        " AND (created, rowid) > (?, ?) LIMIT 1", (chat_id, model, via, last[0], last[1])).fetchone() is None


def _needs_web_consent(chat_id: str, consent: "float | None") -> bool:
    """В истории диалога есть вложение из расшифровки новее подтверждения «расшифровка и Exa»
    (`chats.web_consent`; NULL — не было): запросы модели к Exa составлены из всей истории. Прежний след
    (`_needs_consent`: ответ той же модели) не годится — Exa не модель; расшифровка, вложенная позже
    подтверждения, спрашивает снова."""
    return state.DB.execute(
        "SELECT 1 FROM messages m JOIN attachments a ON a.message_id = m.id WHERE m.chat_id = ?"
        " AND a.source_transcript_id IS NOT NULL AND (? IS NULL OR m.created > ?) LIMIT 1",
        (chat_id, consent, consent)).fetchone() is not None


def _chat_view(user_id: int, row) -> dict:
    """Диалог для страницы (GET, POST, PATCH одного): ещё `local` — модель диалога локальная
    (sections.is_local) и `external` — следующий ход уносит расшифровку истории во внешний сервис без
    подтверждения (страница спрашивает заранее; сервер ответил бы 409 `external`); `web` — кнопка
    «Интернет» и `web_tools` — виды инструментов, которые она даст на этом подключении (web.tools_for; пусто
    у прямого подключения — там `web` хранится, но не действует, — и без ключа Exa); `proxied` — подключение
    диалога через сервер (страница: нет — кнопки нет, есть без ключа — кнопка выключена с подсказкой);
    `external_web` — следующий ход даст модели Exa при расшифровке в истории без подтверждения (сервер ответил
    бы 409 `external_web`)."""
    out, conn = _chat_out(row), None
    if row["connection_id"] is not None:
        try:
            conn = connections.get(user_id, row["connection_id"])
        except HTTPException:                        # подключение удалено — ход ответит 404
            pass
    model = row["model"] or manage.DEFAULT_MODEL
    out["local"] = conn is not None and sections.is_local(conn, model)
    out["external"] = conn is not None and not out["local"] and _needs_consent(
        row["id"], model, "server" if conn.proxied else "browser")
    out["web"] = bool(row["web"])
    out["proxied"] = conn is not None and conn.proxied
    out["web_tools"] = web.tools_for(conn, web.has_exa_key(user_id))
    out["external_web"] = out["web"] and "search" in out["web_tools"] and _needs_web_consent(
        row["id"], row["web_consent"])
    return out


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
    """Сообщение для страницы; `tools` — шаги «Интернета» без текста для модели (web.public)."""
    live = STREAMS.get(row["id"])
    steps = live.steps if live else json.loads(row["tools"] or "[]")
    return {"id": row["id"], "role": row["role"], "content": live.content if live else row["content"],
            "status": row["status"], "via": row["via"], "model": row["model"], "created": row["created"],
            "live": _alive(row),
            "attachments": [_attachment_out(a) for a in atts],
            "tools": [web.public(s) for s in steps]}


def _attachment_out(a) -> dict:
    """Вложение сообщения; из расшифровки — ещё `source` (есть ли ещё своя расшифровка: ссылка «к записи»)."""
    out = {"id": a["id"], "kind": a["kind"], "name": a["name"], "media_id": a["media_id"], "chars": a["chars"]}
    if a["source_transcript_id"] is not None:
        exists = state.DB.execute("SELECT 1 FROM transcripts WHERE id = ? AND user_id = ?",
                                  (a["source_transcript_id"], a["user_id"])).fetchone() is not None
        out["source"] = {"transcript_id": a["source_transcript_id"], "exists": exists}
    return out


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
    """Текст пользователя с документами перед ним; с картинками — части OpenAI: картинка — пока
    только id media, данные ставит fill_photos."""
    docs = "".join(f'<document name="{a["name"]}">\n{a["text"]}\n</document>\n\n'
                   for a in atts if a["kind"] == "document")
    pics = [a for a in atts if a["kind"] == "image"]
    if not pics:
        return docs + text
    return [{"type": "text", "text": docs + text}] + [{"type": "image_url", "media_id": a["media_id"]} for a in pics]


def _merge(first, second):
    """Два сообщения пользователя подряд (ответ между ними — ошибка или пустой): одно — тексты через
    пустую строку, картинки — по порядку. Шаблоны Gemma требуют чередования ролей."""
    if isinstance(first, str) and isinstance(second, str):
        return "\n\n".join(t for t in (first, second) if t)
    parts = [p for c in (first, second) for p in ([{"type": "text", "text": c}] if isinstance(c, str) else c)]
    text = "\n\n".join(p["text"] for p in parts if p["type"] == "text" and p["text"])
    return [{"type": "text", "text": text}] + [p for p in parts if p["type"] != "text"]


def _prefix(block: str, content):
    """Найденное прежнего ответа — в начало сообщения пользователя: строке — префиксом, частям — в начало
    первой текстовой части (её нет — новой первой частью)."""
    if isinstance(content, str):
        return block + content
    for n, p in enumerate(content):
        if p["type"] == "text":
            return [*content[:n], {**p, "text": block + p["text"]}, *content[n + 1:]]
    return [{"type": "text", "text": block}, *content]


def _cap_images(messages: list[dict]) -> None:
    """Не больше MAX_IMAGES картинок на весь запрос (gemma-abl больше не примет): остаются самые
    свежие, прежние — пометкой DROPPED, чтобы модель знала о пропуске; в истории диалога фото целы."""
    left = MAX_IMAGES
    for m in reversed(messages):
        if not isinstance(m["content"], list):
            continue
        parts = []
        for p in reversed(m["content"]):
            if p["type"] == "image_url":
                if left:
                    left -= 1
                else:
                    p = {"type": "text", "text": DROPPED}
            parts.append(p)
        m["content"] = parts[::-1]


def compose(chat_id: str, upto: str) -> list[dict]:
    """Сообщения OpenAI диалога до `upto` включительно: только `done` и `stopped`; сообщения
    пользователя подряд сливаются, картинок — не больше MAX_IMAGES на весь запрос. Найденное «Интернетом»
    (`messages.tools` ответа) — блоками web.history_block в начале сообщения пользователя перед этим ответом,
    без ролей `tool` и `tool_calls`. Синхронно и без файлов (идёт в транзакции хода): картинки — частями с
    id media, данные — fill_photos после COMMIT."""
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
            else:
                steps = json.loads(row["tools"] or "[]")
                if steps and out and out[-1]["role"] == "user":
                    out[-1]["content"] = _prefix(web.history_block(steps), out[-1]["content"])
                if row["content"]:
                    out.append({"role": "assistant", "content": row["content"]})
        if row["id"] == upto:
            break
    _cap_images(out)
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


def today_line(now: float) -> str:
    d = datetime.fromtimestamp(now, MSK)
    return f"Сегодня {WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year} года (Москва)."


def with_date(messages: list[dict]) -> list[dict]:
    """Запрос к модели — с сегодняшней датой первым системным сообщением (модель её не знает)."""
    return [{"role": "system", "content": today_line(clock.NOW())}, *messages]


def estimate_tokens(messages: list[dict]) -> int:
    chars, pics = 0, 0
    for m in messages:
        content = m["content"]
        chars += sum(len(tc["function"]["arguments"]) for tc in m.get("tool_calls", []))
        if content is None:                      # ответ ассистента одними вызовами инструментов
            continue
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

@dataclass
class ToolCall:
    """Вызов инструмента из потока модели: `id` и имя — из первого куска, аргументы — склейкой кусков."""
    id: str
    name: str
    arguments: str


@dataclass(eq=False)
class StreamTask:
    """Запрос к модели ответа `message_id`; очередь — события для потока SSE: ("ping", None),
    ("delta", текст), ("tool", {kind, label}), ("done", статус) или ("error", слова). Поток SSE закрылся
    (клиент ушёл) — `listening` гаснет, и события больше не копятся: ответ только дочитывается и сохраняется.

    `tools` — виды инструментов «Интернета» (web.tools_for; пусто — запрос без tools, как прежде): модель
    зовёт их кругами (не больше TOOL_ROUNDS, в круге — не больше CALLS_PER_ROUND), шаги — в `steps` и сразу в
    `messages.tools`. Перед каждым вызовом — срок ответа и `chats.web` заново: выключили — вызова нет, и
    дальше запросы без tools. `known` — адреса, которые read_page может читать (`_known_links` и найденное
    поиском этого ответа)."""
    user_id: int
    chat_id: str
    message_id: str
    conn: Connection
    model: str
    messages: list
    content: str = ""
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    listening: bool = True
    task: asyncio.Task | None = None
    fresh: bool = False          # данные с прошлого маркера пингов
    saved: float = 0.0
    deadline: float = math.inf   # срок всего ответа (STREAM_TOTAL_SEC), ставит _read
    shutdown: bool = False
    tools: list[str] = field(default_factory=list)
    exa_key: str | None = field(default=None, repr=False)
    limit: int | None = None     # предел контекста модели (chat._limit); None — без обрезки результатов
    steps: list[dict] = field(default_factory=list)
    known: set[str] = field(default_factory=set, repr=False)

    def put(self, kind: str, data) -> None:
        if self.listening:
            self.queue.put_nowait((kind, data))

    async def _pings(self) -> None:
        while True:
            self.fresh = False
            await clock.SLEEP(PING_SEC)
            if not self.fresh:
                self.put("ping", None)

    def _delta(self, text: str) -> None:
        self.content += text
        self.fresh = True
        self.put("delta", text)
        now = clock.NOW()
        if now - self.saved >= SAVE_SEC:
            self.saved = now
            state.DB.execute("UPDATE messages SET content = ? WHERE id = ? AND status = 'streaming'",
                             (self.content, self.message_id))

    def _request(self, offer: bool) -> list[dict]:
        """Сообщения запроса: с tools — к системному сообщению даты дописан WEB_NOTE; копией, история
        (`self.messages`) не меняется."""
        first = self.messages[0] if self.messages else None
        if not offer or first is None or first["role"] != "system":
            return self.messages
        names = ", ".join(spec["function"]["name"] for spec in web.specs(self.tools))
        return [{**first, "content": f"{first['content']}\n\n{WEB_NOTE.format(tools=names)}"}, *self.messages[1:]]

    def _fit(self, step: dict) -> dict:
        """Шаг так, как его получит модель: текст, что не помещается в остаток контекста (`limit` без
        RESERVE_TOKENS), обрезан с пометкой CUT_CONTEXT; остатка меньше 500 знаков — ошибка NO_ROOM. Шаг
        хранит то же, что получила модель: иначе следующий ход не поместился бы из-за полного текста."""
        if self.limit is None or "error" in step:
            return step
        used = estimate_tokens(self._request(True))
        if used + math.ceil(len(step["text"]) / 3) <= self.limit - RESERVE_TOKENS:
            return step
        room = (self.limit - RESERVE_TOKENS - used) * 3 - len(CUT_CONTEXT)
        if room < 500:
            return {**web.public(step), "error": NO_ROOM}
        return {**step, "text": step["text"][:room] + CUT_CONTEXT}

    async def _round(self, payload: dict, offer: bool) -> "tuple[str | None, str | None, list[ToolCall], str]":
        """Один запрос к модели потоком: (слова ошибки или None, finish_reason, вызовы инструментов по
        `index`, текст круга). Текст идёт пользователю дельтами (`_delta`). Отказ 400 о tools, когда запрос
        впервые несёт tools (`offer`), — модель или шлюз их не умеют: NO_TOOLS (vLLM: «"auto" tool choice
        requires…», LiteLLM называет `tools`); прочий 400 (контекст, картинка) — словами шлюза."""
        ending, calls, text = None, {}, ""
        try:
            async with upstream.stream(self.conn, "POST", connections.chat_url(self.conn), json=payload,
                                       timeout=httpx.Timeout(STREAM_READ_SEC, connect=30)) as r:
                if r.status_code != 200:
                    await r.aread()
                    no_tools = offer and r.status_code == 400 and "tool" in r.text.lower()
                    return NO_TOOLS if no_tools else upstream.gateway_error(r, self.conn.name), None, [], text
                async for line in r.aiter_lines():
                    if clock.NOW() > self.deadline:
                        return self._cut_time(), None, [], text
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        ending = ending or "stop"
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
                        return f"модель прервала ответ: {str(words)[:300]}", None, [], text
                    for choice in chunk.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        delta = delta if isinstance(delta, dict) else {}
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            text += piece
                            self._delta(piece)
                        for tc in delta.get("tool_calls") or []:
                            if isinstance(tc, dict):
                                self._collect(calls, tc)
                        if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
                            ending = choice["finish_reason"]
                    if len(self.content) > ANSWER_MAX:
                        self.content = self.content[:ANSWER_MAX] + CUT_SIZE
                        return "ответ слишком длинный — обрезан", None, [], text
        except upstream.UpstreamRefused as e:
            return upstream.refused_words(e, self.conn), None, [], text
        except httpx.TimeoutException:
            return f"шлюз не ответил за {STREAM_READ_SEC} с", None, [], text
        except httpx.HTTPError as e:
            return f"шлюз недоступен: {type(e).__name__}", None, [], text
        return None, ending, [calls[i] for i in sorted(calls)], text

    @staticmethod
    def _collect(calls: dict, tc: dict) -> None:
        """Кусок `delta.tool_calls`: первый кусок вызова несёт `index`, `id` и имя, следующие — части аргументов."""
        index = tc.get("index") if isinstance(tc.get("index"), int) else 0
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        call = calls.setdefault(index, ToolCall(id="", name="", arguments=""))
        if not call.id and isinstance(tc.get("id"), str):
            call.id = tc["id"]
        if not call.name and isinstance(fn.get("name"), str):
            call.name = fn["name"]
        if isinstance(fn.get("arguments"), str):
            call.arguments += fn["arguments"]

    def _cut_time(self) -> str:
        """Срок ответа (STREAM_TOTAL_SEC) вышел: пометка в текст, слова ошибки."""
        self.content += CUT_TIME
        return TIME_WORDS

    def _web_on(self) -> bool:
        """«Интернет» диалога ещё включён (кнопку могли нажать посреди ответа, диалог — удалить)."""
        row = state.DB.execute("SELECT web FROM chats WHERE id = ?", (self.chat_id,)).fetchone()
        return row is not None and bool(row[0])

    async def _tools(self, calls: list[ToolCall], text: str) -> "str | None":
        """Вызовы круга — по порядку: событие `tool`, шаг в `steps` и в базу, ответ модели `role: tool` (и
        вызову без исполнения: id пар должны сойтись). Сверх CALLS_PER_ROUND — шаг TOO_MANY; «Интернет»
        выключили — шаг WEB_OFF, и tools дальше не идут. Итог — слова ошибки ответа (срок вышел) или None."""
        for n, c in enumerate(calls):
            c.id = c.id or f"call_{len(self.steps)}_{n}"
        # битые аргументы — не назад в запрос: vLLM их разбирает и ответил бы 400
        self.messages.append({"role": "assistant", "content": text or None, "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments if web.args_of(c.arguments) is not None else "{}"}}
            for c in calls]})
        for n, c in enumerate(calls):
            if clock.NOW() > self.deadline:
                return self._cut_time()
            kind = web.NAMES.get(c.name, "unknown")
            if n >= CALLS_PER_ROUND:
                step = {"kind": kind, "error": TOO_MANY}
            elif not self.tools or not self._web_on():
                self.tools = []
                step = {"kind": kind, "error": WEB_OFF}
            else:
                self.put("tool", {"kind": kind, "label": web.label(kind, web.args_of(c.arguments) or {})})
                step = self._fit(await web.call(c.name, c.arguments, self.exa_key, self.tools, self.known))
                self.known |= web.found_links(step)
            self.steps.append(step)
            state.DB.execute("UPDATE messages SET tools = ? WHERE id = ? AND status = 'streaming'",
                             (json.dumps(self.steps, ensure_ascii=False), self.message_id))
            log.info("чат %s: ответ %s — шаг %s: %s", self.chat_id[:8], self.message_id[:8], step["kind"],
                     "ошибка" if "error" in step else "ok")
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": web.for_model(step)})
        return None

    async def _read(self) -> str | None:
        """Ответ модели до конца; итог — слова ошибки или None. Конец — `[DONE]` или `finish_reason`:
        поток, кончившийся без них (обрыв, HTML с кодом 200, пустой ответ), — ошибка, а не готовый
        ответ. Обрезанный пределом длины модели, объёма (ANSWER_MAX) или срока (STREAM_TOTAL_SEC) —
        ошибка с пометкой в тексте; прочитанное сохраняется. С `tools` — круги: модель кончила вызовами
        (`finish_reason: tool_calls`) — студия их исполняет и зовёт её снова; после TOOL_ROUNDS кругов
        запрос без tools. Срок — один на весь ответ: проверяется и перед каждым кругом и вызовом. Последний
        круг кончился вызовами, которые уже не исполнялись, без текста — ошибка NO_ANSWER, а не пустой ответ."""
        self.deadline, rounds = clock.NOW() + STREAM_TOTAL_SEC, 0
        while True:
            if clock.NOW() > self.deadline:
                return self._cut_time()
            offer = bool(self.tools) and rounds < TOOL_ROUNDS
            payload = {"model": self.model, "messages": self._request(offer), "stream": True}
            if offer:
                payload |= {"tools": web.specs(self.tools), "tool_choice": "auto"}
            # 400 на круге после вызовов — уже не «не умеет tools», а отказ шлюза его словами
            error, ending, calls, text = await self._round(payload, offer and rounds == 0)
            if error is not None:
                return error
            if not (offer and ending == "tool_calls" and calls):
                break
            rounds += 1
            error = await self._tools(calls, text)
            if error is not None:
                return error
            if text and not text[-1].isspace():          # текст до вызовов и ответ — не слитно
                self._delta("\n\n")
        if ending is None:
            return "модель оборвала ответ: поток кончился без признака конца"
        if ending == "tool_calls" and not text.strip():
            return NO_ANSWER
        if ending == "length":
            self.content += CUT_LENGTH
            return "ответ обрезан пределом длины модели"
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
        self.put(*(("error", error) if error is not None else ("done", status)))


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
            st.put("done", status)
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
    """События SSE хода из очереди ответа; глушит очередь, когда поток закрылся, — `_EventStream`."""
    yield _sse("turn", {"user_message_id": user_message_id, "assistant_message_id": st.message_id})
    while True:
        kind, data = await st.queue.get()
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "delta":
            yield _sse("delta", {"text": data})
        elif kind == "tool":
            yield _sse("tool", data)
        elif kind == "done":
            yield _sse("done", {"status": data})
            return
        else:
            yield _sse("error", {"message": data})
            return


class _EventStream(StreamingResponse):
    """Поток SSE хода. Закрылся (клиент ушёл, ответ кончился) — очередь ответа больше не пополняется,
    и глушит её сам ответ, а не finally генератора: отмена может застать ответ в `send` (генератор так и
    стоял бы на yield до сборщика мусора) или ещё до первого куска (тела генератора не было вовсе)."""

    def __init__(self, st: StreamTask, user_message_id: str):
        super().__init__(_events(st, user_message_id), media_type="text/event-stream",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.st = st

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.st.listening = False
            self.st.queue = asyncio.Queue()              # непрочитанное — прочь
            await self.body_iterator.aclose()           # генератор без finally: отмена не мешает


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
    return _chat_view(user.id, _chat_row(user.id, chat_id))


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: str, request: Request):
    uid = request.state.user.id
    row = _chat_row(uid, chat_id)
    atts = _attachments(chat_id)
    return {"chat": _chat_view(uid, row), "messages": [_message_out(m, atts.get(m["id"], [])) for m in _messages(chat_id)]}


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
    if body.web is not None:
        if body.web and not web.has_exa_key(uid):            # без ключа «Интернет» не включается
            raise HTTPException(409, {"code": "no_exa_key", "message": NO_EXA_KEY})
        changes["web"] = int(body.web)
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE chats SET {cols} WHERE id = ?", (*changes.values(), chat_id))
    return _chat_view(uid, _chat_row(uid, chat_id))


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


def _known_links(chat_id: str) -> set[str]:
    """Адреса, которые read_page может читать в ходе (web.norm, спека 6.4): из сообщений пользователя и
    вложенных документов диалога и результаты поиска прежних ответов (`messages.tools`). Синхронно — в
    транзакции хода."""
    known = set()
    for row in state.DB.execute("SELECT role, content, tools FROM messages WHERE chat_id = ?", (chat_id,)):
        if row["role"] == "user":
            known |= web.links(row["content"])
        for step in json.loads(row["tools"] or "[]"):
            known |= web.found_links(step)
    for row in state.DB.execute("SELECT a.text FROM attachments a JOIN messages m ON m.id = a.message_id"
                                " WHERE m.chat_id = ? AND a.kind = 'document'", (chat_id,)):
        known |= web.links(row["text"])
    return known


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
    free = body.external_ok or sections.is_local(conn, model)
    now, answer_id = clock.NOW(), uuid.uuid4().hex
    # без await до COMMIT: другой запрос не вклинится, отказ откатывает всё
    state.DB.execute("BEGIN")
    try:
        # инструменты «Интернета» этого хода — по кнопке, какой она стала к ходу (`row` прочитан до await
        # _limit); прямому подключению — никаких. Ключ Exa не расшифровался — шаги поиска скажут это (web.NO_KEY)
        fresh = state.DB.execute("SELECT web FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if fresh is None:
            raise _gone()
        kinds = web.tools_for(conn, web.has_exa_key(uid)) if fresh["web"] else []
        try:
            exa_key = web.exa_key(uid) if "search" in kinds else None
        except web.ToolError:
            exa_key = None
        # ответ прямого подключения, который так и не сохранили (вкладку закрыли), — не вечная загрузка;
        # живой ответ сюда не доходит: его ждёт _busy
        state.DB.execute("UPDATE messages SET status = 'error' WHERE chat_id = ? AND via = 'browser'"
                         " AND status = 'streaming'", (chat_id,))
        # расшифровка в истории (и в этом ходе) к нелокальной модели — только с подтверждением; у повтора
        # след подтверждения ищется до удаления прежнего ответа: он мог быть им
        if body.regenerate:
            ask = not free and _needs_consent(chat_id, model, via)
            user_mid = _drop_last_answer(chat_id)
        else:
            user_mid = uuid.uuid4().hex
            state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                             " VALUES (?, ?, 'user', ?, 'done', ?, ?, ?)", (user_mid, chat_id, body.text, via, model, now))
            for a in atts:
                if state.DB.execute("UPDATE attachments SET message_id = ? WHERE id = ? AND message_id IS NULL",
                                    (user_mid, a["id"])).rowcount != 1:
                    raise _gone("вложения нет или оно уже отправлено")
            ask = not free and _needs_consent(chat_id, model, via)
        if ask:
            raise HTTPException(409, {"code": "external",
                                      "message": f"Расшифровка уйдёт во внешний сервис «{conn.name}» — подтвердите"})
        # Exa получает запросы, составленные моделью из всей истории (6.10): расшифровка новее подтверждения —
        # только с `web_ok`; подтверждение — `now`, им покрыта и расшифровка этого хода (created = now)
        if "search" in kinds:
            if body.web_ok:
                state.DB.execute("UPDATE chats SET web_consent = ? WHERE id = ?", (now, chat_id))
            elif _needs_web_consent(chat_id, state.DB.execute(
                    "SELECT web_consent FROM chats WHERE id = ?", (chat_id,)).fetchone()[0]):
                raise HTTPException(409, {"code": "external_web", "message": (
                    "Запросы, составленные из расшифровки, могут уйти в Exa — подтвердите")})
        messages = compose(chat_id, user_mid)
        known = _known_links(chat_id) if "page" in kinds else set()
        tokens = estimate_tokens(messages)
        if limit is not None and tokens > limit - RESERVE_TOKENS:
            log.info("чат %s: ход не помещается в контекст (~%d токенов)", chat_id[:8], tokens)
            raise _too_big(tokens, limit)
        # идущие ответы через сервер — по строкам `streaming` (и те, чей поток ещё не запущен)
        if via == "server" and state.DB.execute(
                "SELECT COUNT(*) FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.user_id = ?"
                " AND m.via = 'server' AND m.status = 'streaming'", (uid,)).fetchone()[0] >= USER_STREAMS:
            log.info("чат %s: ход отклонён — у пользователя идёт предел ответов", chat_id[:8])
            raise HTTPException(429, {"code": "streams", "message": (
                f"Сразу идёт не больше {USER_STREAMS} ответов в разных диалогах — дождитесь одного из них"
                " или остановите")})
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
        messages = with_date(await fill_photos(chat_id, uid, messages))    # файлы — после COMMIT и в потоке
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
    st = StreamTask(user_id=uid, chat_id=chat_id, message_id=answer_id, conn=conn, model=model, messages=messages,
                    tools=kinds, exa_key=exa_key, limit=limit, known=known)
    STREAMS[answer_id] = st
    st.task = asyncio.create_task(st.run())
    return _EventStream(st, user_mid)


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
