"""Поиск по тексту открытого чата «Агентов» (code-find.js) в настоящем браузере: Ctrl+F, счётчик, переход,
подсветка, «Искать в более ранней части»; текст на сервер не уходит."""
import pytest

from test_ui_browser import (CODE_CHAT_A, NO_OVERFLOW, READY, browser, code_chat, code_free_machine, code_online,
                             code_session, live, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

COUNT = "document.querySelector('#code-find-count').textContent"
BAR_HIDDEN = "document.querySelector('#code-find-bar').hidden"
CTRL_F = """(() => { const e = new KeyboardEvent('keydown', {key: 'f', code: 'KeyF', ctrlKey: true, bubbles: true,
  cancelable: true}); document.dispatchEvent(e); return [e.defaultPrevented, document.activeElement?.id || null]; })()"""


def query(value):
    """Новый текст строки поиска (как ввод человека: событие input)."""
    return ("eval", "q-" + value, "(() => { const i = document.querySelector('#code-find-input'); i.value = %r;"
            " i.dispatchEvent(new Event('input', {bubbles: true})); return true; })()" % value)


def messages():
    from fake_bridge import T0
    out = []
    for i in range(50):
        text = "Жираф пасётся" if i == 3 else "Зебра бежит" if i in (45, 47, 49) else f"сообщение {i}"
        # шаг с тем же словом: шаги не ищутся
        steps = [{"id": "s1", "tool": "Bash", "input": {"command": "echo зебра"}, "text": "зебра", "error": False}] if i == 49 else []
        out.append({"id": f"m-{i}", "role": "user" if i % 2 == 0 else "assistant", "text": text, "steps": steps,
                    "time": T0 + i})
    return out


@pytest.mark.parametrize("width", [320, 1280])
def test_find_counts_steps_through_and_loads_earlier_pages(browser, live, width):
    login = f"find-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Поиск", messages=messages())})
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "hidden", BAR_HIDDEN),
            ("eval", "key", CTRL_F), query("зебра"), ("wait", COUNT + " === '1 из 3'"),
            ("eval", "marks", "CSS.highlights.get('code-find')?.size"),
            ("click", "#code-find-next"), ("wait", COUNT + " === '2 из 3'"),
            ("click", "#code-find-prev"), ("wait", COUNT + " === '1 из 3'"),
            ("eval", "again", "(() => { document.querySelector('#code-find-input').focus(); return %s; })()" % CTRL_F),
            query("жираф"), ("wait", COUNT + " === '0 из 0'"),
            ("eval", "earlier", "!document.querySelector('#code-find-earlier').hidden"),
            ("click", "#code-find-earlier"),
            ("wait", COUNT + " === '1 из 1' && document.querySelector('#code-find-earlier').hidden"),
            ("eval", "found", "[...CSS.highlights.get('code-find-current')].map((r) => r.toString())"),
            ("eval", "focus", "document.querySelector('#code-find-input').focus() ?? true"), ("press", "Escape"),
            ("wait", BAR_HIDDEN),
            ("eval", "cleared", "!CSS.highlights.has('code-find') && !CSS.highlights.has('code-find-current')"),
            ("eval", "lens", "document.querySelector('#code-find').click() ?? true"),
            ("wait", f"!{BAR_HIDDEN} && document.activeElement?.id === 'code-find-input'"),
            ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert values["hidden"] is True and values["key"] == [True, "code-find-input"], values
    assert values["marks"] == 3, values                   # шаг с «зеброй» не ищется
    assert values["again"] == [False, "code-find-input"], values   # второе Ctrl+F — поиск браузера
    assert values["earlier"] is True and values["found"] == ["Жираф"], values
    assert values["cleared"] is True and values["fits"], values
    assert not [p for p in r["sent"] if p.endswith("/search")]          # текст на сервер не уходит
    assert not [q for q in machine.requests if "search" in q["op"]]
    assert not problems(r), problems(r)
