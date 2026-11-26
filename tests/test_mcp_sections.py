"""Section tools through the same HTTP contract as the srvllm/s06 MCP service."""

import asyncio
import base64
import json
from urllib.parse import urlsplit

from fastapi import HTTPException
import httpx
import pytest

import mcp_sections
import media
import state
from conftest import make_user, signin
from fakes import MP4, PNG_REAL, WAV_FAKE
from test_mcp import TOKEN, URL, add

JOB = "job_" + "1" * 32
ARTIFACT = "art_" + "2" * 32
PATH = f"/artifacts/{ARTIFACT}/content"


def service(gateway, *, image=PNG_REAL, uploads=False, clone_models=False, limits=False, broken_schema=False,
            named_upload=False, catalogs=False, monkeypatch=None, photo_job=False, catalog_results=None, chat_model=None):
    def params(**types):
        return {key: {"type": value} for key, value in types.items()}

    video = params(artifact_id="string", prompt="string", seconds="integer", quality="string", smooth="boolean")
    tools = {"image_generate": (params(prompt="string", size="string", model="string", n="integer"), ["prompt"]),
             "video_create": (video, ["artifact_id"]), "video_estimate": (video, ["artifact_id"]),
             "text_translate": (params(text="string", source_lang="string", target_lang="string"),
                                ["text", "source_lang", "target_lang"]),
             "speech_synthesize": (params(text="string", voice="string", reference_artifact_id="string", reference_text="string")
                                   | {"engine": {"type": "string", "enum": ["default", "clone"]}}, ["text"]),
             **dict.fromkeys(("artifact_upload", "job_status", "job_result", "internal_action"), ({}, []))}
    if catalogs:
        video["model"] = {"type": "string", "enum": ["wan", "h3"], "default": "wan"}
        tools |= {"services_list": ({}, []), "models_list": ({}, []),
                  "llm_ask": (params(messages="array", model="string"), ["messages"])}
        if chat_model is not None:
            tools["llm_ask"][0]["model"] = chat_model
    if uploads:
        tools.update(dict.fromkeys(("audio_transcribe", "document_read"), (params(artifact_id="string"), ["artifact_id"])))
    if named_upload:
        tools["artifact_upload"] = (params(data_base64="string", mime_type="string", name="string"),
                                    ["data_base64", "mime_type"])
    if clone_models:
        tools["speech_synthesize"][0]["model"] = {"type": "string", "enum": ["qwen3-tts", "f5-tts"], "default": "qwen3-tts"}
    if limits:
        video["seconds"] = {"type": "number", "minimum": 1, "maximum": 600}
        video["quality"] = {"type": "string", "minimum": 10, "maximum": 2}   # бессмысленные пределы — решает инструмент
        tools["image_generate"][0]["size"] = {"type": "string", "enum": ["1024x1024", "1344x768"]}
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
                                                              "required": required}} for name, (props, required) in tools.items()]}
            if broken_schema:
                schema = result["tools"][1]["inputSchema"]
                schema["properties"] = {**schema["properties"], "seconds": {"maximum": "__overflow__"}}
                content = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
                return httpx.Response(200, content=content.replace('"__overflow__"', '1e400'))
        else:
            name, args = (payload["params"][key] for key in ("name", "arguments"))
            calls.append((name, args))
            if name == "image_generate" and not photo_job:
                result = {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(image).decode()}]}
            else:
                data = {"id": JOB, "status": "queued"}
                if catalog_results and name in catalog_results:
                    data = catalog_results[name]
                elif name == "services_list":
                    data = {"declarations": {"images": {"data": [{"id": "photo-a"}, {"id": "wan"}, {"id": "photo-a"}]}},
                            "services": {"video": {"model_limits": {"wan": {}}}}}
                elif name == "models_list":
                    data = {"data": [{"id": "chat-a", "mode": "chat"}, {"id": "chat-b", "mode": "chat"}]}
                elif name == "llm_ask":
                    data = {"text": "Reply from MCP"}
                elif name == "artifact_upload":
                    data = {"id": ARTIFACT}
                elif name == "video_estimate":
                    data = {"text": "Estimated time"}
                elif name == "text_translate":
                    data = {"text": "Translated text"}
                elif name == "speech_synthesize":
                    data = {"text": "Synthesized speech"}
                elif name == "audio_transcribe":
                    data = {"text": "Transcribed audio"}
                elif name == "document_read":
                    data = {"text": "Recognized page"}
                elif name == "job_status":
                    data = {"status": next(statuses)}
                elif name == "job_result" and photo_job:
                    data = {"result": {"artifacts": [
                        {"id": aid, "mime_type": "image/png", "name": f"{index}.png",
                         "download_path": f"/artifacts/{aid}/content"}
                        for index, aid in enumerate((ARTIFACT, "art_" + "3" * 32))
                    ]}}
                elif name == "job_result":
                    data = {"result": {"artifacts": [{"id": ARTIFACT, "mime_type": "video/mp4", "name": "video.mp4",
                                                        "download_path": PATH, "download_url": "https://example.net/ignored"}]}}
                result = {"structuredContent": data}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async def download(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["range"] == "bytes=0-9"
        return httpx.Response(206, headers={"content-range": f"bytes 0-9/{len(MP4)}"}, content=MP4[:10])

    if monkeypatch is None:
        gateway.on.update({"/mcp": rpc, PATH: download})
    else:
        monkeypatch.setitem(gateway.on, "/mcp", rpc)
        monkeypatch.setitem(gateway.on, PATH, download)
    return calls


def test_section_tools_discovered_for_owner_and_called_only_in_matching_section(studio, caplog):
    sid = add(studio)
    image = PNG_REAL + bytes(1 << 20)  # Inline media can exceed the chat RPC budget.
    calls = service(studio.gw, image=image)
    for section, names in [("photo", ["image_generate"]), ("video", ["video_create", "video_estimate"]), ("ocr", [])]:
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


def test_section_upload_sends_the_file_name_only_to_a_server_that_takes_it(studio):
    """Итоги называются по имени файла («Совещание — протокол.docx»), если `artifact_upload` принимает `name`."""
    sid = add(studio)
    route = f"/api/mcp-tools/transcribe/servers/{sid}/run"
    payload = {"tool": "audio_transcribe", "files": {"artifact_id": {
        "data": base64.b64encode(WAV_FAKE).decode(), "mime_type": "audio/wav", "name": "Совещание 22.11.wav"}}}
    calls = service(studio.gw, uploads=True, named_upload=True)
    assert studio.c.post(route, json=payload).status_code == 200
    assert calls[0][0] == "artifact_upload" and calls[0][1]["name"] == "Совещание 22.11.wav"
    calls.clear()
    calls = service(studio.gw, uploads=True)
    assert studio.c.post(route, json=payload).status_code == 200
    assert calls[0][0] == "artifact_upload" and "name" not in calls[0][1]


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


#: Пределы поля схемы → что принимает сервер (`check_bounds`) и чем страница их видит (`schemaLimits`, умолчание формы).
#: Одна таблица на обе стороны: браузерный `test_ui_section_limits.py` сверяет по ней страницу.
LIMIT_CASES = [
    # (случай, поле схемы, пробные значения, принимаются ли, умолчание у страницы)
    ("range", {"type": "number", "minimum": 1, "maximum": 600, "default": 3}, [0, 1, 600, 601, True, "5"],
     [False, True, True, False, False, False], 3),
    ("min_above_max", {"type": "number", "minimum": 50, "maximum": 10, "default": 999}, [5, 45, 999], [True] * 3, None),
    ("huge_enum", {"type": "string", "enum": ["832x1216", *(f"{n}x{n}" for n in range(99))]}, ["nope", "832x1216"],
     [True, True], None),
    ("long_enum_value", {"type": "string", "enum": ["a" * 81, "b"]}, ["c"], [True], None),
    ("astral_enum_value", {"type": "string", "enum": ["🛠" * 80], "default": "🛠" * 80},
     ["🛠" * 80, "🛠" * 79], [True, False], "🛠" * 80),
    ("default_out_of_range", {"type": "integer", "minimum": 1, "maximum": 60, "default": 999}, [60, 61], [True, False], None),
    ("over_limit", {"type": "number", "maximum": 10**7}, [5 * 10**6], [True], None),
    ("non_finite", {"type": "number", "minimum": 1, "maximum": float("inf")}, [0, 10**9], [True, True], None),
    ("text_bound", {"type": "number", "minimum": "1", "maximum": 10}, [0, 20], [True, True], None),
    ("enum", {"type": "string", "enum": ["1024x1024", "1344x768"], "default": "1344x768"}, ["1344x768", "999x999", True],
     [True, False, False], "1344x768"),
]


@pytest.mark.parametrize("case, prop, probes, accepted, default", LIMIT_CASES, ids=[case[0] for case in LIMIT_CASES])
def test_server_bounds_follow_the_page_limit_rules(case, prop, probes, accepted, default):
    tool = {"schema": {"properties": {"value": prop}}}

    def accepts(value):
        try:
            mcp_sections.check_bounds(tool, {"value": value})
        except HTTPException as exc:
            assert exc.status_code == 422
            return False
        return True

    assert [accepts(value) for value in probes] == accepted


def test_run_refuses_values_outside_schema(studio):
    sid = add(studio)
    calls = service(studio.gw, limits=True)
    video = f"/api/mcp-tools/video/servers/{sid}/run"
    for seconds in (601, 0, True, "5"):
        reply = studio.c.post(video, json={"tool": "video_estimate", "arguments": {"seconds": seconds}})
        assert reply.status_code == 422 and reply.json()["detail"]["message"] == "Значение «seconds» — от 1 до 600"
    reply = studio.c.post(f"/api/mcp-tools/photo/servers/{sid}/run",
                          json={"tool": "image_generate", "arguments": {"prompt": "p", "size": "999x999"}})
    assert reply.status_code == 422 and reply.json()["detail"]["message"] == "Значение «size» — не из списка инструмента"
    assert calls == []
    reply = studio.c.post(video, json={"tool": "video_estimate", "arguments": {"seconds": 600, "quality": "high"}})
    assert reply.json()["text"] == "Estimated time"
    assert calls == [("video_estimate", {"seconds": 600, "quality": "high"})]


def test_unavailable_mcp_does_not_hide_other_servers(studio):
    sid = add(studio)
    service(studio.gw)
    other = studio.c.post('/api/mcp', json={'name': 'Offline', 'url': URL + '-offline'}).json()
    reply = studio.c.get("/api/mcp-tools/photo").json()
    assert [server["id"] for server in reply["items"]] == [sid, other["id"]]
    assert reply["items"][0]["tools"][0]["name"] == "image_generate"
    assert reply["items"][1] == {"id": other["id"], "name": "Offline", "unavailable": True}


def test_overflow_in_one_tool_schema_keeps_the_servers_other_tools(studio):
    sid = add(studio)
    calls = service(studio.gw, broken_schema=True)
    base = "/api/mcp-tools/video"
    items = studio.c.get(base).json()["items"]
    assert [item["id"] for item in items] == [sid]
    assert [tool["name"] for tool in items[0]["tools"]] == ["video_estimate"]
    assert studio.c.post(f"{base}/servers/{sid}/run", json={"tool": "video_create"}).status_code == 409
    assert calls == []


def test_video_can_use_only_a_readable_photo_from_studio_history(studio):
    from test_lock_policy import IMAGE, owner

    uid = owner(studio)
    sid = add(studio)
    calls = service(studio.gw, limits=True)
    source = media.save(uid, PNG_REAL, "image/png", "source.png")
    state.DB.execute(IMAGE, ("image", uid, "flux", "source", 1))
    state.DB.execute("UPDATE images SET media_id=? WHERE id='image'", (source,))
    payload = {"tool": "video_estimate", "files": {"artifact_id": {"media_id": source}}, "arguments": {"seconds": 7}}
    route = f"/api/mcp-tools/video/servers/{sid}/run"
    assert studio.c.post(route, json={**payload, "arguments": {"seconds": 601}}).status_code == 422
    assert calls == []
    assert studio.c.post(route, json=payload).json()["text"] == "Estimated time"
    assert calls[0][0] == "artifact_upload"
    assert calls[1] == ("video_estimate", {"artifact_id": ARTIFACT, "seconds": 7})
    calls.clear()
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"photo\"]' WHERE id=?", (uid,))
    state.DB.execute("UPDATE sessions SET seen=NULL")
    assert studio.c.post(route, json=payload).status_code == 423
    assert calls == []


