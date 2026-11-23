"""Личные и общие HTTP-подключения MCP, Streamable HTTP и инструменты чата.

Адреса проходят ту же SSRF-проверку и закрепление DNS, что подключения моделей.
Ключи — шифром в базе, запросы/ответы и заголовки никогда не пишутся в журнал.
Общие серверы (`user_id IS NULL`) учётка видит по группам доступа (access.py): закрытый — как отсутствующий,
закрытый инструмент — не предлагается модели и не вызывается. Каждый запрос к общему серверу несёт
`X-Studio-User` — непрозрачный идентификатор учётки, свой у каждого сервера (`access.studio_user`); личным — не
отправляется.
"""

import asyncio
import json
import logging
import re
from types import SimpleNamespace

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
import httpx

import access
import cleaner_route
import mcp_sources
import security
import state
import connections  # Загрузить Connection до upstream: его аннотации участвуют в цикле импорта.
import upstream

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/mcp")
CATALOG_FAILED = "Список инструментов MCP не прочитан"
MAX_SERVERS = 16
MAX_TOOLS = 128
MAX_BYTES = 1024 * 1024
TIMEOUT = 30
DISCOVER_SEC = 60                            # срок списка инструментов одного сервера в ходе чата
PROMPTS_MAX = 32                             # подсказок сервера в каталоге
PROMPT_TITLE_MAX = 120
PROMPT_DESCRIPTION_MAX = 300
PROMPT_ARGUMENTS_MAX = 16
PROMPT_TEXT_MAX = 20_000                     # текст сообщений `user` из prompts/get
RESOURCES_MAX = 64                           # ресурсов сервера из resources/list
RESOURCE_URI_MAX = 2048
RESOURCE_MAX = 1024 * 1024                   # текст ресурса из resources/read, байт UTF-8
PROTOCOL = "2025-06-18"
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
FORBIDDEN = {"host", "cookie", "content-length", "transfer-encoding", "connection", "content-type", "accept",
             "mcp-session-id", "mcp-protocol-version", "x-studio-user"}


def refused(message, code="mcp", status=409):
    return HTTPException(status, {"code": code, "message": message})


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    token: str | None = Field(default=None, max_length=8192)
    # Значение null — оставить сохранённое значение заголовка с тем же именем (`detail` значения не отдаёт).
    headers: dict[str, str | None] | None = Field(default=None, max_length=32)
    enabled: bool = True
    #: «Защищённый источник» (только общие): после успешного вызова его инструмента диалог без «Интернета»
    #: и ссылок в аргументах (`chat.StreamTask._calls`)
    protected: bool = False
    #: «В диалоге по умолчанию» (смысл — только у общих): выключенный входит в диалог, только когда его включат там
    #: (`chats.mcp_ids`, `mcp_sources.dialog_servers`)
    dialog_default: bool = True


def _gate(uid):
    """Общий сервер, закрытый учётке группами доступа, — как отсутствующий; `uid None` — управление общим."""
    if uid is None:
        return "", ()
    clause, args = access.shared_filter(uid, "mcp", "mcp_servers.id")
    return f" AND {clause}", args


def row(uid, sid, *, own=False):
    scope = "user_id IS ?" if own else "(user_id IS ? OR user_id IS NULL)"
    clause, args = ("", ()) if own else _gate(uid)
    found = state.DB.execute(f"SELECT * FROM mcp_servers WHERE id=? AND {scope}{clause}", (sid, uid, *args)).fetchone()
    if found is None:
        raise refused("Подключение MCP не найдено", "not_found", 404)
    return found


def secret(item):
    try:
        return json.loads(security.decrypt(item["secret_enc"]))
    except (InvalidToken, ValueError):
        raise refused("Ключ MCP не расшифрован — введите его заново") from None


def public(item, uid=None):
    shared = item["user_id"] is None
    out = {"id": item["id"], "name": item["name"], "url": "" if shared and uid is not None else item["url"],
           "enabled": bool(item["enabled"])}
    if shared:
        out["shared"] = True
        out["dialog_default"] = bool(item["dialog_default"])
        if uid is None:                          # флажок — редактору администратора
            out["protected"] = bool(item["protected"])
    return out


def servers(uid, *, enabled_only=False):
    enabled = " AND enabled=1" if enabled_only else ""
    clause, args = _gate(uid)
    return state.DB.execute(f"SELECT * FROM mcp_servers WHERE (user_id IS ? OR user_id IS NULL){enabled}{clause}"
                            " ORDER BY user_id IS NULL, id", (uid, *args)).fetchall()


def selected(uid, ids):
    if (not isinstance(ids, list) or len(ids) > MAX_SERVERS
            or any(type(sid) is not int for sid in ids) or len(set(ids)) != len(ids)):
        raise refused("Выберите не больше 16 подключений MCP", "invalid", 422)
    return [row(uid, sid) for sid in ids]


