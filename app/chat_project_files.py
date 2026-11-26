"""Project folders and files, stored in Studio media. Files from chat history follow their chat."""
import re
import sqlite3
import unicodedata
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import chat_projects
import clock
import lock_policy
import media
import my_files
import operations
import state

router = APIRouter(prefix="/api/chat-projects/{pid}/files")
CHAT_FILES = "chats"
UPLOAD_MAX = 20 * 1024**2  # Shared request limit on the web entry points.
DEPTH_MAX = 20
NAME_MAX = 180
PAGE = 100
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.IGNORECASE)


class FolderIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    parent: str | None = Field(None, max_length=64)


class SourceIn(BaseModel):
    media_id: str = Field(max_length=64)
    section: str = Field(max_length=32)
    parent: str | None = Field(None, max_length=64)


def _error(status, code, message):
    return HTTPException(status, {"code": code, "message": message})


def _name(raw: str) -> str:
    name = unicodedata.normalize("NFC", raw).strip()
    if not name or len(name) > NAME_MAX or name.endswith(".") or _UNSAFE.search(name) or _RESERVED.match(name):
        raise _error(422, "invalid", "недопустимое имя файла или папки")
    return name


def _source_name(raw: str) -> str:
    name = _UNSAFE.sub("_", unicodedata.normalize("NFC", raw)).strip(" .")[:NAME_MAX].rstrip(" .") or "file"
    return "_" + name[:NAME_MAX - 1] if _RESERVED.match(name) else name


def _entry(pid: str, item_id: str):
    row = state.DB.execute("SELECT * FROM chat_project_files WHERE project_id = ? AND id = ?",
                           (pid, item_id)).fetchone()
    if row is None:
        raise _error(404, "gone", "файл или папка не найдены")
    return row


def _folder(pid: str, parent: str | None, *, mkdir=False) -> None:
    depth = 0
    while parent:
        row = _entry(pid, parent)
        if row["media_id"] is not None:
            raise _error(404, "gone", "папка не найдена")
        depth += 1
        parent = row["parent_id"]
    if mkdir and depth >= DEPTH_MAX:
        raise _error(422, "invalid", "достигнут предел вложенности папок")


