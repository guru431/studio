"""Просьбы студии (протокол §2.2, §3, §4): проверка полей, открытость сессий (PIN — на машине), исполнение.

Всё блокирующее — в потоке (`asyncio.to_thread`): цикл держит пинги канала."""

import asyncio
import logging
import re
import time
from collections.abc import Callable

from . import cli, events, files, gitinfo, image_input, limits, permissions, projects, sessions, store
from .errors import Refused
from .pin import OpenSessions, PinStore
from .turns import REQUEST_ID, Runner

log = logging.getLogger(__name__)

ID = re.compile(r"[0-9a-f]{16}")
ID_MAX = 64  # длиннее — без ответа: такой id не вернуть
SESSION = re.compile(r"[0-9a-f]{32}")
VERSION = re.compile(r"[0-9a-f]{64}")
TURN_TEXT_MAX = 100_000
PIN_FIELD_MAX = 64

# Поля просьбы по op (кроме `open`); вид каждого поля — `Agent._field`.
OPS = {
    "projects": (),
    "chats": ("project",),
    "history": ("project", "chat", "before", "limit"),
    "state": ("project", "chat", "model", "effort", "access"),
    "turn": ("project", "chat", "text", "model", "effort", "access"),
    "watch": ("project", "chat"),
    "answer": ("project", "chat", "request_id"),
    "stop": ("project", "chat"),
    "files": ("project", "path"),
    "read": ("project", "path"),
    "raw": ("project", "path"),
    "save": ("project", "path", "text", "version"),
    "git": ("project",),
}


def read_has_pin(pins: PinStore) -> bool:
    """Задан ли PIN (DPAPI — звать в потоке); `pin.bin` не читается (сброс пароля Windows, порча) — `True`:
    безопасная сторона."""
    try:
        return pins.has_pin
    except (OSError, ValueError) as e:
        log.warning("PIN не читается: %s", type(e).__name__)
        return True