def present(uid, raw):
    """id из `chats.mcp_ids`, чьи подключения у учётки ещё есть: удалённый или закрытый группами доступа сервер
    (id не повторяются — AUTOINCREMENT) не остаётся выбранным в диалоге и не мешает смене модели."""
    ids = json.loads(raw)
    if not ids:
        return []
    clause, args = _gate(uid)
    found = {r[0] for r in state.DB.execute(
        f"SELECT id FROM mcp_servers WHERE (user_id IS ? OR user_id IS NULL){clause}"
        f" AND id IN ({','.join('?' * len(ids))})", (uid, *args, *ids))}
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
    encrypted = None
    if old is not None and (body.url, bool(body.enabled), bool(body.protected)) == (
            old["url"], bool(old["enabled"]), bool(old["protected"])):
        try:
            previous = previous if keep else secret(old)
        except HTTPException:
            previous = {}  # полностью переданные новые реквизиты восстанавливают повреждённое подключение
        if (token, headers) == (previous.get("token"), previous.get("headers", {})):
            encrypted = old["secret_enc"]
    if encrypted is None:
        encrypted = security.encrypt(json.dumps({"token": token, "headers": headers}))
    return body.name.strip(), body.url, encrypted, int(body.enabled)


def list_for(uid):
    return {"items": [public(item, uid) for item in servers(uid)]}


def _save_values(uid, body, old=None):
    if uid is None and "?" in body.url:
        raise refused("Адрес общего MCP — без строки запроса. Передайте ключ в токене или HTTP-заголовке", "invalid", 422)
    if uid is not None and body.protected:
        raise refused("«Защищённый источник» — только у общих подключений MCP", "invalid", 422)
    # у личного флажок не действует (он входит в диалог только явно) — хранится умолчание
    return (*_values(body, old), int(body.protected), int(body.dialog_default or uid is not None))


def create_for(uid, body):
    if state.DB.execute("SELECT count(*) FROM mcp_servers WHERE user_id IS ?", (uid,)).fetchone()[0] >= MAX_SERVERS:
        raise refused("Не больше 16 подключений MCP")
    values = _save_values(uid, body)
    cursor = state.DB.execute("INSERT INTO mcp_servers(name,url,secret_enc,enabled,protected,dialog_default,user_id)"
                              " VALUES(?,?,?,?,?,?,?)", (*values, uid))
    return public(row(uid, cursor.lastrowid, own=True), uid)


def detail_for(uid, sid):
    """Имена заголовков без значений: как и токен, значения только записываются (ключ вида `X-Api-Key` не уносится
    с открытого устройства)."""
    item = row(uid, sid, own=True)
    return {**public(item, uid), "headers": dict.fromkeys(secret(item).get("headers", {}))}


def update_for(uid, sid, body):
    values = _save_values(uid, body, row(uid, sid, own=True))
    state.DB.execute("UPDATE mcp_servers SET name=?,url=?,secret_enc=?,enabled=?,protected=?,dialog_default=?"
                     " WHERE id=? AND user_id IS ?", (*values, sid, uid))
    mcp_sources.forget(sid)
    return public(row(uid, sid, own=True), uid)


def delete_for(uid, sid):
    row(uid, sid, own=True)
    state.DB.execute("DELETE FROM mcp_servers WHERE id=? AND user_id IS ?", (sid, uid))
    mcp_sources.forget(sid)


async def check_for(uid, sid, *, actor=None):
    """«Проверить» — и выключенный сервер: проверить его до включения. `uid` — владелец (None — общий),
    `actor` — учётка, от чьего имени идёт запрос к общему серверу (`X-Studio-User`; администратор панели)."""
    session = Session(row(uid, sid, own=True), uid=uid if actor is None else actor, disabled_ok=True)
    mcp_sources.forget(sid)
    try:
        tools = await session.tools()
        return {"tools": [t.get("name", "") for t in tools if isinstance(t, dict)]}
    finally:
        await session.close()


@router.get("")
async def list_servers(request: Request):
    return list_for(request.state.user.id)


@router.post("", status_code=201)
async def create(body: ServerIn, request: Request):
    return create_for(request.state.user.id, body)


class PromptIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=PROMPT_ARGUMENTS_MAX)


def _catalog_tool(item, tool, rules):
    hint = mcp_sources.effective(tool, rules.get(tool["name"]) if item["user_id"] is None else None)
    return {"name": tool["name"], "title": _text(tool.get("title"), 160),
            "description": _text(tool.get("description"), 2000), "open_world": hint["open_world"],
            "confirm": hint["confirm"] is not None}