def test_speech_uses_the_selected_studio_voice_sample(studio):
    from test_lock_policy import owner

    uid = owner(studio)
    sid = add(studio)
    calls = service(studio.gw, clone_models=True)
    sample = media.save(uid, WAV_FAKE, "audio/wav", "sample.wav")
    vid = "e" * 32
    state.DB.execute("INSERT INTO voices(id,user_id,name,media_id,ref_text,duration,created) VALUES (?,?,?,?,?,1,1)",
                     (vid, uid, "Voice sample", sample, "Voice reference text"))
    route = f"/api/mcp-tools/speech/servers/{sid}/run"
    payload = {"tool": "speech_synthesize", "arguments": {"text": "Read this", "model": "f5-tts"}, "voice_id": vid}
    assert studio.c.post(route, json={**payload, "arguments": {"text": "Read this", "model": "foreign"}}).status_code == 422
    assert calls == []
    assert studio.c.post(route, json=payload).json()["text"] == "Synthesized speech"
    assert calls[0] == ("artifact_upload", {"data_base64": base64.b64encode(WAV_FAKE).decode(), "mime_type": "audio/wav"})
    assert calls[1] == ("speech_synthesize", {"text": "Read this", "model": "f5-tts", "engine": "clone", "reference_artifact_id": ARTIFACT,
                                             "reference_text": "Voice reference text"})
    calls.clear()
    assert studio.c.post(route, json={**payload, "voice_id": "f" * 32}).status_code == 404
    assert calls == []


