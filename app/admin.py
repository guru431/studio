"""Панель администратора (спека, раздел 7.10): пользователи и список разрешённых адресов.

Чужую историю панель не отдаёт: ответы о пользователях — только поля учётки и число подключений.
Журнал — только id: логины, имена и пароли туда не пишутся.
"""
import asyncio
import json
import logging
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import auth
import clock
import history
import profile
import security
import state

NAME_MAX = 64
ENTRY_RE = r"[a-z0-9.-]+:([0-9]{1,5})"

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])


class UserCreate(BaseModel):
    login: str = Field(max_length=64)
    name: str = Field(max_length=NAME_MAX)
    password: str = Field(max_length=profile.PASSWORD_MAX)
    is_admin: bool = False


class UserPatch(BaseModel):
    name: str | None = Field(None, max_length=NAME_MAX)
    is_admin: bool | None = None
    disabled: bool | None = None
    password: str | None = Field(None, max_length=profile.PASSWORD_MAX)


class Allowlist(BaseModel):
    entries: list[str]


def parse_entry(text: str) -> str:
    """Заглушка до upstream.parse_entry (задача 4): `host:port` в нижнем регистре, порт 1–65535."""
    entry = text.strip().lower()
    m = re.fullmatch(ENTRY_RE, entry)
    if not m or not 1 <= int(m.group(1)) <= 65535:
        raise ValueError(text)
    return entry


def _user_out(uid: int) -> dict:
    row = state.DB.execute(
        "SELECT u.id, u.login, u.name, u.is_admin, u.disabled, u.created,"
        " (SELECT COUNT(*) FROM connections c WHERE c.user_id = u.id) AS connections"
        " FROM users u WHERE u.id = ?", (uid,)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого пользователя нет"})
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "disabled": bool(row["disabled"]), "created": row["created"], "connections": row["connections"]}


def _name(text: str) -> str:
    name = text.strip()
    if not name:
        raise HTTPException(422, {"code": "name", "message": "нужно имя"})
    return name


def _self(admin: auth.User, uid: int) -> None:
    if admin.id == uid:
        raise HTTPException(409, {"code": "self", "message": "себя отключить, разжаловать и удалить нельзя"})


@router.get("/users")
async def list_users():
    ids = [r["id"] for r in state.DB.execute("SELECT id FROM users ORDER BY id")]
    return {"items": [_user_out(uid) for uid in ids]}


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, admin: auth.User = Depends(auth.require_admin)):
    login = body.login.strip().lower()
    if not re.fullmatch(auth.LOGIN_RE, login):
        raise HTTPException(422, {"code": "login", "message": "логин — 2–32 знака: латиница, цифры, . _ -"})
    name = _name(body.name)
    profile.check_password(body.password)
    pw_hash = await asyncio.to_thread(security.hash_password, body.password)
    try:
        uid = state.DB.execute("INSERT INTO users(login, name, pw_hash, is_admin, created) VALUES (?, ?, ?, ?, ?)",
                               (login, name, pw_hash, int(body.is_admin), clock.NOW())).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, {"code": "taken", "message": "такой логин уже есть"}) from None
    log.info("администратор %d: создан пользователь %d", admin.id, uid)
    return _user_out(uid)


@router.patch("/users/{uid}")
async def patch_user(uid: int, body: UserPatch, request: Request, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)                                            # нет такого — 404 до всяких проверок
    if body.disabled or body.is_admin is False:
        _self(admin, uid)
    changes = {}
    if body.name is not None:
        changes["name"] = _name(body.name)
    if body.is_admin is not None:
        changes["is_admin"] = int(body.is_admin)
    if body.disabled is not None:
        changes["disabled"] = int(body.disabled)
    if body.password is not None:
        profile.check_password(body.password)
        changes["pw_hash"] = await asyncio.to_thread(security.hash_password, body.password)
    state.DB.execute("BEGIN")
    try:
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)    # имена — из кода выше, не из запроса
            state.DB.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), uid))
        if body.disabled or body.password is not None:
            # свой пароль из панели не выкидывает из текущей сессии
            keep = security.digest(request.cookies[auth.COOKIE]) if uid == admin.id else None
            auth.end_sessions(uid, keep_digest=keep)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("администратор %d: пользователь %d изменён (%s)", admin.id, uid,
             ", ".join(sorted(k if k != "pw_hash" else "password" for k in changes)) or "ничего")
    return _user_out(uid)


@router.delete("/users/{uid}", status_code=204)
async def delete_user(uid: int, admin: auth.User = Depends(auth.require_admin)):
    _self(admin, uid)
    _user_out(uid)
    await history.delete_all(uid)
    state.DB.execute("DELETE FROM users WHERE id = ?", (uid,))   # сессии и подключения — каскадом
    log.info("администратор %d: пользователь %d удалён вместе с историей", admin.id, uid)
    return Response(status_code=204)


def _allowlist() -> list[str]:
    return json.loads(state.DB.execute("SELECT value FROM settings WHERE key = 'allowlist'").fetchone()["value"])


@router.get("/allowlist")
async def get_allowlist():
    return {"entries": _allowlist()}


@router.put("/allowlist")
async def put_allowlist(body: Allowlist, admin: auth.User = Depends(auth.require_admin)):
    entries = []
    for text in body.entries:
        try:
            entry = parse_entry(text)
        except ValueError:
            raise HTTPException(422, {"code": "entry", "message": f"«{text[:100]}» — не хост:порт"}) from None
        if entry not in entries:
            entries.append(entry)
    state.DB.execute("INSERT INTO settings(key, value) VALUES ('allowlist', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(entries),))
    log.info("администратор %d: список разрешённых адресов — %d записей", admin.id, len(entries))
    return {"entries": _allowlist()}
