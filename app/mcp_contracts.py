"""Контракты Studio для любых MCP-серверов (docs/architecture/mcp-contracts.md).

Сервер объявляет их в `initialize` — `capabilities.experimental`, значение каждого ключа — объект с `version`:
`studio/files` («Мои файлы»), `studio/user-files` (файлы пользователя для инструментов сервера, список — в `files`)
и `studio/status` (состояние источника). Маршруты контрактов — HTTP на хосте сервера (`mcp_sections.endpoint`) с
теми же заголовками, что у JSON-RPC: ключ сервера и `X-Studio-User` общего сервера. Доступ к серверу — как у
остального MCP (`mcp_client.row`: группы доступа, своё и общее).

Содержимое файлов пользователя Studio не хранит, модели не передаёт и в журнал не пишет: в журнал — только id
сервера и размер.
"""

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
import httpx

import access
import clock
import forms
import mcp_client as mcp
import mcp_sections
import mcp_sources
import upstream

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/mcp")

STATUS_TTL_SEC = 15                          # срок состояния сервера для одной учётки
STATUS_SEC = 5                               # срок ответа `/status`
STATUS_KEEP = 512                            # записей кэша состояния; лишние — старейшие
USER_FILE_MAX = 1024 * 1024                  # потолок файла пользователя поверх `max_bytes` контракта
USER_FILES_MAX = 16                          # файлов пользователя у одного сервера
REPLY_MAX = 64 * 1024                        # JSON-ответ маршрута контракта
TITLE_MAX = 120
ACCEPT_MAX = 200
MESSAGE_MAX = 200
QUEUE_MAX = 10**6
STATES = ("ok", "busy", "down")
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
STATUS_FAILED = "Состояние сервера не получено"

STATUS: dict[tuple, tuple[float, dict]] = {}


@dataclass(frozen=True)
class Features:
    files: dict | None                       # `studio/files`: {max_upload, chunk} или None
    user_files: list = field(default_factory=list)   # `studio/user-files`: [{key, title, accept, max_bytes}]
    status: bool = False                     # `studio/status`


def _contract(experimental, name):
    value = experimental.get(name)
    version = value.get("version") if isinstance(value, dict) else None
    return value if type(version) is int and version == 1 else None


def _size(value):
    return type(value) is int and value > 0


def _text(value, limit):
    return "".join(c for c in value if c.isprintable()).strip()[:limit] if isinstance(value, str) else ""


def features(catalog: mcp_sources.Catalog) -> Features:
    """Контракты сервера из `capabilities.experimental` каталога. Неизвестная версия, неверное значение или
    неверная запись — как не объявленные; предел файла пользователя — не больше USER_FILE_MAX."""
    capabilities = catalog.capabilities if isinstance(catalog.capabilities, dict) else {}
    experimental = capabilities.get("experimental")
    if not isinstance(experimental, dict):
        return Features(None, [], False)
    files = _contract(experimental, "studio/files")
    files = ({"max_upload": files["max_upload"], "chunk": files["chunk"]}
             if files and _size(files.get("max_upload")) and _size(files.get("chunk")) else None)
    listed = _contract(experimental, "studio/user-files")
    raw = listed.get("files") if listed else None
    user_files, keys = [], set()
    for item in raw if isinstance(raw, list) else []:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str) or not KEY.fullmatch(item["key"])
                or item["key"] in keys or not _size(item.get("max_bytes"))):
            continue
        keys.add(item["key"])
        user_files.append({"key": item["key"], "title": _text(item.get("title"), TITLE_MAX) or item["key"],
                           "accept": _text(item.get("accept"), ACCEPT_MAX),
                           "max_bytes": min(item["max_bytes"], USER_FILE_MAX)})
        if len(user_files) >= USER_FILES_MAX:
            break
    return Features(files, user_files, _contract(experimental, "studio/status") is not None)


async def server_features(session) -> Features:
    """Контракты сервера сессии по каталогу (кэш `mcp_sources`)."""
    try:
        return features(await mcp_sources.catalog(session, mcp_sources.LIST_SEC))
    except TimeoutError:
        raise mcp.refused("MCP не ответил вовремя") from None


async def call(session, method, path, *, content=None, timeout=mcp.TIMEOUT):
    """Маршрут контракта на сервере: (код HTTP, разобранный JSON или None). Тело ответа — не больше REPLY_MAX."""
    headers = session.headers(rpc=False)
    headers["Accept"] = "application/json"
    if content is not None:
        headers["Content-Type"] = "application/octet-stream"
    data = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async with upstream.stream(SimpleNamespace(id=session.sid), method, mcp_sections.endpoint(session, path),
                                       keyless=True, headers=headers, content=content, timeout=timeout) as response:
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > REPLY_MAX:
                        raise mcp.refused("Ответ MCP слишком большой")
                code = response.status_code
    except upstream.UpstreamRefused:
        raise mcp.refused("Адрес MCP недоступен по правилам разрешённых адресов") from None
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise mcp.refused("MCP недоступен или вернул неверный ответ") from None
    try:
        return code, json.loads(data) if data else None
    except ValueError:
        return code, None