async def catalog_for(uid, refresh=False):
    """Серверы учётки для окна MCP диалога: `state` — `off` (выключен, не опрашивается), `ok`, `error`
    (`message`); правила сервера, инструменты, открытые учётке группами (с пометками «интернет» и «с
    подтверждением» после политики администратора), подсказки, у общих — `dialog_default` («В диалоге по умолчанию»;
    false — сервер включают в диалоге вручную), объявленные контракты Studio (`contracts`:
    files, user_files, status — mcp_contracts). Опрос параллельно, каждый — до
    `mcp_sources.LIST_SEC`, из кэша; `refresh` — заново, если запись старше `mcp_sources.REFRESH_MIN_SEC`."""

    async def one(item):
        out = {"id": item["id"], "name": item["name"], "shared": item["user_id"] is None}
        if out["shared"]:                            # без отметки — в диалог только вручную (`dialog_servers`)
            out["dialog_default"] = bool(item["dialog_default"])
        empty = {"instructions": None, "tools": [], "prompts": []}
        if not item["enabled"]:
            return {**out, "state": "off", **empty}
        session = Session(item, uid=uid)
        try:
            found = await mcp_sources.catalog(session, mcp_sources.LIST_SEC, refresh=refresh)
            rules = mcp_sources.policy(item["id"]) if item["user_id"] is None else {}
            tools = [_catalog_tool(item, tool, rules) for tool in found.tools
                     if isinstance(tool, dict) and isinstance(tool.get("name"), str) and NAME.fullmatch(tool["name"])
                     and access.tool_allowed(uid, item["id"], tool["name"])]
            instructions = (found.instructions or "").strip()[:mcp_sources.INSTRUCTIONS_MAX] or None
            import mcp_contracts              # здесь, не при загрузке: модуль сам импортирует mcp_client
            import mcp_files                  # здесь, не при загрузке: mcp_files тянет разделы и chat
            features = mcp_contracts.features(found)
            contracts = {"files": features.files is not None, "user_files": bool(features.user_files),
                         "status": features.status}
            chat_files = mcp_files.limit_of(found)          # с потолком mcp_files.MEDIA_MAX, как у загрузки
            upload_policy = None
            if chat_files is not None:
                hint = mcp_sources.effective(mcp_files.upload_tool(found), rules.get(mcp_files.UPLOAD))
                upload_policy = {"open_world": hint["open_world"], "confirm": hint["confirm"] is not None}
        except HTTPException as exc:
            return {**out, "state": "error", "message": exc.detail["message"], **empty}
        except TimeoutError:
            return {**out, "state": "error", "message": "MCP не ответил вовремя", **empty}
        except Exception as exc:                     # сбой одного сервера не роняет каталог; текст исключения — никуда
            log.warning("mcp %d: каталог не прочитан (%s)", item["id"], type(exc).__name__)
            return {**out, "state": "error", "message": CATALOG_FAILED, **empty}
        finally:
            await session.close()
        return {**out, "state": "ok", "instructions": instructions, "tools": tools, "prompts": found.prompts,
                "chat_files": chat_files, "chat_files_policy": upload_policy, "trimmed": found.trimmed,
                "contracts": contracts}

    return {"items": list(await asyncio.gather(*(one(item) for item in servers(uid))))}


@router.get("/catalog")
async def catalog(request: Request, refresh: str = ""):
    return await catalog_for(request.state.user.id, refresh == "1")


@router.post("/{sid}/prompt")
async def prompt(sid: int, body: PromptIn, request: Request):
    """Текст подсказки сервера (`prompts/get`) — страница кладёт его в поле ввода."""
    uid = request.state.user.id
    session = Session(row(uid, sid), uid=uid)
    try:
        return {"text": await session.prompt(body.name, body.arguments)}
    finally:
        await session.close()


@router.get("/{sid}")
async def detail(sid: int, request: Request):
    return detail_for(request.state.user.id, sid)


@router.put("/{sid}")
async def update(sid: int, body: ServerIn, request: Request):
    return update_for(request.state.user.id, sid, body)


@router.delete("/{sid}", status_code=204)
async def delete(sid: int, request: Request):
    delete_for(request.state.user.id, sid)


@router.post("/{sid}/check")
async def check(sid: int, request: Request):
    return await check_for(request.state.user.id, sid)