class Agent:
    """Просьбы студии одного моста; `send` ставит канал (`Link`), без него события хода пропадают.

    `has_pin` для `hello` — в памяти: `hello` идёт на цикле канала, а `pin.bin` — DPAPI. Читается при создании (без
    `has_pin` — сразу, ядро читает в потоке и передаёт) и `refresh_pin` — перед каждым новым каналом ядра."""

    def __init__(self, cfg: store.Config, pins: PinStore, runner: Runner, *, now: Callable[[], float] = time.monotonic,
                 cc_version: str, models: list[dict], efforts: list[str], has_pin: bool | None = None, codex=None):
        self.cfg = cfg
        self.pins = pins
        self.runner = runner
        self.codex = codex
        self.cc_version = cc_version
        self.models = models
        self.efforts = efforts
        self.has_pin = read_has_pin(pins) if has_pin is None else has_pin
        self.open = OpenSessions(now)
        self.send: Callable[[dict], None] | None = None
        self.on_pin_locked: Callable[[], None] | None = None  # PIN заблокировала эта проверка (трей — «Снять блок»)
        self._open_lock = asyncio.Lock()
        self._link = 0  # поколение канала (`new_link`): открытие, начатое прежним каналом, сессию не открывает

    def hello(self) -> dict:
        """Знакомство; к диску не обращается."""
        hello = {"t": "hello", "v": 1, "cc_version": self.cc_version, "projects_root": self.cfg.projects_root,
                 "has_pin": self.has_pin, "models": self.models, "efforts": self.efforts, "images": True, "chat_state": True}
        if defaults := cli.DEFAULTS.get(str(self.cfg.projects_root)):
            hello["defaults"] = defaults
        if self.codex is not None and self.codex.catalog is not None:
            hello["codex"] = self.codex.catalog
        return hello

    async def refresh_pin(self) -> None:
        """`has_pin` заново из `pin.bin` (в потоке): PIN сменили на машине — студия узнает в следующем `hello`."""
        self.has_pin = await asyncio.to_thread(read_has_pin, self.pins)

    def set_models(self, models: list[dict], efforts: list[str]) -> None:
        self.models, self.efforts = models, efforts

    def close_session(self, session: str) -> None:
        self.open.close(session)

    def new_link(self) -> None:
        """Новый канал: все открытые сессии прочь (протокол §3), и те, что открываются сейчас (`_open`)."""
        self._link += 1
        self.open.clear()

    def emit(self, frame: dict) -> None:
        """Событие хода — в канал, если он есть; иначе пропадает (вернувшись, студия просит `watch`)."""
        if self.send is not None:
            self.send(frame)

    # --- просьба ---

    async def handle(self, frame: dict) -> list[dict]:
        """Кадр `req` → `[res, *после]`; `id`, который не вернуть, — без ответа."""
        rid = frame.get("id") if isinstance(frame, dict) else None
        if not (isinstance(rid, str) and len(rid) <= ID_MAX):
            log.warning("просьба без id")
            return []
        op = frame.get("op")
        name = op if op == "open" or op in OPS else "?"  # в журнал — только известные имена
        after: list[dict] = []
        try:
            data = await self._dispatch(frame, op, after)
            res = {"t": "res", "id": rid, "ok": True, "data": data}
        except Refused as e:
            log.info("просьба %s: %s%s", name, e.code, f" ({e.extra['left']})" if "left" in e.extra else "")
            res = {"t": "res", "id": rid, "ok": False, "code": e.code, "message": e.message[:limits.MESSAGE_MAX],
                   **e.extra}
        except Exception as e:
            log.warning("просьба %s: %s", name, type(e).__name__)
            return [_error(rid)]
        try:
            res = events.ready(res)  # кодируется один раз: писатель канала шлёт готовую строку
        except ValueError as e:
            log.warning("просьба %s: ответ не кодируется: %s", name, type(e).__name__)
            return [_error(rid)]
        size = len(res.text)
        if size > limits.FRAME_MAX:
            log.warning("просьба %s: ответ %d байт больше предела", name, size)
            return [{"t": "res", "id": rid, "ok": False, "code": "too_large", "message": "ответ больше предела канала"}]
        return [res, *after]

    async def _dispatch(self, frame: dict, op, after: list[dict]) -> dict:
        if not ID.fullmatch(frame["id"]):
            raise Refused("invalid")
        session = frame.get("session")
        if not (isinstance(session, str) and SESSION.fullmatch(session)):
            raise Refused("invalid")
        if op == "open":
            return await self._open(session, frame.get("pin"))
        if not self.open.touch(session):
            raise Refused("closed")
        if op not in OPS:
            raise Refused("invalid")
        engine = frame.get("engine", "claude")
        if engine not in ("claude", "codex"):
            raise Refused("invalid")
        if engine == "codex" and (self.codex is None or self.codex.catalog is None):
            raise Refused("unsupported", "Codex не найден на компьютере")
        for name in OPS[op]:
            if name == "access" and not permissions.valid(engine, frame.get(name)):
                raise Refused("invalid")
            if name in ("model", "effort") and engine == "codex":
                value = frame.get(name)
                allowed = {m["id"] for m in self.codex.models} if name == "model" else self.codex.efforts
                if value is not None and value not in allowed:
                    raise Refused("invalid")
            elif not self._field(op, name, frame.get(name)):
                raise Refused("invalid")
        return await getattr(self, "_op_" + op)(frame, after)

    def _field(self, op: str, name: str, value) -> bool:
        """Вид поля до исполнения; путь — как `files.resolve`, без диска."""
        if name == "project":
            return isinstance(value, str) and ID.fullmatch(value) is not None
        if name == "chat":
            return isinstance(value, str) and limits.CHAT_ID.fullmatch(value) is not None
        if name == "before":
            return value is None or (type(value) is int and value >= 0)
        if name == "limit":
            return type(value) is int and 1 <= value <= 100
        if name == "text":  # текст файла `save` — предел байтов сверяет files.save_text
            return isinstance(value, str) and (op == "save" or len(value) <= TURN_TEXT_MAX)
        if name == "model":
            return value is None or value in {m["id"] for m in self.models}
        if name == "effort":
            return value is None or value in self.efforts
        if name == "access":
            return value is None or isinstance(value, str) and value in (*permissions.CLAUDE, *permissions.CODEX)
        if name == "version":
            return isinstance(value, str) and VERSION.fullmatch(value) is not None
        if name == "request_id":
            return isinstance(value, str) and REQUEST_ID.fullmatch(value) is not None
        if name == "path":
            try:
                files._parts(value)
            except Refused:
                return False
            return True
        raise AssertionError(name)

    async def _open(self, session: str, pin) -> dict:
        """Порядок — Ruling 4: без PIN — открыть; `null` открытой — продлить; блок; `null` — нужен PIN; проверка.

        Пока шла проверка, пришёл новый канал (`new_link`) — сессия не открывается, `closed`: новый канал сбрасывает
        открытость и той сессии, что открывалась по прежнему (протокол §3)."""
        if not (pin is None or (isinstance(pin, str) and len(pin) <= PIN_FIELD_MAX)):
            raise Refused("invalid")
        link = self._link
        async with self._open_lock:
            has_pin, locked = await asyncio.to_thread(lambda: (self.pins.has_pin, self.pins.locked))
            if not has_pin:
                self._open_now(session, link)
                return {}
            if pin is None and self.open.touch(session):
                return {}
            if locked:
                raise Refused("pin_locked")
            if pin is None:
                raise Refused("pin_needed")
            code, left = await asyncio.to_thread(self.pins.check, pin)
            if code == "pin":
                raise Refused("pin", left=left)
            if code == "pin_locked":
                if self.on_pin_locked is not None:
                    self.on_pin_locked()
                raise Refused("pin_locked")
            self._open_now(session, link)
            return {}

    def _open_now(self, session: str, link: int) -> None:
        if link != self._link:
            log.info("открытие: канал сменился")
            raise Refused("closed")
        self.open.open(session)

    async def _project(self, frame: dict):
        return await asyncio.to_thread(projects.find_project, self.cfg.projects_root, frame["project"])

    # --- op ---

    async def _op_projects(self, frame, after):
        return {"items": await asyncio.to_thread(projects.list_projects, self.cfg.projects_root)}

    async def _op_chats(self, frame, after):
        project = await self._project(frame)
        items = await asyncio.to_thread(sessions.chats, project, self.runner.live())
        if self.codex is not None:
            try:
                items += await self.codex.chats(project)
            except Exception as e:
                log.warning("Codex: сессии: %s", type(e).__name__)
        return {"items": items}

    async def _op_history(self, frame, after):
        project = await self._project(frame)
        if frame.get("engine") == "codex":
            return await self.codex.history(project, frame["chat"], frame.get("before"), frame["limit"])
        return await asyncio.to_thread(sessions.history, project, frame["chat"], frame.get("before"), frame["limit"])

    def _runner(self, frame):
        return self.codex if frame.get("engine") == "codex" else self.runner

    async def _op_state(self, frame, after):
        project = await self._project(frame)
        return await self._runner(frame).state(project, frame["chat"], frame.get("model"), frame.get("effort"),
                                               frame.get("access"))

    async def _op_turn(self, frame, after):
        images = frame.get("images", [])
        await asyncio.to_thread(image_input.validate, images)
        if not frame["text"].strip() and not images:
            raise Refused("invalid", "задание пустое")
        project = await self._project(frame)
        runner = self._runner(frame)
        total = len(self.runner.live()) + (len(self.codex.live()) if self.codex else 0)
        if frame["chat"] not in runner.live() and total >= limits.MAX_TURNS:
            raise Refused("busy")
        return {"turn": runner.start(project, frame["project"], frame["chat"], frame["text"],
                                     frame.get("model"), frame.get("effort"), images=images,
                                     access=frame.get("access"))}

    async def _op_watch(self, frame, after):
        """Хода нет (или он другого проекта) — `{"running": false}` без проверки чата (протокол §4); идёт — снимок
        за ответом."""
        running, frames = await self._runner(frame).watch(frame["project"], frame["chat"])
        after.extend(frames)
        return {"running": running}

    async def _op_answer(self, frame, after):
        self._runner(frame).answer(frame["project"], frame["chat"], frame["request_id"], frame.get("decision"),
                                   frame.get("message"), frame.get("answers"))
        return {}

    async def _op_stop(self, frame, after):
        await self._runner(frame).stop(frame["project"], frame["chat"])
        return {}

    async def _op_files(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.list_dir, project, frame["path"])

    async def _op_read(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.read_text, project, frame["path"])

    async def _op_raw(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.read_raw, project, frame["path"])

    async def _op_save(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.save_text, project, frame["path"], frame["text"], frame["version"])

    async def _op_git(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(gitinfo.status, project)


def _error(rid: str) -> dict:
    return {"t": "res", "id": rid, "ok": False, "code": "error", "message": "ошибка моста"}
