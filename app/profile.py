"""Профиль у каждого: `/api/me` (с токеном CSRF сессии, готовностью разделов и умолчанием чата из
sections.py), тема и вид ленты разделов (общие для всех устройств учётки), ключ Exa чата (`PUT/DELETE
/api/me/exa-key`, шифром; наружу — только «есть/нет»), смена пароля, удаление
всей своей истории (спека, раздел 7.9; ответ — число файлов в очереди удаления, media.py), объём
своих файлов и очередь удаления (`GET /api/me/storage`)."""
import asyncio
import json
import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictInt, field_validator

import auth
import code_actions
import history
import media
import sections
import security
import state

THEMES = ("auto", "dark", "light")
VIEWS = ("preview", "tiles", "table")       # views.js
VIEW_SECTIONS = ("photo", "video")
PASSWORD_MAX = 1024
EXA_KEY_MAX = 200                           # ключ Exa — знаков после strip

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class CodeChoice(BaseModel):
    model: str | None = Field(None, pattern=r"^[A-Za-z0-9._:\[\]/@+-]{1,300}$")
    effort: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    access: Literal["default", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
                    "read-only", "workspace-write", "danger-full-access"] | None = None


class CodeQuickAction(BaseModel):
    title: str = Field(min_length=1, max_length=80, pattern=r"^[^\r\n]+$")
    text: str = Field(min_length=1, max_length=100000)

    @field_validator("title", "text")
    @classmethod
    def _not_blank(cls, value):
        if not value.strip():
            raise ValueError("blank")
        return value


class MePatch(BaseModel):
    theme: str | None = None
    views: dict[str, str] | None = None     # {раздел: вид} — только присланные разделы
    code_defaults: dict[Literal["claude", "codex", "opencode"], CodeChoice] | None = None
    code_quick_actions: list[CodeQuickAction] = Field(default_factory=list, max_length=20)
    computer_pin_timeout: StrictInt | None = Field(None, ge=60, le=604800)

    @field_validator("code_defaults")
    @classmethod
    def _code_defaults(cls, value):
        if value is None or any(engine == "claude" and choice.effort not in
                               (None, "low", "medium", "high", "xhigh", "max") for engine, choice in value.items()):
            raise ValueError("code_defaults")
        for engine, choice in value.items():
            if engine != "opencode" and choice.model is not None and not re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,100}", choice.model):
                raise ValueError("model")
            if engine == "codex" and choice.effort not in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
                raise ValueError("effort")
            allowed = {"codex": ("read-only", "workspace-write", "danger-full-access"),
                       "claude": ("default", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk"),
                       "opencode": ("default", "auto")}[engine]
            if choice.access is not None and choice.access not in allowed:
                raise ValueError("access")
        return value


class ExaKey(BaseModel):
    key: str


class PasswordChange(BaseModel):
    current: str = Field(max_length=PASSWORD_MAX)
    new: str = Field(max_length=PASSWORD_MAX)


def check_password(password: str) -> None:
    if len(password) < security.PASSWORD_MIN:
        raise HTTPException(422, {"code": "short", "message": f"пароль короче {security.PASSWORD_MIN} символов"})


def _me(request: Request) -> dict:
    """Из базы, а не из request.state: после PATCH там прежние значения. Умолчание чата — из раздела."""
    uid = request.state.user.id
    row = state.DB.execute("SELECT id, login, name, is_admin, theme, views, screen_lock,"
                           " exa_key IS NOT NULL AS exa_key, pin_hash IS NOT NULL AS pin, code_defaults,"
                           " screen_lock_sections, screen_lock_timeout, computer_pin_timeout, code_quick_actions FROM users WHERE id = ?",
                           (uid,)).fetchone()
    chat = sections.default(uid, "chat")
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "theme": row["theme"], "views": json.loads(row["views"]),
            "default_connection_id": chat[0].id if chat else None,
            "default_model": chat[1] if chat else None, "csrf": request.state.csrf,
            "capabilities": sections.capabilities(uid), "screen_lock": bool(row["screen_lock"]),
            "exa_key": bool(row["exa_key"]), "pin": bool(row["pin"]), "code_defaults": json.loads(row["code_defaults"]),
            "code_quick_actions": json.loads(row["code_quick_actions"]) if row["code_quick_actions"] is not None else code_actions.DEFAULTS,
            "screen_lock_sections": json.loads(row["screen_lock_sections"]),
            "screen_lock_timeout": row["screen_lock_timeout"], "computer_pin_timeout": row["computer_pin_timeout"]}


@router.get("/me")
async def get_me(request: Request):
    return _me(request)


