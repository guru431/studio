"""Панель администратора (спека, раздел 7.10): пользователи, их подключения и разделы, список разрешённых адресов.

Чужую историю панель не отдаёт: ответы о пользователях — только поля учётки и число подключений.
Журнал — только id: логины, имена и пароли туда не пишутся.
"""
import asyncio
import json
import logging
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import auth
import clock
import connections
import history
import images
import media
import profile
import sections
import security
import state
import upstream

NAME_MAX = 64

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])


class UserCreate(BaseModel):
    login: str = Field(max_length=64)
    name: str = Field(max_length=NAME_MAX)
    password: str = Field(max_length=profile.PASSWORD_MAX)
    is_admin: bool = False


class UserPatch(BaseModel):
    login: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=NAME_MAX)
    is_admin: bool | None = None
    disabled: bool | None = None
    password: str | None = Field(None, max_length=profile.PASSWORD_MAX)


class Allowlist(BaseModel):
    entries: list[str]


def _user_out(uid: int) -> dict:
    row = state.DB.execute(
        "SELECT u.id, u.login, u.name, u.is_admin, u.disabled, u.created,"
        " (SELECT COUNT(*) FROM connections c WHERE c.user_id = u.id) AS connections"
        " FROM users u WHERE u.id = ?", (uid,)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого пользователя нет"})
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "disabled": bool(row["disabled"]), "created": row["created"], "connections": row["connections"]}


def _login(text: str) -> str:
    login = text.strip().lower()
    if not re.fullmatch(auth.LOGIN_RE, login):
        raise HTTPException(422, {"code": "login", "message": "логин — 2–32 знака: латиница, цифры, . _ -"})
    return login


def _taken() -> HTTPException:
    return HTTPException(409, {"code": "taken", "message": "такой логин уже есть"})


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
    login = _login(body.login)
    name = _name(body.name)
    profile.check_password(body.password)
    pw_hash = await asyncio.to_thread(security.hash_password, body.password)
    try:
        uid = state.DB.execute("INSERT INTO users(login, name, pw_hash, is_admin, created) VALUES (?, ?, ?, ?, ?)",
                               (login, name, pw_hash, int(body.is_admin), clock.NOW())).lastrowid
    except sqlite3.IntegrityError:
        raise _taken() from None
    log.info("администратор %d: создан пользователь %d", admin.id, uid)
    return _user_out(uid)


@router.patch("/users/{uid}")
async def patch_user(uid: int, body: UserPatch, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)                                            # нет такого — 404 до всяких проверок
    if body.disabled or body.is_admin is False:
        _self(admin, uid)
    if body.password is not None and uid == admin.id:
        # свой — только с текущим (POST /api/me/password): иначе получивший телефон открытым на минуту
        # сменил бы пароль и потом открывал бы запертую студию сам (спека screen-lock, раздел 4)
        raise HTTPException(409, {"code": "self", "message": "свой пароль меняют в «Настройках» → «Прочее»"})
    changes = {}
    if body.login is not None:
        changes["login"] = _login(body.login)               # сессии — по id: переименование их не гасит
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
            auth.end_sessions(uid)               # себя отключить и сменить себе пароль отсюда нельзя
        state.DB.execute("COMMIT")
    except sqlite3.IntegrityError:
        state.DB.execute("ROLLBACK")
        raise _taken() from None
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("администратор %d: пользователь %d изменён (%s)", admin.id, uid,
             ", ".join(sorted(k if k != "pw_hash" else "password" for k in changes)) or "ничего")
    if body.disabled:
        await history.stop_user(uid)          # отключённая учётка не держит карту и очередь шлюза
    return _user_out(uid)