class Session:
    def __init__(self, item, *, uid, disabled_ok=False):
        self.uid, self.sid = uid, item["id"]
        self.owner_id = item["user_id"]
        self.url, self.stamp = item["url"], item["secret_enc"]
        self.disabled_ok = disabled_ok       # только «Проверить»: ходы и разделы выключенный сервер не зовут
        self.session = None
        self.version = None
        self.instructions = None             # правила сервера из initialize — как прислал, режет chat_system
        self.capabilities = {}
        self.error = None                    # поле `error` последнего ответа JSON-RPC
        self.trimmed = False                 # `tools` урезал список до MAX_TOOLS
        self.seq = 0

    def headers(self, *, rpc=True):
        item = row(self.owner_id, self.sid, own=True)
        if ((not item["enabled"] and not self.disabled_ok) or item["url"] != self.url
                or item["secret_enc"] != self.stamp):
            raise refused("Подключение MCP изменено или выключено — запустите ответ заново")
        values = secret(item)
        headers = httpx.Headers({key: value for key, value in values.get("headers", {}).items()
                                 if key.lower() not in FORBIDDEN})
        if values.get("token"):
            headers["Authorization"] = f"Bearer {values['token']}"
        if self.owner_id is None and self.uid is not None:
            headers["X-Studio-User"] = access.studio_user(self.uid, self.sid)
        if not rpc:
            return dict(headers)
        headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        return dict(headers)

    async def rpc(self, method, params=None, *, notification=False, max_bytes=MAX_BYTES, timeout=TIMEOUT):
        self.seq += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self.seq
        try:
            async with asyncio.timeout(timeout):
                async with upstream.stream(SimpleNamespace(id=self.sid), "POST", self.url, keyless=True,
                                           headers=self.headers(), json=payload, timeout=timeout) as response:
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

    def _result(self, result):
        self.error = result.get("error")     # ответ-ошибка JSON-RPC: Tools.call узнаёт по нему «нет инструмента»
        if "error" in result or not isinstance(result.get("result"), dict):
            raise refused("MCP отклонил запрос")
        return result["result"]

    async def initialize(self):
        result = await self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "Studio", "version": "1"}})
        version = result.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", PROTOCOL):
            raise refused("Версия протокола MCP не поддерживается")
        self.version = version
        instructions = result.get("instructions")
        self.instructions = instructions if isinstance(instructions, str) and instructions.strip() else None
        capabilities = result.get("capabilities")
        self.capabilities = capabilities if isinstance(capabilities, dict) else {}
        try:
            await self.rpc("notifications/initialized", notification=True)
        except HTTPException:
            self.version = None              # знакомство не завершено: `ensure` повторит его
            raise

    async def ensure(self):
        """Знакомство один раз на сессию: при каталоге из кэша сессию знакомит первый вызов инструмента."""
        if self.version is None:
            await self.initialize()

    async def tools(self):
        """Инструменты сервера (`tools/list` с листанием): не больше MAX_TOOLS — набрав их, листание
        останавливается, лишнее отбрасывается, `trimmed` — признак урезанного списка."""
        await self.ensure()
        tools, cursor, seen = [], None, set()
        self.trimmed = False
        while True:
            result = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            batch = result.get("tools")
            if not isinstance(batch, list):
                raise refused("MCP вернул неверный список инструментов")
            tools.extend(batch)
            cursor = result.get("nextCursor")
            if len(tools) >= MAX_TOOLS:
                self.trimmed = len(tools) > MAX_TOOLS or bool(cursor)
                return tools[:MAX_TOOLS]
            if not cursor:
                return tools
            if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in seen or not batch:
                raise refused("MCP вернул неверную страницу инструментов")
            seen.add(cursor)

    async def prompts(self):
        """Подсказки сервера (`prompts/list` с листанием): не больше PROMPTS_MAX, неверные пропускаются, длинные
        подписи обрезаются."""
        await self.ensure()
        prompts, cursor, seen = [], None, set()
        while True:
            result = await self.rpc("prompts/list", {"cursor": cursor} if cursor else {})
            batch = result.get("prompts")
            if not isinstance(batch, list):
                raise refused("MCP вернул неверный список подсказок")
            prompts.extend(item for item in map(_prompt, batch) if item is not None)
            cursor = result.get("nextCursor")
            if len(prompts) >= PROMPTS_MAX or not cursor:
                return prompts[:PROMPTS_MAX]
            if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in seen or not batch:
                raise refused("MCP вернул неверную страницу подсказок")
            seen.add(cursor)

    async def prompt(self, name, args):
        """Текст сообщений `user` подсказки (`prompts/get`), не длиннее PROMPT_TEXT_MAX."""
        if (not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(args, dict)
                or len(args) > PROMPT_ARGUMENTS_MAX
                or any(not isinstance(k, str) or not NAME.fullmatch(k) or not isinstance(v, str)
                       or len(v) > PROMPT_TEXT_MAX for k, v in args.items())):
            raise refused("Неверные параметры подсказки MCP", "invalid", 422)
        await self.ensure()
        result = await self.rpc("prompts/get", {"name": name, "arguments": args})
        messages = result.get("messages")
        texts = [message["content"]["text"] for message in messages if isinstance(message, dict)
                 and message.get("role") == "user" and isinstance(message.get("content"), dict)
                 and message["content"].get("type") == "text" and isinstance(message["content"].get("text"), str)
                 ] if isinstance(messages, list) else []
        text = "\n\n".join(texts)[:PROMPT_TEXT_MAX]
        if not text.strip():
            raise refused("MCP вернул подсказку без текста")
        return text

    async def resources(self):
        """Ресурсы сервера (`resources/list` с листанием): не больше RESOURCES_MAX словарей с `uri`-строкой."""
        await self.ensure()
        found, cursor, seen = [], None, set()
        while True:
            result = await self.rpc("resources/list", {"cursor": cursor} if cursor else {})
            batch = result.get("resources")
            if not isinstance(batch, list):
                raise refused("MCP вернул неверный список ресурсов")
            found.extend(item for item in batch if isinstance(item, dict) and isinstance(item.get("uri"), str)
                         and 0 < len(item["uri"]) <= RESOURCE_URI_MAX)
            cursor = result.get("nextCursor")
            if len(found) >= RESOURCES_MAX or not cursor:
                return found[:RESOURCES_MAX]
            if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in seen or not batch:
                raise refused("MCP вернул неверную страницу ресурсов")
            seen.add(cursor)

    async def read_resource(self, uri):
        """Текст ресурса `uri` (`resources/read`): текстовые части подряд, не больше RESOURCE_MAX байт UTF-8."""
        await self.ensure()
        result = await self.rpc("resources/read", {"uri": uri}, max_bytes=8 * RESOURCE_MAX)
        contents = result.get("contents")
        text = "".join(part["text"] for part in contents if isinstance(part, dict) and isinstance(part.get("text"), str)
                       ) if isinstance(contents, list) else ""
        if len(text.encode("utf-8", "replace")) > RESOURCE_MAX:
            raise refused("Ресурс MCP больше 1 МиБ")   # = RESOURCE_MAX
        return text

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


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _prompt(item):
    """Подсказка `prompts/list` в виде для каталога или None — неверное имя."""
    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not NAME.fullmatch(item["name"]):
        return None
    arguments = item.get("arguments")
    arguments = [{"name": arg["name"], "description": _text(arg.get("description"), PROMPT_DESCRIPTION_MAX),
                  "required": arg.get("required") is True}
                 for arg in (arguments if isinstance(arguments, list) else [])
                 if isinstance(arg, dict) and isinstance(arg.get("name"), str) and NAME.fullmatch(arg["name"])]
    return {"name": item["name"], "title": _text(item.get("title"), PROMPT_TITLE_MAX),
            "description": _text(item.get("description"), PROMPT_DESCRIPTION_MAX),
            "arguments": arguments[:PROMPT_ARGUMENTS_MAX]}


