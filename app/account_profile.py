"""Профиль учётки: `/api/me` (с токеном CSRF сессии, готовностью разделов и умолчанием чата из
sections.py), тема и вид ленты разделов (общие для всех устройств учётки), ключ Exa чата (`PUT/DELETE
/api/me/exa-key`, шифром; наружу — только «есть/нет»), смена пароля, удаление
всей своей истории (спека, раздел 7.9; ответ — число файлов в очереди удаления, media.py), объём
своих файлов и очередь удаления (`GET /api/me/storage`)."""
import asyncio
import json
import logging
import re
import unicodedata
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt, field_validator

import auth
import cleaner_client
import cleaner_route
import history
import lock_policy
import media
import my_files
import ntfy
import release
import sections
import security
import security_log
import state
import totp
import trash

THEMES = ("auto", "dark", "light")
#: акцент учётки (appearance.js, `[data-accent]` в app.css): первый — умолчание. Хранится ключом `accent` в JSON
#: `users.views` (без шага схемы: прежний код этот ключ не читает и при правке вида ленты сохраняет)
ACCENTS = ("orange", "blue", "violet", "rose")
VIEWS = ("preview", "tiles", "table")       # views.js
VIEW_SECTIONS = ("photo", "video")
PASSWORD_MAX = 1024
EXA_KEY_MAX = 200                           # ключ Exa — знаков после strip

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class CodeChoice(BaseModel):
    priority: StrictInt | None = Field(None, ge=1, le=4)
    model: str | None = Field(None, max_length=310)  # вид — по помощнику (`MePatch._code_defaults`)
    effort: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    access: Literal["default", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
                    "read-only", "workspace-write", "danger-full-access"] | None = None


class MePatch(BaseModel):
    name: str | None = Field(None, max_length=64)
    theme: str | None = None
    accent: object = None                   # проверка — по ACCENTS в patch_me: отказ — 422 `accent`
    timezone: str | None = Field(None, min_length=1, max_length=100)
    views: dict[str, str] | None = None     # {раздел: вид} — только присланные разделы
    code_defaults: dict[Literal["claude", "codex", "opencode", "deepseek"], CodeChoice] | None = None
    computer_pin_timeout: StrictInt | None = Field(None, ge=60, le=604800)
    #: язык интерфейса устройства — его push и ntfy (только этой сессии; push-sync.js после смены языка)
    notify_lang: ntfy.Lang | None = None
    memory_on: bool | None = None           # личная память «Чата» (memory.py); null — не трогать

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown timezone") from None
        return value

    @field_validator("code_defaults")
    @classmethod
    def _code_defaults(cls, value):
        if value is None or any(engine == "claude" and choice.effort not in
                               (None, "low", "medium", "high", "xhigh", "max") for engine, choice in value.items()):
            raise ValueError("code_defaults")
        import bridge
        import claude_code

        models = {"opencode": bridge.OPENCODE_MODEL_ID, "deepseek": bridge.DEEPSEEK_MODEL_ID}
        for engine, choice in value.items():
            if choice.model is not None and not (models.get(engine) or bridge.MODEL_ID).fullmatch(choice.model):
                raise ValueError("model")
            if engine == "codex" and choice.effort not in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
                raise ValueError("effort")
            if choice.access is not None and choice.access not in claude_code.ACCESS[engine]:
                raise ValueError("access")
        return value


class ExaKey(BaseModel):
    key: str


class PasswordChange(BaseModel):
    current: str = Field(max_length=PASSWORD_MAX)
    new: str = Field(max_length=PASSWORD_MAX)


def check_password(password: str) -> None:
    if len(password) > PASSWORD_MAX:
        raise HTTPException(422, {"code": "long", "message": f"пароль длиннее {PASSWORD_MAX} символов"})
    if len(password) < security.PASSWORD_MIN:
        raise HTTPException(422, {"code": "short", "message": f"пароль короче {security.PASSWORD_MIN} символов"})


