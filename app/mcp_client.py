"""Личные HTTP-подключения MCP, Streamable HTTP и инструменты чата.

Адреса проходят ту же SSRF-проверку и закрепление DNS, что подключения моделей.
Ключи — шифром в базе, запросы/ответы и заголовки никогда не пишутся в журнал.
"""

import asyncio
import json
import re
from types import SimpleNamespace

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
import httpx

import security
import state
import connections  # Загрузить Connection до upstream: его аннотации участвуют в цикле импорта.
import upstream

router = APIRouter(prefix="/api/mcp")
MAX_SERVERS = 16
MAX_TOOLS = 128
MAX_BYTES = 1024 * 1024
TIMEOUT = 30
DISCOVER_SEC = 60                            # срок списка инструментов одного сервера в ходе чата
PROTOCOL = "2025-06-18"
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
FORBIDDEN = {"host", "cookie", "content-length", "transfer-encoding", "connection", "content-type", "accept",
             "mcp-session-id", "mcp-protocol-version"}


def refused(message, code="mcp", status=409):
    return HTTPException(status, {"code": code, "message": message})


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    token: str | None = Field(default=None, max_length=8192)
    # Значение null — оставить сохранённое значение заголовка с тем же именем (`detail` значения не отдаёт).
    headers: dict[str, str | None] | None = Field(default=None, max_length=32)
    enabled: bool = True


def row(uid, sid):
    found = state.DB.execute("SELECT * FROM mcp_servers WHERE id=? AND user_id=?", (sid, uid)).fetchone()
    if found is None:
        raise refused("Подключение MCP не найдено", "not_found", 404)
    return found


def secret(item):
    try:
        return json.loads(security.decrypt(item["secret_enc"]))
    except (InvalidToken, ValueError):
        raise refused("Ключ MCP не расшифрован — введите его заново") from None


def public(item):
    return {"id": item["id"], "name": item["name"], "url": item["url"], "enabled": bool(item["enabled"])}


def selected(uid, ids):
    if (not isinstance(ids, list) or len(ids) > MAX_SERVERS
            or any(type(sid) is not int for sid in ids) or len(set(ids)) != len(ids)):
        raise refused("Выберите не больше 16 подключений MCP", "invalid", 422)
    return [row(uid, sid) for sid in ids]


def present(uid, raw):
    """id из `chats.mcp_ids`, чьи подключения у учётки ещё есть: удалённый сервер (id не повторяются —
    AUTOINCREMENT) не остаётся выбранным в диалоге и не мешает смене модели."""
    ids = json.loads(raw)
    if not ids:
        return []
    found = {r[0] for r in state.DB.execute(
        f"SELECT id FROM mcp_servers WHERE user_id=? AND id IN ({','.join('?' * len(ids))})", (uid, *ids))}
    return [sid for sid in ids if sid in found]


def _address(url):
    """Куда на самом деле идёт запрос: схема, хост, порт (с умолчанием) и путь."""
    url = httpx.URL(url)
    return url.scheme, url.host, url.port or upstream.DEFAULT_PORTS.get(url.scheme), url.path


def _values(body, old=None):
    if (not body.name.strip() or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in body.name)
            or not upstream.is_allowed(body.url) or httpx.URL(body.url).fragment):
        raise refused("Укажите имя и адрес MCP из списка разрешённых адресов администратора", "invalid", 422)
    keep = body.token is None or body.headers is None or None in body.headers.values()
    previous = secret(old) if old is not None and keep else {}
    # Прежние токен и значения заголовков — только прежнему адресу (как connections.patch): иначе PUT без них
    # унёс бы write-only секреты на другой адрес из списка.
    if previous and _address(body.url) != _address(old["url"]) and (
            (body.token is None and previous.get("token")) or (body.headers is None and previous.get("headers"))
            or (body.headers is not None and None in body.headers.values())):
        raise refused("Новый адрес MCP — введите токен и значения заголовков заново", "invalid", 422)
    token = body.token if body.token is not None else previous.get("token", "")
    kept = previous.get("headers", {})
    headers = kept if body.headers is None else {k: kept.get(k) if v is None else v for k, v in body.headers.items()}
    if (len(headers) > 32 or any(v is None or not HEADER.fullmatch(k) or k.lower() in FORBIDDEN or len(v) > 8192
                              or any(not 32 <= ord(c) <= 126 for c in v) for k, v in headers.items())
            or any(not 32 <= ord(c) <= 126 for c in token)):
        raise refused("Проверьте токен и дополнительные HTTP-заголовки", "invalid", 422)
    return body.name.strip(), body.url, security.encrypt(json.dumps({"token": token, "headers": headers})), int(body.enabled)


