"""«Мои файлы» на серверах MCP с контрактом `studio/files` (docs/architecture/mcp-contracts.md) и личная ссылка на
файл на сутки.

Маршруты `/api/my-files` — прокси на HTTP-маршруты контракта (`mcp_sections.endpoint`) с заголовками сервера и
`X-Studio-User` учётки (`mcp_client.Session.headers`): общий сервер различает учётки по нему, поэтому каждый видит
только свою папку, администратор — тоже свою. Доступ к серверу — как у остального MCP (`mcp_client.row`: своё,
общее, группы доступа). Скачивание и ZIP — по ticket (шифр `STUDIO_DATA_KEY`: учётка, сервер, подпись его адреса и
ключа, срок TICKET_SEC), потоком с Range; загрузка — кусками не больше CHUNK.

Ссылка (`file_links`): токен `secrets.token_urlsafe(32)` только во фрагменте адреса `/f#<token>`, в базе — sha256,
путь и имя — шифром, срок LINK_SEC. Анонимный вход `/f` со своей защитой: тело POST ≤ `chat_shares.PUBLIC_BODY_MAX`
(`chat_shares.public_guard`), чужой `Origin` — 403, все отказы — одинаковый 404 без имени файла, неудачи —
`email_accounts.throttle` по адресу (FAILS за FAIL_SEC, потом 429 `file_locked`). `POST /f/open` выдаёт cookie
`studio_file` (OPEN_SEC, только `/f/file`) со случайным id в памяти процесса; каждый запрос файла проверяет ссылку
заново. В журнал — только id и числа: путей, имён и токенов там нет.
"""
import asyncio
import base64
from contextlib import AsyncExitStack, asynccontextmanager
import json
import logging
import math
import re
import secrets
from types import SimpleNamespace
from urllib.parse import unquote
import uuid

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import httpx
from pydantic import BaseModel, Field

import auth
import chat_shares
import clock
import email_accounts
import forms
import mcp_client as mcp
import mcp_contracts
import mcp_sections
import mcp_sources
import media
import security
import state
import upstream

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/my-files")
public = APIRouter()

LINK_SEC = 86400                              # срок личной ссылки на файл
CHUNK = 32 * 1024 * 1024                      # кусок загрузки (Apache: на 1 МиБ больше)
OPEN_SEC = 600                                # cookie `studio_file` после `POST /f/open`
TICKET_SEC = 3 * 3600                         # ticket скачивания и ZIP: докачка браузером
TICKET_MAX = 8192
PATH_MAX = 1024
NAME_MAX = 255
LIST_MAX = 4 * 1024 * 1024                    # JSON списка папки
ENTRIES_MAX = 10_000
LINKS_MAX = 100                               # действующих ссылок учётки
OPENED_MAX = 1024
STORAGE_SEC = 5                               # опрос объёма для «Хранилища»
FAILS = 30                                    # неудач `/f` с адреса за FAIL_SEC
FAIL_SEC = 900
COOKIE = "studio_file"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
ID_RE = re.compile(r"[0-9a-f]{32}")
RANGE_RE = re.compile(r"bytes (\d{1,15})-(\d{1,15})/(\d{1,15})")
EMPTY_RANGE = "bytes */0"
GONE = "Ссылка недоступна"
UNAVAILABLE = "Файл недоступен"

#: id cookie `studio_file` → (id ссылки, срок)
OPENED: dict[str, tuple[str, float]] = {}


class PathIn(BaseModel):
    path: str = Field(default="", max_length=PATH_MAX)


class IdIn(BaseModel):
    id: str = Field(pattern=ID_RE.pattern, max_length=32)


class TokenIn(BaseModel):
    token: str = Field(max_length=128)


def _not_found(message=UNAVAILABLE):
    return mcp.refused(message, "not_found", 404)