def _insert(uid, pid, parent, name, mid=None, section=None):
    item_id = uuid.uuid4().hex
    try:
        state.DB.execute("INSERT INTO chat_project_files(id, project_id, user_id, parent_id, name, name_key, created,"
                         " media_id, source_section) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (item_id, pid, uid, parent, name, name.casefold(), clock.NOW(), mid, section))
    except sqlite3.IntegrityError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
            raise
        raise _error(409, "exists", "файл или папка с таким именем уже есть") from None
    return {"id": item_id}


def _children(pid, parent):
    return state.DB.execute(
        "SELECT f.*, m.size, m.mime, m.rel_path FROM chat_project_files f LEFT JOIN media m ON m.id = f.media_id"
        " AND m.user_id = f.user_id WHERE f.project_id = ? AND f.parent_id IS ?"
        " ORDER BY f.media_id IS NOT NULL, f.name_key, f.id", (pid, parent)).fetchall()


ATTACHED_MEDIA = """
    SELECT a.media_id, c.user_id, c.project_id, c.protected_used, c.deleted
      FROM attachments a JOIN messages msg ON msg.id = a.message_id
      JOIN chats c ON c.id = msg.chat_id AND a.user_id = c.user_id
    UNION ALL
    SELECT cm.media_id, c.user_id, c.project_id, c.protected_used, COALESCE(c.deleted, cm.deleted)
      FROM chat_media cm JOIN chats c ON c.id = cm.chat_id AND cm.user_id = c.user_id
"""


def _chat_files(uid, pid, *, after="", mid="", limit=PAGE):
    return state.DB.execute(
        "WITH attached AS NOT MATERIALIZED (" + ATTACHED_MEDIA + "), visible AS ("
        " SELECT media.*, MAX(a.protected_used) AS protected FROM attached a JOIN media"
        " ON media.id = a.media_id AND media.user_id = a.user_id WHERE a.project_id = :pid AND a.deleted IS NULL"
        " AND (a.user_id = :uid OR NOT EXISTS (SELECT 1 FROM attached hidden"
        " WHERE hidden.media_id = a.media_id AND hidden.user_id = a.user_id AND hidden.protected_used = 1))"
        " GROUP BY media.id) SELECT * FROM visible"
        " WHERE (:mid = '' OR id = :mid) AND (:after = '' OR (created, id) <"
        " (SELECT created, id FROM visible WHERE id = :after)) ORDER BY created DESC, id DESC LIMIT :limit",
        {"uid": uid, "pid": pid, "after": after, "mid": mid, "limit": limit}).fetchall()


def _visible(uid, mid):
    closed = lock_policy.RESTRICTED.get()
    return not closed or not (lock_policy.media_sections(uid, mid) & closed)


def _readable(uid, author, mid):
    return _visible(author, mid) and (uid == author or not state.DB.execute(
        "SELECT 1 FROM (" + ATTACHED_MEDIA + ") WHERE media_id = ? AND user_id = ? AND protected_used = 1 LIMIT 1",
        (mid, author)).fetchone())


def _file(pid, item_id, name, size, mime, *, from_chat=False):
    kind = my_files.local_kind(name, mime)
    source = f"/api/chat-projects/{pid}/files/content/{item_id}"
    preview = f"/api/chat-projects/{pid}/files/preview/{item_id}" if kind in ("pdf", "text") else source
    return {"id": item_id, "name": name, "dir": False, "size": size, "from_chat": from_chat,
            "download": source + "?download=1", "preview": {"url": preview, "kind": kind} if kind else None}


@router.get("")
async def list_files(pid: str, request: Request, parent: str | None = None, after: str = ""):
    uid = request.state.user.id
    chat_projects.access(uid, pid)
    if parent == CHAT_FILES:
        rows = _chat_files(uid, pid, after=after, limit=PAGE + 1)
        entries = [_file(pid, f"chat-{r['id']}", r["download_name"], r["size"], r["mime"], from_chat=True)
                   for r in rows[:PAGE] if _visible(r["user_id"], r["id"])]
        return {"entries": entries, "next": rows[PAGE - 1]["id"] if len(rows) > PAGE else None}
    _folder(pid, parent)
    entries = []
    for row in _children(pid, parent):
        mid = row["media_id"]
        if mid is None:
            entries.append({"id": row["id"], "name": row["name"], "dir": True})
        elif _readable(uid, row["user_id"], mid):
            entries.append(_file(pid, row["id"], row["name"], row["size"], row["mime"]))
    if parent is None and _chat_files(uid, pid, limit=1):
        entries.insert(0, {"id": CHAT_FILES, "dir": True, "from_chat": True})
    return {"entries": entries, "next": None}


@router.post("/folders", status_code=201)
async def mkdir(pid: str, body: FolderIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    chat_projects.access(uid, pid, write=True)
    _folder(pid, body.parent, mkdir=True)
    return _insert(uid, pid, body.parent, _name(body.name))


@router.post("/upload", status_code=201)
async def upload(pid: str, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    chat_projects.access(uid, pid, write=True)
    parent = request.headers.get("x-studio-folder") or None
    _folder(pid, parent)
    try:
        name = _name(unquote(request.headers.get("x-studio-name", ""), errors="strict"))
    except UnicodeError:
        raise _error(422, "invalid", "недопустимое имя файла") from None
    length = request.headers.get("content-length", "")
    if length.isascii() and length.isdecimal() and (len(length) > 10 or int(length) > UPLOAD_MAX):
        raise _error(413, "too_large", "файл больше 20 МиБ")
    kind = my_files.preview_kind(name)
    mime = kind if kind in my_files.PREVIEW_MEDIA.values() else "application/octet-stream"
    mid = await media.save_stream(uid, request.stream(), mime, name, UPLOAD_MAX)
    try:
        operations.check(uid)
        chat_projects.access(uid, pid, write=True)
        _folder(pid, parent)
        return _insert(uid, pid, parent, name, mid)
    except BaseException:
        await media.delete_async(uid, mid)
        raise


@router.post("/from-section", status_code=201)
async def add_from_section(pid: str, body: SourceIn, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    chat_projects.access(uid, pid, write=True)
    _folder(pid, body.parent)
    if body.section not in my_files.STUDIO_SECTIONS:
        raise _error(422, "invalid", "неизвестный раздел")
    lock_policy.require_media(uid, body.media_id)
    rows = my_files.studio_files(uid, section=body.section, mid=body.media_id, limit=1)
    if not rows:
        raise _error(404, "gone", "файл не найден")
    return _insert(uid, pid, body.parent, _source_name(rows[0]["download_name"]), body.media_id, body.section)


@router.delete("/{item_id}", status_code=204)
async def remove(pid: str, item_id: str, request: Request):
    uid = request.state.user.id
    operations.check(uid)
    chat_projects.access(uid, pid, write=True)
    row = _entry(pid, item_id)
    mid = row["media_id"]
    if mid and not _readable(uid, row["user_id"], mid):
        lock_policy.require_media(row["user_id"], mid)
        raise _error(404, "gone", "файл не найден")
    if mid is None and state.DB.execute("SELECT 1 FROM chat_project_files WHERE parent_id = ? LIMIT 1",
                                        (item_id,)).fetchone():
        raise _error(409, "not_empty", "сначала удалите содержимое папки")
    state.DB.execute("DELETE FROM chat_project_files WHERE id = ?", (item_id,))
    if mid and not media.referenced(row["user_id"], mid):
        await media.delete_async(row["user_id"], mid)


def _asset(uid: int, pid: str, item_id: str):
    chat_projects.access(uid, pid)
    if item_id.startswith("chat-"):
        rows = _chat_files(uid, pid, mid=item_id.removeprefix("chat-"), limit=1)
        if not rows:
            raise _error(404, "gone", "файл не найден")
        author, mid, name = (rows[0][key] for key in ("user_id", "id", "download_name"))
    else:
        row = _entry(pid, item_id)
        author, mid, name = (row[key] for key in ("user_id", "media_id", "name"))
    if mid is None:
        raise _error(404, "gone", "файл не найден")
    lock_policy.require_media(author, mid)
    if not _readable(uid, author, mid):
        raise _error(404, "gone", "файл не найден")
    return author, mid, name


@router.get("/preview/{item_id}")
async def preview(pid: str, item_id: str, request: Request):
    author, mid, name = _asset(request.state.user.id, pid, item_id)
    return await my_files.local_preview(author, mid, name)


@router.get("/content/{item_id}")
async def content(pid: str, item_id: str, request: Request):
    author, mid, name = _asset(request.state.user.id, pid, item_id)
    return await media.serve(author, mid, request, filename=name, cache="no-store")


def _archive_entries(uid, pid, parent):
    entries = []
    if parent != CHAT_FILES:
        _folder(pid, parent)
        stack = [(parent, "")]
        while stack:
            folder, prefix = stack.pop()
            for row in _children(pid, folder):
                name = prefix + row["name"]
                if row["media_id"] is None:
                    entries.append((name + "/", None, row["created"]))
                    stack.append((row["id"], name + "/"))
                else:
                    lock_policy.require_media(row["user_id"], row["media_id"])
                    if _readable(uid, row["user_id"], row["media_id"]):
                        entries.append((name, state.CFG.media_dir / row["rel_path"], row["created"]))
    if parent is None or parent == CHAT_FILES:
        rows = _chat_files(uid, pid, limit=-1)
        if any(row["protected"] for row in rows):
            raise _error(409, "protected", "в архиве есть файлы диалогов с защищённым источником")
        names = {name.split("/", 1)[0].casefold() for name, _, _ in entries}
        label = "Файлы диалогов"
        while label.casefold() in names:
            label += "_"
        prefix = label + "/" if parent is None else ""
        safe = [{"download_name": _source_name(row["download_name"])} for row in rows]
        for name, row in zip(media._entry_names(safe), rows):
            lock_policy.require_media(row["user_id"], row["id"])
            entries.append((prefix + name, state.CFG.media_dir / row["rel_path"], row["created"]))
    return entries


@router.get("/zip")
async def download_zip(pid: str, request: Request, parent: str | None = None):
    uid = request.state.user.id
    project = chat_projects.access(uid, pid)
    entries = _archive_entries(uid, pid, parent)
    title = _entry(pid, parent)["name"] if parent and parent != CHAT_FILES else project["name"]
    return StreamingResponse(media._zip(entries, media.user_zone(uid)), media_type="application/zip",
                             headers={"Content-Disposition": media.disposition(_source_name(title) + ".zip"),
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
