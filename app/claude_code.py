"""Раздел «Claude Code»: `/api/code/*` поверх канала моста (спека 2026-10-28-studio-claude-code-design, 6.3,
6.5–6.7; просьбы и отказы — протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md, §2.2, §3–§5).

- Студия ретранслирует: каждый запрос владельца — просьба мосту (`bridge.call`) с признаком сессии
  `bridge.tag` (не дайджест и не токен); ответ моста — браузеру как есть (`data`), отказ — его кодом и словами
  со статусом по `STATUS`. Проекты, чаты, история, файлы на сервере не оседают: источник правды — машина.
- Открыть компьютер — `open` с PIN-ом: PIN проверяет мост, сервер его не хранит и в журнал не пишет; итог —
  в историю подключений (`open`, `pin_ok`, `pin_fail`). Мост исполняет просьбы только открытых сессий, иначе —
  отказ `closed`, у нас 409: 423 раздел не отдаёт никогда — его ловит экран блокировки страницы.
- Своё студия держит только в `code_chats`: какие чаты она завела и открыт ли чат (понятие студии, в Claude
  Code его нет). Список чатов — слияние сессий машины с этими отметками; записи на GET нет.
- Чужой компьютер, `pid` и `chat` не того вида — 404 и 422 до моста: ему не уходит ни одной просьбы.
- В строке запроса — только id (`agent_id`, `pid` — 16 hex, `chat` — UUID): её пишет журнал Apache. Имена
  проектов, PIN, тексты — только в теле. Журнал службы — «пользователь N: компьютер M — <op>: <код>» на
  отказ, без имён и текстов.
- Открытость кончается и для потоков: поток хода и `live` помнит свою сессию — `bridge.forget` (заперлась,
  отперлась) кладёт в его очередь `closed`, а сам поток на тике пинга и на событиях (не чаще `CHECK_SEC`)
  сверяет строку сессии: вышли, погасили, истекла или заперта по сроку отметки — `event: closed` и конец.
- Данные моста браузеру — только ASCII-JSON без NaN (`_BridgeJSON`): одинокий суррогат или `NaN` из JSON
  моста дали бы 500; не закодировать — 502 `bridge`. Отказы идут обычным ответом FastAPI: суррогаты из их
  слов убирает `bridge._refusal`.
- Имя модуля — не `code.py`: каталог службы стоит в `sys.path` первым, и такой файл заслонил бы стандартный
  `code`, а его берёт `pdb` (pytest импортирует `pdb` при старте и падал бы).
"""
import asyncio
import base64
import json
import logging
import math
import re
import sqlite3
import unicodedata
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

import auth
import bridge
import clock
import ocr_render
import state

log = logging.getLogger("studio")
ODD = "мост ответил непонятно"


class _BridgeJSON(JSONResponse):
    """Ответ раздела: JSON только ASCII и без NaN/Infinity (Python их читает, в JSON их нет)."""

    def render(self, content: Any) -> bytes:
        try:
            return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        except (ValueError, RecursionError):
            raise _refuse(502, "bridge", ODD) from None


router = APIRouter(prefix="/api/code", default_response_class=_BridgeJSON)

PID_RE = re.compile(r"[0-9a-f]{16}")
STATUS = {"closed": 409, "pin": 403, "pin_locked": 403, "pin_needed": 409, "not_found": 404, "outside": 403,
          "too_large": 413, "binary": 415, "conflict": 409, "busy": 409, "running": 409, "idle": 409, "gone": 410,
          "not_git": 409, "invalid": 422}
PIN_MAX = 64
TEXT_MAX = 100_000
FILE_MAX = 2 * 1024 * 1024
FILE_BODY_MAX = 3 * 1024 * 1024
RAW_MAX = 10 * 1024 * 1024
PATH_MAX = 1024
HISTORY_PAGE = 100
PING_SEC = 15
CHECK_SEC = 1                    # поток сверяет свою сессию на событиях не чаще
PDF_TTL_SEC = 300
ANSWER_MAX = 2000                # знаков в словах отказа и в каждом вопросе и ответе
ANSWERS_MAX = 20                 # пар «вопрос → ответ»
NO_TEXT = "задание пустое или длиннее 100 000 знаков"
NO_MODEL = "такой модели нет на компьютере"
NO_EFFORT = "такого усилия нет на компьютере"


