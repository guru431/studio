"""Просьбы студии (протокол §2.2, §3, §4): проверка полей, открытость сессий (PIN — на машине), исполнение.

Всё блокирующее — в потоке (`asyncio.to_thread`): цикл держит пинги канала."""

import asyncio
import logging
import re
import time
from collections.abc import Callable

from . import attachments, branches, cli, computer_info, desktop, discovery, events, file_stats, files, github_status, gitinfo, limits, metadata, permissions, project_summary, projects, sessions, store, subagents, subscription, worktrees
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
    "desktop_status": (), "desktop_start": (), "desktop_frame": (), "desktop_input": (),
    "desktop_clipboard": (), "desktop_stop": (),
    "computer_info": (),
    "agent_update": (),
    "agent_update_status": (),
    "usage": ("refresh",),
    "projects": ("refresh",),
    "project_summary": ("project", "refresh", "remote"),
    "activity": (),
    "chats": ("project", "refresh"),
    "history": ("project", "chat", "before", "limit"),
    "state": ("project", "chat", "model", "effort", "access"),
    "turn": ("project", "chat", "text", "model", "effort", "access"),
    "steer": ("project", "chat", "text"),
    "queue_edit": ("project", "chat", "request_id", "version"),
    "queue_pause": ("project", "chat"),
    "claim": ("project", "chat"),
    "watch": ("project", "chat"),
    "answer": ("project", "chat", "request_id"),
    "stop": ("project", "chat"),
    "files": ("project", "path"),
    "file_stats": ("project", "path"),
    "file_operation": ("project", "operation", "path", "destination"),
    "file_suggestions": ("project", "query"),
    "chat_search": ("project", "query"),
    "chat_fork": ("project", "chat", "message_id"),
    "subagents": ("project", "chat"),
    "subagent_history": ("project", "chat", "request_id"),
    "worktrees": ("project",),
    "worktree_create": ("project", "query"),
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
                 cc_version: str, models: list[dict], efforts: list[str], has_pin: bool | None = None, codex=None,
                 opencode=None, wall: Callable[[], float] = time.time):
        self.cfg = cfg
        self.pins = pins
        self.runner = runner
        self.codex = codex
        self.opencode = opencode
        self.updater = None
        self.desktop = desktop.Desktop(cfg.desktop_enabled, now=now)
        self.set_max_turns(cfg.max_turns)
        self.cc_version = cc_version
        self.models = models
        self.efforts = efforts
        self.has_pin = read_has_pin(pins) if has_pin is None else has_pin
        self.open = OpenSessions(now)
        self.send: Callable[[dict], None] | None = None
        self.on_pin_locked: Callable[[], None] | None = None  # PIN заблокировала эта проверка (трей — «Снять блок»)
        self._open_lock = asyncio.Lock()
        self._usage = subscription.UsageCache(now=now)
        self._file_stats_lock = asyncio.Lock()
        self._files_lock = asyncio.Lock()
        self._search_lock = asyncio.Lock()
        self._file_index = discovery.FileIndex()
        self._catalog = projects.Catalog(now=now)
        self._metadata = metadata.Cache(now=now)
        self._wall = wall
        self._histories = metadata.HistoryIndex()
        self._link = 0  # поколение канала (`new_link`): открытие, начатое прежним каналом, сессию не открывает

    def set_max_turns(self, value: int) -> None:
        """Общий предел и пределы помощников меняются вместе, уже идущие задания продолжаются."""
        self.cfg.max_turns = value
        for backend in (self.runner, self.codex, self.opencode):
            if backend is not None:
                backend.max_turns = value

    def hello(self) -> dict:
        """Знакомство; к диску не обращается."""
        from . import VERSION as agent_version

        hello = {"t": "hello", "v": 1, "cc_version": self.cc_version, "projects_root": self.cfg.projects_root,
                 "agent_version": agent_version, "agent_update": self.updater is not None,
                 "has_pin": self.has_pin, "models": self.models, "efforts": self.efforts, "images": True,
                 "chat_state": True, "chat_control": True, "attachments": True, "project_activity": True,
                 "subscription_usage": True, "project_summary": True, "file_stats": True, "file_operations": True,
                 "file_suggestions": True, "chat_search": True, "queue_edit": True,
                 "chat_fork": True, "subagents": True, "worktrees": True, "pin_timeout": True,
                 "computer_info": True, "desktop": True, "metadata_cache": True}
        if defaults := cli.DEFAULTS.get(str(self.cfg.projects_root)):
            hello["defaults"] = defaults
        for name in ("codex", "opencode"):
            backend = getattr(self, name)
            if backend is not None and backend.catalog is not None:
                hello[name] = backend.catalog
        return hello

    async def refresh_pin(self) -> None:
        """`has_pin` заново из `pin.bin` (в потоке): PIN сменили на машине — студия узнает в следующем `hello`."""
        self.has_pin = await asyncio.to_thread(read_has_pin, self.pins)

    def set_models(self, models: list[dict], efforts: list[str]) -> None:
        self.models, self.efforts = models, efforts

    def close_session(self, session: str) -> None:
        self.open.close(session)
        self.desktop.revoke(session)

    def new_link(self, *, reconnect: bool = False) -> None:
        """Сменить поколение канала; переподключение сохраняет отметки PIN без продления срока.
        Явная настройка сбрасывает доступ, незавершённые проверки прежнего канала всегда отменяются."""
        self._link += 1
        self.desktop.revoke()
        if not reconnect:
            self.open.clear()
            self._metadata.invalidate()
            self._histories.clear()

    def emit(self, frame: dict) -> None:
        """Событие хода — в канал, если он есть; иначе пропадает (вернувшись, студия просит `watch`)."""
        if frame.get("e", {}).get("k") == "done":
            self._metadata.invalidate(frame.get("project"))
            self._histories.clear()
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
        idle_sec = frame.get("idle_sec", limits.OPEN_IDLE_SEC)
        if type(idle_sec) is not int or not 60 <= idle_sec <= 604800:
            raise Refused("invalid")
        if op == "open":
            return await self._open(session, frame.get("pin"), idle_sec)
        if not self.open.touch(session, idle_sec):
            raise Refused("closed")
        if op not in OPS:
            raise Refused("invalid")
        if self.updater is not None and self.updater.busy and op in (
                "turn", "save", "file_operation", "worktree_create", "chat_fork", "desktop_start"):
            raise Refused("busy", "Агент обновляется — дождитесь перезапуска")
        if frame.get("window") is not None and not (
                isinstance(frame["window"], str) and SESSION.fullmatch(frame["window"])):
            raise Refused("invalid")
        engine = frame.get("engine", "claude")
        if engine not in ("claude", "codex", "opencode"):
            raise Refused("invalid")
        backend = self._runner(frame)
        if engine != "claude" and (backend is None or backend.catalog is None):
            label = "Codex" if engine == "codex" else "OpenCode"
            raise Refused("unsupported", f"{label} не найден на компьютере")
        for name in OPS[op]:
            if name == "access" and not permissions.valid(engine, frame.get(name)):
                raise Refused("invalid")
            if name in ("model", "effort") and engine != "claude":
                value = frame.get(name)
                allowed = {m["id"] for m in backend.models} if name == "model" else backend.efforts
                if value is not None and (not isinstance(value, str) or value not in allowed):
                    raise Refused("invalid")
            elif not self._field(op, name, frame.get(name)):
                raise Refused("invalid")
        if op.startswith("desktop_"):
            return await self._desktop_request(op, frame)
        result = await getattr(self, "_op_" + op)(frame, after)
        if op in ("turn", "save", "file_operation", "worktree_create", "chat_fork"):
            self._metadata.invalidate(frame.get("project"))
        return result

    async def _desktop_request(self, op, frame):
        fields = {"desktop_status": set(), "desktop_start": set(), "desktop_stop": {"control"},
                  "desktop_frame": {"control", "monitor"}, "desktop_input": {"control", "events"},
                  "desktop_clipboard": {"control", "text"}}[op]
        common = {"t", "id", "op", "session", "idle_sec"}
        if set(frame) - common != fields:
            raise Refused("invalid")
        control = frame.get("control")
        if "control" in fields and not (isinstance(control, str) and desktop.CONTROL.fullmatch(control)):
            raise Refused("invalid")
        session = frame["session"]
        if op == "desktop_status":
            return self.desktop.status()
        if op == "desktop_start":
            return await self.desktop.start(session)
        if op == "desktop_stop":
            return self.desktop.stop(session, control)
        if op == "desktop_frame":
            monitor = frame["monitor"]
            if type(monitor) is not int or not 0 <= monitor < 32:
                raise Refused("invalid")
            return await self.desktop.frame(session, control, monitor)
        if op == "desktop_input":
            return await self.desktop.input(session, control, frame["events"])
        return await self.desktop.clipboard(session, control, frame["text"])

    async def _op_agent_update(self, frame, after):
        if self.updater is None:
            raise Refused("unsupported")
        return self.updater.start(frame)

    async def _op_computer_info(self, frame, after):
        return await asyncio.to_thread(computer_info.read, list(self.cfg.projects_roots))

    async def _op_agent_update_status(self, frame, after):
        if self.updater is None:
            raise Refused("unsupported")
        return self.updater.status()

    def _field(self, op: str, name: str, value) -> bool:
        """Вид поля до исполнения; путь — как `files.resolve`, без диска."""
        if name == "operation":
            return value in ("create_file", "create_dir", "move", "delete")
        if name == "destination" and value is None:
            return True
        if name in ("refresh", "remote"):
            return value is None or type(value) is bool
        if name == "query":
            return isinstance(value, str) and len(value) <= 200 and not any(ord(c) < 32 for c in value)
        if name == "message_id":
            return value is None or isinstance(value, str) and 1 <= len(value) <= 100 and not any(ord(c) < 32 for c in value)
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
            return value is None or isinstance(value, str) and value in (
                *permissions.CLAUDE, *permissions.CODEX, *permissions.OPENCODE)
        if name == "version":
            if op == "queue_edit":
                return type(value) is int and value >= 1
            return isinstance(value, str) and VERSION.fullmatch(value) is not None
        if name == "request_id":
            return isinstance(value, str) and REQUEST_ID.fullmatch(value) is not None
        if name in ("path", "destination"):
            try:
                files._parts(value)
            except Refused:
                return False
            return True
        raise AssertionError(name)

    async def _open(self, session: str, pin, idle_sec: int = limits.OPEN_IDLE_SEC) -> dict:
        """Порядок — Ruling 4: без PIN — открыть; `null` открытой — продлить; блок; `null` — нужен PIN; проверка.

        Пока шла проверка, пришёл новый канал (`new_link`) — сессия не открывается, `closed`:
        поздняя проверка прежнего канала не должна давать доступ по новому (протокол §3)."""
        if not (pin is None or (isinstance(pin, str) and len(pin) <= PIN_FIELD_MAX)):
            raise Refused("invalid")
        link = self._link
        async with self._open_lock:
            has_pin, locked = await asyncio.to_thread(lambda: (self.pins.has_pin, self.pins.locked))
            if not has_pin:
                self._open_now(session, link)
                return {}
            if pin is None and self.open.touch(session, idle_sec):
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
        return await asyncio.to_thread(self._catalog.find, self.cfg, frame["project"])

    # --- op ---

    async def _op_projects(self, frame, after):
        return {"items": await asyncio.to_thread(self._catalog.list, self.cfg, refresh=bool(frame.get("refresh")))}

    async def _op_project_summary(self, frame, after):
        project = await self._project(frame)
        pid = frame["project"]
        data = dict(await self._metadata.get((pid, "summary"),
            lambda: asyncio.to_thread(project_summary.read, project, local=True, now=self._wall), ttl=20,
            refresh=bool(frame.get("refresh"))))
        remote = data.pop("_github", None)
        if remote:
            branch, head, repo = remote
            key = (pid, "github", branch, head, repo)

            async def read_remote():
                value = await asyncio.to_thread(github_status.read, project, branch, head, repo=repo)
                return {"value": value, "checked_at": self._wall()}

            if frame.get("remote"):
                cached = await self._metadata.get(key, read_remote, ttl=300,
                    refresh=bool(frame.get("refresh")) or key in self._metadata.pending)
            else:
                cached = None if frame.get("refresh") else self._metadata.peek(key)
                if cached is None:
                    self._metadata.start(key, read_remote, ttl=300)
            data["github"] = cached["value"] if cached else dict(github_status.UNKNOWN)
            data["github_pending"] = cached is None
            data["github_checked_at"] = cached['checked_at'] if cached else None
        return data

    async def _op_file_suggestions(self, frame, after):
        return await self._file_index.read(await self._project(frame), frame["query"])

    async def _op_worktrees(self, frame, after):
        return await asyncio.to_thread(worktrees.list_worktrees, await self._project(frame), self.cfg)

    async def _op_worktree_create(self, frame, after):
        return await asyncio.to_thread(worktrees.create, await self._project(frame), self.cfg, frame["query"])

    async def _op_chat_fork(self, frame, after):
        project = await self._project(frame)
        runner = self._runner(frame)
        if runner._turn(frame["project"], frame["chat"]) is not None:
            raise Refused("running", "Дождитесь завершения задания перед ветвлением")
        engine = frame.get("engine", "claude")
        if engine == "opencode":
            raise Refused("unsupported", "OpenCode ACP не предоставляет ветвление истории")
        if engine == "codex":
            result = await runner.fork_chat(project, frame["chat"], frame.get("message_id"))
        else:
            result = await asyncio.to_thread(branches.claude, project, frame["chat"], frame.get("message_id"))
        saved = await asyncio.to_thread(runner.choices.details, frame["chat"])
        copied = {key: value for key, value in saved.items() if key in ("model", "effort", "access")}
        if engine == "claude" and not copied.get("model"):
            copied["model"] = await asyncio.to_thread(sessions.last_model, project, frame["chat"])
        await asyncio.to_thread(runner.choices.update, result["chat"], **copied)
        return {**result, "engine": engine}

    async def _op_subagents(self, frame, after):
        project = await self._project(frame)
        if frame.get("engine") == "codex":
            return await self.codex.subagents(project, frame["chat"])
        if frame.get("engine") == "opencode":
            raise Refused("unsupported", "OpenCode ACP не предоставляет историю дочерних агентов")
        return await asyncio.to_thread(subagents.list_children, project, frame["chat"])

    async def _op_subagent_history(self, frame, after):
        project = await self._project(frame)
        if frame.get("engine") == "codex":
            return await self.codex.subagent_history(project, frame["chat"], frame["request_id"])
        if frame.get("engine") == "opencode":
            raise Refused("unsupported")
        return await asyncio.to_thread(subagents.history, project, frame["chat"], frame["request_id"])

    async def _op_chat_search(self, frame, after):
        project = await self._project(frame)
        if not frame["query"].strip():
            return {"items": [], "complete": True, "scanned": 0}
        if self._search_lock.locked():
            raise Refused("busy", "Предыдущий поиск ещё выполняется")
        async with self._search_lock:
            try:
                async with asyncio.timeout(discovery.SEARCH_SECONDS + 1):
                    deadline = time.monotonic() + discovery.SEARCH_SECONDS
                    catalog, complete = [], True
                    try:
                        catalog += await asyncio.wait_for(asyncio.to_thread(sessions.chats, project, self.runner.live()), 4)
                    except Exception:
                        complete = False
                    for name in ("codex", "opencode"):
                        backend = getattr(self, name)
                        if backend is not None and backend.catalog is not None:
                            try:
                                catalog += await asyncio.wait_for(backend.chats(project), min(4, max(.1, deadline - time.monotonic())))
                            except Exception:
                                complete = False

                    async def history(row, before):
                        return await self._op_history({**frame, "chat": row["chat"],
                            "engine": row.get("engine", "claude"), "before": before, "limit": 100}, [])

                    result = await discovery.search(frame["query"], catalog, history,
                                                    timeout=max(.1, deadline - time.monotonic()))
                    result["complete"] = result["complete"] and complete
                    return result
            except TimeoutError:
                return {"items": [], "complete": False, "scanned": 0}

    async def _op_activity(self, frame, after):
        items = []
        for engine, runner in (("claude", self.runner), ("codex", self.codex), ("opencode", self.opencode)):
            if runner is not None:
                items.extend({**item, "engine": engine} for item in runner.activity())
        return {"items": items}

    async def _op_chats(self, frame, after):
        project = await self._project(frame)
        async def read(name, backend):
            async def load():
                async with asyncio.timeout(8):
                    return (await asyncio.to_thread(sessions.chats, project, {}) if name == "claude"
                            else await backend.chats(project))
            try:
                rows = await self._metadata.get((frame["project"], "chats", name), load, ttl=20,
                                                refresh=bool(frame.get("refresh")))
                live = backend.live()
                return [{**item, **({"engine": name} if name != "claude" else {}),
                         "running": item["chat"] in live, "asking": bool(live.get(item["chat"]))} for item in rows], True
            except Exception as e:
                log.warning("%s: сессии: %s", name, type(e).__name__)
                return [], False
        backends = [("claude", self.runner), ("codex", self.codex), ("opencode", self.opencode)]
        parts = await asyncio.gather(*(read(name, backend) for name, backend in backends
                                      if backend is not None and (name == 'claude' or backend.catalog is not None)))
        return {"items": [item for rows, _ in parts for item in rows], "complete": all(ok for _, ok in parts)}

    async def _op_history(self, frame, after):
        project = await self._project(frame)
        if frame.get("engine", "claude") != "claude":
            return await self._runner(frame).history(project, frame["chat"], frame.get("before"), frame["limit"])
        return await asyncio.to_thread(sessions.history, project, frame["chat"], frame.get("before"), frame["limit"],
                                       index=self._histories)

    def _runner(self, frame):
        return {"codex": self.codex, "opencode": self.opencode}.get(frame.get("engine"), self.runner)

    async def _op_usage(self, frame, after):
        return await self._usage.read(frame.get("engine", "claude"), self.cfg.projects_root,
                                      self._runner(frame), refresh=frame.get("refresh", False))

    async def _op_state(self, frame, after):
        project = await self._project(frame)
        return await self._runner(frame).state(project, frame["chat"], frame.get("model"), frame.get("effort"),
                                               frame.get("access"))

    async def _op_turn(self, frame, after):
        images = frame.get("images", [])
        items = frame.get("attachments", [])
        await asyncio.to_thread(attachments.validate, items, images)
        if not frame["text"].strip() and not images and not items:
            raise Refused("invalid", "задание пустое")
        project = await self._project(frame)
        runner = self._runner(frame)
        total = sum(len(backend.live()) for backend in (self.runner, self.codex, self.opencode) if backend is not None)
        if frame["chat"] not in runner.live() and total >= self.cfg.max_turns:
            raise Refused("busy")
        return {"turn": runner.start(project, frame["project"], frame["chat"], frame["text"],
                                     frame.get("model"), frame.get("effort"), images=images,
                                     **({"attachments": items} if items else {}),
                                     access=frame.get("access"), owner=frame.get("window"))}

    async def _op_steer(self, frame, after):
        images = frame.get("images", [])
        items = frame.get("attachments", [])
        await asyncio.to_thread(attachments.validate, items, images)
        if not frame["text"].strip() and not images and not items:
            raise Refused("invalid", "уточнение пустое")
        await self._project(frame)
        return await self._runner(frame).steer(frame["project"], frame["chat"], frame["text"], images,
                                               **({"attachments": items} if items else {}),
                                               **({"mode": frame["mode"]} if "mode" in frame else {}),
                                               owner=frame.get("window"))

    async def _op_queue_edit(self, frame, after):
        text = frame.get("text")
        if "text" not in frame or text is not None and (not isinstance(text, str) or len(text) > TURN_TEXT_MAX):
            raise Refused("invalid")
        await self._project(frame)
        return await self._runner(frame).edit_queue(frame["project"], frame["chat"], frame["request_id"],
                                                     frame["version"], text, owner=frame.get("window"))

    async def _op_queue_pause(self, frame, after):
        if type(frame.get("paused")) is not bool:
            raise Refused("invalid")
        await self._project(frame)
        return await self._runner(frame).pause_queue(frame["project"], frame["chat"], frame["paused"],
                                                      owner=frame.get("window"))

    async def _op_claim(self, frame, after):
        if frame.get("window") is None or type(frame.get("fork", False)) is not bool:
            raise Refused("invalid")
        project = await self._project(frame)
        runner = self._runner(frame)
        if frame.get("engine") == "codex":
            return await runner.takeover(project, frame["project"], frame["chat"], frame["window"],
                                         fork=frame.get("fork", False))
        return runner.claim(frame["project"], frame["chat"], frame["window"])

    async def _op_watch(self, frame, after):
        """Хода нет (или он другого проекта) — `{"running": false}` без проверки чата (протокол §4); идёт — снимок
        за ответом."""
        running, frames = await self._runner(frame).watch(frame["project"], frame["chat"])
        after.extend(frames)
        return {"running": running}

    async def _op_answer(self, frame, after):
        self._runner(frame).check_owner(frame["project"], frame["chat"], frame.get("window"))
        self._runner(frame).answer(frame["project"], frame["chat"], frame["request_id"], frame.get("decision"),
                                   frame.get("message"), frame.get("answers"))
        return {}

    async def _op_stop(self, frame, after):
        self._runner(frame).check_owner(frame["project"], frame["chat"], frame.get("window"))
        await self._runner(frame).stop(frame["project"], frame["chat"])
        return {}

    async def _op_files(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.list_dir, project, frame["path"])

    async def _op_file_stats(self, frame, after):
        project = await self._project(frame)
        if self._file_stats_lock.locked():
            raise Refused("busy", "подсчёт уже идёт — дождитесь завершения")
        async with self._file_stats_lock:
            return await asyncio.to_thread(file_stats.scan, project, frame["path"])

    async def _op_read(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.read_text, project, frame["path"])

    async def _op_file_operation(self, frame, after):
        project = await self._project(frame)
        async with self._files_lock:
            return await asyncio.to_thread(files.operate, project, frame["operation"], frame["path"],
                                           frame.get("destination"))

    async def _op_raw(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(files.read_raw, project, frame["path"])

    async def _op_save(self, frame, after):
        project = await self._project(frame)
        async with self._files_lock:
            return await asyncio.to_thread(files.save_text, project, frame["path"], frame["text"], frame["version"])

    async def _op_git(self, frame, after):
        project = await self._project(frame)
        return await asyncio.to_thread(gitinfo.status, project)


def _error(rid: str) -> dict:
    return {"t": "res", "id": rid, "ok": False, "code": "error", "message": "ошибка моста"}