def _path(value, *, root=False):
    """Путь от личной папки через `/`: без `..`, `\\` и управляющих; пустой — сама папка (только `root`)."""
    if not isinstance(value, str) or len(value) > PATH_MAX or "\\" in value or not value.isprintable():
        raise mcp.refused("Неверный путь", "invalid", 422)
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if ".." in parts or (not parts and not root):
        raise mcp.refused("Неверный путь", "invalid", 422)
    return "/".join(parts)


def _b64(path):
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def _name(value):
    return value if isinstance(value, str) and 0 < len(value) <= NAME_MAX and value.isprintable() and "/" not in value \
        else None


def _size(value):
    return value if type(value) is int and value >= 0 else 0


async def _features(item, uid):
    """(Session, контракт `studio/files`) включённого сервера учётки; без контракта — 404."""
    if not item["enabled"]:
        raise _not_found("Подключение MCP выключено")
    session = mcp.Session(item, uid=uid)
    try:
        files = (await mcp_contracts.server_features(session)).files
    except BaseException:
        await session.close()
        raise
    if files is None:
        await session.close()
        raise _not_found("У сервера MCP нет «Моих файлов»")
    return session, files


@asynccontextmanager
async def _server(request, sid):
    uid = request.state.user.id
    session, files = await _features(mcp.row(uid, sid), uid)
    try:
        yield session, files
    finally:
        await session.close()


async def _post(session, route, body, reply_max=mcp_contracts.REPLY_MAX, timeout=mcp.TIMEOUT):
    code, reply = await mcp_contracts.call(session, "POST", f"/files/{route}", content=json.dumps(body).encode(),
                                           content_type="application/json", reply_max=reply_max, timeout=timeout)
    if code != 200:
        raise mcp_contracts._failed(code, reply)
    return reply if isinstance(reply, (dict, list)) else {}


def _listing(reply, path):
    reply = reply if isinstance(reply, dict) else {}
    entries = []
    for entry in reply.get("entries") if isinstance(reply.get("entries"), list) else []:
        name = _name(entry.get("name")) if isinstance(entry, dict) else None
        if name is None:
            continue
        mtime = entry.get("mtime")
        entries.append({"name": name, "dir": entry.get("dir") is True, "size": _size(entry.get("size")),
                        "mtime": mtime if type(mtime) in (int, float) and mtime >= 0 else None})
        if len(entries) >= ENTRIES_MAX:
            break
    return {"path": path, "total_bytes": _size(reply.get("total_bytes")), "entries": entries}


async def _entry(session, path):
    """Запись файла `path` из списка его папки (имя, размер); нет или папка — 404."""
    parent, _, name = path.rpartition("/")
    found = _listing(await _post(session, "list", {"path": parent}, LIST_MAX), parent)
    entry = next((e for e in found["entries"] if e["name"] == name and not e["dir"]), None)
    if entry is None:
        raise _not_found("Файла нет на сервере MCP")
    return entry


# ---- ticket скачивания ---------------------------------------------------------------------------------------

def _ticket(session, kind, value):
    raw = json.dumps([session.uid, session.sid, mcp_sources.stamp(session.stamp), kind, clock.NOW(), value],
                     ensure_ascii=False)
    return f"/api/my-files/{session.sid}/download/{security.encrypt(raw).decode()}"


def _redeem(request, sid, encoded):
    uid = request.state.user.id
    try:
        if len(encoded) > TICKET_MAX:
            raise ValueError
        owner, server, stamp, kind, issued, value = json.loads(security.decrypt(encoded.encode()))
        if owner != uid or server != sid or kind not in ("file", "zip") or not 0 <= clock.NOW() - issued <= TICKET_SEC:
            raise ValueError
        item = mcp.row(uid, sid)
        if not item["enabled"] or stamp != mcp_sources.stamp(item["secret_enc"]):
            raise ValueError
    except (InvalidToken, TypeError, ValueError, HTTPException):
        raise _not_found() from None
    return mcp.Session(item, uid=uid), kind, value


