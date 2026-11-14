"""Участники проектов: явное разрешение владельца, только id в базе и журнале.

Выдача привязана к pid, а pid — к имени папки: удаление или переименование папки выдачу не снимает, новая папка
с тем же именем открывается прежнему участнику. Поэтому отзыв (`revoke`) моста не спрашивает, а все выдачи
владельца, и к исчезнувшим папкам, видны и отзываются в «Настройки → Защита» (account.py `/api/me/access`).
Аварийный выход снимает их всегда (`granted`, `drop`, `wake`)."""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import bridge
import clock
import security_log
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/code/agents/{aid}/projects/{pid}/members")


def projects(uid: int, aid: int) -> list[str]:
    return [r[0] for r in state.DB.execute(
        "SELECT m.project FROM project_members m JOIN agents a ON a.id = m.agent_id"
        " JOIN users u ON u.id = a.user_id WHERE m.user_id = ? AND m.agent_id = ? AND u.disabled = 0",
        (uid, aid))]


def agent(uid: int, aid: int, pid: str | None = None, *, shared: bool = False):
    row = state.DB.execute("SELECT id, user_id, name, has_pin FROM agents WHERE id = ?", (aid,)).fetchone()
    if row is not None and (row["user_id"] == uid or
            (pid in projects(uid, aid) if pid is not None else shared and bool(projects(uid, aid)))):
        return row
    raise HTTPException(404, {"code": "not_found", "message": "нет такого компьютера", "computer": True})


def owner(request: Request, aid: int):
    return agent(request.state.user.id, aid)


class Member(BaseModel):
    login: str = Field(min_length=1, max_length=254)


async def _check(request: Request, aid: int, pid: str):
    import claude_code
    owner(request, aid)
    claude_code.check_pid(pid)
    # Подтверждает существование проекта и PIN владельца; названия не сохраняются.
    found = await claude_code.ask(request, aid, "projects")
    owner(request, aid)
    if not any(i.get("pid") == pid for i in found.get("items", []) if isinstance(i, dict)):
        raise HTTPException(404, {"code": "not_found", "message": "нет такого проекта"})


@router.get("")
async def members(aid: int, pid: str, request: Request):
    await _check(request, aid, pid)
    return {"items": [dict(r) for r in state.DB.execute(
        "SELECT u.id, u.login, u.disabled FROM project_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.agent_id = ? AND m.project = ? ORDER BY u.login", (aid, pid))]}


def _journal(request: Request, row, kind: str) -> None:
    """Выдача и отзыв — в журнал безопасности владельца и в историю подключений компьютера: украденная сессия не
    выдаст доступ молча. Только виды и id — ни логинов, ни проектов."""
    ip = auth.client_ip(request)
    security_log.record(request.state.user.id, "project_member_" + kind, digest=request.state.digest, ip=ip)
    bridge.record(request.state.user.id, row["id"], row["name"], "member_" + kind, device=bridge.device(request), ip=ip)


def drop(grants) -> None:
    """В транзакции зовущего: выдачи `(agent_id, project, user_id)` — прочь вместе с личными вкладками и местом
    участника и его ссылками «Поделиться» на этот проект (их создавал прежний код). После COMMIT — `wake(grants)`."""
    for aid, pid, uid in grants:
        for table in ("project_members", "code_member_tabs", "chat_shares"):     # имена — из кода
            state.DB.execute(f"DELETE FROM {table} WHERE agent_id = ? AND project = ? AND user_id = ?", (aid, pid, uid))
        state.DB.execute("UPDATE code_place SET agent_id = NULL, project = NULL WHERE user_id = ?"
                         " AND agent_id = ? AND project = ?", (uid, aid, pid))


def wake(grants) -> None:
    """После отзыва: память службы участника о проекте — прочь, потоки компьютера — разбудить."""
    import claude_code
    for aid, pid, uid in grants:
        pdf = claude_code.PDFS.get(uid)
        if pdf and pdf[0][:2] == (aid, pid):
            del claude_code.PDFS[uid]
        view = claude_code.code_visualizations.VIEWS.get(uid)
        if view and view[2:4] == (aid, pid):
            del claude_code.code_visualizations.VIEWS[uid]
    # Генератор SSE проверяет разрешение до выдачи каждого события; будим и тихие потоки.
    computers = {aid for aid, _, _ in grants}
    for (agent_id, _), queues in list(bridge.WATCHERS.items()):
        if agent_id in computers:
            for q in list(queues):
                if not q.full():
                    q.put_nowait(("ping", None))


def granted(owner_id: int) -> list[tuple[int, str, int]]:
    """Все выдачи на компьютерах учётки, и к проектам, которых уже нет (выдача привязана к имени папки)."""
    return [tuple(r) for r in state.DB.execute(
        "SELECT m.agent_id, m.project, m.user_id FROM project_members m JOIN agents a ON a.id = m.agent_id"
        " WHERE a.user_id = ?", (owner_id,))]


def revoke(request: Request, aid: int, pid: str, uid: int) -> bool:
    """Отозвать одну выдачу компьютера владельца без похода к мосту: папку могли удалить или переименовать."""
    row = owner(request, aid)
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        found = state.DB.execute("SELECT 1 FROM project_members WHERE agent_id = ? AND project = ? AND user_id = ?",
                                 (aid, pid, uid)).fetchone() is not None
        drop([(aid, pid, uid)])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    wake([(aid, pid, uid)])
    if found:
        _journal(request, row, "remove")
        log.info("пользователь %d: отозван доступ участника %d к проекту компьютера %d", request.state.user.id, uid, aid)
    return found


def revoke_all(request: Request) -> int:
    """«Отозвать всё»: все выдачи компьютеров владельца, и к исчезнувшим папкам; итог — сколько снято."""
    uid = request.state.user.id
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        grants = granted(uid)
        drop(grants)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    wake(grants)
    computers = {r["id"]: r for r in state.DB.execute("SELECT id, name FROM agents WHERE user_id = ?", (uid,))}
    for aid, _, _ in grants:
        _journal(request, computers[aid], "remove")
    if grants:
        log.info("пользователь %d: отозваны все доступы к проектам — %d", uid, len(grants))
    return len(grants)


@router.post("", status_code=201)
async def add_member(aid: int, pid: str, body: Member, request: Request):
    await _check(request, aid, pid)
    link = bridge.LINKS.get(aid)
    if link is None or not link.hello.get("project_sharing"):
        raise HTTPException(409, {"code": "unsupported", "message": "обновите агент для совместной работы"})
    identity = body.login.strip().lower()
    user = state.DB.execute("SELECT id FROM users WHERE (login = ? OR email = ?) AND disabled = 0",
                            (identity, identity)).fetchone()
    if user is None or user["id"] == request.state.user.id:
        raise HTTPException(422, {"code": "member", "message": "укажите логин или email другого активного пользователя Студии"})
    if state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created) VALUES (?, ?, ?, ?)"
                        " ON CONFLICT DO NOTHING", (aid, pid, user["id"], clock.NOW())).rowcount:
        _journal(request, owner(request, aid), "add")
        log.info("пользователь %d: выдан доступ участнику %d к проекту компьютера %d",
                 request.state.user.id, user["id"], aid)
    return {"id": user["id"]}


@router.delete("/{uid}", status_code=204)
async def remove_member(aid: int, pid: str, uid: int, request: Request):
    import claude_code
    owner(request, aid)
    claude_code.check_pid(pid)
    revoke(request, aid, pid, uid)
