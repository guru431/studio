"""Формы «Видео» и «Фото» берут пределы из схемы инструмента MCP; бессмысленная схема оставляет свои пределы."""
import json

import pytest
import httpx

from fakes import PNG_REAL
from test_mcp import TOKEN, URL
from test_mcp_sections import LIMIT_CASES
from test_ui_browser import API, browser, live, signin, READY, NO_OVERFLOW, problems, put_files, set_value  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

TEXT = {"type": "string"}
SERVICE = {"artifact_upload": {"properties": {"data_base64": TEXT, "mime_type": TEXT}, "required": ["data_base64", "mime_type"]}}
# Как у обёртки aitools: ролик до 600 с, размеры фото списком, серия до 2.
GOOD = {
    "image_generate": {"properties": {"prompt": TEXT, "size": {"type": "string", "enum": ["832x1216", "1024x1024", "1216x832",
                                                                                         "1344x768"], "default": "1024x1024"},
                                      "n": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1}}, "required": ["prompt"]},
    "video_create": {"properties": {"artifact_id": TEXT, "prompt": TEXT,
                                    "seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 3}},
                     "required": ["artifact_id", "prompt"]},
    "video_estimate": {"properties": {"artifact_id": TEXT, "prompt": TEXT,
                                      "seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10}},
                       "required": ["artifact_id"]},
    **SERVICE,
}
# min > max, enum из 100 значений, default вне пределов.
BAD = {
    "image_generate": {"properties": {"prompt": TEXT, "size": {"type": "string", "enum": ["832x1216", *(f"{n}x{n}" for n in range(99))],
                                                               "default": "nope"},
                                      "n": {"type": "integer", "minimum": 5, "maximum": 1}}, "required": ["prompt"]},
    "video_create": {"properties": {"artifact_id": TEXT, "prompt": TEXT,
                                    "seconds": {"type": "number", "minimum": 50, "maximum": 10, "default": 999}},
                     "required": ["artifact_id", "prompt"]},
    **SERVICE,
}
UPLOADED = "art_" + "c" * 32


def serve(gateway, tools):
    calls = []

    async def rpc(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", **schema}} for name, schema in tools.items()]}
        else:
            name, args = (payload["params"][key] for key in ("name", "arguments"))
            calls.append((name, args))
            data = {"id": UPLOADED} if name == "artifact_upload" else {"text": "ok"}
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    gateway.on["/mcp"] = rpc
    return calls


def choose(tool):
    return ("eval", "tool", "(() => { const s = document.querySelector('#section-mcp-tool');"
                            f"s.value = [...s.options].find(o => o.value.endsWith(':{tool}')).value;"
                            "s.dispatchEvent(new Event('change', { bubbles: true })); })()")


def ready(tool):
    return f"""document.querySelector('#section-mcp-tool option[value$=":{tool}"]')"""


def start(login):
    return [*signin(login), ("wait", READY),
            ("eval", "added", f"(async () => (await {API}.api('POST', '/api/mcp', "
                              f"{{ name: 'Spark', url: '{URL}', token: '{TOKEN}' }})).id)()")]


SECONDS = ("[document.querySelector('#video-seconds').maxLength, document.querySelector('#video-seconds').getAttribute('aria-label'),"
           " document.querySelector('#chip-seconds').title, document.querySelector('#video-seconds').value]")
SIZES = "[...document.querySelectorAll('#chip-size-menu [data-value]')].map(b => b.dataset.value)"
COUNTS = "[...document.querySelectorAll('#photo-series-panel [data-count]')].filter(b => !b.hidden).length"
DONE = "document.querySelector('#section-mcp-result pre')?.textContent === 'ok'"
ERROR = "document.querySelector('#video-error').textContent"


@pytest.mark.parametrize("width", [320, 1280])
def test_section_forms_take_limits_from_the_tool_schema(browser, live, width):
    login = f"limits-good-{width}"
    live.add_user(login)
    calls = serve(live.gw, GOOD)
    result = browser.drive(live.url + "/", [*start(login),
        ("eval", "video", "location.hash='#/video'"), ("wait", ready("video_create")), choose("video_create"),
        ("eval", "videoLimits", SECONDS),
        put_files("#video-file", [("source.png", PNG_REAL)]), ("type", "#video-prompt", "Кот моргает"),
        set_value("#video-seconds", "700"), ("click", "#video-go"), ("wait", ERROR), ("eval", "tooLong", ERROR),
        set_value("#video-seconds", "120"), ("click", "#video-go"), ("wait", DONE),
        choose("video_estimate"), ("eval", "estimateLimits", SECONDS), ("eval", "videoFits", NO_OVERFLOW),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")),
        ("type", "#photo-prompt", "Рыжий кот"), choose("image_generate"),
        ("eval", "sizes", SIZES), ("eval", "counts", COUNTS),
        ("click", "#chip-size"), ("click", '#chip-size-menu [data-value="1344x768"]'),
        ("eval", "sizeChip", "document.querySelector('#chip-size').textContent.trim()"),
        ("click", "#photo-go"), ("wait", DONE), ("eval", "photoFits", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["videoLimits"] == [3, "Длительность: 1–600 с", "Длительность: 1–600 с", "5"]
    assert values["tooLong"] == "Длительность — целое число секунд от 1 до 600"
    # 120 с вне пределов оценки (1–60) — умолчание её схемы
    assert values["estimateLimits"] == [2, "Длительность: 1–60 с", "Длительность: 1–60 с", "10"]
    assert values["sizes"] == ["portrait", "square", "landscape", "1344x768"] and values["sizeChip"] == "1344x768"
    assert values["counts"] == 2
    assert values["videoFits"] and values["photoFits"]
    sent = [(name, args) for name, args in calls if name != "artifact_upload"]
    assert sent == [("video_create", {"artifact_id": UPLOADED, "prompt": "Кот моргает", "seconds": 120}),
                    ("image_generate", {"prompt": "Рыжий кот", "size": "1344x768", "n": 1})]
    assert not problems(result), result.get("log")


def test_page_limits_match_what_the_server_accepts(browser, live):
    """Та же таблица, что у сервера (`LIMIT_CASES`): форма пропускает ровно то, что примет `run`."""
    cases = json.dumps([[case, prop, probes] for case, prop, probes, _, _ in LIMIT_CASES])   # inf → Infinity, литерал JS
    program = f"""(async () => {{
      const {{ schemaLimits }} = await import('/static/js/mcp-sections.js');
      return Object.fromEntries({cases}.map(([name, prop, probes]) => {{
        const l = schemaLimits(prop);
        const accepts = (v) => !l || ((l.min === null && l.max === null || typeof v === 'number')
          && (l.min === null || v >= l.min) && (l.max === null || v <= l.max) && (!l.enum || l.enum.includes(v)));
        return [name, [probes.map(accepts), l ? l.default : null]];
      }}));
    }})()"""
    result = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("eval", "page", program)], width=1280)
    assert not result.get("failed"), result
    page = result["values"]["page"]
    assert page == {case: [accepted, default] for case, _, _, accepted, default in LIMIT_CASES}
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_bad_schema_limits_ignored(browser, live, width):
    login = f"limits-bad-{width}"
    live.add_user(login)
    calls = serve(live.gw, BAD)
    result = browser.drive(live.url + "/", [*start(login),
        ("eval", "video", "location.hash='#/video'"), ("wait", ready("video_create")), choose("video_create"),
        ("eval", "videoLimits", SECONDS),
        put_files("#video-file", [("source.png", PNG_REAL)]), ("type", "#video-prompt", "Кот моргает"),
        set_value("#video-seconds", "45"), ("click", "#video-go"), ("wait", ERROR), ("eval", "tooLong", ERROR),
        set_value("#video-seconds", "7"), ("click", "#video-go"), ("wait", DONE), ("eval", "videoFits", NO_OVERFLOW),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")),
        ("type", "#photo-prompt", "Рыжий кот"), choose("image_generate"),
        ("eval", "sizes", SIZES), ("eval", "counts", COUNTS),
        ("click", "#photo-go"), ("wait", DONE), ("eval", "photoFits", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["videoLimits"] == [2, "Длительность: 1–30 с", "Длительность: 1–30 с", "5"]
    assert values["tooLong"] == "Длительность — целое число секунд от 1 до 30"
    assert values["sizes"] == ["portrait", "square", "landscape"] and values["counts"] == 4
    assert values["videoFits"] and values["photoFits"]
    sent = [(name, args) for name, args in calls if name != "artifact_upload"]
    # Сервер бессмысленные пределы тоже не применяет: решает сам инструмент.
    assert sent == [("video_create", {"artifact_id": UPLOADED, "prompt": "Кот моргает", "seconds": 7}),
                    ("image_generate", {"prompt": "Рыжий кот", "size": "832x1216", "n": 1})]
    assert not problems(result), result.get("log")



def test_mcp_photo_model_and_history_survive_leaving_section(browser, live):
    login = "mcp-photo-history"
    live.add_user(login)
    photo = GOOD["image_generate"]
    tool = {**photo, "properties": {**photo["properties"], "model": {"type": "string", "enum": ["photo-a", "photo-b"]}}}
    calls = serve(live.gw, {**GOOD, "image_generate": tool})
    select = set_value("#mcp-field-model", "photo-b")
    count = "document.querySelectorAll('.mcp-history-entry').length"
    r = browser.drive(live.url + "/", [*start(login),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")), choose("image_generate"), select,
        set_value("#photo-prompt", "First result"), ("click", "#photo-go"), ("wait", count + " === 1"),
        set_value("#photo-prompt", "Second result"), ("click", "#photo-go"), ("wait", count + " === 2"),
        ("eval", "video", "location.hash='#/video'"), ("wait", "document.querySelector('#video-go')"),
        ("eval", "back", "location.hash='#/photo'"), ("wait", count + " === 2"),
        ("eval", "entries", "[...document.querySelectorAll('.mcp-history-prompt')].map(p => p.textContent)")], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["entries"] == ["Second result", "First result"], r
    assert [args["model"] for name, args in calls if name == "image_generate"] == ["photo-b", "photo-b"]
    assert not problems(r), problems(r)
