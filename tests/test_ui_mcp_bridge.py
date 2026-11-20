"""Инструменты разделов внешнего MCP со схемами обёртки aitools: формы «Фото», «Видео» и «Сканы» их показывают и запускают."""
import json

import pytest
import httpx

from fakes import PNG_REAL
from test_mcp import TOKEN, URL
from test_ui_browser import browser, live, signin, READY, NO_OVERFLOW, problems, put_files, set_value  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

JOBS = {"image_generate": "job_" + "1" * 32, "video_create": "job_" + "2" * 32}
FILES = {"art_" + "a" * 32: ("image/png", PNG_REAL), "art_" + "b" * 32: ("video/mp4", b"\x00\x00\x00\x18ftypmp42" + bytes(64))}
UPLOADED = "art_" + "c" * 32
# Схемы — как отдаёт обёртка aitools: свои инструменты разделов и сквозные инструменты aitools с полями по пути.
TOOLS = {
    "image_generate": {"properties": {"prompt": {"type": "string"},
                                      "size": {"type": "string", "enum": ["832x1216", "1024x1024", "1216x832", "1344x768"],
                                               "default": "1024x1024"},
                                      "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                                      "seed": {"type": "integer", "default": -1}}, "required": ["prompt"]},
    "video_create": {"properties": {"artifact_id": {"type": "string"}, "prompt": {"type": "string"},
                                    "seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 3},
                                    "seed": {"type": "integer", "default": -1}}, "required": ["artifact_id", "prompt"]},
    "ocr_image": {"properties": {"artifact_id": {"type": "string"}, "pages": {"type": "string", "default": "all"}},
                  "required": ["artifact_id"]},
    "generate_image": {"properties": {"request": {"type": "string"}, "orientation": {"type": "string"}}, "required": ["request"]},
    "read_document": {"properties": {"path": {"type": "string"}}, "required": ["path"]},
    "artifact_upload": {"properties": {"data_base64": {"type": "string"}, "mime_type": {"type": "string"}},
                        "required": ["data_base64", "mime_type"]},
    "job_status": {"properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    "job_result": {"properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
}


def bridge(gateway):
    calls, polls = [], {}

    def artifact(aid, name):
        mime = FILES[aid][0]
        return {"id": aid, "mime_type": mime, "name": name, "download_path": f"/artifacts/{aid}/content"}

    results = {JOBS["image_generate"]: artifact("art_" + "a" * 32, "cat.png"),
               JOBS["video_create"]: artifact("art_" + "b" * 32, "clip.mp4")}

    async def rpc(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", **schema}} for name, schema in TOOLS.items()]}
        else:
            name, args = (payload["params"][key] for key in ("name", "arguments"))
            calls.append((name, args))
            if name in JOBS:
                data = {"id": JOBS[name], "status": "queued"}
            elif name == "artifact_upload":
                data = {"id": UPLOADED, "mime_type": args["mime_type"]}
            elif name == "ocr_image":
                data = {"text": "Recognized page"}
            elif name == "job_status":
                polls[args["job_id"]] = polls.get(args["job_id"], 0) + 1
                data = {"id": args["job_id"], "status": "running" if polls[args["job_id"]] == 1 else "completed"}
            elif name == "job_result":
                data = {"result": {"text": "", "artifacts": [results[args["job_id"]]]}}
            else:
                raise AssertionError(name)
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def download(aid):
        async def handler(request):
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            mime, data = FILES[aid]
            return httpx.Response(200, headers={"content-type": mime, "content-length": str(len(data))}, content=data)
        return handler

    gateway.on["/mcp"] = rpc
    for aid in FILES:
        gateway.on[f"/artifacts/{aid}/content"] = download(aid)
    return calls


@pytest.mark.parametrize("width", [320, 1280])
def test_bridge_section_tools_run_from_photo_video_and_scan_forms(browser, live, width):
    login = f"bridge-forms-{width}"
    live.add_user(login)
    calls = bridge(live.gw)
    options = ("[...document.querySelectorAll('#section-mcp-tool option')].map(o => o.value.split(':').pop())"
               ".filter(Boolean).join(',')")
    choose = lambda tool: ("eval", "tool", "(() => { const s = document.querySelector('#section-mcp-tool');"
                           f"s.value = [...s.options].find(o => o.value.endsWith(':{tool}')).value;"
                           "s.dispatchEvent(new Event('change', { bubbles: true })); })()")
    ready = lambda tool: f"""document.querySelector('#section-mcp-tool option[value$=":{tool}"]')"""
    file_shown = lambda section: f"document.querySelector('#section-mcp-result [src*=\"/api/mcp-tools/{section}/files/\"], " \
                                 f"#section-mcp-result [href*=\"/api/mcp-tools/{section}/files/\"]')"
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "settings", "location.hash='#/settings/chat'"), ("wait", "document.querySelector('#mcp-add')"),
        ("click", "#mcp-add"), ("type", "#mcp-name", "Spark"), ("type", "#mcp-url", URL),
        ("type", "#mcp-token", TOKEN), ("click", "#mcp-save"),
        ("wait", "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")),
        ("eval", "photoTools", options),
        ("type", "#photo-prompt", "Рыжий кот"), choose("image_generate"),
        ("click", "#chip-size"), ("click", "#chip-size-menu [data-value=square]"), ("click", "#photo-go"),
        ("wait", file_shown("photo")), ("eval", "photoFits", NO_OVERFLOW),
        ("eval", "video", "location.hash='#/video'"), ("wait", ready("video_create")),
        ("eval", "videoTools", options), choose("video_create"),
        put_files("#video-file", [("source.png", PNG_REAL)]),
        ("type", "#video-prompt", "Кот моргает"), set_value("#video-seconds", "5"), ("click", "#video-go"),
        ("wait", file_shown("video")), ("eval", "videoFits", NO_OVERFLOW),
        ("eval", "ocr", "location.hash='#/ocr'"), ("wait", "document.querySelector('#ocr-upload')?.offsetWidth > 0"),
        ("eval", "ocrTools", options),
        put_files("#ocr-add input", [("page.png", PNG_REAL)]),
        ("wait", "document.querySelector('#section-mcp-result pre')?.textContent === 'Recognized page'"),
        ("eval", "ocrFits", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    # Сквозные инструменты aitools требуют поля по пути — страница их в разделах не предлагает.
    assert values["photoTools"] == "image_generate" and values["videoTools"] == "video_create"
    assert values["ocrTools"] == "ocr_image"
    assert values["photoFits"] and values["videoFits"] and values["ocrFits"]
    sent = [(name, args) for name, args in calls if name not in ("job_status", "job_result")]
    assert sent[0] == ("image_generate", {"prompt": "Рыжий кот", "size": "1024x1024", "n": 1})
    assert sent[1][0] == "artifact_upload" and sent[1][1]["mime_type"] == "image/png"
    assert sent[2] == ("video_create", {"artifact_id": UPLOADED, "prompt": "Кот моргает", "seconds": 5})
    assert sent[3][0] == "artifact_upload" and sent[4] == ("ocr_image", {"artifact_id": UPLOADED})
    assert not problems(result), result.get("log")
