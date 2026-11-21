"""Поле ввода чата (спека 2026-11-21-studio-source-capabilities-design, п. 30, 31, 2, 33): до 10 вложений в сообщении —
фото, текстовые файлы и код вместе; 11-е — словами, диалог не создаётся. Строка паспорта над полем, подсказки пустого
экрана (вставляют текст, не отправляют), кольцо заполнения контекста меняет цвет."""
import base64
import json
from types import SimpleNamespace

import pytest

import chat
from fakes import PNG_REAL
from test_mcp import TOKEN as MCP_TOKEN, URL as MCP_URL
from test_mcp_dialog import server
from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, attach, browser, live,  # noqa: F401
                             live_sql, open_new_chat, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PHOTO = base64.b64encode(PNG_REAL).decode()
CHIPS = "document.querySelectorAll('#composer .att-chip').length"


def files(names) -> str:
    """Выражение JS со списком File: `*.png` — настоящая картинка, прочие — строка текста."""
    made = [f"new File([Uint8Array.from(atob({PHOTO!r}), c => c.charCodeAt(0))], '{n}', {{ type: 'image/png' }})"
            if n.endswith(".png") else f"new File(['line of {n}\\n'], '{n}')" for n in names]
    return "[" + ", ".join(made) + "]"


@pytest.mark.parametrize("width", [320, 1280])
def test_ten_attachments_pass_eleventh_is_refused_with_words(browser, live, width):
    ten = [f"p{i}.png" for i in range(5)] + ["main.py", "data.csv", "conf.json", "notes.txt", "readme.md"]
    before = len(live.gw.chat_requests)
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(width == 320),
        ("eval", "accept", "document.querySelector('#attach').accept"),
        attach(files(ten)), ("wait", f"{CHIPS} === 10"),
        ("eval", "quiet", "document.querySelector('#chat-error').textContent"),
        attach(files(["extra.log"])), ("wait", f"{CHIPS} === 11 && document.querySelector('#chat-error').textContent"),
        ("eval", "picked", "document.querySelector('#chat-error').textContent"),
        ("eval", "fits", NO_OVERFLOW),
        ("type", "#chat-input", "Что здесь?"), ("click", "#send"), ("idle",),
        ("eval", "refused", f"[document.querySelector('#chat-error').textContent, location.hash, {CHIPS}]"),
        ("click", "#composer .att-chip:last-child .att-remove"), ("wait", f"{CHIPS} === 10"),
        ("click", "#send"), ("wait", ANSWERED),
        ("eval", "sent", f"""(async () => {{ const m = {API};
          const d = await m.api('GET', '/api/chats/' + location.hash.split('/')[2]);
          return d.messages[0].attachments.map(a => a.name).sort(); }})()""")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert ".py" in v["accept"].split(",") and ".csv" in v["accept"].split(",") and "image/*" in v["accept"]
    assert v["quiet"] == ""
    assert v["picked"] == "не больше 10 вложений в сообщении" and v["fits"] is True
    assert v["refused"] == ["не больше 10 вложений в сообщении", "#/chat", 11]
    assert v["sent"] == sorted(ten)
    assert not [s for s in r["sent"] if s == "POST /api/chats"][1:], r["sent"]   # один диалог — под отправленный ход
    assert len(live.gw.chat_requests) == before + 1
    parts = [m for m in live.gw.chat_requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert sum(p["type"] == "image_url" for p in parts) == 4
    assert '<document name="main.py">\n```python\n' in parts[0]["text"]
    assert not problems(r), problems(r)


PASSPORT = "document.querySelector('#chat-passport')"
RING = "document.querySelector('#ctx-meter')"


def long_text(n) -> tuple:
    """Поле ввода — `n` знаков разом и событие input, как от человека."""
    return ("eval", "typed", f"(() => {{ const e = document.querySelector('#chat-input'); e.value = 'x'.repeat({n});"
                             " e.dispatchEvent(new Event('input', {bubbles: true})); return e.value.length; })()")


@pytest.mark.parametrize("width", [320, 1280])
def test_passport_warning_and_context_ring(browser, live, width, monkeypatch):
    live_sql(live, "UPDATE section_links SET models = ? WHERE section = 'chat'",
             (json.dumps(["local-text-role"]),))
    live_sql(live, "UPDATE connections SET passport = ? WHERE kind = 'srvllm'",
             (json.dumps({"models": [{"id": "local-text-role", "caps": [{"name": "chat", "source": "probe"}]}]}),))
    monkeypatch.setitem(chat.CONTEXT, "local-text-role", 10000)
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(width == 320),
        ("eval", "quiet", f"[{PASSPORT}.hidden, {RING}.hidden]"),
        attach(files(["p.png", "notes.txt"])), ("wait", f"!{PASSPORT}.hidden"),
        ("eval", "warned", f"{PASSPORT}.textContent"),
        ("click", "#chat-passport .passport-model"), ("wait", "document.querySelector('#model-menu').matches(':popover-open')"),
        ("press", "Escape"), ("wait", "!document.querySelector('#model-menu').matches(':popover-open')"),
        ("click", "#chat-passport .passport-drop"), ("wait", f"{CHIPS} === 1 && {PASSPORT}.hidden"),
        ("eval", "left", "document.querySelector('#composer .att-chip .att-name').textContent"),
        ("click", "#composer .att-chip .att-remove"), ("wait", f"{CHIPS} === 0"),
        ("type", "#chat-input", "Привет"), ("click", "#send"), ("wait", ANSWERED),
        ("wait", f"!{RING}.hidden"),
        ("eval", "calm", f"[{RING}.className, {RING}.getAttribute('aria-label')]"),
        ("eval", "sizes", f"""[{RING}.querySelector('.ctx-ring').getBoundingClientRect().width,
          {RING}.getBoundingClientRect().height, document.querySelector('#model-chip').getBoundingClientRect().height]"""),
        long_text(21600), ("wait", f"{RING}.classList.contains('ctx-warn')"),
        ("eval", "warn", f"[{RING}.className, {RING}.title]"),
        long_text(28500), ("wait", f"{RING}.classList.contains('ctx-full')"),
        ("eval", "full", f"{RING}.className"),
        ("eval", "fits", NO_OVERFLOW)],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["quiet"] == [True, True]
    assert v["warned"] == "Модель по паспорту не смотрит фотоУбрать фотоСменить модель"
    assert v["left"] == "notes.txt"
    assert v["calm"][0] == "ctx-meter" and v["calm"][1].startswith("Контекст ~")
    ring, meter, chip = v["sizes"]
    assert ring == 18 and abs(meter - chip) <= 1
    assert "ctx-warn" in v["warn"][0] and "ctx-full" not in v["warn"][0]
    assert v["warn"][1].replace(" ", " ").endswith("из 10 000)") and "~7" in v["warn"][1]
    assert "ctx-full" in v["full"] and "ctx-warn" not in v["full"]
    assert v["fits"] is True
    assert not problems(r), problems(r)


