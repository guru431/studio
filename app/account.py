"""Сессии учётки, аварийный выход и журнал безопасности владельца (IDEAS 2026-11-09 «Сессии учётки»).

- `GET /api/me/sessions` — живые сессии учётки: признак (`security_log.handle`, не дайджест), когда создана,
  последняя активность (`sessions.active`, у прежних сессий — `seen`), тип, браузер, ОС, модель, имя и IP, push и
  ntfy этого устройства, текущая ли. Сессии, заведённые прежним кодом (откат), — без даты и вида: NULL.
- `DELETE /api/me/sessions/<признак>` — завершить другую сессию (текущую — обычный «Выйти», 409 `current`).
  Строка уходит, а с ней каскадом push и ntfy устройства; потоки «Агентов» этой сессии получают `closed`, мостам —
  `close` её признака (`bridge.forget`): следующий её запрос — 401, уведомления ей больше не идут.
- `PATCH /api/me/sessions/<признак> {label}` — назвать свою живую сессию (до 80 символов); пустое имя — сброс.
- `POST /api/me/emergency {password, agents}` — аварийный выход с паролем учётки: все сессии (и текущая), все
  ссылки «Поделиться» (и на чаты её компьютеров), доступы участников к проектам её компьютеров (всегда, без
  галочки), push и ntfy всех устройств и прежний шаблон ntfy; `agents: true` — ещё и ключи компьютеров (как отзыв
  из настроек: `agent_events` `revoke`, канал — 4401). Ответ стирает cookie и HTTP-кэш.
- `GET /api/me/security-events?before=<id>` — журнал безопасности страницами, новые сверху; свёрнутые повторы
  неудач — `count` и время первой `first` (security_log.NOISY).
- Сброс пароля администратором (admin.py) ставит `users.password_reset`: `GET /api/me` отдаёт его владельцу, и
  студия после входа советует проверить сессии и сделать аварийный выход. `DELETE /api/me/password-reset` («Понятно»)
  и аварийный выход флаг снимают. Администратору флаг не виден.
- Все пути — под обычными guard и CSRF; частичная блокировка закрывает их, как управление защитой (lock_policy).
- Журнал службы — только id и числа.
"""
import logging
import re
import unicodedata

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictBool, field_validator

import auth
import bridge
import clock
import project_access
import screenlock
import security_log
import state

EVENTS_PAGE = 50
HANDLE = re.compile(r"[0-9a-f]{16}")

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/me")


class Emergency(BaseModel):
    password: str = Field(max_length=screenlock.PASSWORD_MAX)
    agents: StrictBool = False


class SessionLabel(BaseModel):
    label: str = Field(max_length=80)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        if any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("имя не должно содержать управляющие символы")
        return value.strip()


def _session_digest(uid: int, sid: str) -> str:
    if HANDLE.fullmatch(sid):
        for row in state.DB.execute("SELECT digest FROM sessions WHERE user_id = ? AND expires > ?", (uid, clock.NOW())):
            if security_log.handle(row["digest"]) == sid:
                return row["digest"]
    raise HTTPException(404, {"code": "not_found", "message": "такой сессии нет — она уже завершена"})


def _forget(user_id: int, digests: list[str]) -> None:
    """Завершённые сессии: потоки «Агентов» закрыть, мостам — снять открытый PIN, вызовы passkey — забыть."""
    for digest in digests:
        bridge.forget(user_id, digest, close_computers=True)
        for key in [k for k in screenlock.CHALLENGES if k[0] == digest]:
            del screenlock.CHALLENGES[key]


@router.get("/sessions")
async def list_sessions(request: Request):
    uid, current = request.state.user.id, request.state.digest
    rows = state.DB.execute(
        "SELECT s.digest, s.created, COALESCE(s.active, s.seen) AS active, s.device, s.mobile,"
        " s.browser, s.os, s.model, s.label, s.ip,"
        " EXISTS (SELECT 1 FROM push_subs p WHERE p.digest = s.digest) AS push,"
        " EXISTS (SELECT 1 FROM ntfy_devices n WHERE n.digest = s.digest AND n.enabled = 1) AS ntfy"
        " FROM sessions s WHERE s.user_id = ? AND s.expires > ?", (uid, clock.NOW())).fetchall()
    items = [{"id": security_log.handle(r["digest"]), "current": r["digest"] == current, "created": r["created"],
              "active": r["active"], "device": r["device"] or ("phone" if r["mobile"] else None),
              "browser": r["browser"], "os": r["os"], "model": r["model"], "label": r["label"], "ip": r["ip"],
              "push": bool(r["push"]), "ntfy": bool(r["ntfy"])} for r in rows]
    items.sort(key=lambda i: (not i["current"], -(i["active"] or 0)))
    return {"items": items}


