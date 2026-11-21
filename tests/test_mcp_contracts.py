"""Контракты Studio для любых MCP-серверов (docs/architecture/mcp-contracts.md): возможности из
`capabilities.experimental`, файлы пользователя `studio/user-files` и состояние `studio/status`."""
import json
import logging

import httpx
import pytest

import access
import mcp_contracts
import mcp_sources
from conftest import make_user, signin
from test_access_groups import shared_tools
from test_chat import new_chat, sse, turn
from test_mcp import TOKEN as MCP_KEY

COOKIES = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-cookie-value\n"
CONTRACTS = {
    "studio/files": {"version": 1, "max_upload": 2 << 30, "chunk": 32 << 20},
    "studio/user-files": {"version": 1, "files": [
        {"key": "youtube-cookies", "title": "Cookies YouTube", "accept": ".txt", "max_bytes": 4096}]},
    "studio/status": {"version": 1},
}
GUIDE = "# Руководство aitools\n\nКак пользоваться инструментами."
RESOURCES = [
    {"uri": "studio-guide://aitools", "name": "aitools", "title": "Руководство aitools", "mimeType": "text/markdown",
     "annotations": {"audience": ["user"]}},
    {"uri": "notes://model", "name": "model-notes", "mimeType": "text/markdown",
     "annotations": {"audience": ["assistant"]}},
    {"uri": "notes://plain", "name": "plain", "mimeType": "text/plain", "annotations": {"audience": ["user"]}},
]


@pytest.fixture(autouse=True)
def empty_cache():
    mcp_sources.CACHE.clear()
    mcp_contracts.STATUS.clear()
    yield
    mcp_sources.CACHE.clear()
    mcp_contracts.STATUS.clear()


def contract_server(studio, *, experimental=CONTRACTS, resources=RESOURCES):
    """Мок MCP с контрактами Studio: JSON-RPC на `/mcp` (инструмент `lookup`, ресурсы руководства) и HTTP контрактов
    `/status`, `/user-files[/{key}]`. Итог — записанные запросы (метод, путь, X-Studio-User, тело)."""
    seen, files = [], {}

    async def rpc(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        assert request.headers["authorization"] == f"Bearer {MCP_KEY}"
        payload = json.loads(request.content)
        method, params = payload["method"], payload.get("params", {})
        seen.append((method, "/mcp", request.headers.get("x-studio-user"), params))
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            capabilities = {"tools": {}, "experimental": experimental}
            if resources is not None:
                capabilities["resources"] = {}
            result = {"protocolVersion": "2025-06-18", "capabilities": capabilities}
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]}
        elif method == "resources/list":
            result = {"resources": resources}
        elif method == "resources/read":
            if params.get("uri") != RESOURCES[0]["uri"]:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                                 "error": {"code": -32602, "message": "unknown resource"}})
            result = {"contents": [{"uri": params["uri"], "mimeType": "text/markdown", "text": GUIDE}]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def item(key="youtube-cookies"):
        body = files.get(key)
        return {"key": key, "title": "Cookies YouTube", "present": body is not None,
                "size": len(body) if body is not None else 0, "mtime": 1_700_000_000 if body is not None else None}

    async def status(request):
        assert request.headers["authorization"] == f"Bearer {MCP_KEY}"
        seen.append((request.method, "/status", request.headers.get("x-studio-user"), None))
        return httpx.Response(200, json={"state": "busy", "queue": 2, "message": "Идёт 2 задания"})

    async def user_files(request):
        assert request.headers["authorization"] == f"Bearer {MCP_KEY}"
        seen.append((request.method, request.url.path, request.headers.get("x-studio-user"), request.content))
        if request.url.path == "/user-files":
            return httpx.Response(200, json=[item()])
        key = request.url.path.rsplit("/", 1)[1]
        if request.method == "PUT":
            files[key] = request.content
        elif request.method == "DELETE":
            files.pop(key, None)
        return httpx.Response(200, json=item(key))

    studio.gw.on["/mcp"] = rpc
    studio.gw.on["/status"] = status
    studio.gw.on["/user-files"] = user_files
    studio.gw.on["/user-files/youtube-cookies"] = user_files
    return seen


def member_with_server(studio, **kwargs):
    """Администратор, участник, общий API моделей и общий MCP с контрактами; вход — участником."""
    make_user(studio, "admin", is_admin=True)
    member = make_user(studio, "member")
    sid = shared_tools(studio)
    seen = contract_server(studio, **kwargs)
    signin(studio, "member")
    return member, sid, seen


def catalog(capabilities):
    return mcp_sources.Catalog(0.0, [], None, [], capabilities)


def test_features_read_from_experimental_capabilities():
    found = mcp_contracts.features(catalog({"tools": {}, "experimental": CONTRACTS}))
    assert found.files == {"max_upload": 2 << 30, "chunk": 32 << 20}
    assert found.user_files == [{"key": "youtube-cookies", "title": "Cookies YouTube", "accept": ".txt",
                                 "max_bytes": 4096}]
    assert found.status is True

    assert mcp_contracts.features(catalog({"tools": {}})) == mcp_contracts.Features(None, [], False)
    assert mcp_contracts.features(catalog({"experimental": "yes"})) == mcp_contracts.Features(None, [], False)

    # Предел файла — не больше 1 МиБ, неверные записи и неизвестные версии пропускаются, список — только объектом.
    odd = {
        "studio/files": {"version": 2, "max_upload": 10, "chunk": 10},
        "studio/user-files": {"version": 1, "files": [
            {"key": "big", "title": "Большой", "accept": "", "max_bytes": 50 << 20},
            {"key": "../escape", "title": "x", "accept": "", "max_bytes": 10},
            {"key": "zero", "title": "x", "accept": "", "max_bytes": 0},
            {"key": "big", "title": "Повтор", "accept": "", "max_bytes": 10},
            "not-an-object"]},
        "studio/status": True,
    }
    found = mcp_contracts.features(catalog({"experimental": odd}))
    assert found.files is None and found.status is False
    assert found.user_files == [{"key": "big", "title": "Большой", "accept": "", "max_bytes": 1 << 20}]
    listed = {"studio/user-files": [{"key": "a", "title": "a", "accept": "", "max_bytes": 1}]}
    assert mcp_contracts.features(catalog({"experimental": listed})).user_files == []


def test_user_file_upload_never_reaches_model_or_log(studio, caplog):
    caplog.set_level(logging.DEBUG)
    member, sid, seen = member_with_server(studio)
    base = f"/api/mcp/{sid}/user-files"

    listed = studio.c.get(base)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"items": [{"key": "youtube-cookies", "title": "Cookies YouTube", "accept": ".txt",
                                        "max_bytes": 4096, "present": False, "size": 0, "mtime": None}]}

    put = studio.c.put(f"{base}/youtube-cookies", content=COOKIES, headers={"content-type": "application/octet-stream"})
    assert put.status_code == 200, put.text
    assert put.json()["present"] is True and put.json()["size"] == len(COOKIES)
    sent = [s for s in seen if s[0] == "PUT"]
    assert len(sent) == 1 and sent[0][3] == COOKIES and sent[0][2] == access.studio_user(member, sid)

    assert studio.c.put(f"{base}/youtube-cookies", content=b"x" * 4097).status_code == 413
    assert studio.c.put(f"{base}/unknown", content=b"x").status_code == 404
    assert studio.c.put(f"{base}/youtube-cookies", content=b"").status_code == 422
    assert len([s for s in seen if s[0] == "PUT"]) == 1

    # Ход чата с этим сервером по умолчанию: содержимое файла модели не уходит.
    cid = new_chat(studio)
    events = sse(turn(studio, cid, "Скачай ролик"))
    assert events[-1] == ("done", {"status": "done"}), events
    assert studio.gw.chat_requests
    assert all(b"private-cookie-value" not in json.dumps(body).encode() for body in studio.gw.chat_requests)

    removed = studio.c.delete(f"{base}/youtube-cookies")
    assert removed.status_code == 200 and removed.json()["present"] is False

    assert "private-cookie-value" not in caplog.text and "Netscape" not in caplog.text
    journal = "\n".join(r.getMessage() for r in caplog.records if r.name == "studio")   # журнал службы, не клиента теста
    assert "youtube" not in journal.lower() and "cookies" not in journal.lower()

    # Сервер без контракта — пустой список, без запроса к /user-files; чужой и закрытый сервер — 404.
    signin(studio, "admin")
    other = studio.c.post("/api/mcp", json={"name": "Own", "url": "http://10.77.1.15:4000/mcp", "token": MCP_KEY})
    signin(studio, "member")
    assert studio.c.get(f"/api/mcp/{other.json()['id']}/user-files").status_code == 404


