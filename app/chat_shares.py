"""PIN-protected, revocable chat snapshots. Public secrets travel only in request bodies.

The link token lives in the URL fragment (never Apache's request log). A successful
PIN check grants a separate, expiring read token, never a Studio account session.
"""
import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import auth
import chat
import claude_code
import clock
import media
import lock_policy
import operations
import security
import state

router = APIRouter()
PIN_FAILS = 5
IP_ATTEMPTS = 30
LOCK_SEC = 900
VIEW_SEC = 8 * 3600
MAX_MESSAGES = 10000
MAX_SNAPSHOT = 16 * 1024 * 1024
PUBLIC_BODY_MAX = 4096
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                  "X-Robots-Tag": "noindex, nofollow, noarchive"}


async def public_guard(request: Request, call_next):
    """Bound anonymous JSON before FastAPI parses it, including chunked requests."""
    if request.method == "POST" and request.url.path.startswith("/share/"):
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > PUBLIC_BODY_MAX:
                return JSONResponse({"detail": {"code": "too_big", "message": "слишком большой запрос"}},
                                    status_code=413, headers=PUBLIC_HEADERS)
            body.extend(block)
        request._body = bytes(body)
    return await call_next(request)


class ShareIn(BaseModel):
    pin: str = Field(strict=True, pattern=r"^[0-9]{4}$", min_length=4, max_length=4)
    title: str = Field(default="Чат", max_length=200)


class UnlockIn(BaseModel):
    token: str = Field(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$", max_length=43)
    pin: str = Field(strict=True, pattern=r"^[0-9]{4}$", min_length=4, max_length=4)


class ReadIn(BaseModel):
    access: str = Field(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$", max_length=43)


class FileIn(ReadIn):
    file: str = Field(pattern=r"^[0-9a-f]{32}$", max_length=32)


def _refuse(status=404, code="share_gone", message="ссылка недоступна или отключена"):
    return HTTPException(status, {"code": code, "message": message}, headers=PUBLIC_HEADERS)


def _owner_open(request: Request, section: str) -> None:
    if (auth._live(request.cookies.get(auth.COOKIE), renew=False) is None
            or lock_policy.locked(request.state.digest, section)):
        raise _refuse(409, "closed", "сеанс Студии закрыт")
    operations.check(request.state.user.id)


def _owned(request: Request, *, chat_id=None, agent_id=None, project=None, session_id=None, engine=None):
    return state.DB.execute(
        "SELECT * FROM chat_shares WHERE user_id = ? AND chat_id IS ? AND agent_id IS ?"
        " AND project IS ? AND session_id IS ? AND engine IS ?",
        (request.state.user.id, chat_id, agent_id, project, session_id, engine)).fetchone()


def _out(row) -> dict:
    if row is None:
        return {"enabled": False}
    return {"enabled": True, "url": state.CFG.origin.rstrip("/") + "/share#" + security.decrypt(row["token_enc"]),
            "created": row["created"]}


def _encode(snapshot: dict) -> str:
    try:
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        size = len(raw.encode("utf-8"))
    except (ValueError, UnicodeEncodeError):
        raise _refuse(422, "invalid", "неверные данные переписки") from None
    if size > MAX_SNAPSHOT or len(snapshot["messages"]) > MAX_MESSAGES:
        raise _refuse(413, "share_size", "переписка слишком большая для ссылки")
    return raw


async def _store(request: Request, body: ShareIn, snapshot: dict, expected, files=(), **source):
    raw = _encode(snapshot)
    pin_hash = await asyncio.to_thread(security.hash_password, body.pin)
    if source.get("agent_id"):
        await claude_code.ask(request, source["agent_id"], "history", project=source["project"],
                              chat=source["session_id"], before=None, limit=1)
        claude_code.own_agent(request, source["agent_id"])
    _owner_open(request, "code" if source.get("agent_id") else "chat")
    if source.get("chat_id"):
        chat._chat_row(request.state.user.id, source["chat_id"])
    current = _owned(request, **source)
    if (current["id"] if current else None) != (expected["id"] if expected else None):
        raise _refuse(409, "share_changed", "настройки ссылки изменились — откройте окно заново")
    token, share_id = security.new_token(), uuid.uuid4().hex
    state.DB.execute("BEGIN")
    try:
        if current:
            state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (current["id"],))
        state.DB.execute(
            "INSERT INTO chat_shares(id,user_id,chat_id,agent_id,project,session_id,engine,"
            "token_hash,token_enc,pin_hash,snapshot,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (share_id, request.state.user.id, source.get("chat_id"), source.get("agent_id"),
             source.get("project"), source.get("session_id"), source.get("engine"), security.digest(token),
             security.encrypt(token), pin_hash, raw, clock.NOW()))
        state.DB.executemany("INSERT INTO chat_share_files(share_id,media_id) VALUES(?,?)",
                             [(share_id, mid) for mid in set(files)])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    return _out(_owned(request, **source))