def test_catalog_keeps_photo_video_and_chat_models_separate(studio):
    add(studio)
    calls = service(studio.gw, catalogs=True)
    photo = studio.c.get("/api/mcp-tools/photo").json()["items"][0]["tools"][0]
    assert photo["models"] == ["photo-a"]
    video = studio.c.get("/api/mcp-tools/video").json()["items"][0]["tools"][0]
    assert video["schema"]["properties"]["model"]["enum"] == ["wan", "h3"]
    chat = studio.c.get("/api/mcp-tools/chat").json()["items"][0]
    assert chat["models"] == ["chat-a", "chat-b"]
    assert calls == [("services_list", {}), ("models_list", {})]


def test_mixed_gateway_models_use_the_services_declaration(studio):
    sid = add(studio)
    chat_names = ["qwen-27b", "qwen-native", "qwen-27b-fast", "qwen-native-fast"]
    groups = [
        {"gateway_path": "/v1/messages", "names": chat_names[:2]},
        {"gateway_path": "/v1/embeddings", "names": ["vectors", "vectors-version"]},
        {"gateway_path": "/v1/rerank", "names": ["ranker", "ranker-version"]},
        {"gateway_path": "/pages/ocr", "names": ["reader", "reader-version"]},
        {"gateway_path": "/speech/transcriptions", "names": ["recognizer"]},
    ]
    other_names = [name for group in groups[1:] for name in group["names"]]
    calls = service(studio.gw, catalogs=True, catalog_results={
        "models_list": {"data": [{"id": model} for model in chat_names + other_names]},
        "services_list": {"declarations": {"llm": {"object": "limits", "services": groups}}},
    })
    catalog = studio.c.get("/api/mcp-tools/chat").json()["items"][0]
    assert catalog["models"] == chat_names
    chosen = {"mcp_model_sid": sid, "model": "qwen-27b-fast"}
    created = studio.c.post("/api/chats", json=chosen)
    assert created.status_code == 201, created.text
    path = f"/api/chats/{created.json()['id']}"
    for method, url in ((studio.c.post, "/api/chats"), (studio.c.patch, path)):
        refused = method(url, json={**chosen, "model": "reader"})
        assert refused.status_code >= 400, refused.text
    assert all(name != "llm_ask" for name, _ in calls)