async def _stream(session, route, extra, request, name, missing):
    """Файл сервера потоком через Studio: Range, всегда вложением (`application/octet-stream`, `nosniff`,
    песочница CSP). Сервер ответил 404 — `missing()`."""
    headers = {**session.headers(rpc=False), **extra, "Accept-Encoding": "identity"}
    value = request.headers.get("range", "")
    if value:
        if len(value) > 64 or not media.RANGE_RE.fullmatch(value):
            return Response(status_code=416)
        headers["Range"] = value
    async with AsyncExitStack() as stack:
        stack.push_async_callback(session.close)
        try:
            response = await stack.enter_async_context(upstream.stream(
                SimpleNamespace(id=session.sid), "GET", mcp_sections.endpoint(session, route), keyless=True,
                headers=headers, timeout=mcp.TIMEOUT))
        except (httpx.HTTPError, upstream.UpstreamRefused, ValueError):
            raise mcp.refused("Файл MCP недоступен") from None
        outgoing = {key: response.headers[key] for key in ("content-length", "content-range", "accept-ranges")
                    if key in response.headers}
        if response.status_code == 416:
            return Response(status_code=416, headers={k: v for k, v in outgoing.items() if k != "content-length"})
        if response.status_code == 404:
            raise missing()
        length = outgoing.get("content-length", "")
        if (response.status_code not in (200, 206) or response.headers.get("content-encoding", "identity") != "identity"
                or (length and not re.fullmatch(r"[0-9]{1,15}", length))):
            raise mcp.refused("Файл MCP недоступен")
        outgoing.update({"X-Content-Type-Options": "nosniff", "Content-Disposition": media.disposition(name),
                         "Content-Security-Policy": "default-src 'none'; sandbox"})

        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except httpx.HTTPError:
                raise ConnectionError("MCP file transfer was interrupted") from None

        return mcp_sections.ArtifactStream(stack.pop_all(), chunks(), media_type="application/octet-stream",
                                           status_code=response.status_code, headers=outgoing)


# ---- «Мои файлы» ---------------------------------------------------------------------------------------------

async def _declared(uid):
    """[(сервер, контракт)] включённых серверов учётки с `studio/files`; недоступные пропускаются."""
    async def one(item):
        try:
            session, files = await _features(item, uid)
        except Exception:                        # сбой одного сервера не роняет ответ; текст исключения — никуда
            return None
        await session.close()
        return item, files

    found = await asyncio.gather(*(one(item) for item in mcp.servers(uid, enabled_only=True)))
    return [pair for pair in found if pair is not None]


@router.get("/servers")
async def servers(request: Request):
    """[{sid, name, max_upload, chunk}] серверов учётки с «Моими файлами»; кусок — не больше CHUNK."""
    return [{"sid": item["id"], "name": item["name"], "max_upload": files["max_upload"],
             "chunk": min(files["chunk"], CHUNK)} for item, files in await _declared(request.state.user.id)]


async def storage(uid) -> list:
    """«Хранилище»: [{sid, name, bytes}] — `total_bytes` личной папки на каждом сервере с «Моими файлами»."""
    async def one(item):
        session = mcp.Session(item, uid=uid)
        try:
            reply = await _post(session, "list", {"path": ""}, LIST_MAX, timeout=STORAGE_SEC)
        except Exception:
            return None
        finally:
            await session.close()
        return {"sid": item["id"], "name": item["name"], "bytes": _listing(reply, "")["total_bytes"]}

    found = await asyncio.gather(*(one(item) for item, _ in await _declared(uid)))
    return [row for row in found if row is not None]


@router.post("/{sid}/list")
async def list_files(sid: int, body: PathIn, request: Request):
    path = _path(body.path, root=True)
    async with _server(request, sid) as (session, _):
        return _listing(await _post(session, "list", {"path": path}, LIST_MAX), path)


