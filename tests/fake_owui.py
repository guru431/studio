"""Поддельный Open WebUI 0.13 для быстрых тестов: REST из таблицы протокола спеки
(`docs/superpowers/specs/2026-11-22-studio-open-webui-design.md`), без сети — `FakeGateway.hosts` отдаёт ему запросы
на `ENTRY`. Сроки токенов — по `clock.NOW`, как у настоящего. Сокет — в задаче 5.

Здесь: `/api/config`, вход, постоянные сессии (создание, обновление с ротацией и восстановлением по `request_id`,
отзыв). Чаты и папки только хранятся (`add_chat`, `add_folder`): их маршруты — в следующих задачах.
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
        self.requests: list[httpx.Request] = []
        self.refreshes = 0                 # ротации: новая пара выдана (повтор по `request_id` не считается)
        self.refresh_log: list[tuple[str, str]] = []     # (refresh_token, request_id) каждого запроса обновления
        self.revoked: list[str] = []       # id сессий, отозванных запросом `DELETE /api/v1/auths/sessions/{id}`
        self.chats: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
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

    def add_chat(self, title: str = "Chat", *, folder_id: str | None = None, pinned: bool = False,
                 messages: list[dict] | None = None, chat_id: str | None = None) -> str:
        chat_id = chat_id or f"chat-{next(self._ids)}"
        self.chats[chat_id] = {"id": chat_id, "title": title, "folder_id": folder_id, "pinned": pinned,
                               "messages": list(messages or []), "created_at": int(clock.NOW()),
                               "updated_at": int(clock.NOW())}
        return chat_id

    def add_folder(self, name: str = "Folder", *, folder_id: str | None = None) -> str:
        folder_id = folder_id or f"folder-{next(self._ids)}"
        self.folders[folder_id] = {"id": folder_id, "name": name}
        return folder_id

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
            return httpx.Response(200, json={"name": "Open WebUI", "version": self.version,
                                             "features": {"auth_sessions": True}})
        if path == "/api/v1/auths/signin" and method == "POST":
            return self._signin(request)
        if path == "/api/v1/auths/sessions/refresh" and method == "POST":
            return self._refresh(request)
        if path == "/api/v1/auths/sessions" and method == "POST":
            return self._create_session(request)
        if path.startswith("/api/v1/auths/sessions/") and method == "DELETE":
            return self._revoke(request, path.rsplit("/", 1)[1])
        return httpx.Response(404, json={"detail": "Not Found"})

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
        token = f"jwt-{uuid.uuid4().hex}"
        self._tokens[token] = (None, clock.NOW() + LOGIN_SEC)
        return httpx.Response(200, json={"token": token, "token_type": "Bearer", "id": self.user_id,
                                         "email": self.EMAIL, "name": self.name, "role": self.role,
                                         "expires_at": int(clock.NOW() + LOGIN_SEC)})

    def _create_session(self, request: httpx.Request) -> httpx.Response:
        if not self._caller_valid(request):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        return httpx.Response(200, json=self._issue(f"session-{next(self._ids)}"))

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