@pytest.mark.parametrize(("model_schema", "expected"), [
    ({"type": "string"}, ["confirmed", "default"]),
    ({"type": "string", "enum": ["allowed", "embedding"]}, ["allowed"]),
    ({"type": "string", "const": "allowed"}, ["allowed"]),
])
def test_mcp_chat_models_follow_the_tool_schema_and_catalog_metadata(studio, model_schema, expected):
    add(studio)
    service(studio.gw, catalogs=True, chat_model=model_schema, catalog_results={
        "models_list": {"data": [{"id": "confirmed", "model_info": {"mode": "chat"}},
                                 {"id": "default"}, {"id": "allowed"}, {"id": "unknown"},
                                 {"id": "embedding", "mode": "embedding"}]},
        "services_list": {"services": {"llm": {"model": "default"}}},
    })
    assert studio.c.get("/api/mcp-tools/chat").json()["items"][0]["models"] == expected


def test_recorded_jobs_and_results_are_owned_and_survive_independent_runs(studio):
    sid = add(studio)
    service(studio.gw)
    route = f"/api/mcp-tools/photo/servers/{sid}/run"
    for prompt in ("a red cup", "a blue plate"):
        reply = studio.c.post(route, json={"tool": "image_generate", "record": True,
                                           "arguments": {"prompt": prompt, "size": "832x1216", "seed": 12345}})
        assert reply.status_code == 200, reply.text
        assert reply.json()["history"]["status"] == "completed"
    items = studio.c.get("/api/mcp-tools/photo/history").json()["items"]
    assert [row["prompt"] for row in items] == ["a blue plate", "a red cup"]
    gallery = studio.c.get("/api/images").json()["items"]
    assert {photo["prompt"] for photo in gallery} == {"a blue plate", "a red cup"}
    assert all(photo["state"] == "done" and photo["media_id"] for photo in gallery)
    latest = studio.c.get("/api/images?limit=1").json()["items"][0]
    older = studio.c.get("/api/images", params={"before": latest["created"], "before_id": latest["id"]}).json()["items"]
    assert len(older) == 1 and older[0]["id"] != latest["id"]
    saved = studio.c.get("/api/images/" + latest["id"]).json()
    assert saved["media_id"] == latest["media_id"]
    assert saved["chain"][0]["prompt"] == saved["prompt"]
    assert saved["chain"][0]["seed"] == 12345 and saved["size"] == "832x1216"
    assert saved["mcp_server_id"] == sid and saved["mcp_tool"] == "image_generate"
    first = "/api/mcp-tools/photo/history/" + items[1]["id"]
    url = studio.c.get(first).json()["result"]["files"][0]["url"]
    assert studio.c.get(url).content == PNG_REAL
    assert studio.c.get("/api/my-files/servers").json() == [{"sid": 0, "name": "Студия", "local": True}]
    files = studio.c.post("/api/my-files/0/list", json={"path": ""}).json()["entries"]
    assert len(files) == 2 and all(file["section"] == "photo" for file in files)
    assert studio.c.get(files[0]["preview"]["url"]).content == PNG_REAL
    make_user(studio, "other-history")
    signin(studio, "other-history")
    other = add(studio)
    blocked = studio.c.post(f"/api/mcp-tools/photo/servers/{other}/run",
                            json={"photo_job_id": saved["mcp_job_id"], "tool": "image_generate",
                                  "arguments": {"prompt": saved["prompt"]}})
    assert blocked.status_code == 404
    assert studio.c.get(first).status_code == 404
    assert studio.c.get(url).status_code == 404
    assert studio.c.post("/api/my-files/0/list", json={"path": ""}).json()["entries"] == []