@router.post("/{sid}/ticket")
async def ticket(sid: int, body: PathIn, request: Request):
    """Адрес скачивания файла (`GET /api/my-files/{sid}/download/{ticket}`, Range)."""
    path = _path(body.path)
    async with _server(request, sid) as (session, _):
        return {"url": _ticket(session, "file", path)}


@router.get("/{sid}/download/{encoded}")
async def download(sid: int, encoded: str, request: Request):
    session, kind, value = _redeem(request, sid, encoded)
    if kind == "file" and isinstance(value, str):
        route, extra, name = "/files/content", {"X-Studio-Path": _b64(value)}, value.rpartition("/")[2]
    elif kind == "zip" and isinstance(value, list) and len(value) == 2 and ID_RE.fullmatch(str(value[0])):
        route, extra, name = f"/files/zip/{value[0]}", {}, str(value[1])
    else:
        await session.close()
        raise _not_found()
    return await _stream(session, route, extra, request, name, _not_found)


@router.put("/{sid}/upload")
async def upload(sid: int, request: Request):
    """Кусок файла: путь — `X-Studio-Name` (encodeURIComponent), `Content-Range: bytes a-b/total`, тело — кусок
    не больше CHUNK. Кусок дальше принятого — 409 `gap` с `size`, с которого продолжить."""
    path = _path(unquote(request.headers.get("x-studio-name", "")))
    header = request.headers.get("content-range", "")
    found = RANGE_RE.fullmatch(header)
    if header == EMPTY_RANGE:
        start, end, total = 0, -1, 0
    elif found:
        start, end, total = map(int, found.groups())
        if not start <= end < total:
            raise mcp.refused("Неверный Content-Range", "invalid", 422)
    else:
        raise mcp.refused("Нужен Content-Range: bytes a-b/total", "invalid", 422)
    if end - start + 1 > CHUNK:
        raise mcp.refused("Кусок больше предела", "too_big", 413)
    data = await forms.read_body(request, CHUNK)
    if len(data) != end - start + 1:
        raise mcp.refused("Длина куска не совпадает с Content-Range", "invalid", 422)
    async with _server(request, sid) as (session, files):
        if total > files["max_upload"]:
            raise mcp.refused("Файл больше предела сервера MCP", "too_big", 413)
        code, reply = await mcp_contracts.call(
            session, "PUT", "/files/upload", content=data, headers={"X-Studio-Path": _b64(path), "Content-Range": header},
            timeout=mcp.TIMEOUT + math.ceil(len(data) / (512 << 10)))
    if code == 409 and isinstance(reply, dict) and type(reply.get("size")) is int:
        raise HTTPException(409, {"code": "gap", "message": "Продолжите загрузку с принятого места",
                                  "size": _size(reply["size"])})
    if code != 200:
        raise mcp_contracts._failed(code, reply)
    reply = reply if isinstance(reply, dict) else {}
    return {"done": reply.get("done") is True, "size": _size(reply.get("size"))}


@router.post("/{sid}/mkdir")
async def mkdir(sid: int, body: PathIn, request: Request):
    async with _server(request, sid) as (session, _):
        reply = await _post(session, "mkdir", {"path": _path(body.path)})
    return {"path": reply.get("path") if isinstance(reply.get("path"), str) else ""}


@router.post("/{sid}/delete")
async def delete(sid: int, body: PathIn, request: Request):
    """В корзину сервера: `{id}` записи корзины."""
    async with _server(request, sid) as (session, _):
        reply = await _post(session, "delete", {"path": _path(body.path)})
    return {"id": reply.get("id") if isinstance(reply.get("id"), str) else ""}


@router.post("/{sid}/trash")
async def trash(sid: int, request: Request):
    async with _server(request, sid) as (session, _):
        reply = await _post(session, "trash", {}, LIST_MAX)
    return [{"id": item["id"], "path": item["path"], "deleted": item.get("deleted"), "size": _size(item.get("size"))}
            for item in (reply if isinstance(reply, list) else [])[:ENTRIES_MAX]
            if isinstance(item, dict) and isinstance(item.get("id"), str) and ID_RE.fullmatch(item["id"])
            and isinstance(item.get("path"), str)]


