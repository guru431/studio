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
   (без них — ошибка), у пользователя сразу — не больше USER_STREAMS таких ответов (429). Шлюз студии
   ответил 503 (карта занята) — ход ждёт и повторяет (RETRY_SEC, WAIT_MAX_SEC; событие `wait`, «Не ждать» —
   `POST .../nowait`). Причина отказа ответа — `messages.error`. Поток SSE сверяет сессию и блокировку
   «Чата»: закрылась — событие `closed`, а ответ дочитывается.
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
8. Скан (спека 2026-10-27-studio-ocr-design, 6.5): PDF со страницами без текстового слоя при подключении
   раздела «Распознавание» через сервер — документ ocr.py, вложение без текста, пока он идёт. Ход через
   сервер с таким вложением (или с идущим сканом в истории диалога) создаётся как обычно, а StreamTask до
   запроса к модели ждёт документы (событие SSE `ocr`), затем собирает контекст и проверяет предел; срок
   ответа отсчитывается от готовности. Прямому подключению — 409 `not_ready`: ждёт страница.
"""
import asyncio
import hashlib
from html import escape
import re
import base64
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

import clock
import connections
import cleaner_client as cleaner
import documents
import images
import lock_policy
import manage
import media
import ocr
import ocr_render
import operations
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
# пределы контекста из списка моделей подключения (_limit): список почти не меняется, а у OpenRouter он —
# сотни КБ JSON, до MODELS_TIMEOUT_SEC перед каждым ходом. Удачный список живёт CONTEXT_TTL_SEC, записей —
# не больше CONTEXT_KEEP; неудача не запоминается
CONTEXT_TTL_SEC = 600
CONTEXT_KEEP = 256
LIMITS: dict[tuple, tuple[float, dict[str, int]]] = {}
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
# рассуждение модели (спека 2026-10-22-studio-chat-tools-badge-reasoning, 4.3) — пределы на весь ответ, все круги.
# Видимое (кнопка включена или у модели её нет) — до REASONING_MAX знаков: дальше с кнопкой ответ остановлен (петля
# держала бы карту), у модели без неё — пометка REASONING_CUT, ответ дочитывается. При выключенной кнопке gemma-abl в
# нормальных ответах роняет 7–14 знаков «thought», а в петле — тысячи (3–4,5 тыс. за 75 с, 2026-10-22): сверх
# STRAY_MAX — петля, ответ остановлен, а не идёт до STREAM_TOTAL_SEC, держа карту
REASONING_MAX = 60_000
STRAY_MAX = 2_000
THINK_EVERY_SEC = 1              # «думает» (`think {}`) при выключенной кнопке — на страницу не чаще
THINK_LONG = "рассуждение слишком длинное — ответ остановлен"
REASONING_CUT = "\n\n[рассуждение обрезано: длиннее предела студии]"
LOOPED = "модель зациклилась — ответ остановлен"
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
NO_EXA_KEY = "сначала задайте ключ Exa в Настройках → Ввод и интернет → Интернет"   # PATCH {web: true} без ключа — 409
NO_THINKING = "эта модель не умеет рассуждать по кнопке"   # PATCH {thinking: true} не у роли local-* srvllm — 409
# к системному сообщению даты, пока запрос несёт tools: без неё gemma-abl на «найди в интернете» отвечала по
# памяти (проба 2026-10-21, спека раздел 8). Последняя фраза — против «я заглянула в интернет» без вызова: на
# диалоге, где модель уже так ответила, вызов поиска на «какой курс доллара» — 10 из 12 вместо 5 из 12 (2026-10-22)
WEB_NOTE = ("У тебя есть доступ к интернету через инструменты: {tools}. Если пользователь просит найти что-то в "
            "интернете, прочитать ссылку или ролик YouTube или нужны свежие данные — вызови инструмент, а не "
            "отвечай по памяти. Говори, что нашёл что-то в интернете, только если инструмент в этом ответе вернул "
            "результат; если вернул ошибку — так и скажи и не выдумывай данные.")
STORAGE = "хранилище недоступно"
#: 503 шлюза студии (srvllm, s06) — карта занята генерацией или распознаванием: ход ждёт и повторяет запрос раз в
#: RETRY_SEC, не дольше WAIT_MAX_SEC (как ocr, translate); срок не идёт, пока карту держит своё распознавание
#: пользователя (ocr.holding). Ожидание не съедает срок ответа (STREAM_TOTAL_SEC)
RETRY_SEC = 15
WAIT_MAX_SEC = 1800
CARD_BUSY = "карта занята дольше {} минут — попробуйте позже"
GAVE_UP = "карта занята — ждать не стали"
CHECK_SEC = 1                    # поток SSE сверяет свою сессию на событиях не чаще (на пингах — всегда)
SCAN_POLL_SEC = 1               # ход ждёт скан: состояние документов — из базы раз в столько
NO_OCR = "это скан — подключите «Сканы» в настройках"
SOME_SCANS = "страниц-сканов в PDF — {} из {}, без распознавания модель получила бы документ без них"
#: начало слов ответа-ошибки «скан не распознан: …» — они хранятся в content ответа, страница получает их `error`
SCAN_FAILED = "скан не распознан"
NOT_READY = "скан ещё распознаётся"
NO_PHOTO = "[фото недоступно: файла нет в хранилище]"
DROPPED = f"[фото убрано из контекста: модели — не больше {MAX_IMAGES} картинок на запрос]"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
DOC_MIME = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
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
    thinking: bool | None = None         # кнопка «Рассуждение»; null — не трогать


class CleanerRule(BaseModel):
    action: Literal["mask", "unmask"]
    value: str = Field(min_length=1, max_length=2000)
    type: str | None = Field(None, max_length=32)


class TurnIn(BaseModel):
    request_id: str | None = Field(None, pattern=r"^[a-zA-Z0-9_-]{16,64}$")
    text: str = Field("", max_length=TEXT_MAX)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)
    regenerate: bool = False
    external_ok: bool = False        # расшифровку — и нелокальной модели (человек подтвердил)
    web_ok: bool = False             # расшифровка в диалоге, а «Интернет» даёт Exa (человек подтвердил)
    cleaner_ticket: str | None = Field(None, max_length=64)
    cleaner_rules: list[CleanerRule] = Field(default_factory=list, max_length=100)
    cleaner_local: bool = False


class AnswerIn(BaseModel):
    # как у ответа через сервер: до ANSWER_MAX знаков и пометка обрезки (длиннейшая — CUT_SIZE)
    content: str = Field(max_length=ANSWER_MAX + len(CUT_SIZE))
    status: Literal["done", "stopped", "error"]


class CleanerCopyIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)


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
    бы 409 `external_web`); `thinking` — кнопка «Рассуждение» и `thinking_ok` — действует ли она у модели диалога
    (та же `local`: параметр уходит только ролям `local-*` srvllm через сервер)."""
    out, conn = _chat_out(row), None
    if row["connection_id"] is not None:
        try:
            conn = connections.get(user_id, row["connection_id"])
        except HTTPException:                        # подключение удалено — ход ответит 404
            pass
    model = row["model"] or manage.DEFAULT_MODEL
    out["local"] = conn is not None and sections.is_local(conn, model)
    out["cleaner"] = conn is not None and conn.cleaner
    out["external"] = conn is not None and not conn.cleaner and not out["local"] and _needs_consent(
        row["id"], model, "server" if conn.proxied else "browser")
    out["web"] = bool(row["web"])
    out["proxied"] = conn is not None and conn.proxied
    out["web_tools"] = [] if out["cleaner"] else web.tools_for(conn, web.has_exa_key(user_id))
    out["external_web"] = out["web"] and "search" in out["web_tools"] and _needs_web_consent(
        row["id"], row["web_consent"])
    out["thinking"] = bool(row["thinking"])
    out["thinking_ok"] = out["local"]
    return out


