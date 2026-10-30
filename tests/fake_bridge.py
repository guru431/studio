"""Подставной мост Claude Code (протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md):
привязка к учётке студии, машина по протоколу (§3–§7) и два способа подключить её к студии.

- `FakeMachine` — сам компьютер: проекты, чаты, файлы, git, PIN и открытые сессии; `handle` отвечает на
  просьбу кадром `res` и кадрами `ev` после него. Без службы — её сверяет `test_fake_machine_follows_the_protocol`.
- `InProcessBridge` — канал в процессе службы (быстрые тесты): кадры студии пишутся в `sent`, ответы машины
  приходят через `bridge.deliver` на цикле службы.
- `SocketBridge` — настоящий WebSocket к живой студии (браузерные тесты), в своём потоке.
"""
import asyncio
import base64
import hashlib
import json
import threading

import bridge
import state

PIN_TRIES = 5                    # неверных PIN подряд — дальше pin_locked (§3)
OPEN_MAX = 2 * 1024 * 1024       # текст до 2 МиБ (§6)
RAW_MAX = 10 * 1024 * 1024
T0 = 1_800_000_000               # часы машины: свои, не настоящие


def enroll(s, login="owner", name="Рабочий ПК", password="password-123", ip="10.77.48.5") -> tuple[int, str]:
    """Привязка, как её делает мост: JSON без Origin, адрес — последним в X-Forwarded-For. (agent_id, device_key)."""
    r = s.c.post("/api/agent/enroll", json={"login": login, "password": password, "name": name},
                 headers={"X-Forwarded-For": ip})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["agent_id"], body["device_key"]


