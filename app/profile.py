"""Профиль у каждого: `/api/me` (с токеном CSRF сессии, готовностью разделов и умолчанием чата из
sections.py), тема и вид ленты разделов (общие для всех устройств учётки), смена пароля, удаление
всей своей истории (спека, раздел 7.9)."""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import auth
import clock
import history
import sections
import security
import state

THEMES = ("auto", "dark", "light")
VIEWS = ("preview", "tiles", "table")       # views.js
VIEW_SECTIONS = ("photo", "video")
PASSWORD_MAX = 1024

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class MePatch(BaseModel):
    theme: str | None = None
    views: dict[str, str] | None = None     # {раздел: вид} — только присланные разделы


class PasswordChange(BaseModel):
    current: str = Field(max_length=PASSWORD_MAX)
    new: str = Field(max_length=PASSWORD_MAX)


def check_password(password: str) -> None:
    if len(password) < security.PASSWORD_MIN:
        raise HTTPException(422, {"code": "short", "message": f"пароль короче {security.PASSWORD_MIN} символов"})


def _me(request: Request) -> dict:
    """Из базы, а не из request.state: после PATCH там прежние значения. Умолчание чата — из раздела."""
    uid = request.state.user.id
    row = state.DB.execute("SELECT id, login, name, is_admin, theme, views FROM users WHERE id = ?", (uid,)).fetchone()
    chat = sections.default(uid, "chat")
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "theme": row["theme"], "views": json.loads(row["views"]),
            "default_connection_id": chat[0].id if chat else None,
            "default_model": chat[1] if chat else None, "csrf": request.state.csrf,
            "capabilities": sections.capabilities(uid)}


@router.get("/me")
async def get_me(request: Request):
    return _me(request)


@router.patch("/me")
async def patch_me(body: MePatch, request: Request):
    uid = request.state.user.id
    sent = body.model_fields_set
    changes = {}
    if "theme" in sent:
        if body.theme not in THEMES:
            raise HTTPException(422, {"code": "theme", "message": "тема — auto, dark или light"})
        changes["theme"] = body.theme
    if "views" in sent:
        if body.views is None or any(k not in VIEW_SECTIONS or v not in VIEWS for k, v in body.views.items()):
            raise HTTPException(422, {"code": "views", "message": "вид ленты — preview, tiles или table у photo и video"})
        stored = state.DB.execute("SELECT views FROM users WHERE id = ?", (uid,)).fetchone()["views"]
        changes["views"] = json.dumps({**json.loads(stored), **body.views})
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), uid))
    return _me(request)


@router.post("/me/password", status_code=204)
async def change_password(body: PasswordChange, request: Request):
    uid = request.state.user.id
    # укравший сессию не переберёт текущий пароль: та же пауза, что у входа, по ключу учётки
    key = ("pw", uid)
    if auth.locked_key(key):
        log.warning("пользователь %d: смена пароля — пауза", uid)
        raise HTTPException(429, {"code": "locked", "message": "слишком много попыток — подождите 15 минут"})
    attempt = clock.NOW()
    auth.FAILS.setdefault(key, []).append(attempt)       # до хеша: параллельная пачка не пройдёт разом
    stored = state.DB.execute("SELECT pw_hash FROM users WHERE id = ?", (uid,)).fetchone()["pw_hash"]
    if not await asyncio.to_thread(security.verify_password, body.current, stored):
        log.warning("пользователь %d: смена пароля — неверный текущий", uid)
        raise HTTPException(403, {"code": "password", "message": "текущий пароль неверен"})
    auth.FAILS.pop(key, None)
    check_password(body.new)
    pw_hash = await asyncio.to_thread(security.hash_password, body.new)
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (pw_hash, uid))
        auth.end_sessions(uid, keep_digest=security.digest(request.cookies[auth.COOKIE]))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("пользователь %d: пароль сменён, прочие сессии закрыты", uid)
    return Response(status_code=204)


@router.delete("/history", status_code=204)
async def delete_history(request: Request):
    await history.delete_all(request.state.user.id)
    return Response(status_code=204)
