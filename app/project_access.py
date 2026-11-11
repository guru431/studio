"""Участники проектов: явное разрешение владельца, только id в базе и журнале."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import bridge
import clock
import state

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
    login: str = Field(min_length=1, max_length=100)


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


@router.post("", status_code=201)
async def add_member(aid: int, pid: str, body: Member, request: Request):
    await _check(request, aid, pid)
    link = bridge.LINKS.get(aid)
    if link is None or not link.hello.get("project_sharing"):
        raise HTTPException(409, {"code": "unsupported", "message": "обновите агент для совместной работы"})
    user = state.DB.execute("SELECT id FROM users WHERE login = ? AND disabled = 0",
                            (body.login.strip().lower(),)).fetchone()
    if user is None or user["id"] == request.state.user.id:
        raise HTTPException(422, {"code": "member", "message": "укажите логин другого активного пользователя Студии"})
    state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT DO NOTHING", (aid, pid, user["id"], clock.NOW()))
    return {"id": user["id"]}


@router.delete("/{uid}", status_code=204)
async def remove_member(aid: int, pid: str, uid: int, request: Request):
    import claude_code
    owner(request, aid)
    claude_code.check_pid(pid)
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        state.DB.execute("DELETE FROM project_members WHERE agent_id = ? AND project = ? AND user_id = ?",
                         (aid, pid, uid))
        state.DB.execute("DELETE FROM code_member_tabs WHERE agent_id = ? AND project = ? AND user_id = ?",
                         (aid, pid, uid))
        state.DB.execute("UPDATE code_place SET agent_id = NULL, project = NULL WHERE user_id = ?"
                         " AND agent_id = ? AND project = ?", (uid, aid, pid))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    pdf = claude_code.PDFS.get(uid)
    if pdf and pdf[0][:2] == (aid, pid):
        del claude_code.PDFS[uid]
    view = claude_code.code_visualizations.VIEWS.get(uid)
    if view and view[2:4] == (aid, pid):
        del claude_code.code_visualizations.VIEWS[uid]
    # Генератор SSE проверяет разрешение до выдачи каждого события; будим и тихие потоки.
    for (agent_id, _), queues in list(bridge.WATCHERS.items()):
        if agent_id == aid:
            for q in list(queues):
                if not q.full():
                    q.put_nowait(("ping", None))