def _thinking_ok(user_id: int, conn_id: "int | None", model: "str | None") -> bool:
    """Кнопка «Рассуждение» действует: роль `local-*` srvllm через сервер (sections.is_local) — студия проверила,
    что `chat_template_kwargs.enable_thinking` до неё доходит; облачной роли LiteLLM передал бы его провайдеру."""
    if conn_id is None:
        return False
    try:
        conn = connections.get(user_id, conn_id)
    except HTTPException:                            # подключение удалено
        return False
    return sections.is_local(conn, model or manage.DEFAULT_MODEL)


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


def _message_out(row, atts: list, views: dict | None = None) -> dict:
    """Сообщение для страницы; `tools` — шаги «Интернета» без текста для модели (web.public), `reasoning` и
    `reasoning_sec` — рассуждение ответа (у идущего — прочитанное; не было или скрыто — null), `ocr` — ход
    распознавания, которого ждёт идущий ответ (`{page, pages}`; не ждёт — null), `wait` — секунды ожидания карты
    идущим ответом (не ждёт — null), `error` — слова ответа-ошибки (`messages.error`; у строк до шага 35 — только
    «скан не распознан: …» из content). Слова, записанные и в content (скан, хранилище: прежний код читает их
    оттуда), — не текст ответа: тогда content — пустой, модели ответ не уходил."""
    live = STREAMS.get(row["id"])
    steps = live.steps if live else json.loads(row["tools"] or "[]")
    reasoning, seconds = live.thought_out() if live else (row["reasoning"], row["reasoning_sec"])
    content = live.content if live else row["content"]
    error = None
    if not live and row["status"] == "error" and row["role"] == "assistant":
        error = row["error"] or (row["content"] if row["content"].startswith(SCAN_FAILED) else None)
        if error is not None and content == error:
            content = ""
    return {"id": row["id"], "role": row["role"], "content": content,
            "cleaner_raw": row["cleaner_raw"],
            "error": error,
            "status": row["status"], "via": row["via"], "model": row["model"], "created": row["created"],
            "live": _alive(row),
            "attachments": [views[a["id"]] if views is not None else _attachment_out(a) for a in atts],
            "tools": [web.public(s) for s in steps], "reasoning": reasoning, "reasoning_sec": seconds,
            "ocr": live.scan_state if live else None, "wait": live.waited() if live else None}


def _attachment_out(a, scans: dict | None = None, sources: set | None = None, rescan: set | None = None) -> dict:
    """Вложение сообщения; из расшифровки — ещё `source` (есть ли ещё своя расшифровка: ссылка «к записи»);
    документ, чей скан ещё распознаётся, — `ocr {id}` (знаков у него пока нет; страница опрашивает документ).
    Текст есть — скан кончился: запроса нет. `rescan: true` — скан вложения кончился с нераспознанными
    страницами («Распознать заново», `POST /api/attachments/{id}/ocr`); считается только в GET диалога."""
    out = {"id": a["id"], "kind": a["kind"], "name": a["name"], "media_id": a["media_id"], "chars": a["chars"]}
    if a["kind"] == "document" and a["text"] is None:
        pending = ocr.pending(a["user_id"], [a["id"]]) if scans is None else scans.get(a["id"], [])
        if pending:
            out["ocr"] = {"id": pending[0]}
    if rescan is not None and a["id"] in rescan:
        out["rescan"] = True
    if a["source_transcript_id"] is not None:
        exists = (state.DB.execute("SELECT 1 FROM transcripts WHERE id = ? AND user_id = ?",
                                   (a["source_transcript_id"], a["user_id"])).fetchone() is not None
                  if sources is None else a["source_transcript_id"] in sources)
        out["source"] = {"transcript_id": a["source_transcript_id"], "exists": exists}
    return out


def _sane(text: str) -> str:
    """Текст модели без одиночных суррогатов UTF-16 (`\\ud800` в JSON потока): SQLite такой не кодирует, и ответ
    навсегда остался бы `streaming`. Пара суррогатов — свой символ, одиночный — U+FFFD."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _sane_strings(obj: dict) -> dict:
    """`object_hook` разбора куска потока: строки объекта — `_sane`."""
    return {k: _sane(v) if isinstance(v, str) else v for k, v in obj.items()}


def _finish(message_id: str, chat_id: str, content: str, status: str, reasoning: "str | None" = None,
            reasoning_sec: "float | None" = None, error: "str | None" = None) -> bool:
    """Итог идущего ответа (`error` — причина отказа словами); False — он уже сохранён (или удалён)."""
    done = state.DB.execute("UPDATE messages SET content = ?, status = ?, reasoning = ?, reasoning_sec = ?, error = ?"
                            " WHERE id = ? AND status = 'streaming'",
                            (content, status, reasoning, reasoning_sec, error, message_id)).rowcount == 1
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
    docs = "".join(f'<document name="{escape(a["name"], quote=True)}">\n{a["text"]}\n</document>\n\n'
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


def today_line(now: float, zone: str = "Europe/Moscow") -> str:
    d = datetime.fromtimestamp(now, ZoneInfo(zone))
    label = "Москва" if zone == "Europe/Moscow" else zone
    return f"Сегодня {WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year} года ({label})."


def with_date(messages: list[dict], user_id: int) -> list[dict]:
    """Запрос к модели — с сегодняшней датой первым системным сообщением (модель её не знает)."""
    operations.check(user_id)
    zone = state.DB.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    return [{"role": "system", "content": today_line(clock.NOW(), zone)}, *messages]


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
    сервер (запомненный в LIMITS на CONTEXT_TTL_SEC); не узнать — None (тогда без проверки)."""
    if model in CONTEXT:
        return CONTEXT[model]
    if not conn.proxied:
        return None
    now = clock.NOW()
    for key in [k for k, (at, _) in LIMITS.items() if not 0 <= now - at < CONTEXT_TTL_SEC]:
        del LIMITS[key]
    key = (conn.user_id, conn.id, conn.kind, conn.base_url)
    if key in LIMITS:
        return LIMITS[key][1].get(model)
    try:
        r = await upstream.request(conn, "GET", connections.models_url(conn), timeout=MODELS_TIMEOUT_SEC)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return None
    data = upstream.json_body(r).get("data") if r.is_success else None
    if not isinstance(data, list):
        return None
    lengths = {}
    for m in data:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            length = m.get("context_length")
            if isinstance(length, int) and not isinstance(length, bool) and length > 0:
                lengths.setdefault(m["id"], length)
    while len(LIMITS) >= CONTEXT_KEEP:
        del LIMITS[next(iter(LIMITS))]
    LIMITS[key] = (now, lengths)
    return lengths.get(model)