def pid(name: str) -> str:
    """id проекта — первые 16 знаков hex sha256 имени папки (§5)."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Refused(Exception):
    def __init__(self, code: str, message: str = "", **extra):
        super().__init__(code)
        self.code, self.message, self.extra = code, message, extra


class FakeMachine:
    """Компьютер с мостом. `projects` — имя → {"files": {путь: bytes}, "chats": {chat: {"title", "created",
    "modified", "tag", "messages": [...]}}, "git": dict | None}; `script` — события следующего хода (без `t`,
    `project`, `chat`), `requests` — все просьбы по порядку."""

    def __init__(self, projects: dict | None = None, pin: str | None = None):
        self.projects = projects if projects is not None else {}
        self.pin = pin
        self.pin_fails = 0
        self.read_only: set[str] = set()         # пути файлов «только для чтения»: `save` — отказ read_only
        self.open_sessions: set[str] = set()
        self.requests: list[dict] = []
        self.script: list[dict] = []
        self.running: dict[str, dict] = {}       # chat → идущий ход
        self.now = T0
        self.turns = 0
        self.codex = None

    @property
    def hello(self) -> dict:
        return {"t": "hello", "v": 1, "cc_version": "2.3.1", "projects_root": "C:\\work\\projects",
                "has_pin": self.pin is not None,
                "models": [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}],
                "efforts": ["low", "high"], **({"codex": self.codex} if self.codex is not None else {})}

    # ---- просьбы ----------------------------------------------------------------

    def handle(self, req: dict) -> tuple[dict, list[dict]]:
        self.requests.append(req)
        events: list[dict] = []
        try:
            op = req.get("op")
            if op == "open":
                data = self._open(req)
            else:
                if req.get("session") not in self.open_sessions:
                    raise Refused("closed", "компьютер закрыт")
                handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
                if handler is None:
                    raise Refused("invalid", "неизвестная просьба")
                data = handler(req, events)
        except Refused as e:
            return {"t": "res", "id": req.get("id"), "ok": False, "code": e.code, "message": e.message,
                    **e.extra}, []
        return {"t": "res", "id": req.get("id"), "ok": True, "data": data}, events

    def _open(self, req):
        if self.pin is None:
            self.open_sessions.add(req["session"])
            return {}
        if req.get("pin") is None and req.get("session") in self.open_sessions:
            return {}                            # уже открыта — продление без PIN (протокол §3)
        if self.pin_fails >= PIN_TRIES:
            raise Refused("pin_locked", "PIN заблокирован")
        if req.get("pin") is None:
            raise Refused("pin_needed", "нужен PIN")
        if req["pin"] == self.pin:
            self.pin_fails = 0
            self.open_sessions.add(req["session"])
            return {}
        self.pin_fails += 1
        if self.pin_fails >= PIN_TRIES:
            raise Refused("pin_locked", "PIN заблокирован")
        raise Refused("pin", "неверный PIN", left=PIN_TRIES - self.pin_fails)

    def _project(self, req) -> tuple[str, dict]:
        for name, project in self.projects.items():
            if pid(name) == req.get("project"):
                return name, project
        raise Refused("not_found", "нет такого проекта")

    def _chat(self, req) -> dict:
        _, project = self._project(req)
        chat = project.setdefault("chats", {}).get(req.get("chat"))
        if chat is None or chat.get("engine", "claude") != req.get("engine", "claude"):
            raise Refused("not_found", "нет такого чата")
        return chat

    def _op_projects(self, req, events):
        return {"items": [{"pid": pid(name), "name": name} for name in sorted(self.projects)]}

    def _op_chats(self, req, events):
        """Сессия с `"listed": False` в список не попадает (как старше 200 новейших на настоящем мосте), а история и
        ход у неё работают."""
        _, project = self._project(req)
        items = [{"chat": chat, "title": c.get("title"), "created": c.get("created"), "modified": c.get("modified"),
                  "tag": c.get("tag"), "engine": c.get("engine", "claude"), "running": chat in self.running,
                  "asking": chat in self.running and bool(self.running[chat]["asks"])}
                 for chat, c in project.get("chats", {}).items() if c.get("listed", True)]
        return {"items": sorted(items, key=lambda i: i["modified"] or 0, reverse=True)}

    def _op_history(self, req, events):
        messages = self._chat(req).get("messages", [])
        before, limit = req.get("before"), req.get("limit")
        if not (type(limit) is int and 1 <= limit <= 100) or not (before is None or type(before) is int):
            raise Refused("invalid", "неверные поля")
        end = len(messages) if before is None else max(0, min(before, len(messages)))
        start = max(0, end - limit)
        return {"items": messages[start:end], "start": start, "total": len(messages)}

    def _op_turn(self, req, events):
        _, project = self._project(req)
        chat_id = req.get("chat")
        if chat_id in self.running:
            raise Refused("running", "в чате уже идёт ход")
        chats = project.setdefault("chats", {})
        self.now += 1
        chat = chats.get(chat_id)
        if chat is None:             # новая сессия с session_id = chat и меткой studio
            chat = chats[chat_id] = {"title": req.get("text", "")[:120], "created": self.now, "modified": self.now,
                                     "tag": "studio", "messages": [], "engine": req.get("engine", "claude")}
        chat["messages"].append({"id": f"m-{len(chat['messages'])}", "role": "user", "text": req.get("text", ""),
                                 "steps": [], "time": self.now})
        chat["modified"] = self.now
        self.turns += 1
        turn = f"t-{self.turns}"
        self.running[chat_id] = {"turn": turn, "project": req["project"], "rest": list(self.script),
                                 "text": "", "steps": [], "asks": {}}
        self.script = []
        self._play(chat_id, events)
        return {"turn": turn}

    def _op_watch(self, req, events):
        chat_id = req.get("chat")
        self._chat(req)
        run = self.running.get(chat_id)
        if run is not None:
            asks = [{k: v for k, v in a.items() if k != "k"} for a in run["asks"].values()]
            events.append(self._ev(chat_id, {"k": "snapshot", "text": run["text"],
                                             "steps": [dict(s) for s in run["steps"]], "asks": asks}))
        return {"running": run is not None}

    def _op_answer(self, req, events):
        chat_id = req.get("chat")
        self._chat(req)
        run = self.running.get(chat_id)
        if run is None or req.get("request_id") not in run["asks"]:
            raise Refused("gone", "запроса уже нет")
        if req.get("decision") not in ("allow", "always", "deny"):
            raise Refused("invalid", "неверные поля")
        del run["asks"][req["request_id"]]
        events.append(self._ev(chat_id, {"k": "asked", "request_id": req["request_id"]}))
        self._play(chat_id, events)
        return {}

    def _op_stop(self, req, events):
        chat_id = req.get("chat")
        self._chat(req)
        run = self.running.get(chat_id)
        if run is None:
            raise Refused("idle", "хода нет")
        for request_id in list(run["asks"]):
            events.append(self._ev(chat_id, {"k": "asked", "request_id": request_id}))
        self._finish(chat_id, events, {"k": "done", "status": "stopped"})
        return {}

    def _files(self, req) -> dict:
        _, project = self._project(req)
        path = req.get("path")
        if not isinstance(path, str):
            raise Refused("invalid", "неверные поля")
        if "\\" in path or any(p in ("", ".", "..") for p in (path.split("/") if path else [])):
            raise Refused("outside", "путь вне проекта")
        return project.setdefault("files", {})

    def _op_files(self, req, events):
        files, path = self._files(req), req["path"]
        prefix = path + "/" if path else ""
        if path and not any(f.startswith(prefix) for f in files):
            raise Refused("not_found", "нет такой папки")
        dirs, plain = {}, []
        for f, data in files.items():
            if not f.startswith(prefix):
                continue
            head, _, rest = f[len(prefix):].partition("/")
            if rest:
                dirs[head] = {"name": head, "dir": True, "size": 0, "modified": self.now}
            else:
                plain.append({"name": head, "dir": False, "size": len(data), "modified": self.now})
        return {"items": sorted(dirs.values(), key=lambda i: i["name"]) + sorted(plain, key=lambda i: i["name"])}

    def _file(self, req) -> bytes:
        data = self._files(req).get(req["path"])
        if data is None:
            raise Refused("not_found", "нет такого файла")
        return data

    @staticmethod
    def _text(data: bytes) -> str:
        if len(data) > OPEN_MAX:
            raise Refused("too_large", "файл больше 2 МиБ")
        try:
            if b"\0" in data:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL")
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("binary", "двоичный файл") from None

    def _op_read(self, req, events):
        data = self._file(req)
        return {"text": self._text(data), "version": _version(data)}

    def _op_raw(self, req, events):
        data = self._file(req)
        if len(data) > RAW_MAX:
            raise Refused("too_large", "файл больше 10 МиБ")
        return {"data": base64.b64encode(data).decode(), "size": len(data), "version": _version(data)}

    def _op_save(self, req, events):
        data = self._file(req)
        self._text(data)
        if req.get("version") != _version(data):
            raise Refused("conflict", "файл изменился", version=_version(data))
        text = req.get("text")
        if not isinstance(text, str):
            raise Refused("invalid", "неверные поля")
        new = text.encode()
        if len(new) > OPEN_MAX:
            raise Refused("too_large", "файл больше 2 МиБ")
        if req["path"] in self.read_only:
            raise Refused("read_only", "файл только для чтения")
        self._files(req)[req["path"]] = new
        return {"version": _version(new)}

    def _op_git(self, req, events):
        _, project = self._project(req)
        if project.get("git") is None:
            raise Refused("not_git", "не репозиторий git")
        return dict(project["git"])

    # ---- ход ---------------------------------------------------------------------

    def _ev(self, chat_id: str, e: dict) -> dict:
        run = self.running.get(chat_id) or {}
        return {"t": "ev", "project": run.get("project"), "chat": chat_id, "turn": run.get("turn"), "e": e}

    def _play(self, chat_id: str, events: list) -> None:
        """События хода до первого `ask` включительно или до `done`."""
        run = self.running[chat_id]
        while run["rest"]:
            e = run["rest"].pop(0)
            if e["k"] == "done":
                self._finish(chat_id, events, e)
                return
            events.append(self._ev(chat_id, e))
            if e["k"] == "text":
                run["text"] += e["text"]
            elif e["k"] == "step":
                run["steps"].append({"id": e["id"], "tool": e["tool"], "input": e["input"], "text": "",
                                     "error": False})
            elif e["k"] == "result":
                for step in run["steps"]:
                    if step["id"] == e["id"]:
                        step["text"], step["error"] = e["text"], e["error"]
            elif e["k"] == "ask":
                run["asks"][e["request_id"]] = e
                return

    def _finish(self, chat_id: str, events: list, done: dict) -> None:
        run = self.running[chat_id]
        events.append(self._ev(chat_id, done))
        del self.running[chat_id]
        for project in self.projects.values():
            chat = project.get("chats", {}).get(chat_id)
            if chat is not None:
                self.now += 1
                chat["messages"].append({"id": f"m-{len(chat['messages'])}", "role": "assistant",
                                         "text": run["text"], "steps": run["steps"], "time": self.now})
                chat["modified"] = self.now


# ---- канал в процессе службы ---------------------------------------------------------

class InProcessBridge(bridge.Link):
    """Канал без сокета: кадры студии — в `sent`, ответ и события машины — через `bridge.deliver` следующими
    шагами цикла службы, по порядку. После `close` отправка падает, как у закрытого сокета."""

    def __init__(self, agent_id: int, user_id: int, name: str, machine: FakeMachine, ip: str = "10.77.48.5"):
        super().__init__(agent_id, user_id, name, ip)
        self.hello = bridge.parse_hello(machine.hello)
        self.machine = machine
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def send(self, frame: dict) -> None:
        if self.closed_with is not None:
            raise ConnectionError("канал закрыт")
        self.sent.append(frame)
        if frame["t"] == "req":
            res, evs = self.machine.handle(frame)
            loop = asyncio.get_running_loop()
            for f in (res, *evs):
                loop.call_soon(bridge.deliver, self, f)
        elif frame["t"] == "close":
            self.machine.open_sessions.discard(frame["session"])

    async def close(self, code: int) -> None:
        self.closed_with = code


def connect(s, agent_id: int, machine: FakeMachine) -> InProcessBridge:
    """Мост на связи: новый канал — открытые сессии машины пропадают (§3)."""
    row = state.DB.execute("SELECT user_id, name FROM agents WHERE id = ?", (agent_id,)).fetchone()
    link = InProcessBridge(agent_id, row["user_id"], row["name"], machine)
    machine.open_sessions.clear()
    s.c.portal.call(bridge.attach, link)
    return link


def emit(s, link: InProcessBridge, project: str, chat: str, *events: dict) -> None:
    """Мост сам шлёт события хода (на цикле службы — очереди наблюдателей asyncio)."""
    def deliver():
        for e in events:
            bridge.deliver(link, {"t": "ev", "project": project, "chat": chat, "turn": "t-0", "e": e})
    s.c.portal.call(deliver)


def drop(s, link: InProcessBridge) -> None:
    """Канал оборвался со стороны моста."""
    link.closed_with = 1006
    s.c.portal.call(bridge.detach, link)


def settle(s) -> None:
    """Дождаться фоновых отправок службы (`bridge.TASKS`: уведомления `close`)."""
    async def wait():
        while bridge.TASKS:
            await asyncio.gather(*list(bridge.TASKS), return_exceptions=True)
    s.c.portal.call(wait)


# ---- настоящий WebSocket -----------------------------------------------------------

class SocketBridge:
    """Мост через сеть: `start()` подключается ключом устройства, шлёт `hello`, ждёт `welcome` и дальше в
    своём потоке отвечает машиной; `close_code` — код закрытия сервером (после конца потока)."""

    def __init__(self, url: str, key: str, machine: FakeMachine):
        self.ws_url = "ws" + url[len("http"):] + "/api/agent/link"
        self.key, self.machine = key, machine
        self.welcome: dict | None = None
        self.close_code: int | None = None
        self._ws = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> "SocketBridge":
        from websockets.sync.client import connect as ws_connect
        self._ws = ws_connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self.key}"},
                              open_timeout=5, legacy=True)
        self.machine.open_sessions.clear()
        self._send(self.machine.hello)
        self.welcome = json.loads(self._ws.recv(timeout=5))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _send(self, frame: dict) -> None:
        with self._lock:
            self._ws.send(json.dumps(frame, ensure_ascii=False))

    def _run(self) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            for message in self._ws:
                frame = json.loads(message)
                if frame.get("t") == "req":
                    res, evs = self.machine.handle(frame)
                    for f in (res, *evs):
                        self._send(f)
                elif frame.get("t") == "close":
                    self.machine.open_sessions.discard(frame.get("session"))
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self._ws.close_code

    def send_events(self, project: str, chat: str, *events: dict) -> None:
        for e in events:
            self._send({"t": "ev", "project": project, "chat": chat, "turn": "t-0", "e": e})

    def stop(self) -> None:
        if self._ws is not None:
            self._ws.close()
        if self._thread is not None:
            self._thread.join(5)
