"""PIN-protected, revocable chat snapshots. Public secrets travel only in request bodies.

The link token lives in the URL fragment (never Apache's request log). A successful
PIN check grants a separate, expiring read token, never a Studio account session.

Снимок переписки хранится только шифром STUDIO_DATA_KEY (`chat_shares.sealed`, db.SHARES_36), как токены
подключений: в базе и ночных копиях открытого текста нет. В прежнем столбце `snapshot` — заглушка `PLACEHOLDER`,
её прежний код (откат) покажет вместо снимка. Открытые снимки (до шага 36 или прежнего кода) шифрует `seal()` при
старте. Сменили ключ данных — снимок не расшифровать: ссылка «недоступна», владелец создаёт новую.

Срок ссылки — `days` при создании (1, 7, 30 дней или бессрочно, по умолчанию `DAYS_DEFAULT`): истёкшая удаляется
со снимком при первом же обращении к ссылкам или старте. Неверные PIN считаются и без окна (`fails`): `FAILS_MAX`
отзывает ссылку, владельцу — push/ntfy без содержимого и `share_locked` в журнале безопасности.
"""
import asyncio
import json
import logging
import uuid

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictBool, field_validator

import auth
import chat
import claude_code
import clock
import media
import lock_policy
import operations
import push
import security
import security_log
import state

log = logging.getLogger("studio")
router = APIRouter()
PIN_FAILS = 5
IP_ATTEMPTS = 30
LOCK_SEC = 900
#: все неверные PIN ссылки, без окна: пауза LOCK_SEC сдерживает темп, а этот предел — сам подбор (4 цифры)
FAILS_MAX = 50
#: срок ссылки в днях; None — бессрочно. Неделя по умолчанию: ссылку обычно открывают в первые дни, а снимок
#: переписки и подбор PIN не должны жить дольше нужного — бессрочную владелец выбирает явно
DAYS = (1, 7, 30)
DAYS_DEFAULT = 7
#: в прежнем столбце `snapshot`: прежний код (откат) отдаст получателю это вместо зашифрованной переписки
PLACEHOLDER = {"title": "Ссылка недоступна", "messages": [{
    "role": "assistant", "attachments": [], "text": "Эту ссылку создала более новая версия Студии, и сейчас её не "
    "открыть. Попросите владельца прислать ссылку заново."}]}
PLACEHOLDER_RAW = json.dumps(PLACEHOLDER, ensure_ascii=False, separators=(",", ":"))
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
    #: рассуждение модели обычного чата — черновик, который владелец обычно не перечитывал: только по выбору
    reasoning: StrictBool = False
    days: int | None = Field(default=DAYS_DEFAULT, strict=True)

    @field_validator("days")
    @classmethod
    def _days(cls, value):
        if value is not None and value not in DAYS:
            raise ValueError("срок ссылки — 1, 7, 30 дней или бессрочно")
        return value


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


def _expire() -> None:
    """Истёкшие ссылки — прочь вместе со снимком, читателями и списком файлов (каскад)."""
    state.DB.execute("DELETE FROM chat_shares WHERE expires IS NOT NULL AND expires <= ?", (clock.NOW(),))


def seal() -> int:
    """При старте: снимки открытым текстом — шифром, на их место — заглушка. secure_delete на время записи: SQLite
    обнулит освобождённые страницы с прежним текстом, а не оставит их в файле базы и её копиях."""
    _expire()
    rows = state.DB.execute("SELECT id, snapshot FROM chat_shares WHERE sealed IS NULL").fetchall()
    if not rows:
        return 0
    before = state.DB.execute("PRAGMA secure_delete").fetchone()[0]
    state.DB.execute("PRAGMA secure_delete = ON")
    try:
        for row in rows:
            state.DB.execute("UPDATE chat_shares SET sealed = ?, snapshot = ? WHERE id = ? AND sealed IS NULL",
                             (security.encrypt(row["snapshot"]), PLACEHOLDER_RAW, row["id"]))
    finally:
        state.DB.execute(f"PRAGMA secure_delete = {int(before)}")
    state.DB.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log.info("ссылки «Поделиться»: зашифровано снимков — %d", len(rows))
    return len(rows)


