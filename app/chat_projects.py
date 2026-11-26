"""Projects of Studio conversations. The pinned Inbox and project memory stay private."""
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import clock
import memory
import operations
import security_log
import state

router = APIRouter(prefix="/api/chat-projects")


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    context: str = Field("", max_length=8000)
    memory_on: bool = True


class MemberRole(BaseModel):
    role: Literal["viewer", "editor"]


class MemberIn(MemberRole):
    login: str = Field(min_length=1, max_length=254)


def inbox(uid: int) -> str:
    return f"inbox-{uid}"


def _gone():
    return HTTPException(404, {"code": "gone", "message": "проект не найден"})


def owned(uid: int, pid: str):
    return state.DB.execute("SELECT * FROM chat_projects WHERE id = ? AND user_id = ?", (pid, uid)).fetchone()


def require(uid: int, pid: str):
    row = owned(uid, pid)
    if row is None:
        raise _gone()
    return row


GRANTS = " LEFT JOIN chat_project_members g ON g.project_id = p.id AND g.user_id = :uid"
VISIBLE = "(p.user_id = :uid OR (p.inbox = 0 AND g.role IS NOT NULL AND u.disabled = 0))"
PROJECTS = " FROM chat_projects p JOIN users u ON u.id = p.user_id" + GRANTS


def access(uid: int, pid: str, *, write: bool = False):
    row = state.DB.execute("SELECT p.*, g.role" + PROJECTS + " WHERE p.id = :pid AND " + VISIBLE,
                           {"uid": uid, "pid": pid}).fetchone()
    if row is None:
        raise _gone()
    if write and row["user_id"] != uid and row["role"] != "editor":
        raise HTTPException(403, {"code": "readonly", "message": "проект доступен только для чтения"})
    return row


def scope(uid: int, chat_id: str) -> str | None:
    row = state.DB.execute("SELECT p.id FROM chats c JOIN chat_projects p ON p.id = c.project_id"
                           " WHERE c.id = ? AND c.user_id = ? AND c.deleted IS NULL AND p.inbox = 0",
                           (chat_id, uid)).fetchone()
    return row["id"] if row else None


def _out(uid: int, row) -> dict:
    return {key: row[key] for key in ("id", "name", "description", "context", "created")} | {
        "inbox": bool(row["inbox"]), "memory_on": memory.enabled(uid, None if row["inbox"] else row["id"])}


def _name(body: ProjectIn) -> str:
    name = body.name.strip()
    if not name or name.casefold() == "без проекта":
        raise HTTPException(422, {"code": "invalid", "message": "выберите другое название проекта"})
    return name


@router.get("")
async def list_projects(request: Request):
    uid = request.state.user.id
    rows = state.DB.execute("SELECT p.*, g.role, (SELECT count(*) FROM chat_project_members WHERE project_id = p.id)"
                            " AS members" + PROJECTS + " WHERE " + VISIBLE +
                            " ORDER BY p.inbox DESC, p.created DESC, p.id", {"uid": uid})
    return {"items": [_out(uid, row) | {"member_count": row["members"]} if uid == row["user_id"] else
                      {key: row[key] for key in ("id", "name", "description", "created", "role")} | {"inbox": False}
                      for row in rows]}