@router.get("/api/chats/{chat_id}/share")
async def chat_share(chat_id: str, request: Request):
    chat._chat_row(request.state.user.id, chat_id)
    return _out(_owned(request, chat_id=chat_id))


def regular_snapshot(user_id: int, chat_id: str) -> tuple[dict, list[str]]:
    row = chat._chat_row(user_id, chat_id)
    atts = chat._attachments(chat_id)
    files, messages = [], []
    for message in chat._messages(chat_id):
        attachments = []
        for a in atts.get(message["id"], []):
            # Missing/deleted source files are never turned into broader media access.
            if state.DB.execute("SELECT 1 FROM media WHERE id = ? AND user_id = ?",
                                (a["media_id"], user_id)).fetchone():
                files.append(a["media_id"])
                attachments.append({"file": a["media_id"], "name": a["name"], "kind": a["kind"]})
        messages.append({"role": message["role"], "text": message["content"], "attachments": attachments,
                         "reasoning": message["reasoning"], "status": message["status"]})
    snapshot = {"title": row["title"] or "Чат", "messages": messages}
    _encode(snapshot)
    return snapshot, files


@router.post("/api/chats/{chat_id}/share", status_code=201)
async def create_chat_share(chat_id: str, body: ShareIn, request: Request):
    expected = _owned(request, chat_id=chat_id)
    snapshot, files = regular_snapshot(request.state.user.id, chat_id)
    return await _store(request, body, snapshot, expected, files, chat_id=chat_id)


@router.delete("/api/chats/{chat_id}/share", status_code=204)
async def revoke_chat_share(chat_id: str, request: Request):
    chat._chat_row(request.state.user.id, chat_id)
    state.DB.execute("DELETE FROM chat_shares WHERE chat_id = ? AND user_id = ?",
                     (chat_id, request.state.user.id))


def _agent_source(request, aid, pid, session_id):
    claude_code.own_agent(request, aid)
    claude_code.check_pid(pid)
    claude_code.check_chat(session_id)
    return {"agent_id": aid, "project": pid, "session_id": session_id,
            "engine": claude_code.chat_engine(request, aid, session_id)}


