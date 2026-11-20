"""Группы доступа к общим API моделей, MCP и инструментам MCP (access.py, шаг схемы 56)."""
import re
from contextlib import closing

import pytest
from fastapi import HTTPException

import access
import db
import state
from conftest import add_connection, make_user, signin
from historical_schema import install_schema


def shared_connection() -> int:
    return state.DB.execute(
        "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only)"
        " VALUES (NULL, 'models', 'openai', 'http://10.77.1.15:4000/v1', x'00', '[]', 0, 1)").lastrowid


def shared_mcp() -> int:
    return state.DB.execute("INSERT INTO mcp_servers(user_id, name, url, secret_enc)"
                            " VALUES (NULL, 'tools', 'http://10.77.1.15:4000/mcp', x'00')").lastrowid


# ---- Task 1: схема и модуль ----------------------------------------------------------

def test_schema_56_migrates_from_55(env):
    with closing(db.connect(env.db_path)) as conn:
        install_schema(conn, 55)
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 56
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
        assert {"access_groups", "access_group_members", "access_rules", "access_rule_groups",
                "access_rules_connection_delete", "access_rules_mcp_delete"} <= tables
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rule_semantics(studio):
    member, other = make_user(studio, "member"), make_user(studio, "other")
    admin = make_user(studio, "admin", is_admin=True)
    cid = shared_connection()
    assert all(access.allowed(uid, "connection", cid) for uid in (member, other, admin, None))
    group = access.create_group("Media")["id"]
    access.set_members(group, [member])
    access.set_rule("connection", cid, [group])
    assert access.rule("connection", cid) == [group]
    assert access.allowed(member, "connection", cid) and not access.allowed(other, "connection", cid)
    access.set_rule("connection", cid, [])
    assert access.rule("connection", cid) == []
    assert not access.allowed(member, "connection", cid) and access.allowed(admin, "connection", cid)
    access.set_rule("connection", cid, None)
    assert access.rule("connection", cid) is None and access.allowed(other, "connection", cid)


def test_admin_bypasses_rules(studio):
    admin = make_user(studio, "admin", is_admin=True)
    cid, sid = shared_connection(), shared_mcp()
    group = access.create_group("Nobody")["id"]
    access.set_rule("connection", cid, [group])
    access.set_rule("mcp", sid, [])
    access.set_tool_rules(sid, {"secret_tool": []})
    assert access.is_admin(admin)
    assert access.allowed(admin, "connection", cid) and access.allowed(admin, "mcp", sid)
    assert access.tool_allowed(admin, sid, "secret_tool")
    clause, args = access.shared_filter(admin, "connection", "connections.id")
    assert state.DB.execute(f"SELECT id FROM connections WHERE {clause}", args).fetchall()[0][0] == cid


def test_tool_rule_narrows_server(studio):
    both, server_only, tool_only = (make_user(studio, n) for n in ("both", "server", "tool"))
    sid = shared_mcp()
    g1, g2 = access.create_group("Server")["id"], access.create_group("Tool")["id"]
    access.set_members(g1, [both, server_only])
    access.set_members(g2, [both, tool_only])
    access.set_rule("mcp", sid, [g1])
    access.set_tool_rules(sid, {"draw": [g2]})
    assert access.tool_rules(sid) == {"draw": [g2]}
    assert access.tool_allowed(both, sid, "draw")
    assert not access.tool_allowed(server_only, sid, "draw") and not access.tool_allowed(tool_only, sid, "draw")
    assert access.tool_allowed(server_only, sid, "lookup") and not access.tool_allowed(tool_only, sid, "lookup")
    assert access.tool_allowed(None, sid, "draw")
    access.set_tool_rules(sid, {})
    assert access.tool_rules(sid) == {} and access.rule("mcp", sid) == [g1]


def test_deleted_group_keeps_rule_closed(studio):
    member = make_user(studio, "member")
    cid = shared_connection()
    group = access.create_group("Media")["id"]
    access.set_members(group, [member])
    access.set_rule("connection", cid, [group])
    access.delete_group(group)
    assert access.rule("connection", cid) == []
    assert not access.allowed(member, "connection", cid)
    clause, args = access.shared_filter(member, "connection", "connections.id")
    assert state.DB.execute(f"SELECT id FROM connections WHERE {clause}", args).fetchall() == []