@router.post("", status_code=201)
async def create_project(body: ProjectIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    pid = uuid.uuid4().hex
    state.DB.execute("INSERT INTO chat_projects(id, user_id, name, description, context, memory_on, created)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (pid, uid, _name(body), body.description.strip(), body.context.strip(), body.memory_on, clock.NOW()))
    return _out(uid, require(uid, pid))


@router.put("/{pid}")
async def save_project(pid: str, body: ProjectIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    row = require(uid, pid)
    name = row["name"] if row["inbox"] else _name(body)
    state.DB.execute("UPDATE chat_projects SET name = ?, description = ?, context = ?, memory_on = ?"
                     " WHERE id = ? AND user_id = ?",
                     (name, body.description.strip(), body.context.strip(), body.memory_on, pid, uid))
    if row["inbox"]:
        state.DB.execute("UPDATE users SET memory_on = ? WHERE id = ?", (body.memory_on, uid))
    return _out(uid, require(uid, pid))


def _memory_scope(uid: int, pid: str) -> str | None:
    return None if require(uid, pid)["inbox"] else pid


def _member_owner(request: Request, pid: str):
    if require(request.state.user.id, pid)["inbox"]:
        raise _gone()


def _access_log(request: Request, kind: str) -> None:
    security_log.record(request.state.user.id, "project_member_" + kind, digest=request.state.digest)


@router.get("/{pid}/members")
async def members(pid: str, request: Request):
    _member_owner(request, pid)
    return {"items": [dict(row) for row in state.DB.execute(
        "SELECT u.id, u.login, u.disabled, g.role FROM chat_project_members g JOIN users u ON u.id = g.user_id"
        " WHERE g.project_id = ? ORDER BY u.login", (pid,))]}


@router.post("/{pid}/members", status_code=201)
async def grant(pid: str, body: MemberIn, request: Request):
    _member_owner(request, pid)
    uid = request.state.user.id
    operations.check(uid)
    identity = body.login.strip().lower()
    recipients = state.DB.execute("SELECT id FROM users WHERE (login = ? OR email = ?) AND disabled = 0",
                                  (identity, identity)).fetchall()
    if len(recipients) != 1 or recipients[0]["id"] == uid:
        raise HTTPException(404, {"code": "gone", "message": "пользователь не найден"})
    member_id = recipients[0]["id"]
    if not state.DB.execute("INSERT INTO chat_project_members(project_id, user_id, role) VALUES (?, ?, ?)"
                            " ON CONFLICT(project_id, user_id) DO NOTHING", (pid, member_id, body.role)).rowcount:
        raise HTTPException(409, {"code": "conflict", "message": "пользователю уже открыт доступ"})
    _access_log(request, "add")
    return {"id": member_id}


@router.patch("/{pid}/members/{member_id}", status_code=204)
async def change_role(pid: str, member_id: int, body: MemberRole, request: Request):
    _member_owner(request, pid)
    operations.check(request.state.user.id)
    if not state.DB.execute("UPDATE chat_project_members SET role = ? WHERE project_id = ? AND user_id = ?",
                            (body.role, pid, member_id)).rowcount:
        raise _gone()
    _access_log(request, "add")


@router.delete("/{pid}/members/{member_id}", status_code=204)
async def revoke(pid: str, member_id: int, request: Request):
    _member_owner(request, pid)
    operations.check(request.state.user.id)
    if state.DB.execute("DELETE FROM chat_project_members WHERE project_id = ? AND user_id = ?",
                        (pid, member_id)).rowcount:
        _access_log(request, "remove")


@router.get("/{pid}/chats/{chat_id}")
async def read_dialog(pid: str, chat_id: str, request: Request):
    access(request.state.user.id, pid)
    row = state.DB.execute("SELECT title FROM chats WHERE id = ? AND project_id = ? AND deleted IS NULL"
                           " AND protected_used = 0", (chat_id, pid)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого диалога нет"})
    messages = state.DB.execute("SELECT role, content FROM messages WHERE chat_id = ? AND unchosen = 0"
                                " AND (role = 'user' OR status IN ('done', 'stopped')) ORDER BY created, rowid", (chat_id,))
    return {"title": row["title"], "messages": [dict(message) for message in messages]}


@router.get("/{pid}/memory")
async def list_memory(pid: str, request: Request):
    uid = request.state.user.id
    return {"items": memory.list_for(uid, _memory_scope(uid, pid))}


@router.post("/{pid}/memory", status_code=201)
async def add_memory(pid: str, body: memory.MemoryIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    return {"id": memory.save(uid, body.text, _memory_scope(uid, pid))}


@router.delete("/{pid}/memory/{item_id}", status_code=204)
async def delete_memory(pid: str, item_id: str, request: Request):
    uid = request.state.user.id
    if not memory.forget(uid, item_id, _memory_scope(uid, pid)):
        raise HTTPException(404, {"code": "gone", "message": "запись памяти не найдена"})
