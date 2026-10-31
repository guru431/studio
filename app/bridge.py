"""Мост Claude Code — нижний слой раздела «Claude Code» (спека 2026-10-28-studio-claude-code-design, 6.2–6.4,
6.10; кадры, коды закрытия, просьбы и пределы — протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md).

- Канал: мост за NAT сам держит исходящий WebSocket `/api/agent/link` с ключом устройства в заголовке
  `Authorization` (не в строке запроса — её пишет журнал Apache); по нему студия шлёт мосту просьбы, мост —
  ответы и поток событий Claude Code. «На связи» — есть ли канал (`LINKS`), в памяти, не в базе. Канал
  у компьютера один: новый заменяет прежний (4409). Отзыв, отключение и удаление учётки рвут канал (4401),
  смена пароля — нет: ключ устройства самостоятелен (спека 6.2).
- Просьба (`call`) ждёт ответа с тем же `id` не дольше `CALL_SEC`; канал пропал — сразу `Offline`, а не по
  сроку. События хода раздаются очередям наблюдателей чата (`WATCHERS`); канал пропал или его заменил новый —
  им `("offline", None)`. Кадры канала, который уже не текущий (заменён, снят), никуда не идут.
  Карточку (`ask`) или конец хода (`done`) чата без наблюдателей владельцу сообщает push (`push.notify_later`).
  Своего состояния хода студия не хранит: источник правды — машина (спека 6.10).
- Признак сессии студии для моста — `tag`: не дайджест сессии и тем более не токен, а производное от
  дайджеста; мост открывает компьютер PIN-ом для этого признака. Заперлась сессия или отперлась —
  `forget(close_computers=False)`: очереди наблюдателей этой сессии (`Watch.session`) получают
  `("closed", None)`, кэши уходят, срок PIN компьютеров сохраняется. Явный сброс шлёт мостам `close` (§2.3).
- История подключений — `agent_events`: только добавляется (`record`), «Удалить всю историю» её не трогает,
  уходит только с учёткой. Строка несёт id и имя компьютера на момент события (после отзыва строки `agents`
  нет), краткий признак устройства (`device`) и адрес — ни ключа, ни PIN, ни текстов.
- Журнал — «агент N: …» и адрес неизвестного ключа: ни ключа, ни имени компьютера, проектов и чатов.
"""
import asyncio
import hashlib
import json
import logging
import re
import secrets

from fastapi import APIRouter, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

import auth
import clock
import push
import security
import state

log = logging.getLogger("studio")
router = APIRouter()

CLOSE_PROTOCOL = 4400
CLOSE_AUTH = 4401
CLOSE_REPLACED = 4409
CLOSE_RESTART = 1012
EVENT_KINDS = frozenset({"text", "step", "result", "ask", "asked", "snapshot", "done", "settings", "context", "activity",
                         "input_accepted", "followup", "followup_sent", "ownership"})
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FRAME_MAX = 16 * 1024 * 1024
HELLO_SEC = 10
CALL_SEC = 30
WATCH_MAX = 1000
STOP_SEC = 2
MESSAGE_MAX = 300
CODE_MAX = 40
MODELS_MAX = 50
MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
OPENCODE_MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]/@+-]{1,300}")
OPENCODE_EFFORT = re.compile(r"[A-Za-z0-9._-]{1,64}")
OPENCODE_MODELS_MAX = 500
PID = re.compile(r"[0-9a-f]{16}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class ProtocolError(ValueError):
    """Мост нарушил протокол — канал закрывается 4400."""


class Offline(Exception):
    """Канала нет или он пропал, пока ждали ответа."""


class BridgeError(Exception):
    """Отказ моста (`res` с `ok: false`): код, слова для человека, `extra` — `left` и `version`."""

    def __init__(self, code: str, message: str, extra: dict):
        super().__init__(code)
        self.code, self.message, self.extra = code, message, extra


class Link:
    """Канал одного компьютера. `pending` — просьбы, ждущие ответа: id → будущий кадр `res`."""

    def __init__(self, agent_id: int, user_id: int, name: str, ip: str | None = None):
        self.agent_id, self.user_id, self.name, self.ip = agent_id, user_id, name, ip
        self.hello: dict = {}
        self.pending: dict[str, asyncio.Future] = {}

    async def send(self, frame: dict) -> None:
        raise NotImplementedError

    async def close(self, code: int) -> None:
        raise NotImplementedError


class SocketLink(Link):
    """Канал поверх WebSocket; отправки — под замком: кадры разных просьб не перемежаются."""

    def __init__(self, ws: WebSocket, agent_id: int, user_id: int, name: str, ip: str | None = None):
        super().__init__(agent_id, user_id, name, ip)
        self.ws = ws
        self.lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(frame, ensure_ascii=False))

    async def close(self, code: int) -> None:
        async with self.lock:
            try:
                await self.ws.close(code)
            except Exception:            # уже закрыт или сокет оборвался — закрывать нечего
                pass