TOOL_OFF = "Инструмент выключен в этом диалоге"
WEB_OFF = "«Интернет» выключен — инструмент MCP не вызван"
#: заметка хода (шаг `note`, не ошибка): {0} — предел инструментов запроса (`profiles.max_tools`) или списка сервера
TRIMMED = "Инструментов MCP больше {0} — часть этого сервера не предложена модели"
UPLOAD = "artifact_upload"           # как mcp_files.UPLOAD (mcp_files импортирует этот модуль)
#: строка аргумента со ссылкой: после защищённого источника такой вызов любого инструмента — отказ URL_REFUSED.
#: URL_RE — явные адреса: любая схема `x://`, `www.`, адрес без схемы `//host`, IPv4 (и с портом или путём:
#: `1.2.3.4:8080`, `1.2.3.4/x`; пятая часть `1.2.3.4.5` и версия `v1.2.3.4` — не адрес) и метка punycode `xn--`.
URL_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]{0,15}://"
    r"|\bwww\."
    r"|(?<![\w:/])//[a-z0-9-]+(?:\.[a-z0-9-]+)+"
    r"|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)"
    r"|(?<![\w-])xn--[a-z0-9-]+", re.IGNORECASE)
#: Имя хоста без схемы (`evil.com/x`, `evil.com:8080`, `sub.evil.co.uk`, `evil.ru`) — HOST_RE и `_host_like`.
#: Кандидат — целое слово из меток ASCII через точку, последняя (зона) — только буквы, от двух: не часть пути
#: (перед ним нет `/` и `\`: `out/file.md` — путь), не часть версии (`v1.2`, `3.14` — зона не из букв). Почта
#: (`user@evil.com`) — тоже адрес наружу. Расширения файлов бывают и зонами (.md, .py, .sh, .so), поэтому имя —
#: адрес, только если: зона из короткого списка HOST_ZONES — частых веб-зон, которые не расширения файлов и не частые
#: имена полей кода (`logger.info`, `rect.top`, `point.xyz` — не адрес); или за именем идёт путь `/` (не цифра:
#: `a.value/2` — деление) или порт `:1234`, а зона не из FILE_EXTS (`app.py:42` — файл и строка); или вторая метка с
#: конца — `co`, `com`, `net`, `org`, `gov`, `edu`, `ac` (`evil.co.zz`). Код и SQL (`os.path.join`,
#: `public.users.id`, `report.final.docx`) под эти правила не попадают. Не ловятся зоны-расширения без схемы
#: (`evil.sh/x`) и зоны вне списка без пути и порта (`evil.app`) — их ловят схема, `www.`, `//` и IPv4.
HOST_RE = re.compile(r"(?<![\w./\\-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}))(?![\w-]|\.[\w-])"
                     r"(/(?![\d\s/])|:\d{1,5}(?!\d))?", re.IGNORECASE)