def _me(request: Request) -> dict:
    """Из базы, а не из request.state: после PATCH там прежние значения. Умолчание чата — из раздела."""
    uid = request.state.user.id
    row = state.DB.execute("SELECT id, login, name, is_admin, theme, timezone, views, screen_lock,"
                           " exa_key IS NOT NULL AS exa_key, pin_hash IS NOT NULL AS pin, code_defaults,"
                           " screen_lock_sections, screen_lock_timeout, computer_pin_timeout,"
                           " password_reset, email, email_codes, memory_on, login_notice, ldap_guid FROM users WHERE id = ?",
                           (uid,)).fetchone()
    chat = sections.default(uid, "chat")
    views = json.loads(row["views"])
    accent = views.pop("accent", None)
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "email": row["email"], "email_codes": bool(row["email_codes"]),
            # приложение-аутентификатор: on | off | reenter (ключ сервера сменился), резервных кодов осталось
            "totp": totp.status(uid), "totp_backup_left": totp.backup_left(uid),
            "login_notice": bool(row["login_notice"]),     # письмо о входе с нового устройства
            "theme": row["theme"], "accent": accent if accent in ACCENTS else ACCENTS[0],
            "timezone": row["timezone"], "views": views, "studio_version": release.VERSION,
            "default_connection_id": chat[0].id if chat else None,
            "default_model": chat[1] if chat else None, "csrf": request.state.csrf,
            "capabilities": sections.capabilities(uid), "screen_lock": bool(row["screen_lock"]),
            "exa_key": bool(row["exa_key"]), "pin": bool(row["pin"]), "code_defaults": json.loads(row["code_defaults"]),
            "screen_lock_sections": json.loads(row["screen_lock_sections"]),
            "screen_lock_timeout": row["screen_lock_timeout"], "computer_pin_timeout": row["computer_pin_timeout"],
            # когда пароль сбросил администратор (до «Понятно» или аварийного выхода, account.py), иначе null
            "password_reset": row["password_reset"],
            "ldap": row["ldap_guid"] is not None,
            "memory_on": bool(row["memory_on"]),     # личная память «Чата» (memory.py)
            # флажок администратора «Веб и внешние MCP — только через Cleaner»: «Интернет» вне Cleaner выключен,
            # «Open WebUI» показывает предупреждение
            "cleaner_web": cleaner_route.web_only(), "cleaner_forced": cleaner_route.forced(),
            # языки push и ntfy этой сессии: страница сверяет их с языком интерфейса устройства
            "notify_langs": ntfy.session_langs(request.state.digest)}


@router.get("/me")
async def get_me(request: Request):
    return _me(request)


@router.patch("/me")
async def patch_me(body: MePatch, request: Request):
    uid = request.state.user.id
    sent = body.model_fields_set
    changes = {}
    if "name" in sent:
        name = (body.name or "").strip()
        if not name or any(unicodedata.category(char).startswith("C") for char in name):
            raise HTTPException(422, {"code": "name", "message": "Введите имя без управляющих символов."})
        changes["name"] = name
    if "timezone" in sent:
        if body.timezone is None:
            raise HTTPException(422, {"code": "timezone", "message": "укажите часовой пояс"})
        changes["timezone"] = body.timezone
    if "computer_pin_timeout" in sent:
        if body.computer_pin_timeout is None:
            raise HTTPException(422, {"code": "timeout", "message": "укажите таймер PIN компьютера"})
        changes["computer_pin_timeout"] = body.computer_pin_timeout
    if "theme" in sent:
        if body.theme not in THEMES:
            raise HTTPException(422, {"code": "theme", "message": "тема — auto, dark или light"})
        changes["theme"] = body.theme
    if "accent" in sent and (not isinstance(body.accent, str) or body.accent not in ACCENTS):
        raise HTTPException(422, {"code": "accent", "message": "акцент — " + ", ".join(ACCENTS)})
    if "views" in sent or "accent" in sent:
        if "views" in sent and (body.views is None
                                or any(k not in VIEW_SECTIONS or v not in VIEWS for k, v in body.views.items())):
            raise HTTPException(422, {"code": "views", "message": "вид ленты — preview, tiles или table у photo и video"})
        stored = state.DB.execute("SELECT views FROM users WHERE id = ?", (uid,)).fetchone()["views"]
        views = {**json.loads(stored), **(body.views or {})}
        if "accent" in sent:
            views["accent"] = body.accent
        changes["views"] = json.dumps(views)
    if "code_defaults" in sent:
        stored = json.loads(state.DB.execute("SELECT code_defaults FROM users WHERE id = ?", (uid,)).fetchone()[0])
        for engine, choice in body.code_defaults.items():
            stored[engine] = {**stored.get(engine, {}), **choice.model_dump(exclude_unset=True)}
        priorities = [stored.get(engine, {}).get("priority") for engine in ("claude", "codex", "opencode", "deepseek")]
        # прежний порядок трёх без DeepSeek Harness остаётся верным: он — последним
        if (any(value is not None for value in priorities) and set(priorities) != {1, 2, 3, 4}
                and not (priorities[3] is None and set(priorities[:3]) == {1, 2, 3})):
            raise HTTPException(422, {"code": "priority", "message": "укажите разный приоритет от 1 до 4 для каждого агента"})
        changes["code_defaults"] = json.dumps(stored)
    if body.memory_on is not None:
        changes["memory_on"] = int(body.memory_on)
    if changes:
        cols = ", ".join(f"{k} = ?" for k in changes)        # имена — из кода выше, не из запроса
        state.DB.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), uid))
    if body.notify_lang is not None:
        ntfy.set_session_lang(request.state.digest, body.notify_lang)
    return _me(request)