class Watch(asyncio.Queue):
    """Очередь наблюдателя чата: ("ev", событие) | ("offline", None) | ("closed", None); `session` — `tag`
    сессии студии, что смотрит (её `forget` кладёт `closed`). Событий в ней — не больше `WATCH_MAX`: застрявший
    поток SSE (TCP жив, страница не читает) получает `offline` и снимается (`_put_event`)."""

    def __init__(self, session: str | None):
        super().__init__()
        self.session = session


#: agent_id → канал на связи
LINKS: dict[int, Link] = {}
#: (agent_id, chat) → очереди наблюдателей чата
WATCHERS: dict[tuple[int, str], set[Watch]] = {}
#: фоновые отправки (`forget`) и закрытия каналов (`_close_later`): ссылка держит задачу до конца, `stop` ждёт
#: их не дольше `STOP_SEC` и отменяет остаток
TASKS: set[asyncio.Task] = set()
#: что ещё забыть, когда сессия заперлась или отперлась: (user_id, tag сессии) → None. Ставят разделы выше
#: (`claude_code` — память PDF), импорт отсюда к ним был бы кольцом
FORGET_HOOKS: list = []


def _background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def _close_later(link: "Link", code: int) -> None:
    """Закрыть сокет канала фоном: `close` ждёт замок отправки, а висящая отправка (полный буфер, мёртвая сеть
    посреди большого кадра) держит его до таймаута пинга — столько же ждали бы новый канал и отзыв."""
    _background(link.close(code))


def tag(digest: str) -> str:
    """Признак сессии студии для моста: из него не получить ни дайджест, ни токен сессии."""
    return hashlib.sha256(("studio-code\0" + digest).encode()).hexdigest()[:32]


def device(request: Request) -> str:
    """С какого устройства действие: «телефон» или «браузер» по сессии запроса и начало её признака."""
    row = state.DB.execute("SELECT mobile FROM sessions WHERE digest = ?", (request.state.digest,)).fetchone()
    kind = "телефон" if row is not None and row["mobile"] else "браузер"
    return f"{kind} {tag(request.state.digest)[:6]}"


