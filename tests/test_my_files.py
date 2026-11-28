"""«Мои файлы» на сервере MCP (контракт `studio/files`, my_files.py): список, скачивание с Range, загрузка кусками,
корзина, ZIP — через Studio с `X-Studio-User` учётки; администратор чужого не видит."""
import base64
import io
import json
import re
from urllib.parse import quote
import zipfile

import httpx
import pytest

import access
import mcp_contracts
import mcp_sources
import my_files
from conftest import make_user, signin
from test_access_groups import shared_tools
from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL

FILES = {"studio/files": {"version": 1, "max_upload": 2 << 30, "chunk": 32 << 20}}
ZID = "a" * 32


@pytest.fixture(autouse=True)
def empty_cache():
    mcp_sources.CACHE.clear()
    mcp_contracts.STATUS.clear()
    yield
    mcp_sources.CACHE.clear()


class FileServer:
    """Мок MCP с контрактом `studio/files`: JSON-RPC на `/mcp` и HTTP `/files/*`; папка — своя у каждого
    `X-Studio-User` (как у обёртки aitools). `seen` — (метод, путь, X-Studio-User, путь X-Studio-Path)."""

    def __init__(self, studio):
        self.folders, self.trashed, self.partial, self.seen = {}, {}, {}, []
        self.mtimes = {}                         # путь → mtime записи списка (по умолчанию — общий)
        self.metadata = {}                       # путь → необязательные поля записи списка
        studio.gw.on["/mcp"] = self.rpc
        for path in ("list", "content", "upload", "mkdir", "delete", "trash", "restore", "zip", f"zip/{ZID}"):
            studio.gw.on[f"/files/{path}"] = self.files

    def folder(self, user):
        return self.folders.setdefault(user, {})

    async def rpc(self, request):
        if request.method == "DELETE":
            return httpx.Response(204)
        assert request.headers["authorization"] == f"Bearer {MCP_KEY}"
        payload = json.loads(request.content)
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        if payload["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "experimental": FILES}}
        elif payload["method"] == "tools/list":
            result = {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async def files(self, request):
        assert request.headers["authorization"] == f"Bearer {MCP_KEY}"
        user = request.headers.get("x-studio-user")
        raw = request.headers.get("x-studio-path")
        header_path = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode() if raw else None
        route = request.url.path.removeprefix("/files/")
        self.seen.append((request.method, request.url.path, user, header_path))
        folder = self.folder(user)
        body = json.loads(request.content) if request.method == "POST" and request.content else {}
        if route == "list":
            prefix = body.get("path", "")
            if prefix and not any(p.startswith(prefix + "/") for p in folder):
                return httpx.Response(404, json={"code": "not_found", "message": "Нет такого пути"})
            entries, base = {}, prefix + "/" if prefix else ""
            for path, data in folder.items():
                if not path.startswith(base):
                    continue
                head, _, rest = path[len(base):].partition("/")
                if not head or head == ".keep":          # метка пустой папки mkdir — не запись
                    continue
                old = entries.get(head, {"name": head, "dir": bool(rest), "size": 0,
                                         "mtime": self.mtimes.get(base + head, 1_700_000_000.5)})
                old.update(self.metadata.get(base + head, {}))
                old["size"] += len(data or b"")
                entries[head] = old
            total = sum(len(d or b"") for d in folder.values())
            return httpx.Response(200, json={"path": prefix, "total_bytes": total,
                                             "entries": sorted(entries.values(), key=lambda e: (not e["dir"], e["name"]))})
        if route == "content":
            data = folder.get(header_path)
            if data is None:
                return httpx.Response(404, json={"code": "not_found", "message": "Нет файла"})
            return ranged(request, data)
        if route == "upload":
            a, b, total = map(int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers["content-range"]).groups())
            got = self.partial.setdefault((user, header_path, total), bytearray())
            if a > len(got):
                return httpx.Response(409, json={"code": "gap", "message": "Кусок дальше принятого", "size": len(got)})
            del got[a:]
            got.extend(request.content)
            if len(got) < total:
                return httpx.Response(200, json={"done": False, "size": len(got)})
            folder[header_path] = bytes(self.partial.pop((user, header_path, total)))
            return httpx.Response(200, json={"done": True, "size": total})
        if route == "mkdir":
            folder[body["path"] + "/.keep"] = b""
            return httpx.Response(200, json={"path": body["path"]})
        if route == "delete":
            data = folder.pop(body["path"], None)
            if data is None:
                return httpx.Response(404, json={"code": "not_found", "message": "Нет файла"})
            tid = f"{len(self.trashed) + 1:032x}"
            self.trashed[tid] = (user, body["path"], data)
            return httpx.Response(200, json={"id": tid})
        if route == "trash":
            return httpx.Response(200, json=[{"id": tid, "path": path, "deleted": 1_700_000_100, "size": len(data)}
                                             for tid, (owner, path, data) in self.trashed.items() if owner == user])
        if route == "restore":
            owner, path, data = self.trashed.pop(body["id"])
            folder[path] = data
            return httpx.Response(200, json={"path": path})
        if route == "zip":
            return httpx.Response(200, json={"id": ZID, "size": len(self.zip_bytes(folder, body["path"]))})
        if route == f"zip/{ZID}":
            return ranged(request, self.zip_bytes(folder, ""))
        return httpx.Response(404)

    @staticmethod
    def zip_bytes(folder, prefix):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
            for path, data in sorted(folder.items()):
                if path.startswith(prefix):
                    archive.writestr(path, data)
        return out.getvalue()


def ranged(request, data):
    value = request.headers.get("range")
    if value:
        a, b = re.fullmatch(r"bytes=(\d+)-(\d*)", value).groups()
        a, b = int(a), int(b) if b else len(data) - 1
        return httpx.Response(206, content=data[a:b + 1], headers={
            "content-range": f"bytes {a}-{b}/{len(data)}", "accept-ranges": "bytes"})
    return httpx.Response(200, content=data, headers={"accept-ranges": "bytes"})


def member_with_files(studio):
    """Администратор, участник, общий MCP «Shared tools» с `studio/files`; вход — участником."""
    make_user(studio, "admin", is_admin=True)
    member = make_user(studio, "member")
    sid = shared_tools(studio)
    server = FileServer(studio)
    signin(studio, "member")
    return member, sid, server


def upload(studio, sid, path, data, start=0, total=None):
    total = len(data) if total is None else total
    return studio.c.put(f"/api/my-files/{sid}/upload", content=data, headers={
        "X-Studio-Name": quote(path), "Content-Range": f"bytes {start}-{start + len(data) - 1}/{total}",
        "Content-Type": "application/octet-stream"})


def test_list_download_range_upload_chunks_delete_restore_zip_proxy_with_studio_user(studio, monkeypatch):
    member, sid, server = member_with_files(studio)
    me = access.studio_user(member, sid)

    servers = studio.c.get("/api/my-files/servers")
    assert servers.status_code == 200, servers.text
    assert servers.json() == [{"sid": sid, "name": "Shared tools", "max_upload": 2 << 30, "chunk": my_files.CHUNK,
                               "protected": False}]

    data = "Отчёт за ноябрь\n".encode() * 100
    first = upload(studio, sid, "docs/отчёт.txt", data[:600], total=len(data))
    assert first.status_code == 200, first.text
    assert first.json() == {"done": False, "size": 600}
    gap = upload(studio, sid, "docs/отчёт.txt", data[700:], start=700, total=len(data))
    assert gap.status_code == 409 and gap.json()["detail"]["size"] == 600
    done = upload(studio, sid, "docs/отчёт.txt", data[600:], start=600, total=len(data))
    assert done.json() == {"done": True, "size": len(data)}

    # Кусок больше my_files.CHUNK — 413 до сервера MCP.
    monkeypatch.setattr(my_files, "CHUNK", 1000)
    calls = len(server.seen)
    assert upload(studio, sid, "big.bin", b"x" * 1001, total=5000).status_code == 413
    assert len(server.seen) == calls

    server.metadata["docs/отчёт.txt"] = {"created": 1_699_999_000}
    server.metadata["docs"] = {"files": 1, "folders": 0}
    root = studio.c.post(f"/api/my-files/{sid}/list", json={"path": ""}).json()["entries"]
    assert [(item["size"], item["files"], item["folders"]) for item in root] == [(len(data), 1, 0)]
    listed = studio.c.post(f"/api/my-files/{sid}/list", json={"path": "docs"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == [{"name": "отчёт.txt", "dir": False, "size": len(data),
                                        "created": 1_699_999_000, "mtime": 1_700_000_000.5, "preview_kind": "text"}]
    assert listed.json()["total_bytes"] == len(data)

    url = studio.c.post(f"/api/my-files/{sid}/ticket", json={"path": "docs/отчёт.txt"}).json()["url"]
    assert url.startswith(f"/api/my-files/{sid}/download/")
    whole = studio.c.get(url)
    assert whole.status_code == 200 and whole.content == data
    assert whole.headers["content-disposition"].startswith("attachment;") and "nosniff" in whole.headers["x-content-type-options"]
    part = studio.c.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == data[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(data)}"

    assert studio.c.post(f"/api/my-files/{sid}/mkdir", json={"path": "Новая"}).json() == {"path": "Новая"}
    gone = studio.c.post(f"/api/my-files/{sid}/delete", json={"path": "docs/отчёт.txt"})
    assert gone.status_code == 200, gone.text
    trash = studio.c.post(f"/api/my-files/{sid}/trash", json={}).json()
    assert [item["path"] for item in trash] == ["docs/отчёт.txt"]
    assert studio.c.get(url).status_code == 404                     # файла нет — 404, как у сервера
    back = studio.c.post(f"/api/my-files/{sid}/restore", json={"id": gone.json()["id"]})
    assert back.json() == {"path": "docs/отчёт.txt"}

    packed = studio.c.post(f"/api/my-files/{sid}/zip", json={"path": "docs"})
    assert packed.status_code == 200, packed.text
    archive = studio.c.get(packed.json()["url"])
    assert archive.status_code == 200 and archive.headers["content-disposition"].startswith("attachment;")
    assert zipfile.ZipFile(io.BytesIO(archive.content)).read("docs/отчёт.txt") == data
    assert studio.c.get(packed.json()["url"], headers={"Range": "bytes=0-3"}).content == archive.content[:4]

    # Всё — с X-Studio-User этой учётки; путь файла — base64url в X-Studio-Path.
    assert server.seen and all(user == me for _, _, user, _ in server.seen)
    assert ("PUT", "/files/upload", me, "docs/отчёт.txt") in server.seen

    storage = studio.c.get("/api/me/storage").json()
    assert storage["mcp_files"] == [{"sid": sid, "name": "Shared tools", "bytes": len(data)}]


def test_admin_cannot_open_foreign_files_or_links(studio):
    member, sid, server = member_with_files(studio)
    own = studio.c.post("/api/mcp", json={"name": "Own", "url": MCP_URL, "token": MCP_KEY})
    assert own.status_code == 201, own.text
    own_sid = own.json()["id"]
    assert upload(studio, sid, "secret.txt", b"member only").json()["done"] is True
    ticket = studio.c.post(f"/api/my-files/{sid}/ticket", json={"path": "secret.txt"}).json()["url"]
    link = studio.c.post(f"/api/my-files/{sid}/links", json={"path": "secret.txt"}).json()

    signin(studio, "admin")
    # Общий сервер — папка администратора, не участника; личный сервер участника — как отсутствующий.
    assert studio.c.post(f"/api/my-files/{sid}/list", json={"path": ""}).json()["entries"] == []
    assert studio.c.post(f"/api/my-files/{own_sid}/list", json={"path": ""}).status_code == 404
    assert [s["sid"] for s in studio.c.get("/api/my-files/servers").json()] == [sid]
    assert studio.c.get(ticket).status_code == 404
    assert studio.c.get("/api/my-files/links").json() == []
    assert studio.c.delete(f"/api/my-files/links/{link['id']}").status_code == 404
    assert studio.c.post(f"/api/my-files/{sid}/links", json={"path": "secret.txt"}).status_code == 404
    admin_user = server.seen[-1][2]
    assert admin_user != access.studio_user(member, sid)


def test_previews_are_safely_typed_and_remain_private(studio):
    member, sid, server = member_with_files(studio)
    me = access.studio_user(member, sid)
    server.folder(me).update({"out/note.md": b"<script>plain text</script>", "out/movie.mp4": b"movie",
                              "out/unsafe.svg": b"<svg></svg>"})
    route = f"/api/my-files/{sid}/ticket"
    made = studio.c.post(route, json={"path": "out/note.md", "preview": True}).json()
    reply = studio.c.get(made["url"])
    assert reply.json()["text"] == "<script>plain text</script>"
    assert reply.headers["cache-control"] == "private, no-store"
    clip = studio.c.post(route, json={"path": "out/movie.mp4", "preview": True}).json()
    response = studio.c.get(clip["url"], headers={"Range": "bytes=0-1"})
    assert response.status_code == 206 and response.content == b"mo"
    assert response.headers["content-type"] == "video/mp4"
    assert studio.c.post(route, json={"path": "out/unsafe.svg", "preview": True}).status_code == 422
    signin(studio, "admin")
    assert studio.c.get(made["url"]).status_code == 404