@router.delete("/users/{uid}")
async def delete_user(uid: int, admin: auth.User = Depends(auth.require_admin)):
    """pending_files — сколько файлов учётки ещё ждут удаления на шаре: очередь переживает учётку."""
    _self(admin, uid)
    _user_out(uid)
    # сначала отключить: пока стирается история и кончаются задания, новых запросов учётки нет
    state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    auth.end_sessions(uid)
    await history.delete_all(uid)
    # отменённые задания освобождают карту подключением учётки — его каскад удалит вместе с ней;
    # не дождались (images.SETTLE_SEC) — карту освободит конец аренды шлюза
    if not await images.settle_user(uid):
        log.warning("пользователь %d: задания картинок не кончились — карту освободит срок аренды", uid)
    pending = media.pending(uid)          # после ожидания: и файлы заданий, отменённых в это время
    state.DB.execute("DELETE FROM users WHERE id = ?", (uid,))   # сессии и подключения — каскадом
    log.info("администратор %d: пользователь %d удалён вместе с историей, файлов в очереди удаления — %d",
             admin.id, uid, pending)
    return {"pending_files": pending}


@router.get("/allowlist")
async def get_allowlist():
    return {"entries": upstream.allowlist()}


@router.put("/allowlist")
async def put_allowlist(body: Allowlist, admin: auth.User = Depends(auth.require_admin)):
    entries = []
    for text in body.entries:
        try:
            entry = upstream.parse_entry(text)
        except ValueError:
            raise HTTPException(422, {"code": "entry", "message": f"«{text[:100]}» — не IP:порт и не имя:порт"
                                                                  " (имя — только для https)"}) from None
        if entry not in entries:
            entries.append(entry)
    state.DB.execute("INSERT INTO settings(key, value) VALUES ('allowlist', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(entries),))
    log.info("администратор %d: список разрешённых адресов — %d записей", admin.id, len(entries))
    return {"entries": upstream.allowlist()}


# ---- подключения пользователя: те же действия, что в профиле, но для `uid` ------

@router.get("/users/{uid}/connections")
async def list_user_connections(uid: int):
    _user_out(uid)
    return {"items": [connections.out(c) for c in connections.list_for(uid)]}


@router.post("/users/{uid}/connections", status_code=201)
async def create_user_connection(uid: int, body: connections.ConnIn, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    conn = connections.create(uid, body)
    log.info("администратор %d: пользователю %d добавлено подключение %d", admin.id, uid, conn["id"])
    return conn


@router.patch("/users/{uid}/connections/{conn_id}")
async def patch_user_connection(uid: int, conn_id: int, body: connections.ConnPatch,
                                admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    conn = connections.patch(uid, conn_id, body)
    log.info("администратор %d: у пользователя %d изменено подключение %d", admin.id, uid, conn_id)
    return conn


@router.delete("/users/{uid}/connections/{conn_id}", status_code=204)
async def delete_user_connection(uid: int, conn_id: int, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    connections.delete(uid, conn_id)
    log.info("администратор %d: у пользователя %d удалено подключение %d", admin.id, uid, conn_id)
    return Response(status_code=204)


# ---- разделы пользователя: те же действия, что в настройках, но для `uid` ---------

@router.get("/users/{uid}/sections")
async def get_user_sections(uid: int):
    _user_out(uid)
    return sections.all_out(uid)


@router.post("/users/{uid}/sections/{section}/links")
async def add_user_link(uid: int, section: str, body: sections.LinkIn, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.link(uid, section, body.connection_id)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return sections.section_out(uid, section)


@router.delete("/users/{uid}/sections/{section}/links/{conn_id}", status_code=204)
async def remove_user_link(uid: int, section: str, conn_id: int, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.unlink(uid, section, conn_id)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return Response(status_code=204)


@router.put("/users/{uid}/sections/{section}/links/{conn_id}")
async def put_user_models(uid: int, section: str, conn_id: int, body: sections.ModelsIn,
                          admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.set_models(uid, section, conn_id, body.models)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return sections.section_out(uid, section)


@router.put("/users/{uid}/sections/{section}/default")
async def put_user_default(uid: int, section: str, body: sections.DefaultIn, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.set_default(uid, section, body.connection_id, body.model)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return sections.section_out(uid, section)


@router.get("/users/{uid}/sections/{section}/links/{conn_id}/found")
async def get_user_found(uid: int, section: str, conn_id: int):
    _user_out(uid)
    return await sections.found(uid, section, conn_id)
