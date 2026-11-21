"""План ответа и личная память на странице (спека 2026-11-21-studio-source-capabilities-design, п. 18): блок
«План: N из M» над ответом (`chat.js` `paintTodo`), «Настройки → Чат → Память»: выключатель, список, «Очистить всё»."""
import json

import pytest

from test_ui_browser import (ANSWERED, LAST_ANSWER, NO_OVERFLOW, READY, add_gateway, browser, go, live,  # noqa: F401
                             open_new_chat, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

PLAN = json.dumps({"items": [{"text": "Найти источники", "status": "done"},
                             {"text": "Сравнить " + "очень-длинное-слово" * 6, "status": "in_progress"}]},
                  ensure_ascii=False)
TODO = f"{LAST_ANSWER}?.querySelector('.chat-todo')"


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_plan_block_above_the_answer(browser, live, width):
    login = f"todo-{width}"
    live.add_user(login)
    mobile = width < 1000
    live.gw.chat_script = [{"tool_calls": [("todo_write", PLAN)]}, ["Готово."]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Сравни"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {TODO} && {LAST_ANSWER}.querySelector('.md').textContent.includes('Готово')"),
            ("eval", "plan", f"""[{TODO}.querySelector('.chat-todo-head').textContent,
              [...{TODO}.querySelectorAll('li')].map(li => li.className),
              {TODO}.compareDocumentPosition({LAST_ANSWER}.querySelector('.md')) & Node.DOCUMENT_POSITION_FOLLOWING]"""),
            ("eval", "fits", f"{NO_OVERFLOW} && {TODO}.getBoundingClientRect().right <= innerWidth")],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r.get("failed"), r
    head, classes, below = r["values"]["plan"]
    assert head == "План: 1 из 2" and classes == ["todo-done", "todo-in_progress"] and below
    assert r["values"]["fits"] is True
    assert not problems(r), r.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_memory_settings_toggle_list_and_clear(browser, live, width):
    login = f"memory-{width}"
    live.add_user(login)
    mobile = width < 1000
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        ("eval", "seed", """(async () => { const m = await import('/static/js/api.js');
          await m.api('POST', '/api/memory', { text: 'Любит краткие ответы' }); return true; })()"""),
        *go("settings/chat"),
        ("wait", "document.querySelector('#chat-memory-settings .memory-item')"),
        ("eval", "before", """[document.querySelector('#memory-enabled').checked,
          document.querySelector('#chat-memory-settings .memory-text').textContent]"""),
        ("click", "#memory-enabled"),
        ("wait", "document.querySelector('#memory-status').textContent && !document.querySelector('#memory-enabled').disabled"),
        ("eval", "me", """(async () => (await (await import('/static/js/api.js')).api('GET', '/api/me')).memory_on)()"""),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#memory-clear"), ("click", "#memory-clear-confirm"),
        ("wait", "!document.querySelector('#chat-memory-settings .memory-item') && document.querySelector('#memory-clear').hidden")],
        width=width, mobile=mobile)
    assert not r.get("failed"), r
    assert r["values"]["before"] == [False, "Любит краткие ответы"]
    assert r["values"]["me"] is True and r["values"]["fits"] is True
    assert not problems(r), r.get("log")