def _too_big(tokens: int, limit: int) -> HTTPException:
    return HTTPException(413, {"code": "context", "message": (
        f"Не помещается в контекст модели: ~{round(tokens / 1000)} тыс. токенов при пределе {limit // 1000} тыс. "
        "Уберите документ или начните новый диалог.")})


# ---- поток через сервер --------------------------------------------------------

#: «конец» круга, когда шлюз студии ответил 503 (`StreamTask._round`): не finish_reason модели
BUSY = object()


@dataclass
class ToolCall:
    """Вызов инструмента из потока модели: `id` и имя — из первого куска, аргументы — склейкой кусков."""
    id: str
    name: str
    arguments: str


@dataclass(eq=False)
class StreamTask:
    """Запрос к модели ответа `message_id`; очередь — события для потока SSE: ("ping", None),
    ("delta", текст), ("think", текст или None), ("tool", {kind, label}), ("ocr", {page, pages}) — ход скана, которого
    ждёт ответ (`scans`), ("wait", {seconds} или None) — ожидание карты после 503 шлюза студии (None — шлюз
    принял запрос), ("done", статус) или ("error", слова). Поток SSE закрылся
    (клиент ушёл) — `listening` гаснет, и события больше не копятся: ответ только дочитывается и сохраняется.

    `tools` — виды инструментов «Интернета» (web.tools_for; пусто — запрос без tools, как прежде): модель
    зовёт их кругами (не больше TOOL_ROUNDS, в круге — не больше CALLS_PER_ROUND), шаги — в `steps` и сразу в
    `messages.tools`. Перед каждым вызовом и кругом — срок ответа и `chats.web` заново: выключили — вызова нет,
    и дальше запросы без tools. `known` — адреса, которые read_page может читать, и ролики (по id) для
    youtube_subtitles (`_known_links` и найденное поиском этого ответа).

    Рассуждение модели (`delta.reasoning_content`, иначе `delta.reasoning`) — `_think`: видно (`thinking` не
    False) — текстом в `reasoning` и событием `think`, секунды — `reasoning_sec`; кнопка выключена — только
    «думает». Модели оно назад не уходит: ни в круге инструментов, ни в следующем ходе (compose)."""
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
    known: dict[str, str] = field(default_factory=dict, repr=False)
    #: кнопка «Рассуждение» хода: None — модель без кнопки (параметра в запросе нет), иначе `enable_thinking`
    thinking: bool | None = None
    reasoning: str = field(default="", repr=False)   # видимое рассуждение всех кругов (круги — через пустую строку)
    reasoning_sec: float = 0.0   # его длительность по кончившимся отрезкам
    think_from: float | None = None   # начало идущего отрезка: от первого куска рассуждения до текста ответа
    reasoning_cut: bool = False  # модель без кнопки дошла до REASONING_MAX: дальше рассуждение не хранится
    stray: int = 0             # знаков рассуждения при выключенной кнопке
    thought: float = -math.inf   # когда ушло последнее `think {}`
    #: сканы, которых ждёт ход (ocr.py): `messages` пуст, контекст до `user_message_id` собирает _await_scans
    scans: list[str] = field(default_factory=list)
    user_message_id: str | None = None
    scan_state: dict | None = None   # ход распознавания для страницы ({page, pages}), пока ждём
    cleaner_preview: object = field(default=None, repr=False)
    #: ждём карту (503 шлюза): (начало ожидания — для слов, начало отсчёта срока WAIT_MAX_SEC); не ждём — None
    card_wait: "tuple[float, float] | None" = None
    gave_up: bool = False        # «Не ждать»: отмена ожидания — ошибка GAVE_UP, а не «остановлено»
    freed: bool = False          # модели картинок отпущены (первый 503 локальной модели, раз на ход)

    def waited(self) -> "float | None":
        """Секунды ожидания карты для страницы; не ждём — None."""
        return None if self.card_wait is None else clock.NOW() - self.card_wait[0]

    def _card_gateway(self) -> bool:
        """503 значит «карта занята» только у шлюза студии: srvllm и s06 (у облака — отказ его словами)."""
        return self.conn.kind == "srvllm" or self.conn.s06

    def _card_free(self) -> None:
        """Шлюз принял запрос: ожидание карты кончилось — страница прячет плашку (`wait {}`)."""
        if self.card_wait is not None:
            self.card_wait = None
            self.fresh = True
            self.put("wait", None)

    async def _wait_card(self) -> "str | None":
        """503 шлюза: событие `wait {seconds}` и пауза RETRY_SEC; итог — слова ошибки (ждём дольше WAIT_MAX_SEC)
        или None — повторить запрос. Срок не идёт, пока карту держит своё распознавание пользователя
        (`ocr.holding`: его скан, снимок перевода) — её освободит его конец; срок ответа пауза не съедает."""
        now = clock.NOW()
        shown, counted = self.card_wait or (now, now)
        if ocr.holding(self.user_id):
            counted = now
        if now - counted >= WAIT_MAX_SEC:
            log.info("чат %s: ответ %s — карта занята дольше срока", self.chat_id[:8], self.message_id[:8])
            self.card_wait = None
            return CARD_BUSY.format(WAIT_MAX_SEC // 60)
        if self.card_wait is None:
            log.info("чат %s: ответ %s — жду карту", self.chat_id[:8], self.message_id[:8])
        self.card_wait = (shown, counted)
        self.fresh = True
        self.put("wait", {"seconds": round(now - shown)})
        await clock.SLEEP(RETRY_SEC)
        self.deadline += clock.NOW() - now
        return None

    async def _free_images(self) -> None:
        """Первый 503 локальной модели: release моделей картинок (Lustify, FLUX держат карту арендой до 10 минут
        после генерации), как перед переводом и озвучкой, — но только когда карта занята, а не перед каждым
        ходом: тёплый Lustify без нужды не выгружается. Итог не важен: идущую генерацию release не оборвёт (409),
        её дождётся `_wait_card`."""
        log.info("чат %s: ответ %s — освобождаю модели картинок", self.chat_id[:8], self.message_id[:8])
        try:
            for model in images.MODELS.values():
                await images.release(self.conn, model)
        except (httpx.HTTPError, upstream.UpstreamRefused):
            return

    def put(self, kind: str, data) -> None:
        if self.listening:
            self.queue.put_nowait((kind, data))

    async def _pings(self) -> None:
        while True:
            self.fresh = False
            await clock.SLEEP(PING_SEC)
            if not self.fresh:
                self.put("ping", None)

    def _save(self) -> None:
        """Идущий ответ — в базу не чаще SAVE_SEC: текст, рассуждение и его секунды."""
        now = clock.NOW()
        if now - self.saved >= SAVE_SEC:
            self.saved = now
            state.DB.execute("UPDATE messages SET content = ?, reasoning = ?, reasoning_sec = ?"
                             " WHERE id = ? AND status = 'streaming'", (self.content, *self.thought_out(), self.message_id))

    def _delta(self, text: str) -> None:
        self.content += text
        self.fresh = True
        self.put("delta", text)
        self._save()

    def thought_out(self) -> "tuple[str | None, float | None]":
        """Рассуждение для базы и страницы: (текст, секунды с идущим отрезком); не было или скрыто — (None, None)."""
        if not self.reasoning:
            return None, None
        running = clock.NOW() - self.think_from if self.think_from is not None else 0.0
        return self.reasoning, self.reasoning_sec + running

    def _think_end(self) -> None:
        """Отрезок рассуждения кончился (пошёл текст ответа, кончился круг): его секунды — в сумму."""
        if self.think_from is not None:
            self.reasoning_sec += clock.NOW() - self.think_from
            self.think_from = None

    def _over(self, words: str) -> str:
        log.info("чат %s: ответ %s — рассуждение сверх предела", self.chat_id[:8], self.message_id[:8])
        return words

    def _think(self, piece: str) -> "str | None":
        """Кусок рассуждения модели; итог — слова ошибки (предел) или None. Видно — текстом в `reasoning` и
        событием `think` (первый кусок отрезка — нового круга или после текста — через пустую строку); сверх
        REASONING_MAX — обрезано: кнопка включена — ответ остановлен, у модели без кнопки — пометка REASONING_CUT,
        дальше рассуждение не идёт и не хранится, а ответ дочитывается. Кнопка выключена (`thinking is False`) —
        текст не идёт и не хранится, только `think {}` не чаще THINK_EVERY_SEC; сверх STRAY_MAX знаков — петля."""
        now = clock.NOW()
        if self.thinking is False:
            self.stray += len(piece)
            if self.stray > STRAY_MAX:
                return self._over(LOOPED)
            if now - self.thought >= THINK_EVERY_SEC:
                self.thought, self.fresh = now, True
                self.put("think", None)
            return None
        if self.think_from is None:                  # новый отрезок: новый круг или снова после текста
            self.think_from = now
            if self.reasoning:
                piece = "\n\n" + piece
        if self.reasoning_cut:
            return None
        shown = piece[:REASONING_MAX - len(self.reasoning)]
        over = len(shown) < len(piece)
        if over and self.thinking is None:           # облачная модель: её ответ оплачен — не терять (решение 2026-10-22)
            self.reasoning_cut = True
            shown += REASONING_CUT
            log.info("чат %s: ответ %s — рассуждение обрезано", self.chat_id[:8], self.message_id[:8])
        if shown:
            self.reasoning += shown
            self.fresh = True
            self.put("think", shown)
        if over and not self.reasoning_cut:
            return self._over(THINK_LONG)
        self._save()
        return None

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
                if r.status_code == 503 and self._card_gateway():
                    await r.aread()
                    return None, BUSY, [], text                 # карта занята — ждать (_read)
                if r.status_code != 200:
                    await r.aread()
                    no_tools = offer and r.status_code == 400 and re.search(
                        r"(?:tools?|tool[_ ]choice).{0,40}(?:not supported|unsupported|not enabled|requires)"
                        r"|(?:does not|doesn't|cannot) support (?:tools?|function calling)"
                        r"|(?:not supported|unsupported).{0,20}(?:tools?|tool[_ ]choice)", r.text.lower())
                    return NO_TOOLS if no_tools else upstream.gateway_error(r, self.conn.name), None, [], text
                self._card_free()
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
                        chunk = json.loads(data, object_hook=_sane_strings)
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
                        thought = delta.get("reasoning_content")
                        if not (isinstance(thought, str) and thought):
                            thought = delta.get("reasoning")
                        if isinstance(thought, str) and thought and (error := self._think(thought)) is not None:
                            return error, None, [], text
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            self._think_end()
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
        finally:
            self._think_end()
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
                step = {"kind": kind, **web.asked(kind, c.arguments), "error": TOO_MANY}
            elif not self.tools or not self._web_on():
                self.tools = []
                step = {"kind": kind, **web.asked(kind, c.arguments), "error": WEB_OFF}
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
        if self.conn.cleaner:
            try:
                async with asyncio.timeout(STREAM_TOTAL_SEC):
                    async for kind, data in cleaner.events(self.cleaner_preview):
                        data = _sane_strings(data)
                        if isinstance(data.get("text"), str):
                            self.content = data["text"]
                            if len(self.content) > ANSWER_MAX:
                                self.content = self.content[:ANSWER_MAX] + CUT_SIZE
                                return "Ответ слишком длинный"
                            self.fresh = True
                            self.put("replace", {"text": self.content})
                            self._save()
                        if kind == "done":
                            raw = data.get("raw")
                            if isinstance(raw, str) and len(raw) <= ANSWER_MAX:
                                state.DB.execute("UPDATE messages SET cleaner_raw = ? WHERE id = ?",
                                                 (raw, self.message_id))
                                self.put("cleaner", {"raw": raw})
                            if data.get("status") != "done":
                                return data.get("error") or "Ответ Cleaner оборвался"
                return None
            except HTTPException as exc:
                return exc.detail["message"]
            except (httpx.HTTPError, upstream.UpstreamRefused):
                return "Связь с Cleaner оборвалась: сохранена полученная часть"
            except TimeoutError:
                return self._cut_time()
        self.deadline, rounds = clock.NOW() + STREAM_TOTAL_SEC, 0
        while True:
            if clock.NOW() > self.deadline:
                return self._cut_time()
            if self.tools and not self._web_on():         # выключили на последнем вызове круга — круг без tools
                self.tools = []
            offer = bool(self.tools) and rounds < TOOL_ROUNDS
            payload = {"model": self.model, "messages": self._request(offer), "stream": True}
            if offer:
                payload |= {"tools": web.specs(self.tools), "tool_choice": "auto"}
            if self.thinking is not None:
                payload["chat_template_kwargs"] = {"enable_thinking": self.thinking}
            # 400 на круге после вызовов — уже не «не умеет tools», а отказ шлюза его словами
            error, ending, calls, text = await self._round(payload, offer and rounds == 0)
            if error is not None:
                return error
            if ending == BUSY:                           # 503: тот же круг заново после паузы
                if not self.freed and sections.is_local(self.conn, self.model):
                    self.freed = True                    # первый раз — отпустить модели картинок и сразу повторить
                    await self._free_images()
                    continue
                error = await self._wait_card()
                if error is not None:
                    return error
                continue
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

    async def _await_scans(self) -> str | None:
        """Ход со сканом: ждать документы раз в SCAN_POLL_SEC (событие `ocr` — при смене страниц без слоя),
        затем — пока раннер распознавания освобождает карту (ocr.idle), и только тогда контекст (compose,
        фото, дата) и предел модели. Итог — слова ошибки (документ не распознан, не помещается) или None.
        Отмена хода (не остановка службы) отменяет ждущие в очереди документы, идущий дорабатывает."""
        log.info("чат %s: ответ %s — жду скан (%d док.)", self.chat_id[:8], self.message_id[:8], len(self.scans))
        marks = ", ".join("?" * len(self.scans))
        try:
            while True:
                rows = state.DB.execute(f"SELECT state, detail FROM scans WHERE id IN ({marks})",
                                        tuple(self.scans)).fetchall()
                failed = next((r for r in rows if r["state"] in ("error", "cancelled")), None)
                if len(rows) < len(self.scans) or failed is not None:
                    log.info("чат %s: ответ %s — скан не распознан", self.chat_id[:8], self.message_id[:8])
                    # слова — и в ответ: человек ушёл, пока скан распознаётся (compose ответ-ошибку не берёт)
                    self.content = f"{SCAN_FAILED}: {'документ удалён' if failed is None else failed['detail']}"
                    return self.content
                page, pages = ocr.progress(self.scans)
                if self.scan_state != {"page": page, "pages": pages}:
                    self.scan_state = {"page": page, "pages": pages}
                    self.fresh = True
                    self.put("ocr", self.scan_state)
                if all(r["state"] == "done" for r in rows) and ocr.idle(self.user_id):
                    break
                await clock.SLEEP(SCAN_POLL_SEC)
        except asyncio.CancelledError:
            # «Остановить» ответа отменяет только ждущие в очереди документы; идущий дорабатывает — распознанное
            # уже стоило минут карты, и «Ответить заново» возьмёт его текст. Остановка службы: документы прервёт
            # ocr.stop
            if not self.shutdown:
                for scan_id in self.scans:
                    row = state.DB.execute("SELECT state FROM scans WHERE id = ?", (scan_id,)).fetchone()
                    if row is not None and row["state"] == "queued":
                        await ocr.request_cancel(self.user_id, scan_id)
            raise
        finally:
            self.scan_state = None
        composed = compose(self.chat_id, self.user_message_id)
        tokens = estimate_tokens(composed)
        if self.limit is not None and tokens > self.limit - RESERVE_TOKENS:
            log.info("чат %s: ход не помещается в контекст (~%d токенов)", self.chat_id[:8], tokens)
            return _too_big(tokens, self.limit).detail["message"]
        if self.tools:
            self.known = _known_links(self.chat_id)
        try:
            self.messages = with_date(await fill_photos(self.chat_id, self.user_id, composed), self.user_id)
        except OSError as e:
            log.warning("чат %s: вложение не прочиталось (%s)", self.chat_id[:8], type(e).__name__)
            return STORAGE
        return None

    async def run(self) -> None:
        pings = asyncio.create_task(self._pings())
        error = None
        try:
            if self.scans:
                error = await self._await_scans()
            if error is None:
                error = await self._read()
            status = "done" if error is None else "error"
        except asyncio.CancelledError:               # кнопка «стоп», «Не ждать» или остановка службы
            status = "error" if self.shutdown or self.gave_up else "stopped"
            if self.gave_up and not self.shutdown:
                error = GAVE_UP
        except Exception as e:                       # noqa: BLE001 — ответ не должен висеть
            status, error = "error", f"сбой службы: {type(e).__name__}"
        finally:
            pings.cancel()
        self.card_wait = None
        thought = self.thought_out()
        try:
            _finish(self.message_id, self.chat_id, self.content + (INTERRUPTED if self.shutdown else ""), status,
                    *thought, error=error if status == "error" else None)
        except Exception as e:                       # noqa: BLE001 — ответ не должен остаться в STREAMS
            status, error = "error", f"сбой службы: {type(e).__name__}"
            log.warning("чат %s: ответ %s — итог не записан (%s)", self.chat_id[:8], self.message_id[:8],
                        type(e).__name__)
        finally:
            if STREAMS.get(self.message_id) is self:
                del STREAMS[self.message_id]
        log.info("чат %s: ответ %s — %s", self.chat_id[:8], self.message_id[:8], status)
        self.put(*(("error", error) if error is not None else ("done", _ended_with(status, thought[1]))))


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
        thought = st.thought_out()
        try:                                         # сбой одного итога не оставляет прочие (и lifespan) без своих
            if _finish(st.message_id, st.chat_id, st.content + (INTERRUPTED if st.shutdown else ""), status, *thought):
                st.put("done", _ended_with(status, thought[1]))
        except Exception as e:                       # noqa: BLE001
            log.warning("чат %s: ответ %s — итог не записан (%s)", st.chat_id[:8], st.message_id[:8], type(e).__name__)
        finally:
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
    cleaner.forget(user_id)
    chats = {r["id"] for r in state.DB.execute("SELECT id FROM chats WHERE user_id = ?", (user_id,))}
    _stop_unstarted(chats)
    await _cancel([st for st in STREAMS.values() if st.user_id == user_id])


async def stop_all() -> None:
    """Остановка службы: идущие ответы сохраняются с пометкой о перезапуске."""
    streams = list(STREAMS.values())
    for st in streams:
        st.shutdown = True
    await _cancel(streams)


def _ended_with(status: str, reasoning_sec: "float | None") -> dict:
    """Данные события `done`: статус и секунды рассуждения, что записаны в `messages.reasoning_sec`, — страница
    пишет их, а не свои часы (иначе «1 с» до перечитывания и «2 с» после)."""
    return {"status": status} | ({"reasoning_sec": reasoning_sec} if reasoning_sec is not None else {})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _session_open(digest: str) -> bool:
    """Сессия студии жива, учётка включена и «Чат» не заперт — как их видит `auth.guard` (без продления)."""
    row = state.DB.execute("SELECT u.disabled, s.expires FROM sessions s"
                           " JOIN users u ON u.id = s.user_id WHERE s.digest = ?", (digest,)).fetchone()
    return (row is not None and not row["disabled"] and row["expires"] > clock.NOW()
            and not lock_policy.locked(digest, "chat"))


async def _events(st: StreamTask, user_message_id: str, digest: "str | None" = None):
    """События SSE хода из очереди ответа; глушит очередь, когда поток закрылся, — `_EventStream`. Свою сессию
    (`digest`) поток сверяет на каждом пинге и на событиях не чаще CHECK_SEC: вышли, учётку отключили, сессия
    истекла или «Чат» заперт — `closed`, и поток кончается (ответ дочитывается и сохраняется, как при уходе
    клиента; страница перечитает диалог через обычные проверки входа и блокировки)."""
    yield _sse("turn", {"user_message_id": user_message_id, "assistant_message_id": st.message_id})
    checked = clock.NOW()
    while True:
        kind, data = await st.queue.get()
        now = clock.NOW()
        if digest is not None and kind not in ("done", "error") and (kind == "ping" or now - checked >= CHECK_SEC):
            checked = now
            if not _session_open(digest):
                log.info("чат %s: поток ответа %s закрыт — сессия", st.chat_id[:8], st.message_id[:8])
                yield _sse("closed", {})
                return
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "delta":
            yield _sse("delta", {"text": data})
        elif kind == "think":
            yield _sse("think", {"text": data} if data is not None else {})
        elif kind == "tool":
            yield _sse("tool", data)
        elif kind == "ocr":
            yield _sse("ocr", data)
        elif kind == "wait":
            yield _sse("wait", data if data is not None else {})
        elif kind in ("replace", "cleaner"):
            yield _sse(kind, data)
        elif kind == "done":
            yield _sse("done", data)
            return
        else:
            yield _sse("error", {"message": data})
            return


class _EventStream(StreamingResponse):
    """Поток SSE хода. Закрылся (клиент ушёл, ответ кончился) — очередь ответа больше не пополняется,
    и глушит её сам ответ, а не finally генератора: отмена может застать ответ в `send` (генератор так и
    стоял бы на yield до сборщика мусора) или ещё до первого куска (тела генератора не было вовсе)."""

    def __init__(self, st: StreamTask, user_message_id: str, digest: "str | None" = None):
        super().__init__(_events(st, user_message_id, digest), media_type="text/event-stream",
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
    operations.check(request.state.user.id)
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
    all_atts = [a for group in atts.values() for a in group]
    scans, sources, rescan = {}, set(), set()
    docs = [a["id"] for a in all_atts if a["kind"] == "document"]
    if docs:
        # одним запросом: идущие сканы вложений (`ocr`) и кончившиеся с нераспознанными страницами (`rescan`)
        marks = ",".join("?" * len(docs))
        for scan in state.DB.execute(
                f"SELECT s.attachment_id, s.id, s.state, EXISTS (SELECT 1 FROM scan_pages p WHERE p.scan_id = s.id"
                f" AND p.state IN ('failed', 'waiting')) AS unread FROM scans s WHERE s.user_id = ?"
                f" AND s.attachment_id IN ({marks}) ORDER BY s.created, s.rowid", (uid, *docs)):
            if scan["state"] in ocr.RUNNING:
                scans.setdefault(scan["attachment_id"], []).append(scan["id"])
            elif scan["unread"]:
                rescan.add(scan["attachment_id"])
        rescan -= set(scans)
    linked = {a["source_transcript_id"] for a in all_atts if a["source_transcript_id"] is not None}
    if linked:
        marks = ",".join("?" * len(linked))
        sources = {r[0] for r in state.DB.execute(
            f"SELECT id FROM transcripts WHERE user_id = ? AND id IN ({marks})", (uid, *linked))}
    views = {a["id"]: _attachment_out(a, scans, sources, rescan) for a in all_atts}
    return {"chat": _chat_view(uid, row),
            "messages": [_message_out(m, atts.get(m["id"], []), views) for m in _messages(chat_id)]}


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
        row = _chat_row(uid, chat_id)
        if body.web and row["connection_id"] is not None and connections.get(uid, row["connection_id"]).cleaner:
            raise cleaner.refused("Интернет недоступен в подключении Cleaner: поисковые запросы уходят отдельно")
        if body.web and not web.has_exa_key(uid):            # без ключа «Интернет» не включается
            raise HTTPException(409, {"code": "no_exa_key", "message": NO_EXA_KEY})
        changes["web"] = int(body.web)
    if body.thinking is not None:
        # по паре, какой она станет; смена пары кнопку не сбрасывает — у модели без поддержки она хранится
        row = _chat_row(uid, chat_id)
        conn_id, model = changes.get("connection_id", row["connection_id"]), changes.get("model", row["model"])
        if body.thinking and not _thinking_ok(uid, conn_id, model):
            raise HTTPException(409, {"code": "no_thinking", "message": NO_THINKING})
        changes["thinking"] = int(body.thinking)
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE chats SET {cols} WHERE id = ?", (*changes.values(), chat_id))
    return _chat_view(uid, _chat_row(uid, chat_id))


@router.delete("/chats/{chat_id}", status_code=204)
async def delete_chat(chat_id: str, request: Request):
    """Диалог, его сообщения и вложения; файл, на который ссылается другая запись истории, остаётся."""
    uid = request.state.user.id
    _chat_row(uid, chat_id)
    cleaner.forget(uid, chat_id)
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

def _layer_text(layer: list[tuple[str, bool]], reader) -> str:
    """Текст слоя PDF без «Распознавания» через сервер; страницы-сканы (`ocr_render.is_scan`), и когда это
    только часть страниц, — отказ 422 `scan`, как у переводчика: иначе модель молча получила бы документ без
    них. Пустая страница без картинки — не скан."""
    wanted = sum(scan for _, scan in layer)
    if wanted:
        if wanted == len(layer):
            words = NO_OCR if reader is None else sections.unlisted(reader[0])
        else:
            words = SOME_SCANS.format(wanted, len(layer)) + (
                " — подключите «Сканы» в настройках" if reader is None
                else f"; {sections.unlisted(reader[0])}")
        raise HTTPException(422, {"code": "scan", "message": words})
    text = "\n\n".join(t for t, _ in layer if t)     # как documents.extract
    if not text:
        raise documents.DocumentError(documents.SCAN)
    if len(text) > documents.TEXT_MAX_CHARS:
        raise documents.DocumentError(documents.TOO_LONG)
    return text


@router.post("/attachments", status_code=201)
async def upload_attachment(request: Request):
    """Картинка (PNG, JPEG, WebP — нормализуется до PNG) или документ (текст — сразу); файл — в media.
    К сообщению вложение привязывает ход. PDF со страницами без текстового слоя при подключении раздела
    «Распознавание» через сервер — документ распознавания (`ocr` в ответе), текст вложения допишет он."""
    uid = request.state.user.id
    text, scan, reader = None, None, None
    try:
        name, data = await documents.read_upload(request)
        suffix = PurePosixPath(name.lower()).suffix
        if suffix in IMAGE_SUFFIXES:
            if len(data) > MAX_UPLOAD:
                raise UploadError(f"фото должно быть не больше {MAX_UPLOAD >> 20} МиБ", 413)
            kind, mime, body = "image", "image/png", await asyncio.to_thread(normalize_image, data)
            download = PurePosixPath(name).stem + ".png"
        else:
            reader = sections.default(uid, "ocr") if suffix == ".pdf" else None
            if reader is not None and reader[0].proxied:
                text, scan = ocr.plan_pages(await asyncio.to_thread(ocr_render.pdf_scan_pages, data))
            elif suffix == ".pdf":
                text = _layer_text(await asyncio.to_thread(ocr_render.pdf_layer_pages, data), reader)
            else:
                text = await asyncio.to_thread(documents.extract, name, data)
            kind, mime, body, download = "document", DOC_MIME[suffix], data, name
    except documents.DocumentError as e:
        message = str(e)
        if message == documents.SCAN:                # подключение «Распознавания» есть, но адрес убран из списка
            message = NO_OCR if reader is None else sections.unlisted(reader[0])
        raise HTTPException(e.status, {"code": "document", "message": message}) from None
    except UploadError as e:
        raise HTTPException(e.status, {"code": "image", "message": str(e)}) from None
    operations.check(uid)
    try:
        media_id = await media.save_async(uid, body, mime, download, thumb=kind == "image")
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    att_id, chars = uuid.uuid4().hex, len(text or "")
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
                     " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)", (att_id, uid, kind, name, media_id, text, chars, clock.NOW()))
    out = {"id": att_id, "kind": kind, "name": name, "chars": chars, "media_id": media_id}
    if scan is not None:
        conn, model = reader
        try:
            sid = ocr.create(uid, conn, model, name, origin="chat", source_media_id=media_id, pages=scan,
                             attachment_id=att_id)
        except HTTPException:                        # очередь распознавания полна — вложения нет
            await _drop_attachments([{"id": att_id, "user_id": uid, "media_id": media_id}])
            raise
        ocr.kick(uid)
        out["ocr"] = {"id": sid, "pages": sum(p["state"] == "waiting" for p in scan)}
        log.info("вложение %s: скан, %d стр. на распознавание (%s)", att_id[:8], out["ocr"]["pages"], sid[:8])
    else:
        log.info("вложение %s: %s, %d знаков", att_id[:8], kind, chars)
    return out


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
    """Крестик на вложении, которое ещё не отправили; отправленное — часть сообщения (409). Скан, который ещё
    распознаётся (ход отклонили после загрузки), — отменяется: документ остаётся в «Распознавании»."""
    uid = request.state.user.id
    row = state.DB.execute("SELECT * FROM attachments WHERE id = ? AND user_id = ?", (att_id, uid)).fetchone()
    if row is None:
        raise _gone("такого вложения нет")
    if row["message_id"] is not None:
        raise HTTPException(409, {"code": "sent", "message": "вложение уже отправлено"})
    for scan_id in ocr.pending(uid, [att_id]):
        await ocr.request_cancel(uid, scan_id)
    await _drop_attachments([row])
    return Response(status_code=204)


def _scan_of(user_id: int, att_id: str):
    """Последний документ распознавания вложения (или None)."""
    return state.DB.execute("SELECT id FROM scans WHERE user_id = ? AND attachment_id = ?"
                            " ORDER BY created DESC, rowid DESC LIMIT 1", (user_id, att_id)).fetchone()


@router.post("/attachments/{att_id}/ocr", status_code=202)
async def rescan_attachment(att_id: str, request: Request):
    """«Распознать заново» вложения, чей скан не распознан (или распознан не весь): тот же документ — снова в
    очередь (`ocr.retry`), текст вложения — его новый итог; ход с этим вложением в истории ждёт его."""
    uid = request.state.user.id
    if state.DB.execute("SELECT 1 FROM attachments WHERE id = ? AND user_id = ?", (att_id, uid)).fetchone() is None:
        raise _gone("такого вложения нет")
    scan = _scan_of(uid, att_id)
    if scan is None:
        raise _gone("скана этого вложения больше нет")
    ocr.retry(uid, scan["id"])
    ocr.kick(uid)
    return {"ocr": {"id": scan["id"]}}


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


def _known_links(chat_id: str) -> dict[str, str]:
    """Адреса, которые read_page и youtube_subtitles (по id ролика) могут читать в ходе (web.links: norm →
    адрес, спека 6.4): из сообщений
    пользователя и вложенных документов диалога и результаты поиска прежних ответов (`messages.tools`).
    Синхронно — в транзакции хода."""
    known = {}
    for row in state.DB.execute("SELECT role, content, tools FROM messages WHERE chat_id = ?", (chat_id,)):
        if row["role"] == "user":
            known |= web.links(row["content"])
        for step in json.loads(row["tools"] or "[]"):
            known |= web.found_links(step)
    for row in state.DB.execute("SELECT a.text FROM attachments a JOIN messages m ON m.id = a.message_id"
                                " WHERE m.chat_id = ? AND a.kind = 'document'", (chat_id,)):
        known |= web.links(row["text"])
    return known


def _pending_scans(user_id: int, chat_id: str, atts: list) -> list[str]:
    """Идущие сканы вложений хода и истории диалога (повтор сразу после «Остановить», пока документ
    отменяется): модели вложение уходит только готовым."""
    ids = [a["id"] for a in atts if a["kind"] == "document"] + [r["id"] for r in state.DB.execute(
        "SELECT a.id FROM attachments a JOIN messages m ON m.id = a.message_id WHERE m.chat_id = ?"
        " AND a.kind = 'document'", (chat_id,))]
    return ocr.pending(user_id, ids)


def _title(text: str) -> str | None:
    lines = text.strip().splitlines()
    if not lines:
        return None
    return lines[0].strip()[:TITLE_CHARS].rstrip() or None


def _cleaner_messages(uid, chat_id, body, atts):
    if _pending_scans(uid, chat_id, atts):
        raise HTTPException(409, {"code": "not_ready", "message": NOT_READY})
    if any(a["kind"] == "document" and a["text"] is None for a in atts):
        raise cleaner.refused("Документ ещё не распознан")
    last = state.DB.execute("SELECT id FROM messages WHERE chat_id = ?" +
                            (" AND role = 'user'" if body.regenerate else "") +
                            " ORDER BY created DESC, rowid DESC LIMIT 1", (chat_id,)).fetchone()
    if body.regenerate and last is None:
        raise cleaner.refused("Нечего переспрашивать")
    messages = compose(chat_id, last["id"]) if last else []
    if not body.regenerate:
        content = _content(body.text, atts)
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = _merge(messages[-1]["content"], content)
        else:
            messages.append({"role": "user", "content": content})
    return with_date(messages, uid)


@router.post("/chats/{chat_id}/cleaner-preview")
async def cleaner_preview(chat_id: str, body: TurnIn, request: Request):
    uid = request.state.user.id
    row = _chat_row(uid, chat_id)
    _busy(chat_id)
    if row["connection_id"] is None:
        raise cleaner.refused("Выберите подключение Cleaner")
    conn = connections.get(uid, row["connection_id"])
    atts = [] if body.regenerate else _free_attachments(uid, body.attachment_ids)
    if not body.regenerate and not body.text.strip() and not atts:
        raise cleaner.refused("Пустое сообщение", 422)
    messages = _cleaner_messages(uid, chat_id, body, atts)
    preview, shown = await cleaner.prepare(conn, "chat", chat_id, row["model"], messages,
                                          [rule.model_dump() for rule in body.cleaner_rules], local=body.cleaner_local)
    operations.check(uid)
    _chat_row(uid, chat_id)                        # deleted while Cleaner was preparing: no send ticket
    return {"ticket": cleaner.remember(preview), **shown}


@router.post("/chats/{chat_id}/cleaner-copy")
async def cleaner_copy(chat_id: str, body: CleanerCopyIn, request: Request):
    uid = request.state.user.id
    row = _chat_row(uid, chat_id)
    if row["connection_id"] is None:
        raise cleaner.refused("Выберите подключение Cleaner")
    conn = connections.get(uid, row["connection_id"])
    if not conn.cleaner:
        raise cleaner.refused("Выберите подключение Cleaner")
    message = state.DB.execute("SELECT * FROM messages WHERE id = ? AND chat_id = ?",
                               (body.message_id, chat_id)).fetchone()
    if message is None:
        raise _gone()
    atts = _attachments(chat_id).get(body.message_id, [])
    if any(a["kind"] == "document" and a["text"] is None for a in atts):
        raise cleaner.refused("Документ ещё не распознан")
    text = _content(message["content"], [a for a in atts if a["kind"] == "document"])
    result = await cleaner.redact(conn, "chat", chat_id, text)
    operations.check(uid)
    _chat_row(uid, chat_id)
    return {"text": result}


def _turn_receipt(chat_id: str, request_id: str | None, body: TurnIn | None = None) -> dict | None:
    if request_id is None:
        return None
    receipt = state.DB.execute("SELECT * FROM chat_turn_requests WHERE chat_id=? AND request_id=?",
                               (chat_id, request_id)).fetchone()
    if receipt is None:
        return None
    if body is not None and receipt["fingerprint"] != _turn_fingerprint(body):
        raise HTTPException(409, {"code": "request_conflict", "message": "Эта отправка уже содержит другое сообщение"})
    if state.DB.execute("SELECT 1 FROM messages WHERE id=? AND chat_id=?",
                        (receipt["assistant_message_id"], chat_id)).fetchone() is None:
        raise HTTPException(409, {"code": "request_gone", "message": "Ответ этой отправки уже заменён или удалён"})
    return {"user_message_id": receipt["user_message_id"], "assistant_message_id": receipt["assistant_message_id"]}


def _turn_fingerprint(body: TurnIn) -> str:
    # Consent/one-use Cleaner tickets may change before acceptance. No copy of the text is stored.
    raw = json.dumps([body.text, body.attachment_ids, body.regenerate], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@router.get("/chats/{chat_id}/turns/{request_id}")
async def turn_receipt(chat_id: str, request_id: str, request: Request):
    _chat_row(request.state.user.id, chat_id)
    receipt = _turn_receipt(chat_id, request_id)
    return {"accepted": receipt is not None, **(receipt or {})}


@router.post("/chats/{chat_id}/turns")
async def create_turn(chat_id: str, body: TurnIn, request: Request):
    uid = request.state.user.id
    row = _chat_row(uid, chat_id)
    receipt = _turn_receipt(chat_id, body.request_id, body)
    if receipt is not None:
        return {"replayed": True, **receipt}
    _busy(chat_id)
    if row["connection_id"] is None:
        raise HTTPException(409, {"code": "no_connection", "message": "выберите подключение диалога"})
    conn = connections.get(uid, row["connection_id"])
    model = row["model"] or manage.DEFAULT_MODEL
    atts = [] if body.regenerate else _free_attachments(uid, body.attachment_ids)
    if not body.regenerate and not body.text.strip() and not atts:
        raise HTTPException(422, {"code": "empty", "message": "пустое сообщение"})
    limit = await _limit(conn, model)
    operations.check(uid)
    receipt = _turn_receipt(chat_id, body.request_id, body)
    if receipt is not None:
        return {"replayed": True, **receipt}
    _busy(chat_id)                                   # пока ждали список моделей, мог начаться другой ход
    preview = None
    if conn.cleaner:
        fresh = _chat_row(uid, chat_id)
        if fresh["connection_id"] != conn.id or fresh["model"] != model:
            raise cleaner.refused("Модель диалога изменилась: проверьте запрос ещё раз")
        messages = _cleaner_messages(uid, chat_id, body, atts)
        preview = cleaner.take(body.cleaner_ticket, conn, chat_id, model, messages)
        if preview.route == "local":
            model = preview.local_model
            limit = CONTEXT.get(model)  # The cloud model's context limit does not describe the local recipient.
    via = "server" if conn.proxied else "browser"
    # скан ещё распознаётся: ход через сервер подождёт его в StreamTask, прямой — ждёт страница
    scans = _pending_scans(uid, chat_id, atts)
    if scans and via == "browser":
        raise HTTPException(409, {"code": "not_ready", "message": NOT_READY})
    free = body.external_ok or sections.is_local(conn, model) or conn.cleaner
    now, answer_id = clock.NOW(), uuid.uuid4().hex
    # без await до COMMIT: другой запрос не вклинится, отказ откатывает всё
    state.DB.execute("BEGIN")
    try:
        # инструменты «Интернета» этого хода — по кнопке, какой она стала к ходу (`row` прочитан до await
        # _limit); прямому подключению — никаких. Ключ Exa не расшифровался — шаги поиска скажут это (web.NO_KEY)
        fresh = state.DB.execute("SELECT web, thinking FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if fresh is None:
            raise _gone()
        kinds = web.tools_for(conn, web.has_exa_key(uid)) if fresh["web"] and not conn.cleaner else []
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
        if scans:                                    # контекст и предел — после распознавания (_await_scans)
            messages, known = [], {}
        else:
            messages = compose(chat_id, user_mid)
            known = _known_links(chat_id) if kinds else {}
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
        if body.request_id is not None:
            state.DB.execute("INSERT INTO chat_turn_requests VALUES (?, ?, ?, ?, ?)",
                             (chat_id, body.request_id, _turn_fingerprint(body), user_mid, answer_id))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    try:
        if not scans:
            messages = with_date(await fill_photos(chat_id, uid, messages), uid)  # файлы — после COMMIT и в потоке
    except BaseException as e:
        # строка ответа уже в базе: без итога диалог считался бы занятым (см. _busy)
        words = STORAGE if isinstance(e, OSError) else None
        _finish(answer_id, chat_id, words or "", "error", error=words)
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
                    tools=kinds, exa_key=exa_key, limit=limit, known=known,
                    thinking=bool(fresh["thinking"]) if sections.is_local(conn, model) else None,
                    scans=scans, user_message_id=user_mid, cleaner_preview=preview)
    STREAMS[answer_id] = st
    st.task = asyncio.create_task(st.run())
    return _EventStream(st, user_mid, getattr(request.state, "digest", None))


@router.put("/chats/{chat_id}/messages/{message_id}")
async def put_answer(chat_id: str, message_id: str, body: AnswerIn, request: Request):
    """Ответ прямого подключения: браузер прочитал модель сам и кладёт итог (один раз). Тот же итог повторно —
    200, как в первый раз: ответ на первый PUT мог не дойти, и страница повторяет его после обрыва связи."""
    _chat_row(request.state.user.id, chat_id)
    row = state.DB.execute("SELECT role, status, via, content FROM messages WHERE id = ? AND chat_id = ?",
                           (message_id, chat_id)).fetchone()
    if row is None:
        raise _gone("такого сообщения нет")
    if (row["role"], row["via"], row["status"], row["content"]) == ("assistant", "browser", body.status, body.content):
        return {"id": message_id, "status": body.status}
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


@router.post("/chats/{chat_id}/nowait", status_code=204)
async def stop_waiting(chat_id: str, request: Request):
    """«Не ждать»: ответ, который ждёт карту (503 шлюза), кончается ошибкой GAVE_UP; уже не ждёт — 409 `state`
    (проверка и отмена — без await между ними: начавший читать модель ответ не оборвать)."""
    _chat_row(request.state.user.id, chat_id)
    waiting = [st for st in _chat_streams(chat_id) if st.card_wait is not None]
    if not waiting:
        raise HTTPException(409, {"code": "state", "message": "ответ уже не ждёт карту"})
    for st in waiting:
        st.gave_up = True
    await _cancel(waiting)
    return Response(status_code=204)


@router.post("/chats/{chat_id}/stop", status_code=204)
async def stop_turn(chat_id: str, request: Request):
    _chat_row(request.state.user.id, chat_id)
    _stop_unstarted({chat_id})
    await _cancel(_chat_streams(chat_id))
    return Response(status_code=204)