def test_item_delete_drops_rules(studio):
    cid, sid = shared_connection(), shared_mcp()
    access.set_rule("connection", cid, [])
    access.set_rule("mcp", sid, [])
    access.set_tool_rules(sid, {"draw": []})
    state.DB.execute("DELETE FROM connections WHERE id = ?", (cid,))
    state.DB.execute("DELETE FROM mcp_servers WHERE id = ?", (sid,))
    assert state.DB.execute("SELECT count(*) FROM access_rules").fetchone()[0] == 0


def test_group_name_unique_case_insensitive(studio):
    first = access.create_group("Медиа")
    with pytest.raises(HTTPException) as e:
        access.create_group("  медиа ")
    assert e.value.status_code == 409
    second = access.create_group("Other")
    with pytest.raises(HTTPException) as e:
        access.rename_group(second["id"], "МЕДИА")
    assert e.value.status_code == 409
    assert access.rename_group(first["id"], "МЕДИА")["name"] == "МЕДИА"
    with pytest.raises(HTTPException) as e:
        access.rename_group(9999, "x")
    assert e.value.status_code == 404


def test_set_members_and_rules_reject_unknown_ids(studio):
    group = access.create_group("Media")["id"]
    with pytest.raises(HTTPException) as e:
        access.set_members(group, [9999])
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        access.set_rule("connection", shared_connection(), [9999])
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        access.set_rule("mcp", 9999, None)
    assert e.value.status_code == 404


def test_studio_user_is_stable_and_opaque(studio):
    uid = make_user(studio, "someone")
    other = make_user(studio, "another")
    value = access.studio_user(uid)
    assert re.fullmatch("[0-9a-f]{32}", value)
    assert value == access.studio_user(uid) != access.studio_user(other)
    assert "someone" not in value and str(uid) != value


# ---- Task 2: общие API моделей ---------------------------------------------------------

def closed_models(studio, *members) -> tuple[int, int]:
    """Общий API моделей «Shared models» (чат, модель MODEL), закрытый группой из `members`; вход — администратором."""
    from test_shared_integrations import add_models
    signin(studio, "admin")
    cid = add_models(studio)
    group = access.create_group("Models")["id"]
    access.set_members(group, list(members))
    access.set_rule("connection", cid, [group])
    return cid, group


def chat_links(studio, path="/api/sections") -> list[int]:
    return [link["connection"]["id"] for link in studio.c.get(path).json()["sections"][0]["links"]]


def test_closed_shared_model_hidden_from_sections(studio):
    from test_shared_integrations import MODEL
    make_user(studio, "admin", is_admin=True)
    member, other = make_user(studio, "member"), make_user(studio, "other")
    cid, _ = closed_models(studio, member)
    assert cid in chat_links(studio)                                  # администратор видит всё
    signin(studio, "member")
    assert cid in chat_links(studio)
    assert cid in [c["id"] for c in studio.c.get("/api/connections").json()["items"]]
    assert studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": MODEL}).status_code == 200
    signin(studio, "other")
    assert cid not in chat_links(studio)
    assert cid not in [c["id"] for c in studio.c.get("/api/connections").json()["items"]]
    assert studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": MODEL}).status_code == 404
    assert studio.c.get("/api/me").json()["default_connection_id"] is None


def test_closed_shared_model_in_existing_chat(studio):
    from test_chat import new_chat, sse, turn
    from test_shared_integrations import MODEL
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "existing")
    signin(studio, "existing")
    personal = add_connection(studio)
    cid, group = closed_models(studio)
    access.set_rule("connection", cid, None)                          # пока — всем
    signin(studio, "existing")
    assert studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": MODEL}).status_code == 200
    chat_id = new_chat(studio)
    assert sse(turn(studio, chat_id))[-1] == ("done", {"status": "done"})
    access.set_rule("connection", cid, [group])                       # группа без этой учётки
    opened = studio.c.get(f"/api/chats/{chat_id}")
    assert opened.status_code == 200 and opened.json()["chat"]["connection_id"] == cid
    refused = turn(studio, chat_id)
    assert refused.status_code == 404 and refused.json()["detail"]["code"] == "gone"
    assert studio.c.get("/api/me").json()["default_connection_id"] == personal