def _failed(code, body):
    """Отказ сервера контракта — тем же кодом для 404 и 413, остальное — 409; слова сервера, если он их дал."""
    words = _text(body.get("message"), MESSAGE_MAX) if isinstance(body, dict) else ""
    if code == 404:
        return mcp.refused(words or "Файла нет на сервере MCP", "not_found", 404)
    if code == 413:
        return mcp.refused(words or "Файл больше предела сервера MCP", "too_big", 413)
    return mcp.refused(words or f"MCP ответил HTTP {code}")


def _session(request, sid):
    uid = request.state.user.id
    return mcp.Session(mcp.row(uid, sid), uid=uid)


def _item(spec, reply):
    reply = reply if isinstance(reply, dict) else {}
    size, mtime = reply.get("size"), reply.get("mtime")
    return {**spec, "present": reply.get("present") is True,
            "size": size if type(size) is int and size >= 0 else 0,
            "mtime": mtime if type(mtime) in (int, float) and mtime >= 0 else None}


async def _declared(session, key):
    spec = next((item for item in (await server_features(session)).user_files if item["key"] == key), None)
    if spec is None:
        raise mcp.refused("Такого файла у сервера MCP нет", "not_found", 404)
    return spec


# ---- файлы пользователя (`studio/user-files`) -----------------------------------------------------------

@router.get("/{sid}/user-files")
async def user_files(sid: int, request: Request):
    """Файлы пользователя, объявленные сервером, и есть ли они в его личной папке; без контракта — пусто."""
    session = _session(request, sid)
    try:
        declared = (await server_features(session)).user_files
        if not declared:
            return {"items": []}
        code, body = await call(session, "GET", "/user-files")
        if code != 200:
            raise _failed(code, body)
        found = {item["key"]: item for item in body if isinstance(item, dict) and isinstance(item.get("key"), str)
                 } if isinstance(body, list) else {}
        return {"items": [_item(spec, found.get(spec["key"])) for spec in declared]}
    finally:
        await session.close()


@router.put("/{sid}/user-files/{key}")
async def put_user_file(sid: int, key: str, request: Request):
    """Тело запроса — файл на место файла пользователя `key`: не больше `max_bytes` контракта (и USER_FILE_MAX)."""
    session = _session(request, sid)
    try:
        spec = await _declared(session, key)
        data = await forms.read_body(request, spec["max_bytes"])
        if not data:
            raise mcp.refused("Выберите непустой файл", "invalid", 422)
        code, body = await call(session, "PUT", f"/user-files/{key}", content=data)
        if code != 200:
            raise _failed(code, body)
        log.info("mcp %d: файл пользователя загружен, %d байт", sid, len(data))
        return _item(spec, body)
    finally:
        await session.close()


@router.delete("/{sid}/user-files/{key}")
async def delete_user_file(sid: int, key: str, request: Request):
    session = _session(request, sid)
    try:
        spec = await _declared(session, key)
        code, body = await call(session, "DELETE", f"/user-files/{key}")
        if code != 200:
            raise _failed(code, body)
        log.info("mcp %d: файл пользователя убран", sid)
        return _item(spec, body)
    finally:
        await session.close()


# ---- состояние источника (`studio/status`) --------------------------------------------------------------

def _user(session):
    """Чьё состояние: общий сервер различает учётки по `X-Studio-User`, личный принадлежит своей учётке."""
    return access.studio_user(session.uid, session.sid) if session.owner_id is None else f"user:{session.uid}"


def _state(body):
    if not isinstance(body, dict) or body.get("state") not in STATES:
        return None
    queue = body.get("queue")
    return {"state": body["state"], "queue": queue if type(queue) is int and 0 <= queue <= QUEUE_MAX else 0,
            "message": _text(body.get("message"), MESSAGE_MAX)}


async def _status_of(item, uid):
    session = mcp.Session(item, uid=uid)
    try:
        key = (item["id"], _user(session))
        hit = STATUS.get(key)
        if hit is not None and clock.NOW() - hit[0] < STATUS_TTL_SEC:
            return hit[1]
        try:
            if not (await server_features(session)).status:
                return None
        except HTTPException:
            return None
        try:
            code, body = await call(session, "GET", "/status", timeout=STATUS_SEC)
            value = _state(body) if code == 200 else None
        except HTTPException:
            value = None
        value = value or {"state": "down", "queue": 0, "message": STATUS_FAILED}
        STATUS.pop(key, None)
        STATUS[key] = (clock.NOW(), value)
        while len(STATUS) > STATUS_KEEP:
            del STATUS[next(iter(STATUS))]
        return value
    finally:
        await session.close()


@router.get("/status")
async def status(request: Request):
    """{"<sid>": {state, queue, message}} включённых серверов учётки, объявивших `studio/status`."""
    uid = request.state.user.id
    items = mcp.servers(uid, enabled_only=True)
    found = await asyncio.gather(*(_status_of(item, uid) for item in items))
    return {str(item["id"]): value for item, value in zip(items, found) if value is not None}