@router.patch("/me")
async def patch_me(body: MePatch, request: Request):
    uid = request.state.user.id
    sent = body.model_fields_set
    changes = {}
    if "computer_pin_timeout" in sent:
        if body.computer_pin_timeout is None:
            raise HTTPException(422, {"code": "timeout", "message": "укажите таймер PIN компьютера"})
        changes["computer_pin_timeout"] = body.computer_pin_timeout
    if "theme" in sent:
        if body.theme not in THEMES:
            raise HTTPException(422, {"code": "theme", "message": "тема — auto, dark или light"})
        changes["theme"] = body.theme
    if "views" in sent:
        if body.views is None or any(k not in VIEW_SECTIONS or v not in VIEWS for k, v in body.views.items()):
            raise HTTPException(422, {"code": "views", "message": "вид ленты — preview, tiles или table у photo и video"})
        stored = state.DB.execute("SELECT views FROM users WHERE id = ?", (uid,)).fetchone()["views"]
        changes["views"] = json.dumps({**json.loads(stored), **body.views})
    if "code_defaults" in sent:
        stored = json.loads(state.DB.execute("SELECT code_defaults FROM users WHERE id = ?", (uid,)).fetchone()[0])
        for engine, choice in body.code_defaults.items():
            stored[engine] = {**stored.get(engine, {}), **choice.model_dump(exclude_unset=True)}
        changes["code_defaults"] = json.dumps(stored)
    if "code_quick_actions" in sent:
        changes["code_quick_actions"] = json.dumps([action.model_dump() for action in body.code_quick_actions])
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), uid))
    return _me(request)


@router.post("/me/password", status_code=204)
async def change_password(body: PasswordChange, request: Request):
    uid = request.state.user.id
    confirmed = await auth.confirm_password(uid, body.current, "смена пароля", "текущий пароль неверен")
    check_password(body.new)
    pw_hash = await asyncio.to_thread(security.hash_password, body.new)
    state.DB.execute("BEGIN")
    try:
        # пока считался новый хеш, пароль могли сбросить из панели: сброс не перекрывается
        if not state.DB.execute("UPDATE users SET pw_hash = ? WHERE id = ? AND pw_hash = ? AND disabled = 0",
                                (pw_hash, uid, confirmed)).rowcount:
            raise HTTPException(403, {"code": "password", "message": "текущий пароль неверен"})
        auth.end_sessions(uid, keep_digest=security.digest(request.cookies[auth.COOKIE]))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("пользователь %d: пароль сменён, прочие сессии закрыты", uid)
    return Response(status_code=204)


@router.put("/me/exa-key", status_code=204)
async def put_exa_key(body: ExaKey, request: Request):
    """Ключ Exa — шифром; запросом к Exa не проверяется (тратил бы баланс): неверный скажет первый поиск."""
    uid = request.state.user.id
    key = body.key.strip()
    if not 1 <= len(key) <= EXA_KEY_MAX:
        raise HTTPException(422, {"code": "exa_key", "message": f"ключ Exa — от 1 до {EXA_KEY_MAX} знаков"})
    state.DB.execute("UPDATE users SET exa_key = ? WHERE id = ?", (security.encrypt(key), uid))
    log.info("пользователь %d: ключ Exa сохранён", uid)
    return Response(status_code=204)


@router.delete("/me/exa-key", status_code=204)
async def delete_exa_key(request: Request):
    """Ключ убран — и «Интернет» во всех диалогах учётки выключен: без ключа он не включается (chat.patch_chat),
    и включённым не остаётся. Сохранение ключа ничего не включает."""
    uid = request.state.user.id
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("UPDATE users SET exa_key = NULL WHERE id = ?", (uid,))
        state.DB.execute("UPDATE chats SET web = 0 WHERE user_id = ? AND web <> 0", (uid,))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("пользователь %d: ключ Exa убран", uid)
    return Response(status_code=204)


@router.get("/me/storage")
async def get_storage(request: Request):
    """«Хранилище» в «Прочем»: свои файлы по видам и сколько их путей ждут в очереди удаления (с какого
    времени) — из базы, без путей и имён; шару не трогает. Квот и автоудаления нет."""
    uid = request.state.user.id
    files, since = media.waiting(uid)
    result = {**media.usage(uid), "pending": {"files": files, "since": since}}
    pending = state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions WHERE user_id = ?", (uid,)).fetchone()[0]
    if pending:
        result["cleaner_pending"] = pending
    return result


@router.delete("/history")
async def delete_history(request: Request):
    """pending_files — сколько файлов на шаре ещё ждут удаления (она была недоступна): страница так и скажет."""
    result = {"pending_files": await history.delete_all(request.state.user.id)}
    pending = state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions WHERE user_id = ?",
                               (request.state.user.id,)).fetchone()[0]
    if pending:
        result["cleaner_pending"] = pending
    return result