@router.get("")
async def list_servers(request: Request):
    return {"items": [public(r) for r in state.DB.execute("SELECT * FROM mcp_servers WHERE user_id=? ORDER BY id",
                                                         (request.state.user.id,))]}


@router.post("", status_code=201)
async def create(body: ServerIn, request: Request):
    uid = request.state.user.id
    if state.DB.execute("SELECT count(*) FROM mcp_servers WHERE user_id=?", (uid,)).fetchone()[0] >= MAX_SERVERS:
        raise refused("Не больше 16 подключений MCP")
    values = _values(body)
    cursor = state.DB.execute("INSERT INTO mcp_servers(name,url,secret_enc,enabled,user_id) VALUES(?,?,?,?,?)", (*values, uid))
    return public(row(uid, cursor.lastrowid))


@router.get("/{sid}")
async def detail(sid: int, request: Request):
    """Имена заголовков без значений: как и токен, значения только записываются (ключ вида `X-Api-Key` не уносится
    с открытого устройства)."""
    item = row(request.state.user.id, sid)
    return {**public(item), "headers": dict.fromkeys(secret(item).get("headers", {}))}


@router.put("/{sid}")
async def update(sid: int, body: ServerIn, request: Request):
    uid = request.state.user.id
    values = _values(body, row(uid, sid))
    state.DB.execute("UPDATE mcp_servers SET name=?,url=?,secret_enc=?,enabled=? WHERE id=? AND user_id=?", (*values, sid, uid))
    return public(row(uid, sid))


@router.delete("/{sid}", status_code=204)
async def delete(sid: int, request: Request):
    uid = request.state.user.id
    row(uid, sid)
    state.DB.execute("DELETE FROM mcp_servers WHERE id=? AND user_id=?", (sid, uid))


@router.post("/{sid}/check")
async def check(sid: int, request: Request):
    session = Session(row(request.state.user.id, sid))
    try:
        tools = await session.tools()
        return {"tools": [t.get("name", "") for t in tools if isinstance(t, dict)]}
    finally:
        await session.close()


class Session:
    def __init__(self, item):
        self.uid, self.sid = item["user_id"], item["id"]
        self.url, self.stamp = item["url"], item["secret_enc"]
        self.session = None
        self.version = None
        self.seq = 0

    def headers(self, *, rpc=True):
        item = row(self.uid, self.sid)
        if not item["enabled"] or item["url"] != self.url or item["secret_enc"] != self.stamp:
            raise refused("Подключение MCP изменено или выключено — запустите ответ заново")
        values = secret(item)
        headers = httpx.Headers(values.get("headers", {}))
        if values.get("token"):
            headers["Authorization"] = f"Bearer {values['token']}"
        if not rpc:
            return dict(headers)
        headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        return dict(headers)

    async def rpc(self, method, params=None, *, notification=False, max_bytes=MAX_BYTES):
        self.seq += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self.seq
        try:
            async with asyncio.timeout(TIMEOUT):
                async with upstream.stream(SimpleNamespace(id=self.sid), "POST", self.url, keyless=True,
                                           headers=self.headers(), json=payload, timeout=TIMEOUT) as response:
                    if response.status_code not in (200, 202, 204):
                        raise refused(f"MCP ответил HTTP {response.status_code}")
                    if method == "initialize":
                        session = response.headers.get("mcp-session-id")
                        if session and (len(session) > 1024 or any(not 0x21 <= ord(c) <= 0x7e for c in session)):
                            raise refused("MCP вернул неверный идентификатор сессии")
                        self.session = session
                    if notification:
                        return {}
                    data, size = bytearray(), 0
                    sse = "text/event-stream" in response.headers.get("content-type", "")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise refused(f"Ответ MCP больше {max_bytes >> 20} МиБ")
                        data.extend(chunk)
                        if sse:
                            # SSE допускает LF и CRLF; многострочное data склеивается переводом строки.
                            data = bytearray(data.replace(b"\r\n", b"\n"))
                            while b"\n\n" in data:
                                event, _, rest = data.partition(b"\n\n")
                                data = bytearray(rest)
                                raw = b"\n".join(line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:"))
                                if raw:
                                    result = json.loads(raw)
                                    if isinstance(result, dict) and result.get("id") == self.seq:
                                        return self._result(result)
                    if not sse:
                        result = json.loads(data)
                        if isinstance(result, dict) and result.get("id") == self.seq:
                            return self._result(result)
                    raise refused("MCP не вернул ответ на запрос")
        except upstream.UpstreamRefused:
            raise refused("Адрес MCP недоступен по правилам разрешённых адресов") from None
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            raise refused("MCP недоступен или вернул неверный ответ") from None

    @staticmethod
    def _result(result):
        if "error" in result or not isinstance(result.get("result"), dict):
            raise refused("MCP отклонил запрос")
        return result["result"]

    async def initialize(self):
        result = await self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "Studio", "version": "1"}})
        self.version = result.get("protocolVersion")
        if self.version not in ("2024-11-05", "2025-03-26", PROTOCOL):
            raise refused("Версия протокола MCP не поддерживается")
        await self.rpc("notifications/initialized", notification=True)

    async def tools(self):
        await self.initialize()
        tools, cursor, seen = [], None, set()
        while True:
            result = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            batch = result.get("tools")
            if not isinstance(batch, list):
                raise refused("MCP вернул неверный список инструментов")
            tools.extend(batch)
            if len(tools) > MAX_TOOLS:
                raise refused("MCP предлагает больше 128 инструментов")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in seen or not batch:
                raise refused("MCP вернул неверную страницу инструментов")
            seen.add(cursor)

    async def close(self):
        if not self.session:
            return
        try:
            async with asyncio.timeout(5):
                async with upstream.stream(SimpleNamespace(id=self.sid), "DELETE", self.url, keyless=True,
                                           headers=self.headers(), timeout=5):
                    pass
        except (HTTPException, httpx.HTTPError, upstream.UpstreamRefused, TimeoutError, ValueError):
            pass