def test_status_cached_15s(studio):
    member, sid, seen = member_with_server(studio)
    hits = lambda: [s for s in seen if s[1] == "/status"]

    first = studio.c.get("/api/mcp/status")
    assert first.status_code == 200, first.text
    assert first.json() == {str(sid): {"state": "busy", "queue": 2, "message": "Идёт 2 задания"}}
    assert len(hits()) == 1 and hits()[0][2] == access.studio_user(member, sid)

    studio.clock[0] += mcp_contracts.STATUS_TTL_SEC - 1
    assert studio.c.get("/api/mcp/status").json() == first.json()
    assert len(hits()) == 1

    studio.clock[0] += 2
    assert studio.c.get("/api/mcp/status").json() == first.json()
    assert len(hits()) == 2

    # Другая учётка — свой ключ кэша (свой X-Studio-User): сервер спрошен заново.
    make_user(studio, "second")
    signin(studio, "second")
    assert studio.c.get("/api/mcp/status").json() == first.json()
    assert len(hits()) == 3


def test_status_skips_servers_without_the_contract(studio):
    member, sid, seen = member_with_server(studio, experimental={})
    assert studio.c.get("/api/mcp/status").json() == {}
    assert not [s for s in seen if s[1] == "/status"]
    item = studio.c.get("/api/mcp/catalog").json()["items"][0]
    assert item["contracts"] == {"files": False, "user_files": False, "status": False}


def test_catalog_names_declared_contracts(studio):
    """Страница спрашивает файлы пользователя и состояние только у серверов, которые их объявили."""
    member_with_server(studio)
    item = studio.c.get("/api/mcp/catalog").json()["items"][0]
    assert item["state"] == "ok" and item["contracts"] == {"files": True, "user_files": True, "status": True}