class Open(BaseModel):
    pin: str | None = Field(None, max_length=PIN_MAX)


class ChatState(BaseModel):
    state: Literal["open", "closed"]


class Turn(BaseModel):
    text: str                    # пусто и длина — своим 422 `text` (слова для человека), не общим FastAPI
    model: str | None = None
    effort: str | None = None


class Answer(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    decision: Literal["allow", "always", "deny"]
    message: str | None = Field(None, max_length=ANSWER_MAX)
    answers: dict[str, str] | None = None

    @field_validator("answers")
    @classmethod
    def _answers(cls, value):
        if value is not None and (len(value) > ANSWERS_MAX
                                  or any(len(k) > ANSWER_MAX or len(v) > ANSWER_MAX for k, v in value.items())):
            raise ValueError("answers")
        return value

    @model_validator(mode="after")
    def _message_only_with_deny(self):
        if self.message is not None and self.decision != "deny":
            raise ValueError("message")
        return self


def _refuse(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def own_agent(request: Request, aid: int) -> sqlite3.Row:
    row = state.DB.execute("SELECT id, name, has_pin FROM agents WHERE id = ? AND user_id = ?",
                           (aid, request.state.user.id)).fetchone()
    if row is None:
        raise _refuse(404, "not_found", "нет такого компьютера")
    return row


def check_pid(pid: str) -> str:
    if PID_RE.fullmatch(pid) is None:
        raise _refuse(422, "project", "неверный проект")
    return pid


def check_chat(chat: str) -> str:
    try:
        ok = str(uuid.UUID(chat)) == chat
    except ValueError:
        ok = False
    if not ok:
        raise _refuse(422, "chat", "неверный чат")
    return chat


def _warn(request: Request, aid: int, op: str, error: HTTPException) -> None:
    log.warning("пользователь %d: компьютер %d — %s: %s", request.state.user.id, aid, op, error.detail["code"])


async def ask(request: Request, aid: int, op: str, *, quiet: bool = False, **args) -> dict:
    """Просьба мосту от сессии запроса; отказы — HTTPException с кодом моста или своим, в журнал — `_warn`
    (`quiet` — отказ пишет сам зовущий: `not_found` нового чата — норма). Строка с одиноким суррогатом (из JSON
    запроса) в кадр UTF-8 не ляжет — 422 до моста, а не «не на связи»."""
    try:
        json.dumps(args, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        raise _refuse(422, "invalid", BAD_BODY) from None
    try:
        return await bridge.call(aid, op, bridge.tag(request.state.digest), **args)
    except bridge.Offline:
        error = _refuse(409, "offline", "компьютер не на связи")
    except TimeoutError:
        error = _refuse(504, "timeout", "компьютер не ответил")
    except bridge.BridgeError as e:
        status = STATUS.get(e.code)
        error = (_refuse(502, "bridge", "мост ответил непонятно") if status is None
                 else _refuse(status, e.code, e.message, **e.extra))
    if not quiet:
        _warn(request, aid, op, error)
    raise error


def _event(request: Request, agent: sqlite3.Row, kind: str) -> None:
    bridge.record(request.state.user.id, agent["id"], agent["name"], kind, device=bridge.device(request),
                  ip=auth.client_ip(request))


@router.post("/agents/{aid}/open", status_code=204)
async def open_agent(aid: int, body: Open, request: Request):
    agent = own_agent(request, aid)
    try:
        await ask(request, aid, "open", pin=body.pin)
    except HTTPException as e:
        if e.detail["code"] in ("pin", "pin_locked"):
            _event(request, agent, "pin_fail")
        raise
    _event(request, agent, "pin_ok" if body.pin is not None and agent["has_pin"] else "open")
    return Response(status_code=204)


@router.get("/agents/{aid}/projects")
async def projects(aid: int, request: Request):
    own_agent(request, aid)
    items = (await ask(request, aid, "projects")).get("items")
    return {"items": [{"pid": i["pid"], "name": i["name"]} for i in (items if isinstance(items, list) else [])
                      if isinstance(i, dict) and isinstance(i.get("pid"), str) and PID_RE.fullmatch(i["pid"])
                      and isinstance(i.get("name"), str)]}


def _time(value) -> float | None:
    """Время из ответа моста: число (не bool, не NaN/Infinity) или None."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


@router.get("/agents/{aid}/projects/{pid}/chats")
async def chats(aid: int, pid: str, request: Request):
    """Сессии машины с отметками студии; строки `code_chats` проекта без сессии на машине (новый чат без
    хода) — тоже. Открытые, затем закрытые, внутри — новые сверху."""
    own_agent(request, aid)
    check_pid(pid)
    found = (await ask(request, aid, "chats", project=pid)).get("items")
    marks = {r["session_id"]: r for r in state.DB.execute(
        "SELECT project, session_id, state, created, updated FROM code_chats WHERE agent_id = ? AND user_id = ?",
        (aid, request.state.user.id))}
    items, seen = [], set()
    for i in (found if isinstance(found, list) else []):
        # чат не UUID страница не откроет (422 на каждом маршруте), а кусок пути лёг бы в адрес её запроса
        if (not isinstance(i, dict) or not isinstance(i.get("chat"), str) or not bridge.UUID.fullmatch(i["chat"])
                or i["chat"] in seen):
            continue
        seen.add(i["chat"])
        mark = marks.get(i["chat"])
        title = i.get("title")
        items.append({"chat": i["chat"], "title": title if isinstance(title, str) else None,
                      "created": _time(i.get("created")), "modified": _time(i.get("modified")),
                      "origin": "studio" if i.get("tag") == "studio" else "other",
                      "state": mark["state"] if mark is not None else "open", "running": bool(i.get("running")),
                      "asking": bool(i.get("asking"))})
    for chat, mark in marks.items():
        if chat not in seen and mark["project"] == pid:
            items.append({"chat": chat, "title": None, "created": mark["created"], "modified": mark["updated"],
                          "origin": "studio", "state": mark["state"], "running": False, "asking": False})
    items.sort(key=lambda i: (i["state"] == "closed", -(i["modified"] or 0)))
    return {"items": items}


@router.post("/agents/{aid}/projects/{pid}/chats")
async def new_chat(aid: int, pid: str, request: Request):
    """Номер сессии заводит студия (SDK примет заданный `session_id`); мост не зовётся — строка без хода
    безвредна, даже если компьютер уже закрыт."""
    agent = own_agent(request, aid)
    check_pid(pid)
    chat, now = str(uuid.uuid4()), clock.NOW()
    state.DB.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                     " VALUES (?, ?, ?, ?, 'open', ?, ?)", (request.state.user.id, aid, pid, chat, now, now))
    _event(request, agent, "session_start")
    return JSONResponse({"chat": chat}, status_code=201)


@router.patch("/agents/{aid}/projects/{pid}/chats/{chat}")
async def set_chat_state(aid: int, pid: str, chat: str, body: ChatState, request: Request):
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    now = clock.NOW()
    state.DB.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT(agent_id, session_id) DO UPDATE SET state = excluded.state, updated = excluded.updated",
                     (request.state.user.id, aid, pid, chat, body.state, now, now))
    return {"state": body.state}


def _new_chat(request: Request, aid: int, pid: str, chat: str, e: HTTPException) -> bool:
    """Мост не знает чата, а студия его завела («Новый чат», хода ещё не было — сессии на машине нет)."""
    return e.detail["code"] == "not_found" and state.DB.execute(
        "SELECT 1 FROM code_chats WHERE user_id = ? AND agent_id = ? AND project = ? AND session_id = ?",
        (request.state.user.id, aid, pid, chat)).fetchone() is not None


@router.get("/agents/{aid}/projects/{pid}/chats/{chat}")
async def history(aid: int, pid: str, chat: str, request: Request, before: int | None = None):
    """Страница истории от моста; новый чат студии без хода — пустая страница, а не 404."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    try:
        return await ask(request, aid, "history", quiet=True, project=pid, chat=chat, before=before,
                         limit=HISTORY_PAGE)
    except HTTPException as e:
        if _new_chat(request, aid, pid, chat, e):
            return {"items": [], "start": 0, "total": 0}
        _warn(request, aid, "history", e)
        raise


# ---- ход: поток, наблюдение, карточки, остановка ------------------------------------------

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def _sse(event: str, data: dict) -> str:
    """Строка SSE; данные — ASCII-JSON (одинокий суррогат из JSON моста уронил бы поток), NaN — ValueError."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True, allow_nan=False)}\n\n"


def _session_open(digest: str) -> bool:
    """Сессия студии жива и не заперта — как её видит `auth.guard` (без продления)."""
    row = state.DB.execute("SELECT u.disabled, u.screen_lock, s.expires, s.mobile, s.seen FROM sessions s"
                           " JOIN users u ON u.id = s.user_id WHERE s.digest = ?", (digest,)).fetchone()
    now = clock.NOW()
    return (row is not None and not row["disabled"] and row["expires"] > now
            and not auth.screen_locked(row["screen_lock"], row["mobile"], row["seen"], now))


async def _events(q: bridge.Watch, first: tuple[str, dict] | None, live: bool, digest: str):
    """События SSE из очереди наблюдателя: `event: <k>`, данные — событие без `k`; без событий `PING_SEC` —
    `: ping`. Конец — `done`, `offline` (канал пропал: ход на мосте, может, и жив, но вестей не будет) или
    `closed` (сессия студии заперлась, отперлась, вышла или истекла — компьютер для неё закрыт: страница
    спросит PIN). Свою сессию поток сверяет на каждом тике пинга и на событиях не чаще `CHECK_SEC`.
    Поток `live` до первого `snapshot` пропускает прочие события: мост шлёт снимок после них, они в нём есть."""
    if first is not None:
        yield _sse(*first)
    waiting, checked = live, clock.NOW()
    while True:
        try:
            kind, e = await asyncio.wait_for(q.get(), PING_SEC)
        except TimeoutError:
            kind, e = "ping", None
        now = clock.NOW()
        if kind == "ping" or (kind == "ev" and now - checked >= CHECK_SEC):
            checked = now
            if not _session_open(digest):
                kind = "closed"
        if kind == "ping":
            yield ": ping\n\n"
            continue
        if kind in ("offline", "closed"):
            yield _sse(kind, {})
            return
        k = e["k"]
        if waiting and k not in ("snapshot", "done"):
            continue
        try:
            line = _sse(k, {name: value for name, value in e.items() if name != "k"})
        except (ValueError, RecursionError):          # NaN или Infinity в событии моста — не JSON
            if k != "done":
                continue
            line = _sse("done", {})
        waiting = False
        yield line
        if k == "done":
            return


class _Watch(StreamingResponse):
    """Поток SSE чата для одного наблюдателя. Очередь снимает сам ответ, а не finally генератора: отмена может
    застать ответ в `send` или ещё до первого куска (тела генератора не было вовсе) — как `chat._EventStream`."""

    def __init__(self, aid: int, chat: str, q: bridge.Watch, digest: str, first: tuple[str, dict] | None = None,
                 live: bool = False):
        super().__init__(_events(q, first, live, digest), media_type="text/event-stream", headers=SSE_HEADERS)
        self.aid, self.chat, self.q = aid, chat, q

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            bridge.unwatch(self.aid, self.chat, self.q)
            await self.body_iterator.aclose()


async def _idle():
    yield _sse("idle", {})


async def _watched(request: Request, aid: int, chat: str, op: str, *, quiet: bool = False,
                   **args) -> tuple[bridge.Watch, dict]:
    """Очередь наблюдателя (с признаком сессии запроса) — до просьбы: события хода идут сразу за ответом моста
    и не должны пройти мимо. Отказ — очередь прочь."""
    q = bridge.watch(aid, chat, bridge.tag(request.state.digest))
    try:
        return q, await ask(request, aid, op, quiet=quiet, chat=chat, **args)
    except BaseException:
        bridge.unwatch(aid, chat, q)
        raise


@router.post("/agents/{aid}/projects/{pid}/chats/{chat}/turn")
async def turn(aid: int, pid: str, chat: str, body: Turn, request: Request):
    """Ход: просьба `turn` мосту, затем его события потоком SSE до `done` или `offline`. Модель и усилие
    сверяются со знакомством моста; канала нет — их не сверить, и `ask` скажет `offline`."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    if not body.text.strip() or len(body.text) > TEXT_MAX:
        raise _refuse(422, "text", NO_TEXT)
    link = bridge.LINKS.get(aid)
    if link is not None:
        if body.model is not None and body.model not in {m["id"] for m in link.hello.get("models", ())}:
            raise _refuse(422, "model", NO_MODEL)
        if body.effort is not None and body.effort not in link.hello.get("efforts", ()):
            raise _refuse(422, "effort", NO_EFFORT)
    q, data = await _watched(request, aid, chat, "turn", project=pid, text=body.text, model=body.model,
                             effort=body.effort)
    log.info("пользователь %d: компьютер %d — ход", request.state.user.id, aid)
    turn_id = data.get("turn")
    return _Watch(aid, chat, q, request.state.digest,
                  first=("turn", {"turn": turn_id if isinstance(turn_id, str) else None}))


@router.get("/agents/{aid}/projects/{pid}/chats/{chat}/live")
async def live(aid: int, pid: str, chat: str, request: Request):
    """Вернуться к чату: хода нет — одно `idle`; идёт — снимок (мост шлёт его за ответом на `watch`), дальше
    события. Студия хода не помнит: всё состояние — у моста. Новый чат студии без хода — тоже `idle`."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    try:
        q, data = await _watched(request, aid, chat, "watch", quiet=True, project=pid)
    except HTTPException as e:
        if not _new_chat(request, aid, pid, chat, e):
            _warn(request, aid, "watch", e)
            raise
        q, data = None, {}
    if data.get("running") is not True:
        if q is not None:
            bridge.unwatch(aid, chat, q)
        return StreamingResponse(_idle(), media_type="text/event-stream", headers=SSE_HEADERS)
    return _Watch(aid, chat, q, request.state.digest, live=True)


@router.post("/agents/{aid}/projects/{pid}/chats/{chat}/answer", status_code=204)
async def answer(aid: int, pid: str, chat: str, body: Answer, request: Request):
    """Ответ на карточку — мосту как есть: о самой карточке студия ничего не помнит (перезапуск, другая
    вкладка), её нет у моста — 410 `gone`. Закрытие карточки (`asked`) мост шлёт всем наблюдателям."""
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    await ask(request, aid, "answer", project=pid, chat=chat, request_id=body.request_id, decision=body.decision,
              message=body.message, answers=body.answers)
    return Response(status_code=204)


@router.post("/agents/{aid}/projects/{pid}/chats/{chat}/stop", status_code=204)
async def stop(aid: int, pid: str, chat: str, request: Request):
    own_agent(request, aid)
    check_pid(pid)
    check_chat(chat)
    await ask(request, aid, "stop", project=pid, chat=chat)
    return Response(status_code=204)


# ---- файлы, правка, просмотр PDF, git -----------------------------------------------------

BAD_PATH = "неверный путь"
BAD_BODY = "неверный запрос"
TOO_LARGE = "файл больше 2 МиБ"
TOO_LARGE_RAW = "файл больше 10 МиБ"
NOT_PDF = "это не PDF"
NO_PAGE = "нет такой страницы"
#: последний открытый PDF пользователя: (ключ (aid, pid, path, сессия), байты, страниц, время) — листание
#: не тянет файл с машины на каждую страницу; срок — PDF_TTL_SEC от загрузки
PDFS: dict[int, tuple[tuple, bytes, int, float]] = {}


class PathBody(BaseModel):
    path: Any = None             # вид — check_path: свой 422 `path`, не общий FastAPI


class PdfPage(PathBody):
    page: StrictInt = Field(ge=1)


def check_path(path: object) -> str:
    """Путь внутри проекта (протокол §6): `""` — корень, иначе части через `/` — без пустых, `.` и `..`, без `\\`,
    управляющих знаков и одиноких суррогатов, не длиннее PATH_MAX. Иное — 422 `path` до моста."""
    if (isinstance(path, str) and len(path) <= PATH_MAX and "\\" not in path
            and not any(unicodedata.category(c) in ("Cc", "Cs") for c in path)
            and (path == "" or all(p not in ("", ".", "..") for p in path.split("/")))):
        return path
    raise _refuse(422, "path", BAD_PATH)


def _strings(data: dict, *names: str) -> dict:
    """Поля ответа моста, которые страница берёт строками; не строка — мост ответил непонятно."""
    if not all(isinstance(data.get(n), str) for n in names):
        raise _refuse(502, "bridge", ODD)
    return {n: data[n] for n in names}


@router.post("/agents/{aid}/projects/{pid}/files")
async def files(aid: int, pid: str, body: PathBody, request: Request):
    own_agent(request, aid)
    check_pid(pid)
    return await ask(request, aid, "files", project=pid, path=check_path(body.path))


@router.post("/agents/{aid}/projects/{pid}/read")
async def read(aid: int, pid: str, body: PathBody, request: Request):
    """Чтение — POST: путь только в теле (строку запроса пишет журнал Apache)."""
    own_agent(request, aid)
    check_pid(pid)
    data = await ask(request, aid, "read", project=pid, path=check_path(body.path))
    return _strings(data, "text", "version")


@router.put("/agents/{aid}/projects/{pid}/file")
async def save(aid: int, pid: str, request: Request):
    """Правка: тело читается с пределом FILE_BODY_MAX, текст — до FILE_MAX байтов UTF-8; мост пишет, только если
    `version` совпал с нынешним (иначе 409 `conflict` с нынешним `version`)."""
    own_agent(request, aid)
    check_pid(pid)
    raw = await auth.small_body(request, FILE_BODY_MAX)
    if raw is None:
        raise _refuse(413, "too_large", TOO_LARGE)
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise _refuse(422, "invalid", BAD_BODY)
    path = check_path(body.get("path"))
    text, version = body.get("text"), body.get("version")
    if not isinstance(text, str) or not isinstance(version, str):
        raise _refuse(422, "invalid", BAD_BODY)
    try:
        size = len(text.encode())
    except UnicodeEncodeError:                # одинокий суррогат из JSON — в UTF-8 его нет
        raise _refuse(422, "invalid", BAD_BODY) from None
    if size > FILE_MAX:
        raise _refuse(413, "too_large", TOO_LARGE)
    data = await ask(request, aid, "save", project=pid, path=path, text=text, version=version)
    return _strings(data, "version")


def _pdf_bytes(data: dict) -> bytes:
    """Файл из ответа `raw`: не больше RAW_MAX (предел протокола держит и мост) — иначе 413 `too_large`."""
    text = data.get("data")
    if isinstance(text, str) and len(text) > (RAW_MAX + 2) // 3 * 4:          # не раскодировать заведомо лишнее
        raise _refuse(413, "too_large", TOO_LARGE_RAW)
    try:
        raw = base64.b64decode(text, validate=True)
    except (TypeError, ValueError):           # не строка, не ASCII, не base64 (binascii.Error — ValueError)
        raise _refuse(502, "bridge", ODD) from None
    if len(raw) > RAW_MAX:
        raise _refuse(413, "too_large", TOO_LARGE_RAW)
    if not raw.startswith(b"%PDF-"):
        raise _refuse(415, "not_pdf", NOT_PDF)
    return raw


async def _pdfium(fn, *args):
    """PDFium — в потоке (замок `ocr_render.PDFIUM` держит он сам): страница 300 DPI — десятые доли секунды."""
    try:
        return await asyncio.to_thread(fn, *args)
    except ocr_render.ScanError as e:
        raise _refuse(422, "pdf", str(e)) from None


@router.post("/agents/{aid}/projects/{pid}/pdf")
async def pdf(aid: int, pid: str, body: PdfPage, request: Request):
    """Страница PDF картинкой PNG, число страниц — заголовком `X-Studio-Pages`. Файл — просьба `raw`, затем
    из PDFS: память одна на пользователя и привязана к сессии — другая сессия без PIN памятью не пройдёт мимо
    `closed` моста. И из памяти страница — только после дешёвой просьбы `projects`: открыт ли компьютер этой
    сессии (заперлась и отперлась, новый канал, мост перезапущен, не на связи), решает мост, а не память."""
    own_agent(request, aid)
    check_pid(pid)
    path = check_path(body.path)
    uid, now = request.state.user.id, clock.NOW()
    for user, kept in list(PDFS.items()):
        if now - kept[3] >= PDF_TTL_SEC:
            del PDFS[user]
    key = (aid, pid, path, bridge.tag(request.state.digest))
    kept = PDFS.get(uid)
    if kept is not None and kept[0] == key:
        await ask(request, aid, "projects")
        _, data, pages, _ = kept
    else:
        data = _pdf_bytes(await ask(request, aid, "raw", project=pid, path=path))
        pages = await _pdfium(ocr_render.pdf_page_count, data)
        PDFS[uid] = (key, data, pages, now)
    if body.page > pages:
        raise _refuse(404, "page", NO_PAGE)
    png = await _pdfium(ocr_render.render_page, data, body.page - 1)
    return Response(png, media_type="image/png", headers={"X-Studio-Pages": str(pages)})


@router.get("/agents/{aid}/projects/{pid}/git")
async def git(aid: int, pid: str, request: Request):
    own_agent(request, aid)
    check_pid(pid)
    return await ask(request, aid, "git", project=pid)