def _owned(request: Request, *, chat_id=None, agent_id=None, project=None, session_id=None, engine=None):
    _expire()
    return state.DB.execute(
        "SELECT * FROM chat_shares WHERE user_id = ? AND chat_id IS ? AND agent_id IS ?"
        " AND project IS ? AND session_id IS ? AND engine IS ?",
        (request.state.user.id, chat_id, agent_id, project, session_id, engine)).fetchone()


def _out(row) -> dict:
    if row is None:
        return {"enabled": False}
    try:
        url = state.CFG.origin.rstrip("/") + "/share#" + security.decrypt(row["token_enc"])
    except InvalidToken:          # сменили STUDIO_DATA_KEY: ссылку не показать и не открыть — создать новую
        url = None
    return {"enabled": True, "url": url, "created": row["created"], "expires": row["expires"],
            "views": row["views"], "viewed": row["viewed"], "fails": row["fails"]}


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
    sealed = await asyncio.to_thread(security.encrypt, _encode(snapshot))
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
    token, share_id, now = security.new_token(), uuid.uuid4().hex, clock.NOW()
    state.DB.execute("BEGIN")
    try:
        if current:
            state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (current["id"],))
        state.DB.execute(
            "INSERT INTO chat_shares(id,user_id,chat_id,agent_id,project,session_id,engine,"
            "token_hash,token_enc,pin_hash,snapshot,sealed,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (share_id, request.state.user.id, source.get("chat_id"), source.get("agent_id"),
             source.get("project"), source.get("session_id"), source.get("engine"), security.digest(token),
             security.encrypt(token), pin_hash, PLACEHOLDER_RAW, sealed, now,
             None if body.days is None else now + body.days * 86400))
        state.DB.executemany("INSERT INTO chat_share_files(share_id,media_id) VALUES(?,?)",
                             [(share_id, mid) for mid in set(files)])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    _journal(request, "share_create", source)
    return _out(_owned(request, **source))


def _journal(request: Request, kind: str, source: dict) -> None:
    security_log.record(request.state.user.id, kind, detail="code" if source.get("agent_id") else "chat",
                        digest=request.state.digest, ip=auth.client_ip(request))


@router.get("/api/chats/{chat_id}/share")
async def chat_share(chat_id: str, request: Request):
    chat._chat_row(request.state.user.id, chat_id)
    return _out(_owned(request, chat_id=chat_id))


def regular_snapshot(user_id: int, chat_id: str, reasoning: bool = True) -> tuple[dict, list[str]]:
    """Снимок обычного чата; `reasoning=False` — без рассуждения модели (ссылка по умолчанию)."""
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
                         "reasoning": message["reasoning"] if reasoning else None, "status": message["status"]})
    snapshot = {"title": row["title"] or "Чат", "messages": messages}
    _encode(snapshot)
    return snapshot, files


@router.post("/api/chats/{chat_id}/share", status_code=201)
async def create_chat_share(chat_id: str, body: ShareIn, request: Request):
    expected = _owned(request, chat_id=chat_id)
    snapshot, files = regular_snapshot(request.state.user.id, chat_id, body.reasoning)
    return await _store(request, body, snapshot, expected, files, chat_id=chat_id)


@router.delete("/api/chats/{chat_id}/share", status_code=204)
async def revoke_chat_share(chat_id: str, request: Request):
    chat._chat_row(request.state.user.id, chat_id)
    if state.DB.execute("DELETE FROM chat_shares WHERE chat_id = ? AND user_id = ?",
                        (chat_id, request.state.user.id)).rowcount:
        _journal(request, "share_revoke", {})


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
        _journal(request, "share_revoke", source)


@router.api_route("/share", methods=["GET", "HEAD"])
async def share_page():
    return HTMLResponse(auth.page("share.html"), headers=PUBLIC_HEADERS)


def _same_origin(request):
    if auth.foreign_origin(request) or request.headers.get("sec-fetch-site") == "cross-site":
        raise _refuse(403, "origin", "откройте ссылку в браузере")


