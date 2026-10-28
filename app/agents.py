"""Компьютеры с мостом Claude Code: привязка и учёт (спека 2026-10-28-studio-claude-code-design, 6.1, 6.2, 6.4;
протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md, §1).

- Привязка — `POST /api/agent/enroll {login, password, name}` без сессии (`auth.OPEN_API`): учётка студии —
  право подключать компьютеры, поэтому логин и пароль проверяет `auth.check_password` — тем же путём, с теми
  же паузами и общим счётом неудач, что и `/login`. Ключ устройства — случайный, в ответе один раз, в базе —
  только `security.digest` (как сессия). Смена пароля его не гасит; отзыв — только `DELETE /api/agents/<id>`.
- Компьютеры и история подключений — только свои: чужой id — 404. История только добавляется (`bridge.record`)
  и только читается: ни DELETE, ни «Удалить всю историю».
- Журнал — id и коды: ни ключа, ни логина и пароля, ни имени компьютера.
"""
import json
import logging
import unicodedata

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import auth
import bridge
import clock
import security
import state

log = logging.getLogger("studio")
router = APIRouter()

NAME_MAX = 64
EVENTS_PAGE = 50


def _refuse(ip: str, status: int, code: str, message: str) -> HTTPException:
    log.warning("привязка с %s: %s", ip, code)
    return HTTPException(status, {"code": code, "message": message})


@router.post("/api/agent/enroll")
async def enroll(request: Request):
    ip = auth.client_ip(request)
    if auth.foreign_origin(request):
        raise _refuse(ip, 403, "origin", "запрос пришёл с чужого сайта")
    body = await auth.small_body(request, auth.LOGIN_BODY_MAX)
    if body is None:
        raise _refuse(ip, 413, "too_long", "слишком длинный запрос")
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    fields = [data.get(k, "") for k in ("login", "password", "name")] if isinstance(data, dict) else None
    if fields is None or not all(isinstance(v, str) for v in fields):
        raise _refuse(ip, 422, "invalid", "неверные данные запроса")
    name, password, agent_name = fields[0].strip().lower(), fields[1], fields[2].strip()
    if not 1 <= len(agent_name) <= NAME_MAX or any(unicodedata.category(c).startswith("C") for c in agent_name):
        raise _refuse(ip, 422, "name", "имя компьютера — от 1 до 64 знаков без управляющих")
    if auth.locked(name, ip):
        raise _refuse(ip, 429, "locked", "слишком много попыток — подождите 15 минут")
    uid = await auth.check_password(name, password, ip)
    if uid is None:
        raise _refuse(ip, 401, "login", "неверный логин или пароль")
    # без await после сверки учётки: ключ получает проверенная живая учётка
    key = security.new_token()
    agent_id = state.DB.execute("INSERT INTO agents(user_id, name, key_hash, created) VALUES (?, ?, ?, ?)",
                                (uid, agent_name, security.digest(key), clock.NOW())).lastrowid
    bridge.record(uid, agent_id, agent_name, "enroll", ip=ip)
    log.info("пользователь %d: компьютер %d привязан", uid, agent_id)
    return JSONResponse({"agent_id": agent_id, "device_key": key}, status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.get("/api/agents")
async def list_agents(request: Request):
    rows = state.DB.execute("SELECT id, name, has_pin, cc_version, projects_root, created, last_seen FROM agents"
                            " WHERE user_id = ? ORDER BY created, id", (request.state.user.id,))
    return {"items": [{"id": r["id"], "name": r["name"], "online": False, "has_pin": bool(r["has_pin"]),
                       "cc_version": r["cc_version"], "projects_root": r["projects_root"], "created": r["created"],
                       "last_seen": r["last_seen"], "models": [], "efforts": []} for r in rows]}


@router.get("/api/agents/events")
async def list_events(request: Request, before: int | None = None):
    where, args = ("", ()) if before is None else (" AND id < ?", (before,))
    rows = state.DB.execute("SELECT id, created, kind, agent_id, agent_name, device, ip FROM agent_events"
                            f" WHERE user_id = ?{where} ORDER BY id DESC LIMIT ?",
                            (request.state.user.id, *args, EVENTS_PAGE)).fetchall()
    items = [dict(r) for r in rows]
    return {"items": items, "next": items[-1]["id"] if len(items) == EVENTS_PAGE else None}


@router.delete("/api/agents/{aid}")
async def revoke(request: Request, aid: int):
    uid = request.state.user.id
    row = state.DB.execute("DELETE FROM agents WHERE id = ? AND user_id = ? RETURNING name", (aid, uid)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "not_found", "message": "нет такого компьютера"})
    bridge.record(uid, aid, row["name"], "revoke", device=bridge.device(request), ip=auth.client_ip(request))
    log.info("пользователь %d: компьютер %d отозван", uid, aid)
    return Response(status_code=204)