class Tools:
    def __init__(self, uid, chat_id, ids):
        self.uid, self.chat_id = uid, chat_id
        self.ids = list(ids)
        self.sessions, self.calls, self.specs = [], {}, []
        self.failed = []                     # (имя сервера, слова) — сервер пропущен, ход идёт без него

    @staticmethod
    async def _list(session):
        """Инструменты сервера или слова отказа: недоступный сервер не рвёт ход."""
        try:
            async with asyncio.timeout(DISCOVER_SEC):
                tools = await session.tools()
        except HTTPException as exc:
            return exc.detail["message"]
        except TimeoutError:
            return "MCP не ответил вовремя"
        if any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not NAME.fullmatch(tool["name"])
               or not isinstance(tool.get("inputSchema"), dict) for tool in tools):
            return "MCP вернул неверное описание инструмента"
        return tools

    async def discover(self):
        """Выбранные включённые серверы опрашиваются параллельно, каждый в свой срок DISCOVER_SEC. Отказавший
        пропускается — его имя и слова в `failed`; превышение MAX_TOOLS суммарно — отказ хода."""
        items = [item for sid in self.ids if (item := state.DB.execute(
            "SELECT * FROM mcp_servers WHERE id=? AND user_id=? AND enabled=1", (sid, self.uid)).fetchone()) is not None]
        self.sessions = [Session(item) for item in items]
        found = await asyncio.gather(*(self._list(session) for session in self.sessions))
        for item, session, tools in zip(items, self.sessions, found):
            if isinstance(tools, str):
                self.failed.append((item["name"], tools))
                continue
            for tool in tools:
                name = f"mcp_{item['id']}_{len(self.calls)}"
                if len(self.calls) >= MAX_TOOLS:
                    raise refused("Выберите MCP с суммарно не больше 128 инструментов")
                self.calls[name] = (session, tool["name"], item["name"])
                self.specs.append({"type": "function", "function": {"name": name,
                    "description": (f"{item['name']}: {tool['name']}. " + str(tool.get("description", "")))[:4000],
                    "parameters": tool["inputSchema"]}})

    async def call(self, name, arguments):
        session, tool, server = self.calls[name]
        step = {"kind": "mcp", "server": server, "tool": tool}
        try:
            selected_row = state.DB.execute("SELECT mcp_ids FROM chats WHERE id=? AND user_id=?", (self.chat_id, self.uid)).fetchone()
            if selected_row is None or session.sid not in json.loads(selected_row[0]):
                raise refused("MCP выключен в этом диалоге")
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise refused("Неверные аргументы инструмента MCP")
            result = await session.rpc("tools/call", {"name": tool, "arguments": args})
            text = "\n".join(block["text"] for block in result.get("content", [])
                             if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
            if result.get("structuredContent") is not None:
                text += "\n" + json.dumps(result["structuredContent"], ensure_ascii=True)
            if not text:
                text = "Инструмент MCP вернул результат без текста"
            step["error" if result.get("isError") else "text"] = text[:60_000]
        except HTTPException as exc:
            step["error"] = exc.detail["message"]
        except (ValueError, TypeError):
            step["error"] = "Неверные аргументы или результат MCP"
        return step

    async def close(self):
        await asyncio.gather(*(session.close() for session in self.sessions))
