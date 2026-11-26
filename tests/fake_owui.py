"""Поддельный Open WebUI 0.13 для быстрых тестов: REST из таблицы протокола спеки
(`docs/superpowers/specs/2026-11-22-studio-open-webui-design.md`), без сети — `FakeGateway.hosts` отдаёт ему запросы
на `ENTRY`. Сроки токенов — по `clock.NOW`, как у настоящего. Сокет — в задаче 5.

Здесь: `/api/config`, вход, постоянные сессии (создание, обновление с ротацией и восстановлением по `request_id`,
отзыв), `GET /api/v1/auths/` (кто вошёл — проверка рабочего токена). Ответ входа — как у Open WebUI 0.13.4
(`utils/auth.py::create_signin_response`): при `ENABLE_MFA` учётка не `admin`/`user` — 200 `{next_step: "pending"}`;
нужен второй код — 200 `{next_step: "verify", challenge_token, expires_in}` без токена; без MFA роль `pending` —
обычный ответ входа с `role: "pending"`; неверный пароль — 400 `{detail}`.

Чтение (формы — по Open WebUI v0.13.4, `routers/chats.py`, `folders.py`, `main.py`): `GET /api/v1/chats/` (страницы по 60,
без закреплённых и чатов папок), `/pinned`, `/folder/{id}/list` (страницы по 10), `/search?text=`, `/{id}` (чат
`ChatResponse`; чужой и неизвестный — 401, как у настоящего), `GET /api/v1/folders/` (`folders_enabled = False` — 403),
`GET /api/models` (`{data}`), `GET /api/tasks/chat/{id}` (`{task_ids}`), `GET /api/v1/files/{id}/content`. Данные —
`add_chat`, `add_folder`, `add_model`, `add_file`; `raw_chat[id]` подменяет тело ответа `GET /api/v1/chats/{id}`.

Отправка (формы — по Open WebUI v0.13.4, `main.py`, `utils/tool_approval.py`, `routers/files.py`):
`POST /api/chat/completions` (тело — в `completions`; новый чат создаётся сам; сообщение и пустой ответ — в историю,
`finish` — ответ готов; `queue_next` — ответ `{queued: true}`),
`POST /api/tasks/chat/{id}/stop` (`stopped`), `POST /api/v1/chats/{id}/messages/{mid}/resolve` (`resolved`; повтор
того же вызова — 409), `POST /api/v1/files/` (multipart, поле `file`), `GET /api/v1/files/{id}/process/status`
(статусы по очереди из `processing`), `GET /api/v1/files/{id}`, `GET /api/v1/users/user/settings` (`{ui: user_settings}`).
`GET /api/config` с действующим токеном добавляет `features.enable_*` из `config_features` и
`code.interpreter_engine`.

Сокет (Engine.IO 4 через websocket, socket.io 5) — в памяти: `owui_link.CONNECT` → `socket_connect`. Как Open WebUI
0.13.4 (`socket/main.py`, `always_connect=True`): пакет подключения принимается с любым токеном, сокет с
действующим токеном — «вошедший» (`FakeSocket.user`); `user-join` меняет токен сокета. Тест управляет сервером:
`emit_event` (событие `events` вошедшим сокетам), `call` (событие с ожиданием ответа), `ping`, `drop` (обрыв со стороны
сервера); `received` — кадры от клиента по порядку, `ws_targets` — куда соединялся клиент.
"""
import asyncio
import itertools
import json
import re
import uuid

import httpx

import clock
import owui_tokens
import state

#: срок токена входа и токена сессии, с (как у Open WebUI: токен сессии — 900 с)
LOGIN_SEC = 3600
TOKEN_SEC = 900


def attach(studio) -> "FakeOwui":
    """Поддельный Open WebUI на `FakeOwui.ENTRY` у шлюза теста и в списке разрешённых адресов."""
    import upstream
    fake = FakeOwui()
    studio.gw.hosts[FakeOwui.ENTRY] = fake.handler
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                     (json.dumps([*upstream.allowlist(), FakeOwui.ENTRY]),))
    return fake


