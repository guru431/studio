"""Manual transfers through Cleaner. Only references and encrypted credentials live in Studio."""
import uuid
from typing import Annotated, Literal

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

import cleaner_client
import clock
import connections
import operations
import security
import sections
import state
import upstream

router = APIRouter(prefix="/api/cleaner/manual")
_ID_RE = r"^[0-9a-f]{32}$"
_ID = Annotated[str, Field(pattern=_ID_RE)]
_PathID = Annotated[str, Path(pattern=_ID_RE)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def utf8(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Invalid UTF-8 string") from None
        return value


class AccessIn(Body):
    base_url: str = Field(max_length=connections.URL_MAX)
    key: str = Field(default="", max_length=512)


class TextIn(Body):
    text: str = Field(max_length=500_000)


class PromptIn(TextIn):
    previous: _ID | None


class RuleIn(Body):
    action: Literal["mask", "unmask"]
    item_id: _ID
    value: str = Field(min_length=1, max_length=2000)
    type: Literal["TERM"] = "TERM"


class CopyIn(Body):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RestoreIn(TextIn):
    transfer: _ID


def _active(uid: int):
    return state.DB.execute("SELECT * FROM cleaner_manual_access WHERE user_id = ? AND active = 1", (uid,)).fetchone()


def _context(uid: int, chat_id: str):
    row = state.DB.execute("SELECT * FROM cleaner_contexts WHERE resource = 'manual' AND user_id = ? AND id = ?",
                           (uid, chat_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "missing", "message": "Диалог не найден"})
    return row


def _secret(row) -> str:
    try:
        return security.decrypt(row["token_enc"])
    except (InvalidToken, ValueError):
        raise cleaner_client.refused("Введите личный ключ Cleaner заново") from None


async def _call(uid: int, base_url: str, key: str, method: str, path: str, **kwargs) -> dict:
    conn = connections.Connection(0, uid, "Cleaner", "openai", base_url, frozenset(),
                                  upstream.is_allowed(base_url), server_only=True, cleaner=True)
    return await cleaner_client.call(conn, method, "/manual" + path, auth=f"Bearer {key}", **kwargs)


async def _chat(request: Request, chat_id: str, method: str, path: str = "", **kwargs) -> dict:
    uid = request.state.user.id
    operations.check(uid)
    row = _context(uid, chat_id)
    result = await _call(uid, row["base_url"], _secret(row), method, f'/chats/{row["remote_id"]}{path}', **kwargs)
    operations.check(uid)
    _context(uid, chat_id)
    return result


@router.get("/access")
async def access(request: Request):
    row = _active(request.state.user.id)
    return {"configured": row is not None, "base_url": row["base_url"] if row else ""}


@router.put("/access")
async def save_access(body: AccessIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    address = connections.check_base_url("openai", body.base_url)
    connections._check_cleaner("openai", address, [])
    address = str(httpx.URL(address)).rstrip("/")
    current = _active(uid)
    if body.key.strip() or current is None or current["base_url"] != address:
        key = connections.check_token(body.key)
    else:
        key = _secret(current)
    result = await _call(uid, address, key, "GET", "/access")
    owner = result.get("owner")
    if not isinstance(owner, str) or not 0 < len(owner) <= 128:
        raise cleaner_client.refused("Cleaner вернул неверные данные учётки", 502)
    operations.check(uid)
    if current != _active(uid):
        raise cleaner_client.refused("Подключение изменилось: проверьте запрос ещё раз")
    encrypted = security.encrypt(key)
    with sections.transaction():
        state.DB.execute("UPDATE cleaner_manual_access SET active = 0 WHERE user_id = ? AND active = 1", (uid,))
        row = state.DB.execute(
            "INSERT INTO cleaner_manual_access(user_id, base_url, remote_owner, token_enc) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id, base_url, remote_owner) DO UPDATE SET active = 1, token_enc = excluded.token_enc"
            " RETURNING id", (uid, address, owner, encrypted),
        ).fetchone()
        state.DB.execute("UPDATE cleaner_contexts SET token_enc = ?"
                         " WHERE user_id = ? AND resource = 'manual' AND connection_id = ?",
                         (encrypted, uid, -row["id"]))
    return {"configured": True, "base_url": address}


@router.get("/chats")
async def chats(request: Request):
    uid = request.state.user.id
    rows = state.DB.execute("SELECT id, created FROM cleaner_contexts WHERE resource = 'manual' AND user_id = ?"
                            " ORDER BY created DESC, id DESC", (uid,))
    return {"items": [dict(row) for row in rows], "configured": _active(uid) is not None}


@router.post("/chats", status_code=201)
async def create(request: Request):
    uid = request.state.user.id
    operations.check(uid)
    access = _active(uid)
    if access is None:
        raise cleaner_client.refused("Подключите ручную очистку Cleaner в настройках")
    token = _secret(access)
    identifier = uuid.uuid4().hex
    created = clock.NOW()
    base_url = access["base_url"]
    state.DB.execute("INSERT INTO cleaner_contexts(id, user_id, resource, item_id, connection_id, remote_id,"
                     " base_url, token_enc, created) VALUES (?, ?, 'manual', ?, ?, ?, ?, ?, ?)",
                     (identifier, uid, identifier, -access["id"], identifier, base_url, access["token_enc"], created))
    try:
        await _call(uid, base_url, token, "PUT", "/chats", json={"id": identifier})
        operations.check(uid)
        _context(uid, identifier)
    except BaseException:
        state.DB.execute("DELETE FROM cleaner_contexts WHERE id = ?", (identifier,))
        # Deletion may have reached Cleaner before the create. Queue it again in case the PUT went through.
        state.DB.execute("INSERT OR IGNORE INTO cleaner_deletions(id, user_id, remote_id, base_url, token_enc, queued)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (identifier, uid, identifier, base_url, access["token_enc"], created))
        raise
    return {"id": identifier}


@router.get("/chats/{chat_id}")
async def view(chat_id: _PathID, request: Request):
    return await _chat(request, chat_id, "GET")


@router.delete("/chats/{chat_id}", status_code=204)
async def delete(chat_id: _PathID, request: Request):
    uid = request.state.user.id
    _context(uid, chat_id)
    state.DB.execute("DELETE FROM cleaner_contexts WHERE resource = 'manual' AND id = ? AND user_id = ?", (chat_id, uid))
    return Response(status_code=204)


@router.post("/chats/{chat_id}/texts", status_code=202)
async def add_text(chat_id: _PathID, body: PromptIn, request: Request):
    return await _chat(request, chat_id, "POST", "/texts", json=body.model_dump())


@router.post("/chats/{chat_id}/rules")
async def rule(chat_id: _PathID, body: RuleIn, request: Request):
    return await _chat(request, chat_id, "POST", "/rules", json=body.model_dump())


@router.post("/chats/{chat_id}/refresh", status_code=204)
async def refresh(chat_id: _PathID, request: Request):
    await _chat(request, chat_id, "POST", "/refresh")
    return Response(status_code=204)


@router.post("/chats/{chat_id}/copy")
async def copy(chat_id: _PathID, body: CopyIn, request: Request):
    return await _chat(request, chat_id, "POST", "/copy", json=body.model_dump())


@router.post("/chats/{chat_id}/restore")
async def restore(chat_id: _PathID, body: RestoreIn, request: Request):
    return await _chat(request, chat_id, "POST", "/restore", json=body.model_dump())


@router.delete("/chats/{chat_id}/transfer/{transfer}", status_code=204)
async def cancel(chat_id: _PathID, transfer: _PathID, request: Request):
    await _chat(request, chat_id, "DELETE", f"/transfer/{transfer}")
    return Response(status_code=204)