HOST_ZONES = frozenset("com net org biz ru su ua by kz uz uk de fr eu cn jp nl io co tv pw tk gq icu onion"
                       " online website".split())
FILE_EXTS = frozenset(
    "md markdown txt rst csv tsv json jsonl yaml yml toml ini cfg conf env log xml html htm css scss js mjs cjs jsx"
    " ts tsx vue py pyi ipynb sh bash zsh ps1 psm1 bat cmd exe dll so bin dat bak tmp lock sql db sqlite rs go rb"
    " php java kt c h cc cpp hpp cs swift pdf doc docx xls xlsx ppt pptx odt ods rtf zip gz tgz tar bz2 xz rar png"
    " jpg jpeg gif webp svg ico bmp tif tiff heic mp3 wav ogg flac m4a mp4 mkv mov webm avi srt vtt parquet".split())
SECOND_LEVEL = frozenset("co com net org gov edu ac".split())
URL_REFUSED = "Ссылки в аргументах после защищённого источника запрещены"


def _host_like(text: str) -> bool:
    """В строке есть имя хоста без схемы по правилам HOST_RE (выше)."""
    for found in HOST_RE.finditer(text):
        labels = found.group(1).lower().split(".")
        zone, tail = labels[-1], found.group(3)
        if (zone in HOST_ZONES or (tail and zone not in FILE_EXTS)
                or (len(labels) > 2 and labels[-2] in SECOND_LEVEL)):
            return True
    return False