def test_background_photo_series_appears_in_gallery_and_deletes_individual_files(studio):
    sid = add(studio)
    service(studio.gw, photo_job=True)

    async def artifact(_request):
        return httpx.Response(200, content=PNG_REAL)

    studio.gw.on[PATH] = studio.gw.on["/artifacts/art_" + "3" * 32 + "/content"] = artifact
    route = f"/api/mcp-tools/photo/servers/{sid}/run"
    body = {"tool": "image_generate", "record": True, "arguments": {"prompt": "a cup", "model": "photo-b", "n": 2}}
    job = studio.c.post(route, json=body).json()["history"]
    rid = job["id"]
    pending = studio.c.get("/api/images/" + rid).json()
    assert pending["state"] == "queued" and pending["media_id"] is None
    assert studio.c.delete("/api/images/" + rid).status_code == 409
    for _ in range(2):
        row = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE id = ?", (rid,)).fetchone()
        asyncio.run(mcp_sections.refresh_history(row))
    photos = studio.c.get("/api/images?model=photo-b").json()["items"]
    assert len(photos) == 2 and all(p["mcp_job_id"] == rid and p["state"] == "done" for p in photos)
    polled = studio.c.get("/api/images/" + rid)
    assert polled.status_code == 200 and polled.json()["id"] in {p["id"] for p in photos}
    first, second = ["/api/images/" + p["id"] for p in photos]
    share = studio.c.post(first + "/share", json={"pin": "0123"})
    assert share.status_code == 201, share.text
    credentials = {"token": urlsplit(share.json()["url"]).fragment, "pin": "0123"}
    snapshot = studio.c.post("/share/unlock", json=credentials)
    assert snapshot.json()["item"]["file"]["file"] == photos[0]["media_id"]
    state.DB.execute("UPDATE mcp_servers SET protected = 1 WHERE id = ?", (sid,))
    assert studio.c.post("/share/unlock", json=credentials).status_code == 404
    assert studio.c.post(second + "/share", json={"pin": "0123"}).json()["detail"]["code"] == "protected"
    assert studio.c.delete(first).status_code == 204
    assert not state.DB.execute("SELECT 1 FROM chat_shares WHERE item_id = ?", (photos[0]["id"],)).fetchone()
    assert studio.c.get(first).status_code == 404
    assert studio.c.get(second).json()["state"] == "done"
    history = studio.c.get("/api/mcp-tools/photo/history/" + rid).json()
    remaining = "/api/media/" + photos[1]["media_id"]
    assert [f["url"] for f in history["result"]["files"]] == [remaining]
    assert studio.c.get(remaining).content == PNG_REAL


