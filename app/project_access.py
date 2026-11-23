"""Участники проектов: явное разрешение владельца, только id в базе и журнале.

Выдача привязана к pid, а pid — к имени папки: удаление или переименование папки выдачу не снимает, новая папка
с тем же именем открывается прежнему участнику. Поэтому отзыв (`revoke`) моста не спрашивает, а все выдачи
владельца, и к исчезнувшим папкам, видны и отзываются в «Настройки → Защита» (account.py `/api/me/access`).
Аварийный выход снимает их всегда (`granted`, `drop`, `wake`).

Роль выдачи (шаг 74): соавтор (`editor`) или наблюдатель (`viewer`) — ему сервер пропускает только читающие
просьбы `READ_OPS`, изменяющие маршруты — 403 `readonly` (`editable`). Срок (`expires`, `TERMS`): истёкшая выдача
не действует и снимается при первом обращении (`expire`)."""
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import bridge
import clock
import security_log
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/code/agents/{aid}/projects/{pid}/members")

#: сроки выдачи «на день / неделю / месяц», секунды; без срока — бессрочно
TERMS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}
#: просьбы мосту, которые наблюдатель может слать: чтение проекта, чатов, файлов и git — без хода, ответа на
#: карточки, правки файлов и новых чатов. `state` читает настройки и контекст (мост ничего не пишет)
READ_OPS = frozenset({"projects", "activity", "project_summary", "chats", "history", "history_image", "state", "watch",
                      "files", "file_stats", "file_suggestions", "file_search", "chat_search", "subagents", "subagent_history",
                      "read", "raw", "visualization", "git", "git_compare"})
READONLY = "у наблюдателя проекта — только просмотр"
#: действует ли выдача сейчас (`?` — clock.NOW())
LIVE = "(m.expires IS NULL OR m.expires > ?)"


def expire() -> None:
    """Истёкшие выдачи — прочь, как отзыв (`drop`, `wake`): при первом обращении к выдачам после срока. Внутри
    чужой транзакции не снимает — фильтр `LIVE` всё равно их не пропустит."""
    if state.DB.in_transaction or state.DB.execute(
            "SELECT 1 FROM project_members WHERE expires <= ? LIMIT 1", (clock.NOW(),)).fetchone() is None:
        return
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        grants = [tuple(r) for r in state.DB.execute(
            "SELECT agent_id, project, user_id FROM project_members WHERE expires <= ?", (clock.NOW(),))]
        drop(grants)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    wake(grants)
    log.info("истекли выдачи доступа к проектам — %d", len(grants))


def projects(uid: int, aid: int) -> list[str]:
    expire()
    return [r[0] for r in state.DB.execute(
        "SELECT m.project FROM project_members m JOIN agents a ON a.id = m.agent_id"
        " JOIN users u ON u.id = a.user_id WHERE m.user_id = ? AND m.agent_id = ? AND u.disabled = 0 AND " + LIVE,
        (uid, aid, clock.NOW()))]


def role(uid: int, aid: int, pid: str | None) -> str | None:
    """Роль действующей выдачи участника (`editor`, `viewer`); владелец компьютера и чужой — None."""
    if pid is None:
        return None
    row = state.DB.execute(
        "SELECT m.role FROM project_members m JOIN agents a ON a.id = m.agent_id JOIN users u ON u.id = a.user_id"
        " WHERE m.user_id = ? AND m.agent_id = ? AND m.project = ? AND u.disabled = 0 AND " + LIVE,
        (uid, aid, pid, clock.NOW())).fetchone()
    return row[0] if row else None


def editable(request: Request, aid: int, pid: str | None) -> None:
    """Изменяющее действие (ход, ответ на карточку, правка, новый чат, отметка чата) наблюдателю — 403 до моста."""
    if role(request.state.user.id, aid, pid) == "viewer":
        raise HTTPException(403, {"code": "readonly", "message": READONLY})


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
    role: Literal["editor", "viewer"] = "editor"
    term: Literal["day", "week", "month"] | None = None


class Role(BaseModel):
    role: Literal["editor", "viewer"]


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
    expire()
    return {"items": [dict(r) for r in state.DB.execute(
        "SELECT u.id, u.login, u.disabled, m.role, m.expires FROM project_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.agent_id = ? AND m.project = ? AND " + LIVE + " ORDER BY u.login", (aid, pid, clock.NOW()))]}


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
    expire()
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


