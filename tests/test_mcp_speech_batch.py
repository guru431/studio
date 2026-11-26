"""Speech uses a persistent Studio queue and the source's per-part constraints."""
import asyncio
import json

import httpx

import mcp_sections
import state
from fakes import WAV_FAKE
from test_mcp import TOKEN, add

PARTS = ["First.", "Second.", "Third."]
TEXT = "\n\n".join(PARTS)


def serve_speech(gateway):
    calls = []
    props = {"text": {"type": "string", "maxLength": 9},
             "model": {"type": "string", "enum": ["first", "second"]},
             "voice": {"type": "string", "enum": ["alina", "nova"]},
             "language": {"type": "string", "enum": ["ru", "en"]},
             "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 5, "default": 0}}
    tools = [{"name": "speech_synthesize", "inputSchema": {"type": "object", "properties": props, "required": ["text"]}}]
    tools.extend({"name": name, "inputSchema": {"type": "object", "properties": {}}}
                 for name in ("job_status", "job_result"))

    async def content(request):
        ordinal = int(request.url.path.split("/")[2].removeprefix("art_"), 16)
        return httpx.Response(200, content=WAV_FAKE + bytes([ordinal]))

    async def rpc(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content)
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        match payload["method"]:
            case "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
            case "tools/list":
                result = {"tools": tools}
            case "tools/call":
                name, args = (payload["params"][key] for key in ("name", "arguments"))
                if name == "speech_synthesize":
                    calls.append(args)
                    assert 0 < len(args["text"]) <= 9
                    data = {"id": f"job_{len(calls):032x}"}
                elif name == "job_status":
                    data = {"status": "completed"}
                else:
                    assert name == "job_result"
                    number = int(args["job_id"].removeprefix("job_"), 16)
                    aid = f"art_{number:032x}"
                    path = f"/artifacts/{aid}/content"
                    gateway.on[path] = content
                    data = {"result": {"artifacts": [
                        {"id": aid, "mime_type": "audio/wav", "name": f"{number}.wav", "download_path": path},
                    ]}}
                result = {"structuredContent": data}
            case _:
                raise AssertionError(payload["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    gateway.on["/mcp"] = rpc
    return calls


def test_speech_parts_survive_the_request_and_keep_order_in_one_history(studio):
    sid = add(studio)
    calls = serve_speech(studio.gw)
    settings = {"language": "en", "voice": "nova", "model": "second"}
    response = studio.c.post(f"/api/mcp-tools/speech/servers/{sid}/run", json={
        "tool": "speech_synthesize", "record": True, "arguments": {"text": TEXT, **settings},
    })
    assert response.status_code == 200
    rid = response.json()["history"]["id"]
    assert "speech_batch" not in response.json()["history"]["result"]
    assert not calls
    route = f"/api/mcp-tools/speech/history/{rid}"
    assert studio.c.get(route).json()["active"]
    for i in range(len(PARTS)):
        for _ in range(2):  # Launch a part, then read its completed upstream job.
            row = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE id = ?", (rid,)).fetchone()
            asyncio.run(mcp_sections.refresh_history(row))
        history = studio.c.get(route).json()
        assert history["active"] == (i + 1 < len(PARTS))
        assert "speech_batch" not in history["result"]
        assert len(history["result"]["files"]) == i + 1
    assert calls == [{"text": part, **settings} for part in PARTS]
    assert history["status"] == "completed" and history["prompt"] == TEXT
    assert [studio.c.get(item["url"]).content for item in history["result"]["files"]] == [
        WAV_FAKE + bytes([i]) for i in range(1, len(PARTS) + 1)]


def test_interrupted_speech_dispatch_is_not_replayed(studio):
    sid = add(studio)
    calls = serve_speech(studio.gw)
    response = studio.c.post(f"/api/mcp-tools/speech/servers/{sid}/run", json={
        "tool": "speech_synthesize", "record": True, "arguments": {"text": TEXT},
    })
    assert response.status_code == 200
    rid = response.json()["history"]["id"]
    state.DB.execute("UPDATE mcp_section_jobs SET status = 'saving' WHERE id = ?", (rid,))
    row = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE id = ?", (rid,)).fetchone()
    asyncio.run(mcp_sections.refresh_history(row))
    history = studio.c.get(f"/api/mcp-tools/speech/history/{rid}").json()
    assert history["status"] == "failed" and not history["active"] and not calls
    assert "speech_batch" not in history["result"]
