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
"""
import asyncio
import itertools
import json
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
                 pending_approval: bool = False) -> str:
        """Чат; `messages` — сообщения истории (`id`, `parentId`, …), текущее — `current_id` или последнее."""
        chat_id = chat_id or f"chat-{next(self._ids)}"
        stamp = int(clock.NOW()) if updated_at is None else updated_at
        self.chats[chat_id] = {"id": chat_id, "title": title, "folder_id": folder_id, "pinned": pinned,
                               "messages": {m["id"]: m for m in messages or []},
                               "current_id": current_id or (messages[-1]["id"] if messages else None),
                               "models": models or [], "created_at": stamp, "updated_at": stamp, "active": active,
                               "pending_approval": pending_approval}
        return chat_id

    def add_folder(self, name: str = "Folder", *, folder_id: str | None = None, parent_id: str | None = None) -> str:
        folder_id = folder_id or f"folder-{next(self._ids)}"
        self.folders[folder_id] = {"id": folder_id, "name": name, "parent_id": parent_id}
        return folder_id

    def add_model(self, model_id: str, name: str | None = None, **capabilities) -> None:
        model = {"id": model_id, "name": name or model_id, "object": "model"}
        if capabilities:
            model["info"] = {"meta": {"capabilities": capabilities}}
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
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("нет соединения", request=request)
        method, path = request.method, request.url.path
        if path == "/api/config" and method == "GET":
            features = {"auth_sessions": True} if self.auth_sessions else {}
            return httpx.Response(200, json={"name": "Open WebUI", "version": self.version, "features": features})
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
                "chat": {"title": chat["title"], "models": chat["models"],
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
        chat_id = path.removeprefix("/api/v1/chats/")
        if chat_id in self.raw_chat:
            return httpx.Response(200, json=self.raw_chat[chat_id])
        if chat_id in self.chats:
            return httpx.Response(200, json=self._chat_response(self.chats[chat_id]))
        return httpx.Response(401, json={"detail": "We could not find what you're looking for :/"})

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
