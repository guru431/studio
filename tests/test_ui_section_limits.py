"""Формы «Видео» и «Фото» берут пределы из схемы инструмента MCP; бессмысленная схема оставляет свои пределы."""
import base64
import json

import pytest
import httpx

from fakes import PNG_REAL
from test_mcp import TOKEN, URL
from test_mcp_sections import LIMIT_CASES
from test_ui_browser import API, browser, live, signin, READY, NO_OVERFLOW, NEWEST_DONE, problems, put_files, set_value  # noqa: F401

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


def serve(gateway, tools, *, photo_result=False):
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
            if name == "image_generate" and photo_result:
                data["text"] = "832×1216, 1 шт., seed 12345. Запрос для модели: draft"
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
            if name == "image_generate" and photo_result:
                result["content"].append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG_REAL).decode()})
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


def paste_image(selector):
    return ("eval", "paste", f"""(() => {{ const data = new DataTransfer();
      const bytes = Uint8Array.from(atob({base64.b64encode(PNG_REAL).decode()!r}), c => c.charCodeAt(0));
      data.items.add(new File([bytes], 'pasted.png', {{ type: 'image/png' }}));
      data.setData('text/plain', 'https://example.com/pasted.png');
      return !document.querySelector({selector!r}).dispatchEvent(new ClipboardEvent('paste',
        {{ clipboardData: data, bubbles: true, cancelable: true }})); }})()""")


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
        ("type", "#video-prompt", "Кот моргает"), paste_image("#video-prompt"),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth > 0"),
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
    assert values["paste"]
    assert base64.b64decode(calls[0][1]["data_base64"]) == PNG_REAL
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
    tool = {**photo, "properties": {**photo["properties"], "model": {"type": "string", "enum": ["photo-a", "photo-b"]},
                                     "seed": {"type": "integer", "minimum": -1, "maximum": 2**32 - 1}}}
    calls = serve(live.gw, {**GOOD, "image_generate": tool}, photo_result=True)
    select = set_value("#mcp-field-model", "photo-b")
    count = "document.querySelectorAll('#photo-feed .photo-card[data-state=done] img').length"
    r = browser.drive(live.url + "/", [*start(login),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")), choose("image_generate"), select,
        set_value("#photo-prompt", "First result"), ("click", "#photo-go"), ("wait", count + " === 1"),
        set_value("#photo-prompt", "Second result"), ("click", "#photo-go"), ("wait", count + " === 2"),
        ("eval", "video", "location.hash='#/video'"), ("wait", "document.querySelector('#video-go')"),
        ("eval", "back", "location.hash='#/photo'"), ("wait", count + " === 2"),
        ("eval", "defaultView", "document.querySelector('#photo-feed').dataset.view"),
        ("eval", "entries", f"(async () => (await {API}.api('GET','/api/images')).items.map(p=>p.prompt))()"),
        ("click", "#photo-feed .photo-card .card-open"),
        ("wait", "document.querySelector('#viewer img')?.naturalWidth > 0"),
        ("eval", "shownPrompt", "document.querySelector('#viewer .chain-prompt')?.textContent || null"),
        ("eval", "meta", "document.querySelector('#viewer .chain-meta').textContent"),
        ("click", "#viewer .act-to-field"),
        ("wait", "document.querySelector('#photo-prompt').value === 'Second result'"),
        ("eval", "restoredModel", "document.querySelector('#mcp-field-model').value"),
        ("click", "#photo-feed .photo-card .card-open"), ("click", "#viewer .act-edit"),
        ("wait", "document.querySelector('#photo-viewer-error').textContent.includes('MCP')"),
        ("click", "#viewer .act-variants"),
        set_value("#photo-variants-count", "2"), ("click", "#photo-variants-go"),
        ("wait", count + " === 3"),
        ("click", "#photo-feed .photo-card .card-open"),
        ("click", "#viewer .act-delete"), ("click", "#photo-delete-confirm"), ("wait", count + " === 2"),
        ("click", "#photo-feed .photo-card .card-open"), ("click", "#viewer .act-repeat"),
        ("wait", count + " === 3"),
        ("click", "#photo-feed .photo-card .card-open"),
        ("click", "#viewer .act-delete"), ("click", "#photo-delete-confirm"), ("wait", count + " === 2"),
        ("click", "#photo-feed .photo-card .card-open"),
        ("click", "#viewer .act-delete"), ("click", "#photo-delete-confirm"), ("wait", count + " === 1"),
        ("eval", "last", f"(async () => (await {API}.api('GET','/api/images')).items[0].prompt)()")], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["entries"] == ["Second result", "First result"], r
    assert r["values"]["last"] == "First result", r
    assert r["values"]["defaultView"] == "table"
    assert r["values"]["shownPrompt"] == "Second result"
    assert r["values"]["restoredModel"] == "photo-b"
    assert "seed 12345" in r["values"]["meta"]
    assert [args["model"] for name, args in calls if name == "image_generate"] == ["photo-b", "photo-b", "photo-b", "photo-b"]
    assert calls[-2][1]["n"] == 2 and "seed" not in calls[-2][1]
    assert calls[-1][1]["seed"] == 12345
    assert not problems(r), problems(r)


def test_photo_reference_stays_visible_and_requires_a_tool_that_accepts_it(browser, live):
    login = "mcp-photo-reference"
    live.add_user(login)
    tools = {**GOOD, "image_edit": {
        "properties": {"prompt": TEXT, "artifact_id": TEXT}, "required": ["prompt", "artifact_id"],
    }}
    calls = serve(live.gw, tools)
    result = browser.drive(live.url + "/", [*start(login),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")), choose("image_generate"),
        ("eval", "clip", "!document.querySelector('#chip-photo').hidden"),
        set_value("#photo-prompt", "Keep the composition"),
        put_files("#photo-file", [("reference.png", PNG_REAL)]),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth > 0"),
        ("eval", "preview", "!document.querySelector('#photo-source').hidden"),
        ("click", "#photo-source-clear"),
        paste_image("body"),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth > 0"),
        ("click", "#photo-source-clear"),
        ("eval", "drop", "(() => { const data = new DataTransfer();"
         f" data.items.add(new File([Uint8Array.from(atob({base64.b64encode(PNG_REAL).decode()!r}), c => c.charCodeAt(0))],"
         " 'dropped.png', { type: 'image/png' }));"
         " document.querySelector('#photo-composer').dispatchEvent(new DragEvent('drop', { dataTransfer: data })); })()"),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth > 0"),
        ("click", "#photo-go"), ("eval", "blocked", "document.querySelector('#photo-error').textContent"),
        ("click", "#photo-source-change"),
        ("wait", "document.querySelector('#section-mcp-tool').value.endsWith(':image_edit')"),
        ("eval", "retained", "[!document.querySelector('#photo-source').hidden, document.querySelector('#photo-prompt').value]"),
        ("click", "#photo-go"), ("wait", DONE)], width=1280)
    assert not result.get("failed"), result
    assert result["values"]["clip"] and result["values"]["preview"] and result["values"]["paste"]
    assert "исходное фото" in result["values"]["blocked"]
    assert result["values"]["retained"] == [True, "Keep the composition"]
    assert [name for name, _ in calls] == ["artifact_upload", "image_edit"]
    assert calls[-1][1] == {"artifact_id": UPLOADED, "prompt": "Keep the composition"}
    assert not problems(result), problems(result)


def test_mcp_photo_viewer_reuses_the_generated_image_with_an_editing_tool(browser, live):
    login = "mcp-photo-result-as-source"
    live.add_user(login)
    tools = {**GOOD, "image_edit": {
        "properties": {"prompt": TEXT, "artifact_id": TEXT}, "required": ["prompt", "artifact_id"],
    }}
    calls = serve(live.gw, tools, photo_result=True)
    result = browser.drive(live.url + "/", [*start(login),
        ("eval", "photo", "location.hash='#/photo'"), ("wait", ready("image_generate")), choose("image_generate"),
        set_value("#photo-prompt", "First result"), ("click", "#photo-go"), ("wait", f"document.querySelector('{NEWEST_DONE}')"),
        set_value("#photo-prompt", "Keep the composition"),
        ("click", NEWEST_DONE), ("click", "#viewer .act-use-source"),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth > 0"),
        ("eval", "tool", "document.querySelector('#section-mcp-tool').value.split(':').at(-1)"),
        ("click", "#photo-go"), ("wait", DONE)], width=1280)
    assert not result.get("failed"), result
    assert result["values"]["tool"] == "image_edit"
    assert [name for name, _ in calls] == ["image_generate", "artifact_upload", "image_edit"]
    assert base64.b64decode(calls[1][1]["data_base64"]) == PNG_REAL
    assert calls[-1][1] == {"artifact_id": UPLOADED, "prompt": "Keep the composition"}
    assert not problems(result), problems(result)