class FakeSocket:
    """Соединение сокета со стороны сервера: `owui_link.Transport` для клиента."""

    def __init__(self, fake: "FakeOwui"):
        self.fake = fake
        self.received: list[str] = []      # кадры клиента по порядку
        self.closed = False
        self.sid: str | None = None        # sid socket.io после пакета подключения
        self.token: str | None = None
        self._inbox: asyncio.Queue = asyncio.Queue()

    @property
    def user(self) -> str | None:
        """Вошедший пользователь: подключение или `user-join` с действующим токеном."""
        return self.fake._user_of(self.token) if self.sid else None

    def push(self, frame: str) -> None:
        self._inbox.put_nowait(frame)

    def close_from_server(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)

    async def recv(self) -> str:
        frame = await self._inbox.get()
        if frame is None:
            self._inbox.put_nowait(None)
            raise ConnectionResetError("соединение закрыто")
        return frame

    async def send(self, text: str) -> None:
        await asyncio.sleep(0)
        if self.closed:
            raise ConnectionResetError("соединение закрыто")
        self.received.append(text)
        if text.startswith("40"):
            auth = json.loads(text[2:] or "{}")
            self.token = auth.get("token") if isinstance(auth, dict) else None
            self.sid = f"sid-{next(self.fake._ids)}"
            self.push("40" + json.dumps({"sid": self.sid}))
        elif text.startswith("42"):
            args = json.loads(text[2:])
            if args[0] == "user-join" and isinstance(args[1], dict) and isinstance(args[1].get("auth"), dict):
                self.token = args[1]["auth"].get("token")

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)