def test_background_paths_ignore_rules(studio):
    import ocr
    import videos
    make_user(studio, "admin", is_admin=True)
    other = make_user(studio, "other")
    cid, _ = closed_models(studio)
    assert videos._connection({"user_id": other, "connection_id": cid}).id == cid
    ocr.LEASED.clear()
    ocr.lease_lost(other, cid)
    assert ocr.LEASED.pop("http://10.77.1.15:4000/v1").id == cid


def test_admin_views_user_with_users_access(studio):
    make_user(studio, "admin", is_admin=True)
    member, other = make_user(studio, "member"), make_user(studio, "other")
    cid, _ = closed_models(studio, member)
    assert cid in chat_links(studio, f"/api/admin/users/{member}/sections")
    assert cid not in chat_links(studio, f"/api/admin/users/{other}/sections")


# ---- Task 3: общие MCP и их инструменты --------------------------------------------------

def shared_tools(studio) -> int:
    """Общий API моделей (для хода чата) и общий MCP «Shared tools»; вход — администратором."""
    from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL
    from test_shared_integrations import add_models
    signin(studio, "admin")
    add_models(studio)
    response = studio.c.post("/api/admin/shared/mcp", json={"name": "Shared tools", "url": MCP_URL, "token": MCP_KEY})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def recorded(studio, path="/mcp") -> list:
    """Значения X-Studio-User запросов к MCP (None — заголовка нет) поверх уже поставленного мока."""
    seen, inner = [], studio.gw.on[path]

    async def handler(request):
        seen.append(request.headers.get("x-studio-user"))
        return await inner(request)

    studio.gw.on[path] = handler
    return seen


def video_tools(studio, sid) -> "list[str] | None":
    item = next((s for s in studio.c.get("/api/mcp-tools/video").json()["items"] if s["id"] == sid), None)
    return None if item is None else [t["name"] for t in item["tools"]]


def png_file() -> dict:
    import base64
    from fakes import PNG_REAL
    return {"artifact_id": {"data": base64.b64encode(PNG_REAL).decode(), "mime_type": "image/png"}}


def test_closed_mcp_hidden(studio):
    from test_chat import new_chat
    from test_mcp_sections import service
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    sid = shared_tools(studio)
    service(studio.gw)
    access.set_rule("mcp", sid, [])
    assert [i["id"] for i in studio.c.get("/api/mcp").json()["items"]] == [sid]
    assert video_tools(studio, sid)
    signin(studio, "member")
    assert studio.c.get("/api/mcp").json()["items"] == []
    assert video_tools(studio, sid) is None
    chat_id = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{chat_id}", json={"mcp_ids": [sid]}).status_code == 404
    run = studio.c.post(f"/api/mcp-tools/video/servers/{sid}/run", json={"tool": "video_estimate", "files": png_file()})
    assert run.status_code == 404


def test_tool_rule_hides_tool_in_chat_and_sections(studio):
    from test_chat import new_chat, sse, turn
    from test_mcp_sections import service
    make_user(studio, "admin", is_admin=True)
    member = make_user(studio, "member")
    sid = shared_tools(studio)
    calls = service(studio.gw)
    group = access.create_group("Video")["id"]
    access.set_tool_rules(sid, {"video_create": [group]})
    signin(studio, "member")
    assert video_tools(studio, sid) == ["video_estimate"]
    run = studio.c.post(f"/api/mcp-tools/video/servers/{sid}/run", json={"tool": "video_create", "files": png_file()})
    assert run.status_code == 422 and calls == []
    chat_id = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{chat_id}", json={"mcp_ids": [sid]}).status_code == 200
    studio.gw.chat_script = [["Done"]]
    assert sse(turn(studio, chat_id))[-1] == ("done", {"status": "done"})
    described = [t["function"]["description"] for t in studio.gw.chat_requests[-1]["tools"]]
    assert described and not any("video_create" in d for d in described)
    assert any("video_estimate" in d for d in described)
    access.set_members(group, [member])
    assert "video_create" in video_tools(studio, sid)