def _public_share(token_hash):
    row = state.DB.execute("SELECT s.* FROM chat_shares s JOIN users u ON u.id = s.user_id"
                            " WHERE s.token_hash = ? AND u.disabled = 0 AND (s.expires IS NULL OR s.expires > ?)",
                            (token_hash, clock.NOW())).fetchone()
    if row is None:
        raise _refuse()
    return row


def _open(row, pin: str) -> "dict | None":
    """В потоке: верен ли PIN и, если да, снимок (scrypt и расшифровка многомегабайтного шифра — мимо цикла
    событий). Снимок не расшифровать (сменили STUDIO_DATA_KEY) — `_refuse`, как отозванную ссылку."""
    if not security.verify_password(pin, row["pin_hash"]):
        return None
    try:
        raw = row["snapshot"] if row["sealed"] is None else security.decrypt(row["sealed"])
        return json.loads(raw)
    except (InvalidToken, ValueError):
        raise _refuse(404, "share_gone", "ссылка недоступна — попросите владельца создать новую") from None


def _wrong_pin(request: Request, row, reserved: int) -> None:
    """Неверный PIN: в журнал владельца (повторы свернёт security_log), общий счёт ссылки; FAILS_MAX — отзыв."""
    uid, ip = row["user_id"], auth.client_ip(request)
    detail = "code" if row["agent_id"] else "chat"
    fails = state.DB.execute("UPDATE chat_shares SET fails = fails + 1 WHERE id = ? RETURNING fails",
                             (row["id"],)).fetchone()[0]
    security_log.record(uid, "share_pin_fail", detail=detail, ip=ip)
    if fails >= FAILS_MAX:
        state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (row["id"],))
        security_log.record(uid, "share_locked", detail="revoked", ip=ip)
        push.notify_later(uid, f"Ссылка «Поделиться» отключена: {FAILS_MAX} неверных PIN", "settings/security",
                          "share-" + row["id"], title="Студия")
        log.info("пользователь %d: ссылка отозвана после %d неверных PIN", uid, fails)
        raise _refuse()
    if reserved == PIN_FAILS:
        security_log.record(uid, "share_locked", detail="pause", ip=ip)


@router.post("/share/unlock")
async def unlock(body: UnlockIn, request: Request):
    _same_origin(request)
    now = clock.NOW()
    _expire()
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
    reserved = state.DB.execute("UPDATE chat_shares SET attempts = attempts + 1 WHERE id = ? RETURNING attempts",
                                (row["id"],)).fetchone()[0]
    snapshot = await asyncio.to_thread(_open, row, body.pin)
    current = _public_share(row["token_hash"])
    if current["id"] != row["id"]:
        raise _refuse()
    if snapshot is None:
        _wrong_pin(request, current, reserved)
        raise _refuse(403, "share_pin", "неверный PIN")
    state.DB.execute("UPDATE chat_shares SET attempts = MAX(0, attempts - 1), views = views + 1, viewed = ?"
                     " WHERE id = ?", (now, row["id"]))
    state.DB.execute("UPDATE chat_share_attempts SET attempts = MAX(0, attempts - 1) WHERE ip_hash = ?", (ip_hash,))
    access = security.new_token()
    # Bound successful visitor sessions, retaining the newest 100 per link.
    state.DB.execute("DELETE FROM chat_share_visitors WHERE digest IN (SELECT digest FROM chat_share_visitors"
                     " WHERE share_id = ? ORDER BY expires DESC LIMIT -1 OFFSET 99)", (row["id"],))
    state.DB.execute("INSERT INTO chat_share_visitors VALUES(?,?,?)",
                     (security.digest(access), row["id"], now + VIEW_SEC))
    return JSONResponse({"access": access, **snapshot}, headers=PUBLIC_HEADERS)


def _visitor(access):
    row = state.DB.execute(
        "SELECT s.id, s.user_id FROM chat_share_visitors v JOIN chat_shares s ON s.id = v.share_id"
        " JOIN users u ON u.id = s.user_id WHERE v.digest = ? AND v.expires > ? AND u.disabled = 0"
        " AND (s.expires IS NULL OR s.expires > ?)", (security.digest(access), clock.NOW(), clock.NOW())).fetchone()
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
