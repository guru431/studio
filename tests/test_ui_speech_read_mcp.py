"""«Прочитать» через инструмент MCP по умолчанию «Озвучки» (409 `read_mcp`, speech-read.js `readMcp`): каждое чтение —
своё задание MCP, а не общее место раздела (C-I4); инструмент без поля `voice` (C-M1); ни одного инструмента озвучки —
слова, а не вечное «Ищу…» (C-M2). Chromium."""
import json
import time
import uuid

import httpx
import pytest

from test_mcp import TOKEN
from test_ui_browser import add_gateway, browser, go, live, problems, set_value  # noqa: F401
from test_ui_section_limits import SERVICE, TEXT, start

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

ANSWERS = ("Первый ответ модели.", "Второй ответ модели.")
READ = "document.querySelectorAll('.msg.assistant button.msg-read')"
BOX = "document.querySelectorAll('.msg.assistant div.msg-read')"
JOBS = {"job_status": {"properties": {"job_id": TEXT}, "required": ["job_id"]},
        "job_result": {"properties": {"job_id": TEXT}, "required": ["job_id"]}}


def chat_with_answers(live, uid) -> str:
    import db
    cid, now = uuid.uuid4().hex, time.time()
    conn = db.connect(live.db_path)
    try:
        conn.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, 'чтение', ?, ?)",
                     (cid, uid, now, now))
        turns = [("user", "вопрос 1"), ("assistant", ANSWERS[0]), ("user", "вопрос 2"), ("assistant", ANSWERS[1])]
        for n, (role, content) in enumerate(turns):
            conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                         " VALUES (?, ?, ?, ?, 'done', 'server', 'local-gemma-abl', ?)",
                         (uuid.uuid4().hex, cid, role, content, now + n))
    finally:
        conn.close()
    return cid


def speaker(gateway, tools) -> list:
    """MCP с инструментами `tools`; `speech_synthesize` с `job_status` — задание, которое идёт, пока тест не кончится,
    без них — итог сразу."""
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
            if name == "job_status":
                data = {"status": "running"}
            elif name == "speech_synthesize" and "job_status" in tools:
                data = {"id": f"job_{len(calls):032x}"}
            else:
                data = {"text": "ok"}
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    gateway.on["/mcp"] = rpc
    return calls


def opened(cid):
    return [("eval", "go", f"location.hash = '#/chat/{cid}'"), ("wait", f"{READ}.length === 2")]


def read_problems(r):
    """409 `read_mcp` — штатный переход к инструменту MCP, а не сбой страницы."""
    return [line for line in problems(r) if "/api/speeches/read" not in str(line)]


@pytest.mark.parametrize("width", [320, 1280])
def test_each_read_runs_its_own_mcp_job(browser, live, width):
    login = f"readmcp{width}"
    cid = chat_with_answers(live, live.add_user(login))
    voice = {"type": "string", "enum": ["alloy", "nova"], "default": "nova"}
    calls = speaker(live.gw, {"speech_synthesize": {"properties": {"text": TEXT, "voice": voice}, "required": ["text"]},
                              **JOBS, **SERVICE})
    r = browser.drive(live.url + "/", [*start(login), *opened(cid),
        ("eval", "first", f"{READ}[0].click(); true"),
        ("wait", f"{BOX}[0]?.querySelector('.mcp-run .status')?.textContent === 'Выполняется'"),
        ("eval", "second", f"{READ}[1].click(); true"),
        ("wait", f"{BOX}[1]?.querySelector('.mcp-run .status')?.textContent === 'Выполняется'"),
        ("wait", "performance.getEntriesByType('resource').filter(e => e.name.endsWith('/run')).length === 2"),
        ("eval", "controls", f"[...{BOX}].map(box => box.querySelector('.mcp-result').id)")],
        width=width, mobile=width == 320, timeout=20000)
    assert not r.get("failed"), r
    spoken = [args for name, args in calls if name == "speech_synthesize"]
    assert [args["text"] for args in spoken] == list(ANSWERS), spoken
    assert all(args["voice"] == "nova" for args in spoken)       # голос — умолчание схемы
    assert len(set(r["values"]["controls"])) == 2
    assert not read_problems(r), read_problems(r)


def test_read_mcp_tool_without_voice_field(browser, live):
    login = "readmcpnovoice"
    cid = chat_with_answers(live, live.add_user(login))
    calls = speaker(live.gw, {"speech_synthesize": {"properties": {"text": TEXT}, "required": ["text"]}, **SERVICE})
    r = browser.drive(live.url + "/", [*start(login), *opened(cid),
        ("eval", "first", f"{READ}[0].click(); true"),
        ("wait", f"{BOX}[0]?.querySelector('.mcp-result pre')?.textContent === 'ok'")], width=1280, timeout=20000)
    assert not r.get("failed"), r
    assert [args for name, args in calls if name == "speech_synthesize"] == [{"text": ANSWERS[0]}]
    assert not read_problems(r), read_problems(r)


def test_speech_only_sends_a_model_selected_from_the_mcp_schema(browser, live):
    login = "mcpspeechmodel"
    live.add_user(login)
    model = {"type": "string", "enum": ["first", "second"], "default": "first"}
    calls = speaker(live.gw, {"speech_synthesize": {"properties": {"text": TEXT, "model": model},
                                                  "required": ["text"]}, **SERVICE})
    r = browser.drive(live.url + "/", [*start(login), add_gateway(), *go("speech"),
        ("wait", "document.querySelector('#section-mcp-tool option[value*=speech_synthesize]')"),
        ("eval", "tool", """(() => {
          const pick = document.querySelector('#section-mcp-tool');
          pick.value = [...pick.options].find(o => o.value.endsWith(':speech_synthesize')).value;
          pick.dispatchEvent(new Event('change', { bubbles: true })); return true;
        })()"""),
        set_value("#chip-mcp-model", "second"), ("type", "#speech-text", "Read this"),
        ("eval", "submit", "document.querySelector('#speech-composer').requestSubmit(); true"),
        ("wait", "document.querySelector('.mcp-result pre')?.textContent === 'ok'")])
    assert not r.get("failed"), r
    assert [args for name, args in calls if name == "speech_synthesize"] == [{"text": "Read this", "model": "second"}]


def test_read_mcp_without_a_speech_tool_says_so(browser, live):
    login = "readmcpnone"
    cid = chat_with_answers(live, live.add_user(login))
    speaker(live.gw, dict(SERVICE))
    r = browser.drive(live.url + "/", [*start(login), *opened(cid),
        ("eval", "first", f"{READ}[0].click(); true"),
        ("wait", f"{BOX}[0]?.textContent.includes('Нет источника озвучки')"),
        ("eval", "words", f"{BOX}[0].textContent")], width=1280, timeout=20000)
    assert not r.get("failed"), r
    assert "Нет источника озвучки — выберите его в «Озвучке»" in r["values"]["words"]
    assert "Ищу инструмент" not in r["values"]["words"]
    assert not read_problems(r), read_problems(r)