def test_durable_video_job_copies_artifact_in_background(studio):
    sid = add(studio)
    calls = service(studio.gw, catalogs=True)

    async def artifact(_request):
        return httpx.Response(200, content=MP4)

    studio.gw.on[PATH] = artifact
    body = {"tool": "video_create", "record": True, "arguments": {"model": "h3", "prompt": "turn slowly", "seconds": 5, "seed": 17},
            "files": {"artifact_id": {"data": base64.b64encode(PNG_REAL).decode(), "mime_type": "image/png"}}}
    response = studio.c.post(f"/api/mcp-tools/video/servers/{sid}/run", json=body)
    assert response.status_code == 200, response.text
    job = response.json()["history"]
    assert job["active"] and job["model"] == "h3"
    queued = studio.c.get("/api/videos?model=h3").json()["items"]
    assert [(clip["id"], clip["state"]) for clip in queued] == [(job["id"], "queued")]
    assert queued[0]["source_media_id"] and not queued[0]["media_id"]
    other = studio.c.get("/api/mcp-tools/video/history?without_clips=true").json()
    assert other["items"] == [] and other["active"]["id"] == job["id"]
    assert studio.c.delete("/api/videos/" + job["id"]).status_code == 409
    for expected in ("in_progress", "done"):
        row = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE id = ?", (job["id"],)).fetchone()
        asyncio.run(mcp_sections.refresh_history(row))
        viewed = studio.c.get("/api/videos/" + job["id"]).json()
        assert viewed["state"] == expected
    studio.gw.on.pop(PATH)
    result = studio.c.get("/api/mcp-tools/video/history/" + job["id"]).json()
    assert result["status"] == "completed" and result["active"] is False
    assert studio.c.get("/api/mcp-tools/video/history?without_clips=true").json() == {
        "items": [], "next": None, "active": None}
    assert studio.c.get(result["result"]["files"][0]["url"]).content == MP4
    assert viewed["prompt"] == "turn slowly" and viewed["seconds"] == 5
    assert viewed["chain"][-1]["prompt"] == "turn slowly"
    assert studio.c.get("/api/media/" + viewed["source_media_id"]).status_code == 200
    assert [entry["name"] for entry in studio.c.post("/api/my-files/0/list", json={"path": ""}).json()["entries"]] == ["video.mp4"]

    path = "/api/videos/" + viewed["id"]
    new = studio.c.post(path + "/repeat", json={"count": 2})
    assert new.status_code == 201, new.text
    assert len(new.json()["items"]) == 2
    repeated = [params for name, params in calls if name == "video_create"][1:]
    assert repeated == [{"artifact_id": ARTIFACT, "prompt": "turn slowly", "model": "h3", "seconds": 5}] * 2
    assert all(clip["source_media_id"] == viewed["source_media_id"] for clip in new.json()["items"])

    share = studio.c.post(path + "/share", json={"pin": "0123"})
    assert share.status_code == 201, share.text
    credentials = {"token": urlsplit(share.json()["url"]).fragment, "pin": "0123"}
    opened = studio.c.post("/share/unlock", json=credentials)
    assert opened.json()["item"]["prompt"] == "turn slowly"
    assert studio.c.delete(path).status_code == 204
    assert studio.c.post("/share/unlock", json=credentials).status_code == 404
    assert studio.c.get("/api/media/" + viewed["media_id"]).status_code == 404
    assert studio.c.get("/api/media/" + viewed["source_media_id"]).status_code == 200  # retained by the repeats