def record(user_id: int, agent_id: int, agent_name: str, kind: str, *,
           device: str | None = None, ip: str | None = None) -> None:
    """Строка истории подключений."""
    state.DB.execute("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, agent_id, agent_name, clock.NOW(), kind, device, ip))


# ---- знакомство и учёт канала ----------------------------------------------------

def _string(value, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ProtocolError("hello")
    return value


def parse_hello(frame: dict) -> dict:
    """Кадр `hello` (протокол §2.1) → поля, которые студия держит; иначе `ProtocolError`."""
    if not isinstance(frame, dict) or frame.get("t") != "hello" or type(frame.get("v")) is not int \
            or frame["v"] != 1 or not isinstance(frame.get("has_pin"), bool):
        raise ProtocolError("hello")
    models, efforts = frame.get("models"), frame.get("efforts")
    if not isinstance(models, list) or len(models) > MODELS_MAX or not isinstance(efforts, list):
        raise ProtocolError("hello")
    out_models = []
    for m in models:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not MODEL_ID.fullmatch(m["id"]):
            raise ProtocolError("hello")
        out_models.append({"id": m["id"], "label": _string(m.get("label"), 100)})
    if not all(isinstance(e, str) and e in EFFORTS for e in efforts):
        raise ProtocolError("hello")
    result = {"cc_version": _string(frame.get("cc_version"), 64),
              "projects_root": _string(frame.get("projects_root"), 1024),
              "has_pin": frame["has_pin"], "models": out_models, "efforts": list(dict.fromkeys(efforts))}
    if "images" in frame:
        if type(frame["images"]) is not bool:
            raise ProtocolError("hello")
        result["images"] = frame["images"]
    for capability in ("chat_state", "chat_control", "attachments", "project_activity", "subscription_usage", "project_summary"):
        if capability in frame:
            if type(frame[capability]) is not bool:
                raise ProtocolError("hello")
            result[capability] = frame[capability]
    if isinstance(frame.get("defaults"), dict):
        result["defaults"] = {k: v for k, v in frame["defaults"].items() if k in ("model", "effort", "access")
                              and (v is None or isinstance(v, str) and len(v) <= 100)}
    if "codex" in frame:
        codex = frame["codex"]
        if not isinstance(codex, dict):
            raise ProtocolError("hello")
        levels = codex.get("efforts")
        if not isinstance(levels, list) or not all(isinstance(e, str) and e in
                ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra") for e in levels):
            raise ProtocolError("hello")
        parsed = parse_hello({"t": "hello", "v": 1, "has_pin": False, "projects_root": "",
                              "cc_version": codex.get("version"), "models": codex.get("models"), "efforts": []})
        result["codex"] = {"version": parsed["cc_version"], "models": parsed["models"],
                           "efforts": list(dict.fromkeys(levels))}
        if isinstance(codex.get("defaults"), dict):
            result["codex"]["defaults"] = {k: v for k, v in codex["defaults"].items()
                if k in ("model", "effort", "access") and (v is None or isinstance(v, str) and len(v) <= 100)}
    if "opencode" in frame:
        catalog = frame["opencode"]
        if not isinstance(catalog, dict):
            raise ProtocolError("hello")
        models, efforts = catalog.get("models"), catalog.get("efforts")
        if (not isinstance(models, list) or len(models) > OPENCODE_MODELS_MAX or not isinstance(efforts, list)
                or len(efforts) > 100 or not all(isinstance(e, str) and OPENCODE_EFFORT.fullmatch(e) for e in efforts)):
            raise ProtocolError("hello")
        out_models = []
        for model in models:
            if (not isinstance(model, dict) or not isinstance(model.get("id"), str)
                    or not OPENCODE_MODEL_ID.fullmatch(model["id"])):
                raise ProtocolError("hello")
            out_models.append({"id": model["id"], "label": _string(model.get("label"), 100)})
        result["opencode"] = {"version": _string(catalog.get("version"), 64), "models": out_models,
                              "efforts": list(dict.fromkeys(efforts))}
        if isinstance(catalog.get("defaults"), dict):
            result["opencode"]["defaults"] = {k: v for k, v in catalog["defaults"].items()
                if k in ("model", "effort", "access") and (v is None or isinstance(v, str) and len(v) <= 300)}
    return result


async def attach(link: Link) -> None:
    """Канал на связи: прежний канал компьютера закрывается 4409 (фоном — `welcome` новому сразу). Компьютер
    отозвали или учётку отключили, пока шло знакомство, — 4401 и в `LINKS` его нет. `welcome` несёт логин
    учётки компьютера — мост показывает, под кем он на связи."""
    hello = link.hello
    row = state.DB.execute(
        "UPDATE agents SET has_pin = ?, cc_version = ?, projects_root = ?, last_seen = ?"
        " WHERE id = ? AND user_id IN (SELECT id FROM users WHERE disabled = 0)"
        " RETURNING name, (SELECT login FROM users WHERE users.id = agents.user_id) AS login",
        (int(hello["has_pin"]), hello["cc_version"], hello["projects_root"], clock.NOW(), link.agent_id)).fetchone()
    if row is None:
        log.warning("агент %d: отозван до знакомства", link.agent_id)
        await link.close(CLOSE_AUTH)
        return
    link.name = row["name"]
    old = LINKS.get(link.agent_id)
    LINKS[link.agent_id] = link
    record(link.user_id, link.agent_id, link.name, "online", ip=link.ip)
    log.info("агент %d: на связи", link.agent_id)
    if old is not None and old is not link:
        log.info("агент %d: прежний канал заменён", link.agent_id)
        _fail_pending(old)               # ответов по закрытому каналу уже не будет
        _offline(link.agent_id)          # потоки переподключатся к новому каналу
        _close_later(old, CLOSE_REPLACED)
    try:
        await link.send({"t": "welcome", "agent_id": link.agent_id, "name": link.name, "login": row["login"]})
    except Exception:
        pass                             # канал уже рвётся: его конец сделает detach


def _fail_pending(link: Link) -> None:
    for fut in link.pending.values():
        if not fut.done():
            fut.set_exception(Offline())
    link.pending.clear()


def _offline(agent_id: int) -> None:
    """Наблюдателям чатов компьютера — вестей по этому каналу больше не будет."""
    for (aid, _chat), queues in WATCHERS.items():
        if aid == agent_id:
            for q in queues:
                q.put_nowait(("offline", None))


async def detach(link: Link) -> None:
    """Канал кончился. Без await внутри: зовётся из `finally` обработчика, и отмена задачи (остановка
    службы) не должна оборвать его посередине."""
    _fail_pending(link)
    if LINKS.get(link.agent_id) is not link:
        return
    del LINKS[link.agent_id]
    _offline(link.agent_id)
    state.DB.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (clock.NOW(), link.agent_id))
    record(link.user_id, link.agent_id, link.name, "offline", ip=link.ip)
    log.info("агент %d: связь потеряна", link.agent_id)


def _event_ok(frame: dict) -> bool:
    project, chat, e = frame.get("project"), frame.get("chat"), frame.get("e")
    return (isinstance(project, str) and PID.fullmatch(project) is not None
            and isinstance(chat, str) and UUID.fullmatch(chat) is not None
            and isinstance(e, dict) and e.get("k") in EVENT_KINDS)


def deliver(link: Link, frame: dict) -> None:
    """Кадр моста после знакомства: ответ — его просьбе, событие — наблюдателям чата."""
    t = frame.get("t")
    if t == "res":
        rid = frame.get("id")
        fut = link.pending.get(rid) if isinstance(rid, str) else None
        if fut is not None and not fut.done():
            fut.set_result(frame)
    elif t == "ev":
        if LINKS.get(link.agent_id) is not link:
            return                       # канал заменён или снят (отзыв): между detach и close сокет ещё читается
        if not _event_ok(frame):
            log.warning("агент %d: неверное событие", link.agent_id)
            return
        for q in list(WATCHERS.get((link.agent_id, frame["chat"]), ())):
            _put_event(link.agent_id, frame["chat"], q, frame["e"])
        _push(link, frame)
    else:
        raise ProtocolError("t")


#: повод push → слова тела (тексты сервера плана)
ASK_WORDS = {"permission": "ждёт подтверждения", "question": "вопрос"}
DONE_WORDS = {"done": "ход окончен", "stopped": "ход прерван", "error": "ошибка"}


def _push(link: Link, frame: dict) -> None:
    """Карточка или конец хода, которых никто не смотрит, — push владельцу (спека 6.8)."""
    e = frame["e"]
    words, value = {"ask": (ASK_WORDS, e.get("kind")), "done": (DONE_WORDS, e.get("status"))}.get(e["k"], ({}, None))
    text = words.get(value) if isinstance(value, str) else None
    if text is None or watching(link.agent_id, frame["chat"]):
        return
    push.notify_later(link.user_id, f"{link.name}: {text}",
                      f"code/{link.agent_id}/{frame['project']}/{frame['chat']}", f"code-{frame['chat']}")


# ---- просьбы -------------------------------------------------------------------

SURROGATE = re.compile(r"[\ud800-\udfff]")


def _refusal(frame: dict) -> BridgeError:
    """Слова и `version` отказа уходят браузеру обычным ответом FastAPI (UTF-8): одинокий суррогат (Node режет
    UTF-16 посреди эмодзи) уронил бы его 500 — в словах он заменяется «�», `version` с ним не передаётся."""
    code = frame.get("code")
    message = frame.get("message")
    extra = {}
    if type(frame.get("left")) is int:
        extra["left"] = frame["left"]
    if isinstance(frame.get("version"), str) and not SURROGATE.search(frame["version"]):
        extra["version"] = frame["version"]
    return BridgeError(code[:CODE_MAX] if isinstance(code, str) and code else "bridge",
                       SURROGATE.sub("�", message[:MESSAGE_MAX]) if isinstance(message, str) else "", extra)


async def call(agent_id: int, op: str, session: str, timeout: float | None = None, **args) -> dict:
    """Просьба мосту и его `data`. Нет канала или пропал — `Offline`; отказ — `BridgeError`; молчание дольше
    `timeout` (по умолчанию `CALL_SEC`) — `TimeoutError`."""
    link = LINKS.get(agent_id)
    if link is None:
        raise Offline()
    rid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    link.pending[rid] = fut

    async def ask():
        try:
            await link.send({"t": "req", "id": rid, "op": op, "session": session, **args})
        except Exception:
            raise Offline() from None
        return await fut

    try:
        frame = await asyncio.wait_for(ask(), CALL_SEC if timeout is None else timeout)
    finally:
        link.pending.pop(rid, None)
    if frame.get("ok") is True:
        data = frame.get("data")
        return data if isinstance(data, dict) else {}
    raise _refusal(frame)


# ---- наблюдатели ---------------------------------------------------------------

def watch(agent_id: int, chat: str, session: str | None = None) -> Watch:
    q = Watch(session)
    WATCHERS.setdefault((agent_id, chat), set()).add(q)
    return q


def unwatch(agent_id: int, chat: str, q: Watch) -> None:
    queues = WATCHERS.get((agent_id, chat))
    if queues is not None:
        queues.discard(q)
        if not queues:
            del WATCHERS[(agent_id, chat)]


def watching(agent_id: int, chat: str) -> bool:
    return bool(WATCHERS.get((agent_id, chat)))


def _put_event(agent_id: int, chat: str, q: Watch, e: dict) -> None:
    """Событие наблюдателю; отстал на `WATCH_MAX` — вместо события `offline` (поток кончится, страница дочитает
    снимком `live`), и очередь снята: больше в неё ничего не идёт."""
    if q.qsize() < WATCH_MAX:
        q.put_nowait(("ev", e))
        return
    unwatch(agent_id, chat, q)
    q.put_nowait(("offline", None))
    log.warning("агент %d: наблюдатель отстал — снят", agent_id)


# ---- закрытие ------------------------------------------------------------------

def forget(user_id: int, digest: str, *, close_computers: bool = True) -> None:
    """Снять потоки и кэши сессии. Блокировка экрана сохраняет срок PIN компьютеров;
    явный сброс дополнительно шлёт мостам `close` фоном, не ожидая чужой сокет."""
    session = tag(digest)
    for hook in FORGET_HOOKS:
        hook(user_id, session)
    for queues in WATCHERS.values():
        for q in queues:
            if q.session == session:
                q.put_nowait(("closed", None))
    if not close_computers:
        return
    frame = {"t": "close", "session": session}
    for link in [x for x in LINKS.values() if x.user_id == user_id]:
        async def send(link=link):
            try:
                await link.send(frame)
            except Exception as e:
                log.warning("агент %d: закрытие сессии не отправлено (%s)", link.agent_id, type(e).__name__)
        _background(send())


async def drop(agent_id: int, code: int = CLOSE_AUTH) -> None:
    """Канал компьютера — закрыть: сперва снять с учёта (новые просьбы — `Offline`), потом закрыть сокет —
    фоном (`_close_later`): отзыв и удаление учётки не ждут висящую отправку."""
    link = LINKS.get(agent_id)
    if link is not None:
        await detach(link)
        _close_later(link, code)


async def drop_user(user_id: int) -> None:
    for agent_id in [x.agent_id for x in LINKS.values() if x.user_id == user_id]:
        await drop(agent_id)


async def stop() -> None:
    """Остановка службы: каналы — 1012 (мост переподключится); фоновые закрытия и отправки ждут не дольше
    `STOP_SEC`, остаток отменяется (висящий сокет закроет сам uvicorn)."""
    for agent_id in list(LINKS):
        await drop(agent_id, CLOSE_RESTART)
    if TASKS:
        await asyncio.wait(list(TASKS), timeout=STOP_SEC)
    for task in list(TASKS):
        task.cancel()
    await asyncio.gather(*list(TASKS), return_exceptions=True)


# ---- канал ---------------------------------------------------------------------

def _frame(message: dict) -> dict:
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    if not isinstance(text, str) or len(text) > FRAME_MAX or len(text.encode()) > FRAME_MAX:
        raise ProtocolError("frame")
    try:
        frame = json.loads(text)
    except (ValueError, RecursionError):          # RecursionError — вложенность глубже стека разбора
        raise ProtocolError("json") from None
    if not isinstance(frame, dict):
        raise ProtocolError("json")
    return frame


@router.websocket("/api/agent/link")
async def link_socket(ws: WebSocket):
    ip = auth.client_ip(ws)
    header = ws.headers.get("authorization", "")
    row = None
    if header.startswith("Bearer ") and len(header) > len("Bearer "):
        row = state.DB.execute("SELECT a.id, a.user_id, a.name FROM agents a JOIN users u ON u.id = a.user_id"
                               " WHERE a.key_hash = ? AND u.disabled = 0",
                               (security.digest(header[len("Bearer "):]),)).fetchone()
    await ws.accept()
    if row is None:
        log.warning("канал: неизвестный ключ с %s", ip)
        await ws.close(CLOSE_AUTH)
        return
    link = SocketLink(ws, row["id"], row["user_id"], row["name"], ip)
    try:
        try:
            link.hello = parse_hello(_frame(await asyncio.wait_for(ws.receive(), HELLO_SEC)))
        except (TimeoutError, ProtocolError):
            log.warning("агент %d: нарушение протокола", link.agent_id)
            await link.close(CLOSE_PROTOCOL)
            return
        await attach(link)
        if LINKS.get(link.agent_id) is not link:
            return
        while True:
            deliver(link, _frame(await ws.receive()))
    except ProtocolError:
        log.warning("агент %d: нарушение протокола", link.agent_id)
        await link.close(CLOSE_PROTOCOL)
    except WebSocketDisconnect:
        pass
    finally:
        await detach(link)
