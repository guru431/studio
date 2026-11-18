"""Section tools through the same HTTP contract as the srvllm/s06 MCP service."""

import base64
import json

import httpx

import state
from conftest import make_user, signin
from fakes import MP4, PNG_REAL
from test_mcp import TOKEN, URL, add

JOB = "job_" + "1" * 32
ARTIFACT = "art_" + "2" * 32
PATH = f"/artifacts/{ARTIFACT}/content"


def service(gateway, *, image=PNG_REAL):
    tools = {"image_generate": {"prompt": {"type": "string"}},
             "video_create": {"artifact_id": {"type": "string"}},
             "artifact_upload": {}, "job_status": {}, "job_result": {}, "internal_action": {}}
    calls = []
    statuses = iter(("running", "completed"))

    async def rpc(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": props,
                                                              "required": list(props)}} for name, props in tools.items()]}
        else:
            name, args = (payload["params"][key] for key in ("name", "arguments"))
            calls.append((name, args))
            if name == "image_generate":
                result = {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(image).decode()}]}
            else:
                data = {"id": JOB, "status": "queued"}
                if name == "artifact_upload":
                    data = {"id": ARTIFACT}
                elif name == "job_status":
                    data = {"status": next(statuses)}
                elif name == "job_result":
                    data = {"result": {"artifacts": [{"id": ARTIFACT, "mime_type": "video/mp4", "name": "video.mp4",
                                                        "download_path": PATH, "download_url": "https://example.net/ignored"}]}}
                result = {"structuredContent": data}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async def download(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["range"] == "bytes=0-9"
        return httpx.Response(206, headers={"content-range": f"bytes 0-9/{len(MP4)}"}, content=MP4[:10])

    gateway.on["/mcp"] = rpc
    gateway.on[PATH] = download
    return calls


def test_section_tools_discovered_for_owner_and_called_only_in_matching_section(studio, caplog):
    sid = add(studio)
    image = PNG_REAL + bytes(1 << 20)  # Inline media can exceed the chat RPC budget.
    calls = service(studio.gw, image=image)
    for section, names in [("photo", ["image_generate"]), ("video", ["video_create"]), ("speech", [])]:
        reply = studio.c.get(f"/api/mcp-tools/{section}")
        assert reply.status_code == 200
        assert [tool["name"] for server in reply.json()["items"] for tool in server["tools"]] == names
        assert TOKEN not in reply.text
    assert not calls
    reply = studio.c.post(f"/api/mcp-tools/video/servers/{sid}/run", json={"tool": "image_generate"})
    assert reply.status_code == 422 and not calls
    reply = studio.c.post(f"/api/mcp-tools/photo/servers/{sid}/run",
                          json={"tool": "image_generate", "arguments": {"prompt": "private prompt"}})
    assert base64.b64decode(reply.json()["files"][0]["base64"]) == image
    assert calls == [("image_generate", {"prompt": "private prompt"})]
    assert "private prompt" not in caplog.text and TOKEN not in caplog.text


def test_video_job_upload_status_and_private_range_download(studio):
    sid = add(studio)
    calls = service(studio.gw)
    base = "/api/mcp-tools/video"
    reply = studio.c.post(f"{base}/servers/{sid}/run", json={"tool": "video_create", "files": {
        "artifact_id": {"data": base64.b64encode(PNG_REAL).decode(), "mime_type": "image/png"}}})
    assert reply.status_code == 200, reply.text
    ticket = {"ticket": reply.json()["job"]}
    assert studio.c.post("/api/mcp-tools/photo/status", json=ticket).status_code == 404
    assert studio.c.post(f"{base}/status", json=ticket).json() == {"status": "running", "active": True, "error": ""}
    file = studio.c.post(f"{base}/status", json=ticket).json()["files"][0]
    assert file["mime"] == "video/mp4" and file["url"].startswith(f"{base}/files/")
    assert calls[0][0] == "artifact_upload" and calls[0][1]["mime_type"] == "image/png"
    assert calls[1:] == [("video_create", {"artifact_id": ARTIFACT}), ("job_status", {"job_id": JOB}),
                         ("job_status", {"job_id": JOB}), ("job_result", {"job_id": JOB})]
    ranged = studio.c.get(file["url"], headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206 and ranged.content == MP4[:10]
    assert ranged.headers["content-range"] == f"bytes 0-9/{len(MP4)}"
    assert ranged.headers["cache-control"] == "no-store"
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"video\"]' WHERE login='owner'")
    state.DB.execute("UPDATE sessions SET seen=NULL")
    assert studio.c.get(file["url"]).status_code == 423
    assert studio.c.post(f"{base}/status", json=ticket).status_code == 423
    assert studio.c.get("/api/mcp-tools/photo").status_code == 200
    state.DB.execute("UPDATE users SET screen_lock=0 WHERE login='owner'")
    assert studio.c.put(f"/api/mcp/{sid}", json={"name": "Off", "url": URL, "enabled": False}).status_code == 200
    assert studio.c.get(base).json()["items"] == []
    assert studio.c.get(file["url"]).status_code == 404
    make_user(studio, "other")
    signin(studio, "other")
    assert studio.c.get(file["url"]).status_code == 404
    assert studio.c.post(f"{base}/status", json=ticket).status_code == 404
    assert studio.c.post(f"{base}/servers/{sid}/run", json={"tool": "video_create"}).status_code == 404


def test_unavailable_mcp_does_not_hide_other_servers(studio):
    sid = add(studio)
    service(studio.gw)
    other = studio.c.post('/api/mcp', json={'name': 'Offline', 'url': URL + '-offline'}).json()
    reply = studio.c.get("/api/mcp-tools/photo").json()
    assert [server["id"] for server in reply["items"]] == [sid, other["id"]]
    assert reply["items"][0]["tools"][0]["name"] == "image_generate"
    assert reply["items"][1] == {"id": other["id"], "name": "Offline", "unavailable": True}