def test_legacy_mcp_video_results_are_listed_and_owner_scoped(studio):
    uid = make_user(studio, "video-owner")
    signin(studio, "video-owner")
    sid = add(studio)
    stamp = mcp_sections.mcp_sources.stamp(state.DB.execute("SELECT secret_enc FROM mcp_servers WHERE id = ?", (sid,)).fetchone()[0])
    calls = service(studio.gw, catalogs=True)
    preview = "640×640, 5 с (1 отрезк.), seed 424648865. Запрос для Wan: expanded prompt."
    files = [media.save(uid, MP4, "video/mp4", f"clip-{number}.mp4") for number in range(2)]
    legacy_id = "e" * 32
    result = {"text": preview, "files": [{"url": f"/api/media/{mid}", "mime": "video/mp4"} for mid in files]}
    state.DB.execute("INSERT INTO mcp_section_jobs(id, user_id, section, server_id, server, tool, model, prompt, "
                     "created, stamp, status, result) VALUES (?, ?, 'video', ?, 'Spark', 'video_create', '', ?, ?, ?, 'completed', ?)",
                     (legacy_id, uid, sid, "original prompt", studio.clock[0], stamp, json.dumps(result)))
    state.DB.executemany("INSERT INTO mcp_section_files(job_id, user_id, media_id) VALUES (?, ?, ?)",
                         [(legacy_id, uid, mid) for mid in files])
    opts = {"model": "wan", "limit": 1}
    first, = studio.c.get("/api/videos", params=opts).json()["items"]
    second, = studio.c.get("/api/videos", params=opts | {"before": first["created"], "before_id": first["id"]}).json()["items"]
    assert {first["id"], second["id"]} == set(files)
    assert studio.c.get("/api/videos", params=opts | {"after": second["created"], "after_id": second["id"]}).json()["items"] == [first]
    assert (first["prompt"], first["seconds"], first["seed"], first["size"]) == ("original prompt", 5, 424648865, "640×640")
    path = "/api/videos/" + first["id"]
    step = studio.c.get(path).json()["chain"][-1]
    assert step["media_id"] == first["media_id"] and not step.get("gone")
    make_user(studio, "other-video")
    signin(studio, "other-video")
    assert studio.c.get(path).status_code == studio.c.delete(path).status_code == 404
    assert studio.c.get("/api/videos").json()["items"] == []
    signin(studio, "video-owner")
    assert studio.c.post(path + "/repeat", json={}).status_code == 409
    assert studio.c.delete(path).status_code == 204
    assert [clip["id"] for clip in studio.c.get("/api/videos").json()["items"]] == [second["id"]]
    assert studio.c.get("/api/media/" + second["id"], headers={"Range": "bytes=0-9"}).content == MP4[:10]
    source = {"data": base64.b64encode(PNG_REAL).decode(), "mime_type": "image/png"}
    repeat = studio.c.post("/api/videos/" + second["id"] + "/repeat", json={"count": 2, "source": source})
    assert repeat.status_code == 201, repeat.text
    assert len(repeat.json()["items"]) == 2
    assert len({clip["source_media_id"] for clip in repeat.json()["items"]}) == 1
    assert [args for name, args in calls if name == "video_create"] == [
        {"artifact_id": ARTIFACT, "prompt": "original prompt", "seconds": 5}] * 2


def test_chat_model_uses_mcp_and_saves_normal_conversation(studio):
    sid = add(studio)
    calls = service(studio.gw, catalogs=True)
    created = studio.c.post("/api/chats", json={"mcp_model_sid": sid, "model": "chat-b"})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["mcp_model_sid"] == sid and row["connection_id"] is None
    path = "/api/chats/" + row["id"]
    sent = studio.c.post(path + "/turns", json={"text": "hello", "request_id": "mcp-chat-first-message"})
    assert sent.status_code == 200 and "Reply from MCP" in sent.text, sent.text
    assert any(name == "llm_ask" and args["model"] == "chat-b" for name, args in calls)
    assert [m["content"] for m in studio.c.get(path).json()["messages"]] == ["hello", "Reply from MCP"]
    another = studio.c.post(path + "/turns", json={"text": "follow-up"})
    assert another.status_code == 200
    sent = [args["messages"] for name, args in calls if name == "llm_ask"][-1]
    assert [m["content"] for m in sent[-3:]] == ["hello", "Reply from MCP", "follow-up"]