@router.post("/{sid}/restore")
async def restore(sid: int, body: IdIn, request: Request):
    async with _server(request, sid) as (session, _):
        reply = await _post(session, "restore", {"id": body.id})
    return {"path": reply.get("path") if isinstance(reply.get("path"), str) else ""}


@router.post("/{sid}/zip")
async def zip_files(sid: int, body: PathIn, request: Request):
    """ZIP папки или файла на сервере → `{url, size}`: адрес скачивания по ticket (Range — докачка)."""
    path = _path(body.path, root=True)
    async with _server(request, sid) as (session, _):
        reply = await _post(session, "zip", {"path": path}, timeout=mcp.TIMEOUT * 10)
        zid = reply.get("id")
        if not isinstance(zid, str) or not ID_RE.fullmatch(zid):
            raise mcp.refused("MCP не вернул архив")
        name = (path.rpartition("/")[2] or "files") + ".zip"
        return {"url": _ticket(session, "zip", [zid, name]), "size": _size(reply.get("size"))}


# ---- личные ссылки -------------------------------------------------------------------------------------------

def _expire():
    state.DB.execute("DELETE FROM file_links WHERE expires <= ?", (clock.NOW(),))


def _plain(value):
    try:
        return security.decrypt(value.encode())
    except (InvalidToken, ValueError):
        return None


@router.post("/{sid}/links")
async def create_link(sid: int, body: PathIn, request: Request):
    """Ссылка на файл на LINK_SEC: `{id, url: "https://<хост>/f#<token>", expires}`."""
    uid = request.state.user.id
    path = _path(body.path)
    async with _server(request, sid) as (session, _):
        entry = await _entry(session, path)
        stamp = mcp_sources.stamp(session.stamp)
    _expire()
    active = state.DB.execute("SELECT COUNT(*) FROM file_links WHERE user_id = ? AND revoked IS NULL",
                              (uid,)).fetchone()[0]
    if active >= LINKS_MAX:
        raise mcp.refused("Слишком много действующих ссылок — отзовите ненужные", "links", 409)
    token, lid, now = secrets.token_urlsafe(32), uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO file_links(id, token_hash, user_id, server_id, stamp, path_enc, name_enc, size,"
                     " created, expires) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (lid, security.digest(token), uid, sid, stamp, security.encrypt(path).decode(),
                      security.encrypt(entry["name"]).decode(), entry["size"], now, now + LINK_SEC))
    log.info("пользователь %d: ссылка на файл %s, mcp %d", uid, lid[:8], sid)
    origin = auth.request_origin(request) or state.CFG.origin.rstrip("/")
    return {"id": lid, "url": f"{origin}/f#{token}", "expires": now + LINK_SEC}


@router.get("/links")
async def links(request: Request):
    """Свои действующие ссылки, новые первыми."""
    _expire()
    rows = state.DB.execute("SELECT l.*, s.name AS server FROM file_links l JOIN mcp_servers s ON s.id = l.server_id"
                            " WHERE l.user_id = ? AND l.revoked IS NULL ORDER BY l.created DESC",
                            (request.state.user.id,)).fetchall()
    return [{"id": row["id"], "sid": row["server_id"], "server": row["server"], "name": _plain(row["name_enc"]) or "",
             "size": row["size"], "created": row["created"], "expires": row["expires"]} for row in rows]


@router.delete("/links/{lid}", status_code=204)
async def revoke(lid: str, request: Request):
    uid = request.state.user.id
    done = state.DB.execute("UPDATE file_links SET revoked = ? WHERE id = ? AND user_id = ? AND revoked IS NULL"
                            " AND expires > ?", (clock.NOW(), lid, uid, clock.NOW())).rowcount
    if not done:
        raise _not_found("Ссылки нет")
    for key in [key for key, (link, _) in OPENED.items() if link == lid]:
        OPENED.pop(key, None)
    log.info("пользователь %d: ссылка на файл %s отозвана", uid, lid[:8])
    return Response(status_code=204)


