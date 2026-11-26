"""Personal projects of Studio conversations. The pinned Inbox keeps the account's existing memory."""
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import clock
import memory
import operations
import state

router = APIRouter(prefix="/api/chat-projects")


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    context: str = Field("", max_length=8000)
    memory_on: bool = True


def inbox(uid: int) -> str:
    return f"inbox-{uid}"


def require(uid: int, pid: str):
    row = state.DB.execute("SELECT * FROM chat_projects WHERE id = ? AND user_id = ?", (pid, uid)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "проект не найден"})
    return row


def scope(uid: int, chat_id: str) -> str | None:
    row = state.DB.execute("SELECT p.id FROM chats c JOIN chat_projects p ON p.id = c.project_id AND p.user_id = c.user_id"
                           " WHERE c.id = ? AND c.user_id = ? AND c.deleted IS NULL AND p.inbox = 0",
                           (chat_id, uid)).fetchone()
    return row["id"] if row else None


def _out(uid: int, row) -> dict:
    return {key: row[key] for key in ("id", "name", "description", "context")} | {
        "inbox": bool(row["inbox"]), "memory_on": memory.enabled(uid, None if row["inbox"] else row["id"])}


def _name(body: ProjectIn) -> str:
    name = body.name.strip()
    if not name or name.casefold() == "без проекта":
        raise HTTPException(422, {"code": "invalid", "message": "выберите другое название проекта"})
    return name


@router.get("")
async def list_projects(request: Request):
    uid = request.state.user.id
    rows = state.DB.execute("SELECT * FROM chat_projects WHERE user_id = ? ORDER BY inbox DESC, created DESC, id",
                            (uid,)).fetchall()
    return {"items": [_out(uid, row) for row in rows]}


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
