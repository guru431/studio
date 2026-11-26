"""Инструменты разделов внешнего MCP со схемами обёртки aitools: формы «Фото», «Видео» и «Сканы» их показывают и запускают."""
import base64
import io
import json

import pytest
import httpx
from PIL import Image

from fakes import PNG_REAL
from test_mcp import TOKEN, URL
from test_ui_browser import browser, live, signin, READY, NO_OVERFLOW, problems, put_files, set_value, viewer_details  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

JOBS = {"image_generate": "job_" + "1" * 32, "video_create": "job_" + "2" * 32, "upscale_image": "job_" + "3" * 32}
FILES = {"art_" + "a" * 32: ("image/png", PNG_REAL), "art_" + "b" * 32: ("video/mp4", b"\x00\x00\x00\x18ftypmp42" + bytes(64))}
UPLOADED = "art_" + "c" * 32
# Схемы — как отдаёт обёртка aitools: свои инструменты разделов и сквозные инструменты aitools с полями по пути.
TOOLS = {
    "image_generate": {"properties": {"prompt": {"type": "string"},
                                      "size": {"type": "string", "enum": ["832x1216", "1024x1024", "1216x832", "1344x768"],
                                               "default": "1024x1024"},
                                      "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                                      "seed": {"type": "integer", "default": -1}}, "required": ["prompt"]},
    "upscale_image": {"properties": {"artifact_id": {"type": "string"},
                                    "mode": {"type": "string", "enum": ["restore", "fast"]},
                                    "scale": {"type": "number", "minimum": 1, "maximum": 4}}, "required": ["artifact_id"]},
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
               JOBS["upscale_image"]: artifact("art_" + "a" * 32, "upscaled.png"),
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
                if args["job_id"] == JOBS["video_create"]:
                    data["result"]["text"] = "640×640, 5 с (1 отрезк.), seed 424648865. Запрос для Wan: expanded prompt."
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
    clip = "#video-feed .video-card[data-state=done]"
    two_clips = f"document.querySelectorAll('{clip}').length === 2"
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "settings", "location.hash='#/settings/chat'"), ("wait", "document.querySelector('#mcp-add')"),
        ("click", "#mcp-add"), ("type", "#mcp-name", "Spark"), ("type", "#mcp-url", URL),
        ("type", "#mcp-token", TOKEN), ("click", "#mcp-save"),
        ("wait", "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")),
        ("eval", "photoTools", options),
        ("type", "#photo-prompt", "Рыжий кот"), choose("image_generate"),
        ("click", "#chip-size"), ("click", "#chip-size-menu [data-value=square]"), ("click", "#photo-go"),
        ("wait", "document.querySelector('#photo-feed .photo-card[data-state=done] img')"), ("eval", "photoFits", NO_OVERFLOW),
        ("eval", "video", "location.hash='#/video'"), ("wait", ready("video_create")),
        ("eval", "videoTools", options), choose("video_create"),
        put_files("#video-file", [("source.png", PNG_REAL)]),
        ("type", "#video-prompt", "Кот моргает"), set_value("#video-seconds", "5"), ("click", "#video-go"),
        ("wait", f"""document.querySelector('{clip} video[src^="/api/media/"]')"""),
        ("eval", "inlineVideo", "!!document.querySelector('#section-mcp-result video')"),
        ("click", "#video-view"), ("click", "#video-view-table"),
        ("eval", "videoRow", f"document.querySelector('{clip}').textContent"),
        ("click", f"{clip} .video-open"),
        ("wait", "document.querySelector('#video-viewer .chain-prompt')?.textContent.includes('Кот моргает')"),
        ("eval", "viewerFile", "document.querySelector('#video-viewer video').getAttribute('src')"),
        *viewer_details(width, "#video-viewer"),
        ("click", "#video-viewer .act-to-field"),
        ("wait", "!document.querySelector('#video-viewer') && document.querySelector('#video-source img')?.naturalWidth > 0"),
        ("eval", "restored", "[document.querySelector('#video-prompt').value, document.querySelector('#video-seconds').value]"),
        ("click", f"{clip} .card-repeat"), ("wait", two_clips),
        ("eval", "away", "location.hash='#/photo'"), ("wait", "document.querySelector('#photo-feed')"),
        ("eval", "back", "location.hash='#/video'"), ("wait", two_clips),
        ("eval", "videoFits", NO_OVERFLOW),
        ("eval", "ocr", "location.hash='#/ocr'"), ("wait", "document.querySelector('#ocr-upload')?.offsetWidth > 0"),
        ("eval", "ocrTools", options),
        put_files("#ocr-add input", [("page.png", PNG_REAL)]),
        ("wait", "document.querySelector('#section-mcp-result pre')?.textContent === 'Recognized page'"),
        ("eval", "ocrFits", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    # Сквозные инструменты aitools требуют поля по пути — страница их в разделах не предлагает.
    assert values["photoTools"] == "image_generate,upscale_image" and values["videoTools"] == "video_create"
    assert values["ocrTools"] == "ocr_image"
    assert values["photoFits"] and values["videoFits"] and values["ocrFits"]
    assert not values["inlineVideo"] and values["viewerFile"].startswith("/api/media/")
    assert "Кот моргает" in values["videoRow"] and "seed 424648865" in values["videoRow"]
    assert values["restored"] == ["Кот моргает", "5"]
    sent = [(name, args) for name, args in calls if name not in ("job_status", "job_result")]
    assert sent[0] == ("image_generate", {"prompt": "Рыжий кот", "size": "1024x1024", "n": 1})
    assert sent[1][0] == "artifact_upload" and sent[1][1]["mime_type"] == "image/png"
    assert sent[2] == ("video_create", {"artifact_id": UPLOADED, "prompt": "Кот моргает", "seconds": 5})
    assert sent[3][0] == "artifact_upload" and sent[4] == sent[2]
    assert sent[5][0] == "artifact_upload" and sent[6] == ("ocr_image", {"artifact_id": UPLOADED})
    assert not problems(result), result.get("log")


def test_photo_upscaler_appears_in_mcp_menu_and_saves_the_result(browser, live):
    login = "mcp-upscale"
    live.add_user(login)
    calls = bridge(live.gw)
    original = io.BytesIO()
    Image.new("RGB", (2300, 1200), "blue").save(original, "PNG")
    button = "#section-mcp-tool-menu [role=menuitemradio]"
    choices = f"[...document.querySelectorAll('{button}')]"
    choose = lambda label: f"{choices}.find(e => e.querySelector('.pick-option-text').textContent === {label!r}).click()"
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "settings", "location.hash='#/settings/chat'"), ("wait", "document.querySelector('#mcp-add')"),
        ("click", "#mcp-add"), ("type", "#mcp-name", "srvllm"), ("type", "#mcp-url", URL),
        ("type", "#mcp-token", TOKEN), ("click", "#mcp-save"),
        ("wait", "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ("eval", "photo", "location.hash='#/photo'"),
        ("wait", "document.querySelector('#section-mcp-tool option[value$=\":upscale_image\"]')"),
        ("click", "#section-mcp-tool-pick"),
        ("eval", "menu", f"{choices}.map(e => e.querySelector('.pick-option-text').textContent)"),
        ("eval", "chooseUpscale", choose("MCP · srvllm · Улучшить фото")),
        ("wait", "document.querySelector('#photo-prompt').closest('.field-box').hidden"),
        ("eval", "uploadVisible", "document.querySelector('#chip-photo').getBoundingClientRect().width > 0"),
        put_files("#photo-file", [("source.png", original.getvalue())]),
        ("wait", "!document.querySelector('#photo-source').hidden && !document.querySelector('#photo-go').disabled"),
        set_value("#mcp-field-mode", "restore"), set_value("#mcp-field-scale", "1"), ("click", "#photo-go"),
        ("wait", "document.querySelector('#photo-feed .photo-card[data-state=done] img') && "
                 "document.querySelector('#section-mcp-tool-pick:not([disabled])')"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#section-mcp-tool-pick"), ("eval", "chooseGenerate", choose("MCP · srvllm · Создать фото")),
        ("wait", "!document.querySelector('#photo-prompt').closest('.field-box').hidden && "
                 "document.querySelector('.field-tools #chip-photo')")], width=390, mobile=True)
    assert not result.get("failed"), result
    assert "MCP · srvllm · Улучшить фото" in result["values"]["menu"]
    assert result["values"]["uploadVisible"] and result["values"]["fits"]
    assert [args for name, args in calls if name == "upscale_image"] == [{"artifact_id": UPLOADED, "mode": "restore", "scale": 1}]
    payload = next(args["data_base64"] for name, args in calls if name == "artifact_upload")
    with Image.open(io.BytesIO(base64.b64decode(payload))) as source:
        assert source.size == (2300, 1200)
    assert not problems(result), result.get("log")
