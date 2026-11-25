"""Shared credentials stay server-side and the integrations work in ordinary user sessions."""

import base64

import mcp_sources
import state
from conftest import add_connection, make_user, signin
from fakes import PNG_REAL, TOKEN as MODEL_KEY
from test_chat import new_chat, sse, turn
from test_chat_web import call
from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL, mock
from test_mcp_dialog import name_of
from test_mcp_sections import service

ADMIN = "/api/admin/shared"
MODELS_URL = "http://10.77.1.15:4000/v1"
MODEL = "test-chat"


def add_models(studio):
    body = {"name": "Shared models", "base_url": MODELS_URL, "kind": "openai", "sections": ["chat"], "token": MODEL_KEY}
    rejected = studio.c.post(f"{ADMIN}/connections", json={**body, "base_url": "https://other.test/v1"})
    assert rejected.status_code == 422
    response = studio.c.post(f"{ADMIN}/connections", json=body)
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    assert studio.c.get(f"{ADMIN}/sections/chat/links/{cid}/found").status_code == 200
    assert studio.c.put(f"{ADMIN}/sections/chat/links/{cid}", json={"models": [MODEL]}).status_code == 200
    return cid


def test_shared_model_catalog_is_inherited_and_revocation_applies_to_existing_chats(studio):
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "existing")
    signin(studio, "existing")
    personal = add_connection(studio)
    signin(studio, "admin")
    cid = add_models(studio)
    stored = state.DB.execute("SELECT token_enc,user_id FROM connections WHERE id=?", (cid,)).fetchone()
    assert stored[1] is None and MODEL_KEY.encode() not in stored[0]

    signin(studio, "existing")
    chat = studio.c.get("/api/sections").json()["sections"][0]
    assert chat["default"]["connection_id"] == personal
    shared = next(link for link in chat["links"] if link["connection"]["id"] == cid)
    assert shared["models"] == [MODEL] and shared["connection"]["shared"]
    assert shared["connection"]["base_url"] == shared["connection"]["token_hint"] == ""
    assert studio.c.get(f"{ADMIN}/connections").status_code == 403
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403
    assert studio.c.patch(f"/api/connections/{cid}", json={"name": "Changed"}).status_code == 404
    assert studio.c.delete(f"/api/connections/{cid}").status_code == 404
    assert studio.c.get(f"/api/sections/chat/links/{cid}/found").status_code == 404
    assert studio.c.put(f"/api/sections/chat/links/{cid}", json={"models": ["other"]}).status_code == 404
    assert studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": MODEL}).status_code == 200
    chat_id = new_chat(studio)
    assert sse(turn(studio, chat_id))[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_requests[-1]["model"] == MODEL
    assert studio.gw.chat_auth[-1] == f"Bearer {MODEL_KEY}"

    make_user(studio, "newcomer")
    signin(studio, "newcomer")
    assert studio.c.get("/api/me").json()["default_connection_id"] == cid
    assert studio.c.get(f"/api/chats/{chat_id}").status_code == 404
    signin(studio, "admin")
    assert studio.c.put(f"{ADMIN}/sections/chat/links/{cid}", json={"models": []}).status_code == 200
    signin(studio, "existing")
    assert turn(studio, chat_id).status_code == 422
    assert studio.c.get("/api/me").json()["default_connection_id"] == personal
    signin(studio, "admin")
    assert studio.c.delete(f"{ADMIN}/connections/{cid}").status_code == 204
    signin(studio, "existing")
    assert studio.c.get(f"/api/chats/{chat_id}").json()["chat"]["connection_id"] is None


def test_shared_mcp_uses_the_current_users_chat_and_keeps_result_tickets_private(studio):
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    make_user(studio, "other")
    signin(studio, "admin")
    add_models(studio)
    body = {"name": "Shared tools", "url": MCP_URL, "token": MCP_KEY}
    response = studio.c.post(f"{ADMIN}/mcp", json=body)
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    calls = mock(studio)
    assert studio.c.post(f"{ADMIN}/mcp/{sid}/check").json() == {"tools": ["lookup"]}

    signin(studio, "member")
    assert studio.c.get("/api/mcp").json()["items"] == [
        {"id": sid, "name": "Shared tools", "url": "", "enabled": True, "shared": True, "dialog_default": True}]
    assert studio.c.get(f"{ADMIN}/mcp/{sid}").status_code == 403
    assert studio.c.get(f"/api/mcp/{sid}").status_code == 404
    assert studio.c.put(f"/api/mcp/{sid}", json=body).status_code == 404
    assert studio.c.delete(f"/api/mcp/{sid}").status_code == 404
    chat_id = new_chat(studio)
    assert studio.c.get(f"/api/chats/{chat_id}").json()["chat"]["mcp_ids"] == [sid]   # общий — по умолчанию
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Done"]]
    assert sse(turn(studio, chat_id, "Call the tool"))[-1] == ("done", {"status": "done"})
    assert calls[-2][0] == "tools/call"

    service(studio.gw)
    mcp_sources.forget(sid)          # тот же адрес отвечает другим набором: каталог в кэше — до CATALOG_TTL_SEC
    base = "/api/mcp-tools/video"
    assert studio.c.get(base).json()["items"][0]["id"] == sid
    result = studio.c.post(f"{base}/servers/{sid}/run", json={"tool": "video_create", "files": {
        "artifact_id": {"data": base64.b64encode(PNG_REAL).decode(), "mime_type": "image/png"}}})
    assert result.status_code == 200, result.text
    ticket = {"ticket": result.json()["job"]}
    signin(studio, "other")
    assert studio.c.post(f"{base}/status", json=ticket).status_code == 404
    signin(studio, "member")
    assert studio.c.post(f"{base}/status", json=ticket).json()["status"] == "running"
    signin(studio, "admin")
    assert studio.c.put(f"{ADMIN}/mcp/{sid}", json={"name": "Shared tools", "url": MCP_URL, "enabled": False}).status_code == 200
    signin(studio, "member")
    assert studio.c.get(base).json()["items"] == []
    assert studio.c.post(f"{base}/status", json=ticket).status_code == 404