# ---- анонимный вход `/f` -------------------------------------------------------------------------------------

def _gone():
    return HTTPException(404, {"code": "file_gone", "message": GONE}, headers=chat_shares.PUBLIC_HEADERS)


def _ip(request):
    return security.digest(auth.client_ip(request))


def _attempt(request):
    """Попытка резервируется до ожидания (как у `email_accounts.throttle`); удача её возвращает."""
    try:
        email_accounts.throttle("file_link", _ip(request), FAILS, FAIL_SEC)
    except HTTPException:
        raise HTTPException(429, {"code": "file_locked", "message": "слишком много попыток — подождите 15 минут"},
                            headers=chat_shares.PUBLIC_HEADERS) from None


def _live(clause, value):
    return state.DB.execute(f"SELECT l.* FROM file_links l JOIN users u ON u.id = l.user_id WHERE {clause} = ?"
                            " AND l.revoked IS NULL AND l.expires > ? AND u.disabled = 0",
                            (value, clock.NOW())).fetchone()


def _resolve(row):
    """(Session владельца, путь, имя) действующей ссылки: сервер открыт владельцу, включён, адрес и ключ — те
    же (`stamp`); иначе `_gone`."""
    if row is None:
        raise _gone()
    try:
        item = mcp.row(row["user_id"], row["server_id"])
    except HTTPException:
        raise _gone() from None
    path, name = _plain(row["path_enc"]), _plain(row["name_enc"])
    if not item["enabled"] or mcp_sources.stamp(item["secret_enc"]) != row["stamp"] or not path or not name:
        raise _gone()
    return mcp.Session(item, uid=row["user_id"]), path, name


async def _verified(body, request):
    chat_shares._same_origin(request)
    _attempt(request)
    if not TOKEN_RE.fullmatch(body.token):
        raise _gone()
    row = _live("l.token_hash", security.digest(body.token))
    session, path, name = _resolve(row)
    try:
        entry = await _entry(session, path)
    except HTTPException:
        raise _gone() from None
    finally:
        await session.close()
    email_accounts.refund("file_link", _ip(request))
    return row, name, entry


@public.api_route("/f", methods=["GET", "HEAD"])
async def page():
    return HTMLResponse(auth.page("file.html"), headers=chat_shares.PUBLIC_HEADERS)


@public.post("/f/check")
async def check(body: TokenIn, request: Request):
    row, name, entry = await _verified(body, request)
    return JSONResponse({"name": name, "size": entry["size"], "expires": row["expires"]},
                        headers=chat_shares.PUBLIC_HEADERS)


@public.post("/f/open", status_code=204)
async def open_link(body: TokenIn, request: Request):
    row, _, _ = await _verified(body, request)
    now = clock.NOW()
    for key in [key for key, (_, until) in OPENED.items() if until <= now]:
        OPENED.pop(key, None)
    while len(OPENED) >= OPENED_MAX:
        OPENED.pop(next(iter(OPENED)))
    opened = secrets.token_urlsafe(32)
    OPENED[opened] = (row["id"], now + OPEN_SEC)
    log.info("ссылка на файл %s открыта", row["id"][:8])
    response = Response(status_code=204, headers=chat_shares.PUBLIC_HEADERS)
    response.set_cookie(COOKIE, opened, max_age=OPEN_SEC, path="/f/file", secure=True, httponly=True,
                        samesite="strict")
    return response


@public.get("/f/file")
async def link_file(request: Request):
    held = OPENED.get(request.cookies.get(COOKIE, ""))
    if held is None or held[1] <= clock.NOW():
        raise _gone()
    session, path, name = _resolve(_live("l.id", held[0]))
    return await _stream(session, "/files/content", {"X-Studio-Path": _b64(path)}, request, name, _gone)
