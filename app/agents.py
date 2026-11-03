"""Компьютеры с мостом Claude Code: привязка и учёт (спека 2026-10-28-studio-claude-code-design, 6.1, 6.2, 6.4;
протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md, §1).

- Привязка — `POST /api/agent/enroll {login, password, name}` без сессии (`auth.OPEN_API`): учётка студии —
  право подключать компьютеры, поэтому логин и пароль проверяет `auth.check_password` — тем же путём, с теми
  же паузами и общим счётом неудач, что и `/login`. Ключ устройства — случайный, в ответе один раз, в базе —
  только `security.digest` (как сессия). Смена пароля его не гасит; отзыв — `DELETE /api/agents/<id>` из
  студии или `DELETE /api/agent/enroll` самим мостом (ключ — заголовком `Authorization`; «Отключиться» в окне
  моста); канал на связи закрывается 4401 (`bridge.drop`). «На связи», модели и усилия — из канала (`bridge.LINKS`).
- Установщик моста — `GET /api/agent/download` (только вошедшим): `AGENT_SETUP` рядом с кодом кладёт выкладка
  (`deploy/deploy.sh` собирает EXE и ZIP из одного HEAD); секретов в них нет.
  `GET /api/agent/package` — ZIP самому мосту по действующему ключу устройства активной учётки.
- Компьютеры и история подключений — только свои: чужой id — 404. История только добавляется (`bridge.record`)
  и только читается: ни DELETE, ни «Удалить всю историю».
- Журнал — id и коды: ни ключа, ни логина и пароля, ни имени компьютера.
"""
import asyncio
import json
import logging
import unicodedata
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import auth
import agent_release
import bridge
import clock
import security
import state

log = logging.getLogger("studio")
router = APIRouter()

NAME_MAX = 64
EVENTS_PAGE = 50
#: архив моста — `<корень кода>/agent/studio-agent.zip`, рядом с `app/` (кладёт выкладка; тесты подменяют)
AGENT_ZIP = Path(__file__).resolve().parent.parent / "agent" / "studio-agent.zip"
AGENT_SETUP = AGENT_ZIP.with_name("studio-agent-setup.exe")
FOREIGN = "запрос пришёл с чужого сайта"


def _refuse(ip: str, status: int, code: str, message: str, what: str = "привязка") -> HTTPException:
    log.warning("%s с %s: %s", what, ip, code)
    return HTTPException(status, {"code": code, "message": message})


@router.post("/api/agent/enroll")
async def enroll(request: Request):
    ip = auth.client_ip(request)
    if auth.foreign_origin(request):
        raise _refuse(ip, 403, "origin", FOREIGN)
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
    return JSONResponse({"agent_id": agent_id, "device_key": key, "login": name}, status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.delete("/api/agent/enroll")
async def unenroll(request: Request):
    """Отвязка самим мостом («Отключиться»): ключ устройства — заголовком, как у канала; учётка может быть и
    отключённой — забыть компьютер мост вправе. Дальше — как отзыв из студии: строка прочь, событие `revoke`
    (устройство — «мост»), канал на связи — 4401."""
    ip = auth.client_ip(request)
    if auth.foreign_origin(request):
        raise _refuse(ip, 403, "origin", FOREIGN, "отвязка")
    header = request.headers.get("authorization", "")
    row = None
    if header.startswith("Bearer ") and len(header) > len("Bearer "):
        row = state.DB.execute("DELETE FROM agents WHERE key_hash = ? RETURNING id, user_id, name",
                               (security.digest(header[len("Bearer "):]),)).fetchone()
    if row is None:
        raise _refuse(ip, 401, "key", "ключ компьютера не принят", "отвязка")
    bridge.record(row["user_id"], row["id"], row["name"], "revoke", device="мост", ip=ip)
    log.info("компьютер %d: отвязан мостом", row["id"])
    await bridge.drop(row["id"])
    return Response(status_code=204)


@router.get("/api/agent/download")
async def download(request: Request):
    """Один установочный EXE для Windows — только вошедшим."""
    if not AGENT_SETUP.is_file():
        raise HTTPException(404, {"code": "no_agent",
                                  "message": "установщика моста нет на сервере — его кладёт выкладка студии"})
    log.info("пользователь %d: скачан мост", request.state.user.id)
    return FileResponse(AGENT_SETUP, media_type="application/octet-stream", filename="studio-agent-setup.exe",
                        headers={"Cache-Control": "private, no-cache"})


@router.get("/api/agents")
async def list_agents(request: Request):
    release = await asyncio.to_thread(agent_release.release, AGENT_ZIP)
    rows = state.DB.execute("SELECT id, name, has_pin, cc_version, projects_root, created, last_seen FROM agents"
                            " WHERE user_id = ? ORDER BY created, id", (request.state.user.id,))
    items = []
    for r in rows:
        link = bridge.LINKS.get(r["id"])
        hello = link.hello if link is not None else {}
        items.append({"id": r["id"], "name": r["name"], "online": link is not None, "has_pin": bool(r["has_pin"]),
                      "agent_version": hello.get("agent_version"), "agent_update": hello.get("agent_update", False),
                      "latest_version": release["version"] if release else None,
                      "cc_version": r["cc_version"], "projects_root": r["projects_root"], "created": r["created"],
                      "last_seen": r["last_seen"], "models": hello.get("models", []),
                      "efforts": hello.get("efforts", []), "images": hello.get("images", False),
                      "chat_state": hello.get("chat_state", False), "defaults": hello.get("defaults", {}),
                      "chat_control": hello.get("chat_control", False),
                      "attachments": hello.get("attachments", False),
                      "project_activity": hello.get("project_activity", False),
                      "project_summary": hello.get("project_summary", False),
                      "metadata_cache": hello.get("metadata_cache", False),
                      "computer_info": hello.get("computer_info", False),
                      "desktop": hello.get("desktop", False),
                      "file_stats": hello.get("file_stats", False),
                      "file_operations": hello.get("file_operations", False),
                      "file_suggestions": hello.get("file_suggestions", False),
                      "chat_search": hello.get("chat_search", False),
                      "queue_edit": hello.get("queue_edit", False),
                      "chat_fork": hello.get("chat_fork", False),
                      "subagents": hello.get("subagents", False),
                      "worktrees": hello.get("worktrees", False),
                      "subscription_usage": hello.get("subscription_usage", False),
                      **{engine: hello[engine] for engine in ("codex", "opencode") if engine in hello}})
    return {"items": items}


@router.get("/api/agent/package")
async def update_package(request: Request):
    """Загрузка самим мостом: только действующий ключ устройства активной учётки."""
    header = request.headers.get("authorization", "")
    row = state.DB.execute("SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id"
                           " WHERE a.key_hash = ? AND u.disabled = 0",
                           (security.digest(header[7:]),)).fetchone() if header.startswith("Bearer ") else None
    if row is None:
        raise HTTPException(401, {"code": "key", "message": "ключ компьютера не принят"})
    if auth.foreign_origin(request):
        raise HTTPException(403, {"code": "origin", "message": FOREIGN})
    if not AGENT_ZIP.is_file():
        raise HTTPException(404, {"code": "no_agent", "message": "архива агента нет на сервере"})
    return FileResponse(AGENT_ZIP, media_type="application/zip", headers={"Cache-Control": "no-store"})


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
    await bridge.drop(aid)
    return Response(status_code=204)