@router.get("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share")
async def agent_share(aid: int, pid: str, session_id: str, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    await claude_code.ask(request, aid, "history", project=pid, chat=session_id, before=None, limit=1)
    return _out(_owned(request, **source))


async def agent_snapshot(request: Request, aid: int, pid: str, session_id: str, title: str) -> dict:
    _agent_source(request, aid, pid, session_id)
    pages, before, total_size, count = [], None, 0, 0
    while True:
        data = await claude_code.ask(request, aid, "history", project=pid, chat=session_id, before=before, limit=100)
        items, start = data.get("items"), data.get("start")
        if not isinstance(items, list) or type(start) is not int or start < 0 or (before is not None and start >= before):
            raise _refuse(502, "bridge", "мост ответил непонятно")
        total_size += len(json.dumps(items, ensure_ascii=True))
        count += len(items)
        if total_size > MAX_SNAPSHOT or count > MAX_MESSAGES:
            raise _refuse(413, "share_size", "переписка слишком большая для ссылки")
        pages.append(items)
        if start == 0:
            break
        before = start
    messages = []
    for page in reversed(pages):
        for item in page:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                continue
            # Explicit fields: never publish project paths, machine/session metadata or live controls.
            steps, images = item.get("steps"), item.get("images")
            messages.append({"role": item["role"], "text": item.get("text") if isinstance(item.get("text"), str) else "",
                             "steps": [{key: step[key] for key in ("name", "tool", "input", "text", "error") if key in step}
                                       for step in (steps if isinstance(steps, list) else []) if isinstance(step, dict)],
                             "images": [{key: image[key] for key in ("mime", "name", "data") if key in image}
                                        for image in (images if isinstance(images, list) else []) if isinstance(image, dict)]})
    snapshot = {"title": title, "messages": messages}
    _encode(snapshot)
    return snapshot


@router.post("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share", status_code=201)
async def create_agent_share(aid: int, pid: str, session_id: str, body: ShareIn, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    expected = _owned(request, **source)
    snapshot = await agent_snapshot(request, aid, pid, session_id, body.title)
    return await _store(request, body, snapshot, expected, **source)


@router.delete("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share", status_code=204)
async def revoke_agent_share(aid: int, pid: str, session_id: str, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    row = _owned(request, **source)
    if row:
        state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (row["id"],))


@router.api_route("/share", methods=["GET", "HEAD"])
async def share_page():
    return HTMLResponse(auth.page("share.html"), headers=PUBLIC_HEADERS)


def _same_origin(request):
    if auth.foreign_origin(request) or request.headers.get("sec-fetch-site") == "cross-site":
        raise _refuse(403, "origin", "откройте ссылку в браузере")


def _public_share(token_hash):
    row = state.DB.execute("SELECT s.* FROM chat_shares s JOIN users u ON u.id = s.user_id"
                            " WHERE s.token_hash = ? AND u.disabled = 0", (token_hash,)).fetchone()
    if row is None:
        raise _refuse()
    return row


@router.post("/share/unlock")
async def unlock(body: UnlockIn, request: Request):
    _same_origin(request)
    now = clock.NOW()
    state.DB.execute("DELETE FROM chat_share_attempts WHERE until <= ?", (now,))
    state.DB.execute("DELETE FROM chat_share_visitors WHERE expires <= ?", (now,))
    ip_hash = security.digest(auth.client_ip(request))
    state.DB.execute("INSERT INTO chat_share_attempts VALUES(?,1,?) ON CONFLICT(ip_hash)"
                     " DO UPDATE SET attempts = attempts + 1", (ip_hash, now + LOCK_SEC))
    ip = state.DB.execute("SELECT attempts FROM chat_share_attempts WHERE ip_hash = ?", (ip_hash,)).fetchone()
    if ip[0] > IP_ATTEMPTS:
        raise _refuse(429, "share_locked", "слишком много попыток — подождите 15 минут")
    row = _public_share(security.digest(body.token))
    if row["attempt_until"] <= now:
        state.DB.execute("UPDATE chat_shares SET attempts = 0, attempt_until = ? WHERE id = ?",
                         (now + LOCK_SEC, row["id"]))
    elif row["attempts"] >= PIN_FAILS:
        raise _refuse(429, "share_locked", "слишком много попыток — подождите 15 минут")
    # Reserve before awaiting the hash: a concurrent batch cannot bypass the limit.
    state.DB.execute("UPDATE chat_shares SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
    valid = await asyncio.to_thread(security.verify_password, body.pin, row["pin_hash"])
    current = _public_share(row["token_hash"])
    if current["id"] != row["id"]:
        raise _refuse()
    if not valid:
        raise _refuse(403, "share_pin", "неверный PIN")
    state.DB.execute("UPDATE chat_shares SET attempts = MAX(0, attempts - 1) WHERE id = ?", (row["id"],))
    state.DB.execute("UPDATE chat_share_attempts SET attempts = MAX(0, attempts - 1) WHERE ip_hash = ?", (ip_hash,))
    access = security.new_token()
    # Bound successful visitor sessions, retaining the newest 100 per link.
    state.DB.execute("DELETE FROM chat_share_visitors WHERE digest IN (SELECT digest FROM chat_share_visitors"
                     " WHERE share_id = ? ORDER BY expires DESC LIMIT -1 OFFSET 99)", (row["id"],))
    state.DB.execute("INSERT INTO chat_share_visitors VALUES(?,?,?)",
                     (security.digest(access), row["id"], now + VIEW_SEC))
    return JSONResponse({"access": access, **json.loads(row["snapshot"])}, headers=PUBLIC_HEADERS)


def _visitor(access):
    row = state.DB.execute(
        "SELECT s.id, s.user_id FROM chat_share_visitors v JOIN chat_shares s ON s.id = v.share_id"
        " JOIN users u ON u.id = s.user_id WHERE v.digest = ? AND v.expires > ? AND u.disabled = 0",
        (security.digest(access), clock.NOW())).fetchone()
    if row is None:
        raise _refuse()
    return row


@router.post("/share/check", status_code=204)
async def check_access(body: ReadIn, request: Request):
    _same_origin(request)
    _visitor(body.access)
    return Response(status_code=204, headers=PUBLIC_HEADERS)


@router.post("/share/file")
async def shared_file(body: FileIn, request: Request):
    _same_origin(request)
    visitor = _visitor(body.access)
    row = state.DB.execute(
        "SELECT m.* FROM chat_share_files f JOIN media m ON m.id = f.media_id"
        " WHERE f.share_id = ? AND m.id = ? AND m.user_id = ?",
        (visitor["id"], body.file, visitor["user_id"])).fetchone()
    if row is None:
        raise _refuse()
    path = media._file(row, False)
    try:
        size = (await asyncio.to_thread(path.stat)).st_size
    except FileNotFoundError:
        raise _refuse() from None
    except OSError:
        raise _refuse(503, "storage", "хранилище недоступно") from None
    _visitor(body.access)

    async def chunks():
        reader = media._read(path, 0, size)
        try:
            while True:
                block = await asyncio.to_thread(next, reader, b"")
                _visitor(body.access)
                if not block:
                    break
                yield block
        finally:
            await asyncio.to_thread(reader.close)

    # Always a download; potentially active HTML/SVG must never become a same-origin document.
    return StreamingResponse(chunks(), media_type="application/octet-stream", headers=PUBLIC_HEADERS | {
        "Content-Disposition": media.disposition(row["download_name"]), "Content-Length": str(size),
        "Content-Security-Policy": "default-src 'none'; sandbox"})