class FakeOwui:
    URL = "http://10.77.1.20:8080"
    ENTRY = "10.77.1.20:8080"
    EMAIL = "owner@example.com"
    PASSWORD = "owui-test-password"

    def __init__(self):
        self.version = "0.13.4"
        self.down = False                  # сетевой отказ на любой запрос
        self.user_id = "owui-user-1"
        self.name = "Owner"
        self.role = "user"
        self.auth_sessions = True          # `features.auth_sessions` в `/api/config`
        self.enable_mfa = False            # `ENABLE_MFA` сервера: учётка `pending` — `{next_step: "pending"}`
        self.mfa_required = False          # вход паролем требует второй код: `{next_step: "verify", …}`
        self.requests: list[httpx.Request] = []
        self.refreshes = 0                 # ротации: новая пара выдана (повтор по `request_id` не считается)
        self.refresh_log: list[tuple[str, str]] = []     # (refresh_token, request_id) каждого запроса обновления
        self.revoked: list[str] = []       # id сессий, отозванных запросом `DELETE /api/v1/auths/sessions/{id}`
        self.chats: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
        self.folders_enabled = True        # `folders.enable` сервера: выключено — 403 на список папок
        self.models: list[dict] = []
        self.files: dict[str, tuple[str, bytes]] = {}      # id файла → (Content-Type, содержимое)
        self.raw_chat: dict[str, object] = {}              # id чата → тело ответа вместо настоящего (проверка формы)
        self._ids = itertools.count(1)
        self._tokens: dict[str, tuple[str | None, float]] = {}      # токен → (id сессии или None для входа, конец срока)
        self._sessions: dict[str, dict] = {}                         # id сессии → {"refresh", "revoked"}
        self._issued: dict[str, tuple[str, dict]] = {}               # request_id → (прежний refresh_token, выдача)
        self.sockets: list[FakeSocket] = []
        self.ws_targets: list = []         # `upstream.WsTarget` каждого соединения сокета
        self.ws_headers: list[dict] = []
        self.ping_ms = (25000, 20000)      # `pingInterval`, `pingTimeout` пакета открытия, мс
        # отправка
        self.completions: list[dict] = []  # тела `POST /api/chat/completions` по порядку
        self.queue_next = False            # в чате идёт ответ: сообщение ставится в очередь
        self.stopped: list[str] = []
        self.resolved: list[tuple[str, str, dict]] = []    # (чат, ответ, тело) каждого принятого решения
        self.user_settings: dict = {}      # `ui` настроек пользователя Open WebUI
        self.config_features: dict = {}    # `features.enable_*` для вошедшего
        self.interpreter_engine = "pyodide"
        self.processing = ["completed"]    # статусы обработки нового файла по очереди опроса
        self.file_names: dict[str, str] = {}
        self.file_status: dict[str, list[str]] = {}
        self.uploads = 0

    # ---- сокет ----

    async def socket_connect(self, target, headers: dict) -> "FakeSocket":
        """`owui_link.CONNECT`: новое соединение сокета; сервер сразу шлёт пакет открытия Engine.IO."""
        await asyncio.sleep(0)
        if self.down:
            raise ConnectionRefusedError("нет соединения")
        self.ws_targets.append(target)
        self.ws_headers.append(dict(headers))
        sock = FakeSocket(self)
        self.sockets.append(sock)
        sock.push("0" + json.dumps({"sid": f"eio-{next(self._ids)}", "upgrades": [], "pingInterval": self.ping_ms[0],
                                    "pingTimeout": self.ping_ms[1], "maxPayload": 1000000}))
        return sock

    @property
    def socket(self) -> "FakeSocket | None":
        """Последнее открытое соединение."""
        live = [s for s in self.sockets if not s.closed]
        return live[-1] if live else None

    @property
    def received(self) -> list[str]:
        return [frame for sock in self.sockets for frame in sock.received]

    def _user_of(self, token) -> str | None:
        entry = self._tokens.get(token) if isinstance(token, str) else None
        if entry is None or entry[1] <= clock.NOW() or (entry[0] and self._sessions[entry[0]]["revoked"]):
            return None
        return self.user_id

    def emit_event(self, chat_id: str | None, message_id: str | None, data: dict) -> None:
        """Событие `events` `{chat_id, message_id, data}` в комнату пользователя: всем вошедшим сокетам."""
        frame = "42" + json.dumps(["events", {"chat_id": chat_id, "message_id": message_id, "data": data}])
        for sock in self.sockets:
            if not sock.closed and sock.user:
                sock.push(frame)

    def call(self, name: str, data: dict, ack: int = 1) -> None:
        """Событие с ожиданием ответа (socket.io `call`) последнему соединению: `42<ack>[name, data]`."""
        self.socket.push(f"42{ack}" + json.dumps([name, data]))

    def ping(self) -> None:
        self.socket.push("2")

    def drop(self) -> None:
        """Сервер рвёт последнее соединение."""
        self.socket.close_from_server()

    # ---- для тестов ----

    def issue_session(self) -> "owui_tokens.Secret":
        """Сессия без входа: долгий токен для строки учётки в базе теста."""
        session_id = f"session-{next(self._ids)}"
        issue = self._issue(session_id)
        return owui_tokens.Secret(session_id, issue["refresh_token"], None)

    def revoke_all(self) -> None:
        """Сервер отозвал все сессии: рабочие и долгие токены больше не принимаются."""
        for session in self._sessions.values():
            session["revoked"] = True

    def expire_tokens(self) -> None:
        """Рабочие токены и токены входа больше не принимаются (долгие токены сессий живы)."""
        self._tokens.clear()

    @property
    def session_names(self) -> list[str]:
        """Имена созданных входом сессий, по порядку (сессии `issue_session` — без имени)."""
        return [s["name"] for s in self._sessions.values() if s.get("name") is not None]

    def add_chat(self, title: str = "Chat", *, folder_id: str | None = None, pinned: bool = False,
                 messages: list[dict] | None = None, current_id: str | None = None, models: list[str] | None = None,
                 chat_id: str | None = None, updated_at: int | None = None, active: bool = False,
                 pending_approval: bool = False, files: list[dict] | None = None) -> str:
        """Чат; `messages` — сообщения истории (`id`, `parentId`, …), текущее — `current_id` или последнее; `files` —
        `chat.files` (документы, по которым Open WebUI ищет в ходе)."""
        chat_id = chat_id or f"chat-{next(self._ids)}"
        stamp = int(clock.NOW()) if updated_at is None else updated_at
        self.chats[chat_id] = {"id": chat_id, "title": title, "folder_id": folder_id, "pinned": pinned,
                               "messages": {m["id"]: m for m in messages or []},
                               "current_id": current_id or (messages[-1]["id"] if messages else None),
                               "models": models or [], "created_at": stamp, "updated_at": stamp, "active": active,
                               "pending_approval": pending_approval, "files": list(files or [])}
        return chat_id

    def add_folder(self, name: str = "Folder", *, folder_id: str | None = None, parent_id: str | None = None) -> str:
        folder_id = folder_id or f"folder-{next(self._ids)}"
        self.folders[folder_id] = {"id": folder_id, "name": name, "parent_id": parent_id}
        return folder_id

    def add_model(self, model_id: str, name: str | None = None, meta: dict | None = None, **capabilities) -> None:
        """Модель `/api/models`; `meta` — прочие поля `info.meta` (`toolIds`, `defaultFeatureIds` …)."""
        model = {"id": model_id, "name": name or model_id, "object": "model"}
        if capabilities or meta:
            model["info"] = {"meta": {**(meta or {}), **({"capabilities": capabilities} if capabilities else {})}}
        self.models.append(model)

    def add_file(self, file_id: str, content: bytes, content_type: str) -> None:
        self.files[file_id] = (content_type, content)

    # ---- токены ----

    def _issue(self, session_id: str) -> dict:
        """Новая пара сессии; прежний рабочий токен действует до конца срока, прежний refresh_token — нет."""
        session = self._sessions.setdefault(session_id, {"refresh": None, "revoked": False})
        token, refresh = f"jwt-{uuid.uuid4().hex}", f"refresh-{uuid.uuid4().hex}"
        session["refresh"] = refresh
        self._tokens[token] = (session_id, clock.NOW() + TOKEN_SEC)
        return {"session_id": session_id, "token": token, "refresh_token": refresh, "token_type": "Bearer",
                "expires_at": int(clock.NOW() + TOKEN_SEC)}

    def _caller_valid(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        entry = self._tokens.get(header.removeprefix("Bearer ")) if header.startswith("Bearer ") else None
        if entry is None or entry[1] <= clock.NOW():
            return False
        return entry[0] is None or not self._sessions[entry[0]]["revoked"]

    # ---- REST ----

    async def handler(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)             # как сеть: параллельные вызовы успевают дойти до блокировки
        await request.aread()
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("нет соединения", request=request)
        method, path = request.method, request.url.path
        if path == "/api/config" and method == "GET":
            features = {"auth_sessions": True} if self.auth_sessions else {}
            body = {"name": "Open WebUI", "version": self.version, "features": features}
            if self._caller_valid(request):
                features.update(self.config_features)
                body["code"] = {"engine": "pyodide", "interpreter_engine": self.interpreter_engine}
            return httpx.Response(200, json=body)
        if method == "POST" and (path in ("/api/chat/completions", "/api/v1/files/")
                                 or path.startswith(("/api/tasks/chat/", "/api/v1/chats/"))):
            if not self._caller_valid(request):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            return self._send(request, path)
        if path == "/api/v1/users/user/settings" and method == "GET":
            if not self._caller_valid(request):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            return httpx.Response(200, json={"ui": self.user_settings} if self.user_settings else None)
        if path == "/api/v1/auths/" and method == "GET":
            if not self._caller_valid(request):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            return httpx.Response(200, json={"id": self.user_id, "email": self.EMAIL, "name": self.name,
                                             "role": self.role})
        if path == "/api/v1/auths/signin" and method == "POST":
            return self._signin(request)
        if path == "/api/v1/auths/sessions/refresh" and method == "POST":
            return self._refresh(request)
        if path == "/api/v1/auths/sessions" and method == "POST":
            return self._create_session(request)
        if path.startswith("/api/v1/auths/sessions/") and method == "DELETE":
            return self._revoke(request, path.rsplit("/", 1)[1])
        if path.startswith("/api/v1/chats/") and method == "DELETE":
            if not self._caller_valid(request):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            chat_id = path.removeprefix("/api/v1/chats/")
            if self.chats.pop(chat_id, None) is None:
                return httpx.Response(404, json={"detail": "Not found"})
            return httpx.Response(200, json=True)
        if path.startswith("/api/v1/files/") and method == "DELETE":
            if not self._caller_valid(request):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            file_id = path.removeprefix("/api/v1/files/")
            if self.files.pop(file_id, None) is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            self.file_names.pop(file_id, None)
            self.file_status.pop(file_id, None)
            return httpx.Response(200, json={"message": "File deleted successfully"})
        if method == "GET" and (path.startswith(("/api/v1/chats/", "/api/v1/folders/", "/api/v1/files/", "/api/tasks/chat/"))
                                or path == "/api/models"):
            if not self._caller_valid(request):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            return self._read(request, path)
        return httpx.Response(404, json={"detail": "Not Found"})

    # ---- чтение ----

    def _title_item(self, chat: dict) -> dict:
        """`ChatTitleIdResponse`."""
        return {"id": chat["id"], "title": chat["title"], "updated_at": chat["updated_at"],
                "created_at": chat["created_at"], "last_read_at": None, "snippet": None, "active": chat["active"],
                "pending_approval": chat["pending_approval"], "archived": False}

    def _newest(self, chats, page: int, size: int) -> list[dict]:
        ordered = sorted(chats, key=lambda c: -c["updated_at"])
        return [self._title_item(c) for c in ordered[(page - 1) * size:page * size]]

    def _chat_response(self, chat: dict) -> dict:
        """`ChatResponse` с телом чата, как его хранит Open WebUI: `chat.history.messages` — словарь по id."""
        return {"id": chat["id"], "user_id": self.user_id, "title": chat["title"],
                "chat": {"title": chat["title"], "models": chat["models"], "files": chat["files"],
                         "history": {"messages": chat["messages"], "currentId": chat["current_id"]}},
                "updated_at": chat["updated_at"], "created_at": chat["created_at"], "share_id": None,
                "archived": False, "pinned": chat["pinned"], "meta": {}, "variables": {},
                "folder_id": chat["folder_id"], "current_message_id": chat["current_id"], "write_access": True}

    def _read(self, request: httpx.Request, path: str) -> httpx.Response:
        query = request.url.params
        page = int(query.get("page") or 1)
        mine = list(self.chats.values())
        if path == "/api/v1/chats/":
            return httpx.Response(200, json=self._newest([c for c in mine if not c["pinned"] and not c["folder_id"]],
                                                         page, 60))
        if path == "/api/v1/chats/pinned":
            return httpx.Response(200, json=self._newest([c for c in mine if c["pinned"]], 1, 1000))
        if path == "/api/v1/chats/search":
            text = (query.get("text") or "").lower()
            return httpx.Response(200, json=self._newest([c for c in mine if text in c["title"].lower()], page, 60))
        if path.startswith("/api/v1/chats/folder/") and path.endswith("/list"):
            folder = path.removeprefix("/api/v1/chats/folder/").removesuffix("/list")
            return httpx.Response(200, json=self._newest([c for c in mine if c["folder_id"] == folder], page, 10))
        if path == "/api/v1/folders/":
            if not self.folders_enabled:
                return httpx.Response(403, json={"detail": "You do not have permission to access this resource."})
            return httpx.Response(200, json=[{"id": f["id"], "user_id": self.user_id, "name": f["name"],
                                              "parent_id": f.get("parent_id"), "is_expanded": False,
                                              "unread_count": 0, "created_at": 1} for f in self.folders.values()])
        if path == "/api/models":
            return httpx.Response(200, json={"data": self.models})
        if path.startswith("/api/tasks/chat/"):
            chat = self.chats.get(path.removeprefix("/api/tasks/chat/"))
            return httpx.Response(200, json={"task_ids": ["task-1"] if chat and chat["active"] else []})
        if path.startswith("/api/v1/files/") and path.endswith("/content"):
            found = self.files.get(path.removeprefix("/api/v1/files/").removesuffix("/content"))
            if found is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, content=found[1], headers={"content-type": found[0]})
        if path.startswith("/api/v1/files/") and path.endswith("/process/status"):
            statuses = self.file_status.get(path.removeprefix("/api/v1/files/").removesuffix("/process/status"))
            if statuses is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            current = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            return httpx.Response(200, json={"status": current})
        if path.startswith("/api/v1/files/"):
            file_id = path.removeprefix("/api/v1/files/")
            if file_id not in self.files:
                return httpx.Response(404, json={"detail": "Not Found"})
            mime, data = self.files[file_id]
            name = self.file_names.get(file_id, file_id)
            return httpx.Response(200, json={"id": file_id, "user_id": self.user_id, "filename": name,
                                             "meta": {"name": name, "content_type": mime, "size": len(data)},
                                             "data": {"status": self.file_status.get(file_id, ["completed"])[-1]}})
        chat_id = path.removeprefix("/api/v1/chats/")
        if chat_id.endswith("/pinned"):
            chat = self.chats.get(chat_id.removesuffix("/pinned"))
            if chat is not None:
                return httpx.Response(200, json=chat["pinned"])
        if chat_id in self.raw_chat:
            return httpx.Response(200, json=self.raw_chat[chat_id])
        if chat_id in self.chats:
            return httpx.Response(200, json=self._chat_response(self.chats[chat_id]))
        return httpx.Response(401, json={"detail": "We could not find what you're looking for :/"})

    # ---- отправка ----

    def _send(self, request: httpx.Request, path: str) -> httpx.Response:
        chat_id = path.removeprefix("/api/v1/chats/")
        if chat_id.endswith("/pin") and (chat := self.chats.get(chat_id.removesuffix("/pin"))) is not None:
            chat["pinned"] = not chat["pinned"]
            return httpx.Response(200, json=self._chat_response(chat))
        if chat_id in self.chats:
            update = json.loads(request.content)["chat"]
            self.chats[chat_id]["title"] = update["title"]
            return httpx.Response(200, json=self._chat_response(self.chats[chat_id]))
        if path == "/api/chat/completions":
            body = self._body(request)
            self.completions.append(body)
            if self.queue_next:
                return httpx.Response(200, json={"status": True, "queued": True, "chat_id": body.get("chat_id")})
            chat_id = body.get("chat_id")
            if chat_id is None:
                chat_id = self.add_chat("New Chat", models=[body.get("model")])
            elif chat_id not in self.chats:
                return httpx.Response(404, json={"detail": "We could not find what you're looking for :/"})
            if body.get("files") is not None:          # как `main.py` 0.13.4: присланное перезаписывает `chat.files`
                self.chats[chat_id]["files"] = body["files"]
            self._insert_turn(self.chats[chat_id], body)
            return httpx.Response(200, json={"status": True, "task_ids": [f"task-{next(self._ids)}"], "chat_id": chat_id})
        if path == "/api/v1/files/":
            return self._upload(request)
        stop = re.fullmatch(r"/api/tasks/chat/([^/]+)/stop", path)
        if stop:
            if stop[1] not in self.chats:
                return httpx.Response(404, json={"detail": "We could not find what you're looking for :/"})
            self.stopped.append(stop[1])
            self.chats[stop[1]]["active"] = False
            return httpx.Response(200, json={"status": True, "message": "Stopped"})
        resolve = re.fullmatch(r"/api/v1/chats/([^/]+)/messages/([^/]+)/resolve", path)
        if resolve:
            chat_id, message_id = resolve[1], resolve[2]
            body = self._body(request)
            if chat_id not in self.chats:
                return httpx.Response(404, json={"detail": "We could not find what you're looking for :/"})
            if any((c, m, b.get("call_id")) == (chat_id, message_id, body.get("call_id")) for c, m, b in self.resolved):
                return httpx.Response(409, json={"detail": "Tool call has already been resolved."})
            self.resolved.append((chat_id, message_id, body))
            return httpx.Response(200, json={"status": True, "chat_id": chat_id, "message_id": message_id,
                                             "task_ids": [f"task-{next(self._ids)}"]})
        return httpx.Response(404, json={"detail": "Not Found"})

    @staticmethod
    def _insert_turn(chat: dict, body: dict) -> None:
        """Как Open WebUI 0.13 при серверном ходе: сообщение пользователя и пустой ответ (`done: false`) — в историю
        чата, ответ — текущий; в чате идёт ответ."""
        user = body.get("user_message")
        if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not isinstance(body.get("id"), str):
            return
        chat["messages"][user["id"]] = {**user, "parentId": body.get("parent_id")}
        chat["messages"][body["id"]] = {"id": body["id"], "parentId": user["id"], "childrenIds": [], "role": "assistant",
                                        "content": "", "model": body.get("model"), "done": False,
                                        "timestamp": int(clock.NOW())}
        chat["current_id"] = body["id"]
        chat["active"] = True

    def finish(self, chat_id: str, message_id: str, text: str = "", **fields) -> None:
        """Ответ закончен: текст, `done: true` и прочие поля сохранённого сообщения (`output`, `error` …)."""
        chat = self.chats[chat_id]
        chat["messages"][message_id].update({"content": text, "done": True, **fields})
        chat["active"] = False

    def _upload(self, request: httpx.Request) -> httpx.Response:
        """multipart с полем `file` — как `routers/files.py::upload_file`: ответ сразу, обработка — потом."""
        boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
        for part in request.content.split(b"--" + boundary):
            head, _, data = part.partition(b"\r\n\r\n")
            if b'name="file"' not in head:
                continue
            name = re.search(rb'filename="([^"]*)"', head)[1].decode()
            mime = re.search(rb"Content-Type: ([^\r\n]+)", head)
            file_id = str(uuid.uuid4())
            self.files[file_id] = (mime[1].decode() if mime else "application/octet-stream", data.removesuffix(b"\r\n"))
            self.file_names[file_id] = name
            # `?process=false` (картинки страницы Open WebUI): обработки нет, статус так и остаётся `pending`
            unprocessed = request.url.params.get("process") == "false"
            self.file_status[file_id] = ["pending"] if unprocessed else list(self.processing)
            self.uploads += 1
            return httpx.Response(200, json={"status": True, "id": file_id, "user_id": self.user_id, "filename": name,
                                             "meta": {"name": name, "content_type": self.files[file_id][0],
                                                      "size": len(self.files[file_id][1])},
                                             "data": {"status": "pending"}})
        return httpx.Response(422, json={"detail": "file required"})

    @staticmethod
    def _body(request: httpx.Request) -> dict:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _signin(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        if body.get("email") != self.EMAIL or body.get("password") != self.PASSWORD:
            return httpx.Response(400, json={"detail": "Your email or password is incorrect"})
        if self.enable_mfa and self.role not in ("admin", "user"):
            return httpx.Response(200, json={"next_step": "pending"})
        if self.mfa_required:
            return httpx.Response(200, json={"next_step": "verify", "challenge_token": f"challenge-{uuid.uuid4().hex}",
                                             "expires_in": 300})
        token = f"jwt-{uuid.uuid4().hex}"
        self._tokens[token] = (None, clock.NOW() + LOGIN_SEC)
        return httpx.Response(200, json={"token": token, "token_type": "Bearer", "id": self.user_id,
                                         "email": self.EMAIL, "name": self.name, "role": self.role,
                                         "expires_at": int(clock.NOW() + LOGIN_SEC)})

    def _create_session(self, request: httpx.Request) -> httpx.Response:
        if not self._caller_valid(request):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        issue = self._issue(f"session-{next(self._ids)}")
        self._sessions[issue["session_id"]]["name"] = self._body(request).get("name")
        return httpx.Response(200, json=issue)

    def _refresh(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        refresh, request_id = body.get("refresh_token"), body.get("request_id")
        self.refresh_log.append((refresh, request_id))
        live = {s["refresh"] for s in self._sessions.values() if not s["revoked"]}
        issued = self._issued.get(request_id)
        if issued is not None and issued[0] == refresh and issued[1]["refresh_token"] in live:
            return httpx.Response(200, json=issued[1])             # обрыв после ответа: та же выдача
        for session_id, session in self._sessions.items():
            if session["refresh"] == refresh and not session["revoked"]:
                issue = self._issue(session_id)
                self._issued[request_id] = (refresh, issue)
                self.refreshes += 1
                return httpx.Response(200, json=issue)
        return httpx.Response(401, json={"detail": "Invalid refresh token"})

    def _revoke(self, request: httpx.Request, session_id: str) -> httpx.Response:
        if not self._caller_valid(request):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if session_id not in self._sessions:
            return httpx.Response(404, json={"detail": "Not Found"})
        self._sessions[session_id]["revoked"] = True
        self.revoked.append(session_id)
        return httpx.Response(200, json=True)