PROMPTS = [{"name": "brief", "title": "Обзор темы", "description": "Короткий обзор",
            "arguments": [{"name": "topic", "description": "Тема", "required": True}]}]
SUGGEST = "document.querySelectorAll('#chat-suggest .suggest-chip')"


@pytest.mark.parametrize("width", [320, 1280])
def test_suggestion_inserts_text_without_sending(browser, live, width):
    server(SimpleNamespace(gw=live.gw), prompts=PROMPTS)
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "setup", f"""(async () => {{ const m = {API};
          const conn = (await m.api('GET', '/api/connections')).items.find(c => c.kind === 'srvllm');
          await m.api('PUT', '/api/connections/' + conn.id + '/profile', {{ suggestions: [
            {{ title: 'Сводка', text: 'Сделай сводку' }}] }});
          if (!(await m.api('GET', '/api/mcp')).items.some(s => s.name === 'Prompts'))
            await m.api('POST', '/api/mcp', {{ name: 'Prompts', url: {MCP_URL!r}, token: {MCP_TOKEN!r} }});
          return conn.id; }})()"""),
        # чат мог открыться первым экраном до профиля: заново
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        *open_new_chat(width == 320), ("wait", f"{SUGGEST}.length === 1"),
        ("click", "#chat-suggest .suggest-chip"),
        ("eval", "inserted", "document.querySelector('#chat-input').value"),
        ("eval", "mcp", f"""(async () => {{ const m = {API};
          const sid = (await m.api('GET', '/api/mcp')).items.find(s => s.name === 'Prompts').id;
          const c = await m.api('POST', '/api/chats', {{}});
          await m.api('PATCH', '/api/chats/' + c.id, {{ mcp_ids: [sid] }});
          location.hash = '#/chat/' + c.id; return sid; }})()"""),
        ("wait", f"{SUGGEST}.length === 2"),
        ("click", "#chat-suggest .suggest-chip[data-source=mcp]"),
        ("wait", "document.querySelector('.suggest-form')?.matches(':popover-open')"),
        ("type", ".suggest-form input[name=topic]", "кино"), ("click", ".suggest-form .suggest-go"),
        ("wait", "document.querySelector('#chat-input').value.endsWith('Обзор: кино')"),
        ("eval", "after", "[document.querySelector('#chat-input').value, document.querySelectorAll('#messages .msg').length,"
                          " document.querySelector('.suggest-form').matches(':popover-open')]"),
        ("eval", "fits", NO_OVERFLOW)],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["inserted"] == "Сделай сводку"
    assert v["after"] == ["Сделай сводку\n\nОбзор: кино", 0, False]
    assert not [s for s in r["sent"] if s.endswith("/turns")], r["sent"]
    assert v["fits"] is True
    assert not problems(r), problems(r)