def test_tool_rule_rechecked_on_call(studio):
    from test_chat import messages, new_chat, sse, turn
    from test_chat_web import call
    from test_mcp import mock
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    sid = shared_tools(studio)
    calls = mock(studio)
    signin(studio, "member")
    chat_id = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{chat_id}", json={"mcp_ids": [sid]}).status_code == 200
    model = studio.gw.builtin

    async def closing_model(request):                 # правило ставят после discover, до вызова инструмента
        access.set_tool_rules(sid, {"lookup": []})
        return await model(request)

    studio.gw.on["/v1/chat/completions"] = closing_model
    studio.gw.chat_script = [{"tool_calls": [call(f"mcp_{sid}_0")]}, ["Done"]]
    assert sse(turn(studio, chat_id, "Call the tool"))[-1] == ("done", {"status": "done"})
    assert "tools/call" not in [method for method, _ in calls]
    step = messages(studio, chat_id)[1]["tools"][0]
    assert step["kind"] == "mcp" and step["tool"] == "lookup" and step["error"]


def test_service_calls_ignore_tool_rules(studio):
    from test_mcp_sections import service
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    sid = shared_tools(studio)
    calls = service(studio.gw)
    access.set_tool_rules(sid, {"artifact_upload": [], "job_status": [], "job_result": []})
    signin(studio, "member")
    base = "/api/mcp-tools/video"
    result = studio.c.post(f"{base}/servers/{sid}/run", json={"tool": "video_create", "files": png_file()})
    assert result.status_code == 200, result.text
    ticket = {"ticket": result.json()["job"]}
    assert studio.c.post(f"{base}/status", json=ticket).json()["status"] == "running"
    done = studio.c.post(f"{base}/status", json=ticket).json()
    assert done["files"][0]["mime"] == "video/mp4"
    assert [name for name, _ in calls] == ["artifact_upload", "video_create", "job_status", "job_status", "job_result"]


def test_closed_mcp_dropped_from_chat_selection(studio):
    from test_chat import new_chat, sse, turn
    from test_mcp import mock
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    sid = shared_tools(studio)
    calls = mock(studio)
    signin(studio, "member")
    chat_id = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{chat_id}", json={"mcp_ids": [sid]}).status_code == 200
    access.set_rule("mcp", sid, [])
    assert studio.c.get(f"/api/chats/{chat_id}").json()["chat"]["mcp_ids"] == []
    studio.gw.chat_script = [["Done"]]
    assert sse(turn(studio, chat_id))[-1] == ("done", {"status": "done"})
    assert calls == [] and "tools" not in studio.gw.chat_requests[-1]


def test_ticket_rechecks_server_access(studio):
    from test_mcp_sections import service
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    sid = shared_tools(studio)
    service(studio.gw)
    signin(studio, "member")
    base = "/api/mcp-tools/video"
    ticket = {"ticket": studio.c.post(f"{base}/servers/{sid}/run",
                                      json={"tool": "video_create", "files": png_file()}).json()["job"]}
    assert studio.c.post(f"{base}/status", json=ticket).json()["status"] == "running"
    url = studio.c.post(f"{base}/status", json=ticket).json()["files"][0]["url"]
    access.set_rule("mcp", sid, [])
    assert studio.c.post(f"{base}/status", json=ticket).status_code == 404
    assert studio.c.get(url, headers={"Range": "bytes=0-9"}).status_code == 404


def test_studio_user_header_only_for_shared(studio):
    from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL, mock
    admin = make_user(studio, "admin", is_admin=True)
    member = make_user(studio, "member")
    sid = shared_tools(studio)
    mock(studio)
    seen = recorded(studio)
    assert studio.c.post(f"/api/admin/shared/mcp/{sid}/check").status_code == 200
    assert set(seen) == {access.studio_user(admin)}
    signin(studio, "member")
    seen.clear()
    assert studio.c.get("/api/mcp-tools/video").status_code == 200
    assert seen and set(seen) == {access.studio_user(member)}
    personal = studio.c.post("/api/mcp", json={"name": "Mine", "url": MCP_URL, "token": MCP_KEY})
    assert personal.status_code == 201, personal.text
    seen.clear()
    assert studio.c.post(f"/api/mcp/{personal.json()['id']}/check").status_code == 200
    assert seen and set(seen) == {None}
    forged = studio.c.post("/api/mcp", json={"name": "Forged", "url": MCP_URL, "headers": {"X-Studio-User": "0" * 32}})
    assert forged.status_code == 422