@router.post("/me/password", status_code=204)
async def change_password(body: PasswordChange, request: Request):
    uid = request.state.user.id
    if state.DB.execute("SELECT ldap_guid FROM users WHERE id=?", (uid,)).fetchone()[0] is not None:
        raise HTTPException(409, {"code": "ldap_password", "message": "Пароль этой учётной записи меняется в Active Directory"})
    confirmed = await auth.confirm_password(uid, body.current, "смена пароля", "текущий пароль неверен",
                                            digest=request.state.digest)
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
    security_log.record(uid, "password", digest=request.state.digest, ip=auth.client_ip(request))
    log.info("пользователь %d: пароль сменён, прочие сессии закрыты", uid)
    return Response(status_code=204)


@router.put("/me/exa-key", status_code=204)
async def put_exa_key(body: ExaKey, request: Request):
    """Ключ Exa — шифром; запросом к Exa не проверяется (тратил бы баланс): неверный скажет первый поиск."""
    uid = request.state.user.id
    key = body.key.strip()
    if not 1 <= len(key) <= EXA_KEY_MAX:
        raise HTTPException(422, {"code": "exa_key", "message": f"ключ Exa — от 1 до {EXA_KEY_MAX} знаков"})
    if not re.fullmatch(r"[\x20-\x7e]+", key):     # заголовок x-api-key — только ASCII: иначе каждый поиск — сбой
        raise HTTPException(422, {"code": "exa_key", "message": "ключ Exa — только латиница, цифры и знаки ASCII: "
                                                                "проверьте, не попали ли кириллица или особый пробел"})
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
        result["cleaner_stalled"] = cleaner_client.stalled(uid)
    # «Мои файлы» на серверах MCP: объём личной папки на каждом (my_files.storage; недоступный — без строки)
    return {**result, **trash.storage(uid, lock_policy.hidden(request.state.digest)),
            "mcp_files": await my_files.storage(uid)}


class FailedSections(BaseModel):
    sections: list[str] = Field(min_length=1, max_length=len(trash.FAILED))


@router.post("/me/storage/failed")
async def move_failed(body: FailedSections, request: Request):
    """Неудачные и отменённые записи разделов — в корзину (trash.py). Пока заперт хоть один раздел — 423
    (`lock_policy.blocks`)."""
    if any(section not in trash.FAILED_SECTIONS for section in body.sections):
        raise HTTPException(422, {"code": "sections", "message": "разделы — " + ", ".join(trash.FAILED_SECTIONS)})
    return {"moved": trash.move_failed(request.state.user.id, body.sections)}


@router.delete("/history")
async def delete_history(body: trash.Confirm, request: Request):
    """Окончательно, вместе с корзиной, и только паролем учётки (`auth.confirm_password`: проверка и паузы — как у
    выключения блокировки). pending_files — сколько файлов на шаре ещё ждут удаления (она была недоступна): страница
    так и скажет. Clear-Site-Data: "cache" — картинки и ролики не остаются в HTTP-кэше этого браузера до суток."""
    await auth.confirm_password(request.state.user.id, body.password, "удаление истории", digest=request.state.digest)
    result = {"pending_files": await history.delete_all(request.state.user.id)}
    pending = state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions WHERE user_id = ?",
                               (request.state.user.id,)).fetchone()[0]
    if pending:
        result["cleaner_pending"] = pending
    return JSONResponse(result, headers={"Clear-Site-Data": auth.CLEAR_CACHE})
