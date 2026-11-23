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
- `GET /api/me/access` — все выдачи проектов компьютеров учётки (участник, компьютер, проект — имя от моста, если
  он на связи и открыт, иначе id; `missing` — папки уже нет) и все её ссылки «Поделиться» (срок, открытия, неверные
  PIN; куда перейти — `chat_id` или `computer_id`/`project`/`session_id`). `GET /api/me/access/shares/<id>/link` —
  адрес своей ссылки для «Копировать ссылку» (`no-store`). `DELETE /api/me/access/members/<компьютер>/<проект>/<участник>`
  и `DELETE /api/me/access/shares/<id>` — отозвать одну, без моста; `DELETE /api/me/access` — «Отозвать всё». `DELETE /api/me/access/shared/<компьютер>` —
  участник сам покидает совместные проекты чужого компьютера (`project_access.leave`).
- `GET /api/me/security-events?before=<id>` — журнал безопасности страницами, новые сверху; свёрнутые повторы
  неудач — `count` и время первой `first` (security_log.NOISY).
- Сброс пароля администратором (admin.py) ставит `users.password_reset`: `GET /api/me` отдаёт его владельцу, и
  студия после входа советует проверить сессии и сделать аварийный выход. `DELETE /api/me/password-reset` («Понятно»)
  и аварийный выход флаг снимают. Администратору флаг не виден.