def leave(request: Request, aid: int) -> int:
    """Участник сам покидает совместные проекты чужого компьютера: снимаются его выдачи (и к исчезнувшим папкам).
    След — в журнале безопасности участника и в истории подключений владельца (`claude_code.MEMBER`, без id и
    адреса участника, как новый чат участника). Свой компьютер так не «покинуть» — 404."""
    import claude_code
    uid = request.state.user.id
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        row = state.DB.execute("SELECT id, user_id, name FROM agents WHERE id = ? AND user_id != ?", (aid, uid)).fetchone()
        grants = [] if row is None else [(aid, r[0], uid) for r in state.DB.execute(
            "SELECT project FROM project_members WHERE agent_id = ? AND user_id = ?", (aid, uid))]
        drop(grants)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    if not grants:
        raise HTTPException(404, {"code": "not_found", "message": "нет такого компьютера", "computer": True})
    wake(grants)
    security_log.record(uid, "project_leave", digest=request.state.digest, ip=auth.client_ip(request))
    bridge.record(row["user_id"], aid, row["name"], "member_remove", device=claude_code.MEMBER)
    log.info("пользователь %d: покинул совместные проекты компьютера %d — %d", uid, aid, len(grants))
    return len(grants)


@router.post("", status_code=201)
async def add_member(aid: int, pid: str, body: Member, request: Request):
    await _check(request, aid, pid)
    link = bridge.LINKS.get(aid)
    if link is None or not link.hello.get("project_sharing"):
        raise HTTPException(409, {"code": "unsupported", "message": "обновите мост на компьютере до 0.19.0 для совместной работы"})
    identity = body.login.strip().lower()
    user = state.DB.execute("SELECT id FROM users WHERE (login = ? OR email = ?) AND disabled = 0",
                            (identity, identity)).fetchone()
    if user is None or user["id"] == request.state.user.id:
        raise HTTPException(422, {"code": "member", "message": "укажите логин или email другого активного пользователя Студии"})
    now = clock.NOW()
    expire()
    if state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created, role, expires)"
                        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (aid, pid, user["id"], now, body.role,
                         None if body.term is None else now + TERMS[body.term])).rowcount:
        _journal(request, owner(request, aid), "add")
        log.info("пользователь %d: выдан доступ участнику %d к проекту компьютера %d",
                 request.state.user.id, user["id"], aid)
    return {"id": user["id"]}


@router.patch("/{uid}")
async def set_role(aid: int, pid: str, uid: int, body: Role, request: Request):
    """Сменить роль участника без пересоздания выдачи (срок, вкладки и место сохраняются). Понижение — без моста,
    как отзыв: папку могли переименовать. Повышение до соавтора даёт ход и правку файлов — требует того же, что
    выдача (`_check`: проект есть, компьютер открыт этой сессией, PIN). Журнал — как выдача (`project_member_add`,
    `member_add`): права участника изменились."""
    import claude_code
    row = owner(request, aid)
    claude_code.check_pid(pid)
    expire()
    current = state.DB.execute("SELECT role FROM project_members WHERE agent_id = ? AND project = ? AND user_id = ?",
                               (aid, pid, uid)).fetchone()
    if current is None:
        raise HTTPException(404, {"code": "not_found", "message": "нет такого участника"})
    if body.role == "editor" and current[0] != "editor":
        await _check(request, aid, pid)
    if not state.DB.execute("UPDATE project_members SET role = ? WHERE agent_id = ? AND project = ? AND user_id = ?",
                            (body.role, aid, pid, uid)).rowcount:
        raise HTTPException(404, {"code": "not_found", "message": "нет такого участника"})
    _journal(request, row, "add")
    log.info("пользователь %d: роль участника %d проекта компьютера %d изменена", request.state.user.id, uid, aid)
    return {"role": body.role}


@router.delete("/{uid}", status_code=204)
async def remove_member(aid: int, pid: str, uid: int, request: Request):
    import claude_code
    owner(request, aid)
    claude_code.check_pid(pid)
    revoke(request, aid, pid, uid)