def has_url(value) -> bool:
    """В аргументах (JSON любой вложенности; ключи тоже) есть строка со ссылкой: URL_RE или имя хоста (`_host_like`)."""
    if isinstance(value, str):
        return URL_RE.search(value) is not None or _host_like(value)
    if isinstance(value, dict):
        return any(has_url(k) or has_url(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_url(v) for v in value)
    return False


def _unknown_tool(error, text=""):
    """Ответ сервера «нет такого инструмента»: JSON-RPC −32602 или слова в ответе-ошибке."""
    words = (error.get("message") if isinstance(error, dict) else "") or text
    return (isinstance(error, dict) and error.get("code") == -32602) or (
        isinstance(words, str) and "unknown tool" in words.lower())


class Tools:
    """Инструменты MCP хода: `ids` — действующий набор диалога (`mcp_sources.dialog_servers`, явные первыми),
    `explicit` — его явно включённые, `conn` — подключение хода (для перепроверки набора перед вызовом)."""

    def __init__(self, uid, chat_id, ids, *, explicit=(), conn=None):
        self.uid, self.chat_id, self.conn = uid, chat_id, conn
        self.ids, self.explicit = list(ids), set(explicit)
        self.sessions, self.calls, self.specs = [], {}, []
        self.failed = []                     # (имя сервера, слова) — сервер пропущен, ход идёт
        self.notes = []                      # (имя сервера, слова) — список урезан пределом (TRIMMED), не ошибка
        self.instructions = []               # (имя сервера, правила) серверов, давших инструменты ходу
        self.found = []                      # (строка сервера, сессия, каталог) ответивших серверов — mcp_files.sync
        self.protected = set()               # id общих серверов набора с флажком «Защищённый источник»
        self.open_world = set()              # имена вызовов веб-инструментов (пометка «интернет») — снимает `lock`
        self.reached = set()                 # id серверов, которым ход послал `tools/call` (с любым итогом)

    def _chat(self):
        return state.DB.execute("SELECT * FROM chats WHERE id=? AND user_id=? AND deleted IS NULL",
                                (self.chat_id, self.uid)).fetchone()

    @staticmethod
    async def _list(session, timeout):
        """Каталог сервера (кэш `mcp_sources`) или слова отказа: недоступный сервер не рвёт ход."""
        try:
            found = await mcp_sources.catalog(session, timeout)
        except HTTPException as exc:
            return exc.detail["message"]
        except TimeoutError:
            return "MCP не ответил вовремя"
        if any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not NAME.fullmatch(tool["name"])
               or not isinstance(tool.get("inputSchema"), dict) for tool in found.tools):
            return "MCP вернул неверное описание инструмента"
        return found

    @staticmethod
    def _offered(item, tool, rules, web):
        """Пометки инструмента и решение, предлагать ли его ходу: веб-инструмент — только при «Интернете».
        «С подтверждением» предлагается всегда: вызов ждёт «Запустить» (`chat_pending`, `confirm_hint`)."""
        hint = mcp_sources.effective(tool, rules.get(tool["name"]) if item["user_id"] is None else None)
        if hint["open_world"] and not web:
            return hint, WEB_OFF
        return hint, None

    def reached_protected(self):
        """Ход послал вызов защищённому серверу — с любым итогом: ответ-ошибка тоже несёт его данные; отказ студии
        до запроса (доступ, «Интернет», выключение, неверные аргументы) — нет."""
        return bool(self.reached & self.protected)

    def lock(self):
        """Защищённый источник сработал: веб-инструменты MCP больше не предлагаются модели в этом ходе (вызов и так
        перепроверит «Интернет» в `call`)."""
        self.specs = [spec for spec in self.specs if spec["function"]["name"] not in self.open_world]

    def confirm_hint(self, name):
        """Пометки инструмента хода `name` с политикой администратора, прочитанной заново (`mcp_sources.effective`);
        для `mcp_sources.confirm_needed`. Не инструмент MCP хода — None."""
        if name not in self.calls:
            return None
        session, tool, _, spec = self.calls[name]
        rules = mcp_sources.policy(session.sid) if session.owner_id is None else {}
        return mcp_sources.effective(spec, rules.get(tool))

    async def discover(self, *, web=None):
        """Каталоги серверов набора — из кэша (`mcp_sources.catalog`), без знакомства с сервером: его делает первый
        вызов. Холодный кэш: явные ждут каждый DISCOVER_SEC, серверы по умолчанию — все вместе
        `mcp_sources.DEFAULT_WAIT_SEC`; не успевший пропускается словами в `failed`. Отбор — группы доступа,
        выключенные в диалоге, «Интернет» (`_offered`; при флажке «Веб только через Cleaner» — только ходу через
        Cleaner, `cleaner_route.web_allowed`) и UPLOAD «с подтверждением» без явного включения; сверх
        предела запроса подключения (`profiles.max_tools`, без подключения — MAX_TOOLS) лишнее по порядку набора
        отбрасывается заметкой в `notes` (TRIMMED), ход не отказывает. `web` — «Интернет» вместо прочитанного из
        диалога (`web_tools`)."""
        chat = self._chat()
        if chat is None:
            return
        off, on = mcp_sources.tool_state(chat["mcp_tools_off"])
        import profiles                       # здесь, не при загрузке: profiles тянет разделы
        cap = profiles.max_tools(self.conn) if self.conn is not None else MAX_TOOLS
        if web is None:
            web = bool(chat["web"]) and not chat["protected_used"]   # после защищённого источника — без веб-инструментов
        web = web and cleaner_route.web_allowed(self.conn)          # флажок «Веб только через Cleaner»
        enabled = {item["id"]: item for item in servers(self.uid, enabled_only=True)}
        items = [enabled[sid] for sid in self.ids if sid in enabled]
        self.sessions = [Session(item, uid=self.uid) for item in items]
        found = await asyncio.gather(*(self._list(session, DISCOVER_SEC if item["id"] in self.explicit
                                                  else mcp_sources.DEFAULT_WAIT_SEC)
                                       for item, session in zip(items, self.sessions)))
        for item, session, catalog in zip(items, self.sessions, found):
            if isinstance(catalog, str):
                self.failed.append((item["name"], catalog))
                continue
            self.found.append((item, session, catalog))
            if item["user_id"] is None and item["protected"]:
                self.protected.add(item["id"])
            rules =mcp_sources.policy(item["id"]) if item["user_id"] is None else {}
            given = trimmed = 0
            for tool in catalog.tools:
                if not access.tool_allowed(self.uid, item["id"], tool["name"]):   # закрыт группами доступа
                    continue
                hint, refusal = self._offered(item, tool, rules, web)
                # загрузка файлов «с подтверждением» — только явно включённая, как в окне MCP (mcp_files.CONFIRM)
                gated = tool["name"] == UPLOAD and hint["confirm"] is not None and tool["name"] not in on.get(item["id"], ())
                if tool["name"] in off.get(item["id"], ()) or refusal is not None or gated:
                    continue
                if len(self.calls) >= cap:
                    trimmed += 1
                    continue
                name = f"mcp_{item['id']}_{len(self.calls)}"
                self.calls[name] = (session, tool["name"], item["name"], tool)
                if hint["open_world"]:
                    self.open_world.add(name)
                self.specs.append({"type": "function", "function": {"name": name,
                    "description": (f"{item['name']}: {tool['name']}. " + str(tool.get("description", "")))[:4000],
                    "parameters": tool["inputSchema"]}})
                given += 1
            if trimmed or catalog.trimmed:
                self.notes.append((item["name"], TRIMMED.format(cap if trimmed else MAX_TOOLS)))
            if given:
                self.instructions.append((item["name"], catalog.instructions))

    async def call(self, name, arguments):
        """Вызов инструмента. Перед ним — заново: сервер в действующем наборе диалога, инструмент не выключен,
        открыт группами, веб-инструмент — при включённом «Интернете»; иначе шаг-ошибка без запроса к MCP.
        Подтверждение вызова спрашивает ход до него (`chat.StreamTask._calls`). Знакомство с сервером — здесь, при
        первом вызове."""
        session, tool, server, spec = self.calls[name]
        step = {"kind": "mcp", "server": server, "tool": tool}
        try:
            chat = self._chat()
            if chat is None or session.sid not in mcp_sources.dialog_servers(self.uid, chat, self.conn):
                raise refused("MCP выключен в этом диалоге")
            off, _ = mcp_sources.tool_state(chat["mcp_tools_off"])
            if tool in off.get(session.sid, ()):
                raise refused(TOOL_OFF)
            if not access.tool_allowed(self.uid, session.sid, tool):   # правило могли сменить посреди хода
                raise refused("Нет доступа к инструменту MCP")
            item = {"id": session.sid, "user_id": session.owner_id}
            rules = mcp_sources.policy(session.sid) if session.owner_id is None else {}
            refusal = self._offered(item, spec, rules, bool(chat["web"]) and not chat["protected_used"]
                                    and cleaner_route.web_allowed(self.conn))[1]
            if refusal is not None:
                raise refused(refusal)
            args = json.loads(arguments) if arguments.strip() else {}   # инструмент без параметров: модели шлют ""
            if not isinstance(args, dict):
                raise refused("Неверные аргументы инструмента MCP")
            await session.ensure()
            self.reached.add(session.sid)     # дальше — ответ сервера, и ошибка тоже (`reached_protected`)
            try:
                result = await session.rpc("tools/call", {"name": tool, "arguments": args})
            except HTTPException:
                if _unknown_tool(session.error):
                    mcp_sources.forget(session.sid)       # каталог устарел: следующий ход спросит список заново
                raise
            text = "\n".join(block["text"] for block in result.get("content", [])
                             if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
            if result.get("isError") and _unknown_tool(None, text):
                mcp_sources.forget(session.sid)
            if result.get("structuredContent") is not None:
                text += "\n" + json.dumps(result["structuredContent"], ensure_ascii=True)
            if not text:
                text = "Инструмент MCP вернул результат без текста"
            step["error" if result.get("isError") else "text"] = text[:60_000]
            import mcp_files                  # здесь, не при загрузке: mcp_files тянет разделы и chat
            files = [] if result.get("isError") else mcp_files.result_files(result)
            if not files and not result.get("isError"):
                from mcp_contracts import features
                catalog = next((found for _, srv, found in self.found if srv is session), None)
                entry = mcp_files.document_result(tool, text)
                if entry is not None and catalog is not None and features(catalog).files:
                    files = [entry]
            if files:                         # файлы под ответом: маршрут скачивания сверит сервер и отпечаток
                step |= {"sid": session.sid, "stamp": mcp_sources.stamp(session.stamp), "files": files}
        except HTTPException as exc:
            step["error"] = exc.detail["message"]
        except (ValueError, TypeError):
            step["error"] = "Неверные аргументы или результат MCP"
        return step

    async def close(self):
        await asyncio.gather(*(session.close() for session in self.sessions))


async def web_tools(uid, row, conn) -> bool:
    """Даст ли ход диалога `row` (строка `chats`) при «Интернете» хоть один веб-инструмент MCP (пометка
    «интернет»): отбор и каталоги — как у хода (`Tools.discover`), холодный кэш — не дольше
    `mcp_sources.DEFAULT_WAIT_SEC`. Ради них «Интернет» включается и без ключа Exa (`chat.patch_chat`)."""
    tools = Tools(uid, row["id"], mcp_sources.dialog_servers(uid, row, conn), conn=conn)
    try:
        await tools.discover(web=True)
        return bool(tools.open_world)
    finally:
        await tools.close()
