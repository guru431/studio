"""Профиль у каждого: `/api/me` (с токеном CSRF сессии), тема и умолчания чата, смена пароля,
удаление всей своей истории (спека, раздел 7.9)."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import auth
import history
import security
import state

THEMES = ("auto", "dark", "light")
MODEL_MAX = 200
PASSWORD_MAX = 1024

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class MePatch(BaseModel):
    theme: str | None = None
    default_connection_id: int | None = None
    default_model: str | None = Field(None, max_length=MODEL_MAX)


class PasswordChange(BaseModel):
    current: str = Field(max_length=PASSWORD_MAX)
    new: str = Field(max_length=PASSWORD_MAX)


def capabilities(user_id: int) -> dict[str, bool]:
    """Разделы, для которых у пользователя есть подключение; до подключений (задача 4) — ни одного."""
    return {"chat": False, "photo": False, "video": False}


def check_password(password: str) -> None:
    if len(password) < security.PASSWORD_MIN:
        raise HTTPException(422, {"code": "short", "message": f"пароль короче {security.PASSWORD_MIN} символов"})


def _me(request: Request) -> dict:
    """Из базы, а не из request.state: после PATCH там прежние значения."""
    uid = request.state.user.id
    row = state.DB.execute("SELECT id, login, name, is_admin, theme, default_connection_id, default_model"
                           " FROM users WHERE id = ?", (uid,)).fetchone()
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "theme": row["theme"], "default_connection_id": row["default_connection_id"],
            "default_model": row["default_model"], "csrf": request.state.csrf, "capabilities": capabilities(uid)}


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
    if "default_connection_id" in sent:
        cid = body.default_connection_id
        if cid is not None and state.DB.execute("SELECT 1 FROM connections WHERE id = ? AND user_id = ?",
                                                (cid, uid)).fetchone() is None:
            raise HTTPException(422, {"code": "connection", "message": "такого подключения в профиле нет"})
        changes["default_connection_id"] = cid
    if "default_model" in sent:
        changes["default_model"] = body.default_model or None
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), uid))
    return _me(request)


@router.post("/me/password", status_code=204)
async def change_password(body: PasswordChange, request: Request):
    uid = request.state.user.id
    stored = state.DB.execute("SELECT pw_hash FROM users WHERE id = ?", (uid,)).fetchone()["pw_hash"]
    if not await asyncio.to_thread(security.verify_password, body.current, stored):
        log.warning("пользователь %d: смена пароля — неверный текущий", uid)
        raise HTTPException(403, {"code": "password", "message": "текущий пароль неверен"})
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