@router.patch("/sessions/{sid}", status_code=204)
async def rename_session(sid: str, body: SessionLabel, request: Request):
    uid = request.state.user.id
    digest = _session_digest(uid, sid)
    state.DB.execute("UPDATE sessions SET label = ? WHERE digest = ? AND user_id = ?", (body.label or None, digest, uid))
    return Response(status_code=204)


@router.delete("/sessions/{sid}", status_code=204)
async def end_session(sid: str, request: Request):
    uid, current = request.state.user.id, request.state.digest
    if sid == security_log.handle(current):
        raise HTTPException(409, {"code": "current", "message": "эту сессию закрывает «Выйти»"})
    digest = _session_digest(uid, sid)
    state.DB.execute("DELETE FROM sessions WHERE digest = ? AND user_id = ?", (digest, uid))
    _forget(uid, [digest])
    security_log.record(uid, "session_end", detail=sid, digest=current, ip=auth.client_ip(request))
    log.info("пользователь %d: сессия завершена", uid)
    return Response(status_code=204)


@router.post("/emergency", status_code=204)
async def emergency(body: Emergency, request: Request):
    """Пароль — как у прочих действий из сессии (auth.confirm_password: общая пауза). После проверки — без await
    до конца транзакции: завершается то, что было на момент проверки, и ничего нового не проскочит."""
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "аварийный выход")
    security_log.record(uid, "emergency", detail="agents" if body.agents else None, digest=request.state.digest,
                        ip=auth.client_ip(request))
    digests = [r["digest"] for r in state.DB.execute("SELECT digest FROM sessions WHERE user_id = ?", (uid,))]
    state.DB.execute("BEGIN")
    try:
        # выдачи проектов её компьютеров — всегда: украденная сессия могла выдать доступ своей учётке
        grants = project_access.granted(uid)
        project_access.drop(grants)
        for table in ("chat_shares", "push_subs", "ntfy_devices", "ntfy_settings", "sessions"):
            state.DB.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))   # имена — из кода
        state.DB.execute("DELETE FROM chat_shares WHERE agent_id IN (SELECT id FROM agents WHERE user_id = ?)", (uid,))
        state.DB.execute("UPDATE users SET password_reset = NULL WHERE id = ?", (uid,))  # совет сброса исполнен
        revoked = state.DB.execute("DELETE FROM agents WHERE user_id = ? RETURNING id, name",
                                   (uid,)).fetchall() if body.agents else []
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    ip = auth.client_ip(request)
    for agent in revoked:
        bridge.record(uid, agent["id"], agent["name"], "revoke", device="аварийный выход", ip=ip)
    project_access.wake(grants)
    _forget(uid, digests)
    for agent in revoked:
        await bridge.drop(agent["id"])
    log.info("пользователь %d: аварийный выход, сессий %d, компьютеров %d, выдач проектов %d",
             uid, len(digests), len(revoked), len(grants))
    response = Response(status_code=204, headers={"Clear-Site-Data": auth.CLEAR_CACHE})
    response.delete_cookie(auth.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.delete("/password-reset", status_code=204)
async def dismiss_password_reset(request: Request):
    """«Понятно» у совета после сброса пароля администратором: флаг снят, совет больше не показывается."""
    state.DB.execute("UPDATE users SET password_reset = NULL WHERE id = ?", (request.state.user.id,))
    return Response(status_code=204)


@router.get("/security-events")
async def security_events(request: Request, before: int | None = None):
    uid, current = request.state.user.id, security_log.handle(request.state.digest)
    where, args = ("", ()) if before is None else (" AND id < ?", (before,))
    rows = state.DB.execute("SELECT id, created, kind, detail, device, session, ip, count, first FROM security_events"
                            f" WHERE user_id = ?{where} ORDER BY id DESC LIMIT ?", (uid, *args, EVENTS_PAGE)).fetchall()
    items = [dict(r) | {"current": r["session"] == current} for r in rows]
    return {"items": items, "next": items[-1]["id"] if len(items) == EVENTS_PAGE else None}