- Все пути — под обычными guard и CSRF; частичная блокировка закрывает их, как управление защитой (lock_policy).
- Журнал службы — только id и числа.
"""
import asyncio
import logging
import re
import unicodedata

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictBool, field_validator

import auth
import bridge
import clock
import db
import email_accounts
import history
import images
import media
import operations
import owui
import project_access
import screenlock
import security
import security_log
import state

EVENTS_PAGE = 50
HANDLE = re.compile(r"[0-9a-f]{16}")
#: сколько список доступов ждёт от моста имена проектов; не дождался — показывает id проекта
ACCESS_NAMES_SEC = 5

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


class DeleteAccount(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=screenlock.PASSWORD_MAX)


def keep_active_admin(uid: int) -> None:
    if state.DB.execute("SELECT 1 FROM users WHERE id = ? AND is_admin = 1 AND disabled = 0"
                        " AND NOT EXISTS (SELECT 1 FROM users WHERE is_admin = 1 AND disabled = 0 AND id != ?)",
                        (uid, uid)).fetchone():
        raise HTTPException(409, {"code": "last_admin", "message": "Нужен хотя бы один активный администратор."})


async def delete_user_data(uid: int, *, allow_disabled: bool = False) -> int:
    operations.check(uid, allow_disabled=allow_disabled)
    keep_active_admin(uid)
    with operations.barrier(uid):
        state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
        auth.end_sessions(uid)
        email_accounts.revoke_invited_by(uid)
        await bridge.drop_user(uid)
        await owui.drop_user(uid)
        await history.delete_all(uid)
        if not await images.settle_user(uid):
            log.warning("пользователь %d: задания картинок не кончились — карту освободит срок аренды", uid)
        pending = media.pending(uid)
        db.retire_user_id(state.DB, uid)
        state.DB.execute("DELETE FROM users WHERE id = ?", (uid,))
    return pending


@router.delete("/account")
async def delete_own_account(body: DeleteAccount, request: Request):
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "удаление учётки", digest=request.state.digest)
    row = state.DB.execute("SELECT login FROM users WHERE id = ? AND disabled = 0", (uid,)).fetchone()
    if row is None or body.login != row["login"]:
        raise HTTPException(409, {"code": "login", "message": "Введите точный логин учётки."})
    pending = await delete_user_data(uid)
    log.info("пользователь %d: учётка удалена, файлов в очереди %d", uid, pending)
    response = JSONResponse({"pending_files": pending}, headers={"Clear-Site-Data": auth.CLEAR_CACHE})
    response.delete_cookie(auth.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


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


@router.delete("/sessions")
async def end_other_sessions(request: Request):
    uid, current = request.state.user.id, request.state.digest
    digests = [row["digest"] for row in state.DB.execute(
        "DELETE FROM sessions WHERE user_id = ? AND digest != ? RETURNING digest", (uid, current)).fetchall()]
    _forget(uid, digests)
    if digests:
        security_log.record(uid, "sessions_end", digest=current, ip=auth.client_ip(request))
        log.info("пользователь %d: завершено других сессий %d", uid, len(digests))
    return {"ended": len(digests)}


@router.post("/emergency", status_code=204)
async def emergency(body: Emergency, request: Request):
    """Пароль — как у прочих действий из сессии (auth.confirm_password: пауза этой сессии и общий предел учётки). После проверки — без await
    до конца транзакции: завершается то, что было на момент проверки, и ничего нового не проскочит."""
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "аварийный выход", digest=request.state.digest)
    security_log.record(uid, "emergency", detail="agents" if body.agents else None, digest=request.state.digest,
                        ip=auth.client_ip(request))
    digests = [r["digest"] for r in state.DB.execute("SELECT digest FROM sessions WHERE user_id = ?", (uid,))]
    state.DB.execute("BEGIN")
    try:
        # выдачи проектов её компьютеров — всегда: украденная сессия могла выдать доступ своей учётке
        grants = project_access.granted(uid)
        project_access.drop(grants)
        # и почтовые ссылки и коды (смена почты, восстановление, код входа): чужой, знающий пароль, мог их запросить;
        # и ссылки на файлы «Моих файлов» — уже открытый по ним файл закрывает проверка строки на каждом запросе
        for table in ("chat_shares", "file_links", "push_subs", "ntfy_devices", "ntfy_settings", "email_tokens",
                      "sessions"):
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


async def _project_names(request: Request, aids) -> dict:
    """Имена проектов компьютеров владельца от мостов: `{aid: {pid: имя}}` или `None` — компьютер не на связи, закрыт
    PIN или молчит дольше `ACCESS_NAMES_SEC`. Фоновая просьба: срок PIN не продлевает."""
    import claude_code

    async def one(aid):
        try:
            data = await asyncio.wait_for(claude_code.ask(request, aid, "projects", passive=True, quiet=True),
                                          ACCESS_NAMES_SEC)
        except (HTTPException, TimeoutError):
            return aid, None
        return aid, {i["pid"]: i["name"] for i in data.get("items", [])
                     if isinstance(i, dict) and isinstance(i.get("pid"), str) and isinstance(i.get("name"), str)}

    return dict(await asyncio.gather(*(one(aid) for aid in aids)))


def _place(names: dict, aid, pid) -> dict:
    """Имя проекта, если мост его назвал; `missing` — мост ответил, а такой папки нет (удалили, переименовали)."""
    known = names.get(aid)
    return {"name": None if known is None else known.get(pid), "missing": None if known is None else pid not in known}


def _label(blob) -> "str | None":
    """Название чата «Агентов» у ссылки (`chat_shares.label`, шаг 53); до шага или после смены ключа — нет."""
    if blob is None:
        return None
    try:
        return security.decrypt(blob)
    except InvalidToken:
        return None


def _item_share(row) -> dict:
    """Строка ссылки как dict; у ссылки на запись раздела (`chat_shares.ITEMS`) — ещё `section`, название записи
    (имя, у картинки и ролика — начало промпта) и в корзине ли она: тогда получателю ссылка недоступна."""
    import chat_shares
    out = dict(row) | {"section": None}
    if row["item_kind"] in chat_shares.ITEMS:
        column = "prompt" if row["item_kind"] in ("images", "videos") else "name"
        item = state.DB.execute(f"SELECT substr({column}, 1, 80) AS title, deleted IS NOT NULL AS trashed"
                                f" FROM {row['item_kind']} WHERE id = ?", (row["item_id"],)).fetchone()
        out |= {"section": chat_shares.ITEMS[row["item_kind"]][0], "title": item["title"] if item else None,
                "trashed": bool(item and item["trashed"])}
    return out


def _file_links(uid) -> int:
    """Действующие ссылки на файлы «Моих файлов» учётки (my_files.py: список — там же, «Мои ссылки»)."""
    return state.DB.execute("SELECT COUNT(*) FROM file_links WHERE user_id = ? AND revoked IS NULL AND expires > ?",
                            (uid, clock.NOW())).fetchone()[0]


@router.get("/access")
async def access(request: Request):
    """Все выданные доступы к проектам компьютеров учётки (и к исчезнувшим папкам), все её ссылки «Поделиться» и
    число действующих ссылок на файлы (`file_links`; сами ссылки — в «Моих файлах»)."""
    uid = request.state.user.id
    members = state.DB.execute(
        "SELECT m.agent_id, a.name AS computer, m.project, m.user_id, u.login, u.disabled, m.created"
        " FROM project_members m JOIN agents a ON a.id = m.agent_id JOIN users u ON u.id = m.user_id"
        " WHERE a.user_id = ? ORDER BY a.name, m.project, u.login", (uid,)).fetchall()
    shares = [_item_share(r) for r in state.DB.execute(
        "SELECT s.id, s.chat_id, s.agent_id, a.name AS computer, s.project, s.session_id, c.title, s.label, s.created,"
        " s.expires, c.deleted IS NOT NULL AS trashed, c.protected_used = 1 AS protected,"
        " s.views, s.viewed, s.fails, s.item_kind, s.item_id FROM chat_shares s LEFT JOIN chats c ON c.id = s.chat_id"
        " LEFT JOIN agents a ON a.id = s.agent_id WHERE s.user_id = ? AND (s.expires IS NULL OR s.expires > ?)"
        " ORDER BY s.created DESC", (uid, clock.NOW()))]
    aids = sorted({r["agent_id"] for r in members} | {r["agent_id"] for r in shares if r["agent_id"] is not None})
    names = await _project_names(request, aids) if aids else {}
    return {"members": [{"computer_id": r["agent_id"], "computer": r["computer"], "project": r["project"],
                         **_place(names, r["agent_id"], r["project"]), "user_id": r["user_id"], "login": r["login"],
                         "disabled": bool(r["disabled"]), "created": r["created"]} for r in members],
            "shares": [{"id": r["id"], "kind": r["section"] or ("code" if r["agent_id"] else "chat"),
                        "title": r["title"] if r["chat_id"] or r["section"] else _label(r["label"]),
                        # запись раздела (chat_shares.ITEMS): открыть — #/<раздел>?id=<item_id>
                        "item_id": r["item_id"],
                        "chat_id": r["chat_id"], "computer_id": r["agent_id"], "session_id": r["session_id"],
                        # чат в корзине: ссылка получателю недоступна, после «Вернуть» откроется снова (trash.py)
                        "trashed": bool(r["trashed"]),
                        # в чате сработал защищённый источник: ссылка недоступна навсегда (chat_shares.LIVE)
                        "protected": bool(r["protected"]),
                        "computer": r["computer"], "project": r["project"],
                        **(_place(names, r["agent_id"], r["project"]) if r["agent_id"] else {}),
                        "created": r["created"], "expires": r["expires"], "views": r["views"], "viewed": r["viewed"],
                        "fails": r["fails"]} for r in shares],
            "file_links": _file_links(uid)}


@router.delete("/access/members/{aid}/{pid}/{member}", status_code=204)
async def revoke_member(aid: int, pid: str, member: int, request: Request):
    """Отозвать одну выдачу без похода к мосту — и к папке, которой уже нет (`_check` проекта не нужен)."""
    import claude_code
    claude_code.check_pid(pid)
    project_access.revoke(request, aid, pid, member)
    return Response(status_code=204)


@router.delete("/access/shared/{aid}", status_code=204)
async def leave_shared(aid: int, request: Request):
    """Участник покидает совместные проекты чужого компьютера («Настройки → Агенты → Компьютеры»), без моста."""
    project_access.leave(request, aid)
    return Response(status_code=204)


@router.get("/access/shares/{share_id}/link")
async def share_link(share_id: str, request: Request):
    """«Копировать ссылку» из общего списка: адрес расшифровывается только владельцу живой ссылки; ответ — `no-store`,
    в журнал — ничего. Сменили `STUDIO_DATA_KEY` — адреса не восстановить (409 `share_key`), создать новую."""
    row = state.DB.execute("SELECT token_enc FROM chat_shares WHERE id = ? AND user_id = ?"
                           " AND (expires IS NULL OR expires > ?)", (share_id, request.state.user.id, clock.NOW())).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "not_found", "message": "такой ссылки нет — она отключена или истекла"})
    try:
        token = security.decrypt(row["token_enc"])
    except InvalidToken:
        raise HTTPException(409, {"code": "share_key", "message": "адрес этой ссылки не восстановить — создайте новую"}) from None
    return JSONResponse({"url": state.CFG.origin.rstrip("/") + "/share#" + token}, headers={"Cache-Control": "no-store"})


@router.delete("/access/shares/{share_id}", status_code=204)
async def revoke_share(share_id: str, request: Request):
    """Отключить свою ссылку «Поделиться» из общего списка: и ту, чей чат или компьютер сейчас не открыть."""
    uid = request.state.user.id
    import chat_shares
    row = state.DB.execute("DELETE FROM chat_shares WHERE id = ? AND user_id = ? RETURNING agent_id, item_kind",
                           (share_id, uid)).fetchone()
    if row is not None:
        security_log.record(uid, "share_revoke", detail=chat_shares._detail(row["agent_id"], row["item_kind"]),
                            digest=request.state.digest, ip=auth.client_ip(request))
    return Response(status_code=204)


@router.delete("/access")
async def revoke_access(request: Request):
    """«Отозвать всё»: выдачи проектов компьютеров учётки, все ссылки «Поделиться» (и на чаты её компьютеров) и все
    ссылки на файлы «Моих файлов» (открытый по ним файл закрывает проверка строки на каждом запросе `/f/file`)."""
    uid = request.state.user.id
    members = project_access.revoke_all(request)
    shares = state.DB.execute("DELETE FROM chat_shares WHERE user_id = ? OR agent_id IN"
                              " (SELECT id FROM agents WHERE user_id = ?)", (uid, uid)).rowcount
    files = _file_links(uid)
    state.DB.execute("DELETE FROM file_links WHERE user_id = ?", (uid,))      # и отозванные, и истёкшие
    if shares:
        security_log.record(uid, "share_revoke", detail="all", digest=request.state.digest, ip=auth.client_ip(request))
    if files:
        security_log.record(uid, "file_link_revoke", detail="all", digest=request.state.digest,
                            ip=auth.client_ip(request))
    log.info("пользователь %d: отозваны все ссылки «Поделиться» — %d, ссылки на файлы — %d", uid, shares, files)
    return {"members": members, "shares": shares, "file_links": files}


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
