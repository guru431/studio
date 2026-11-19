"""Оболочка страницы: адрес раздела, блокировка, несохранённое и слова интерфейса в настоящем браузере."""
import pytest

from test_ui_browser import READY, browser, go, live, open_new_chat, problems, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: POST /api/chats ждёт `window.releaseChat()`: ответ создания диалога приходит, когда раздел уже другой
HOLD_CHAT = """(() => { const real = window.fetch; let release; const gate = new Promise(r => { release = r; });
  window.releaseChat = () => { release(); return true; };
  window.fetch = (url, options = {}) => String(url) === '/api/chats' && options.method === 'POST'
    ? gate.then(() => real(url, options)) : real(url, options);
  return true; })()"""


def test_chat_created_after_leaving_keeps_the_address_of_the_open_section(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("eval", "hold", HOLD_CHAT), ("type", "#chat-input", "Ответ дочитает «В работе»"), ("press", "Enter"),
        *go("photo"), ("wait", "document.querySelector('#rail [data-section=photo][aria-current=page]')"),
        ("eval", "release", "window.releaseChat()"),
        ("wait", "fetch('/api/chats').then(r => r.json()).then(j => j.items.length > 0)"),
        ("eval", "settled", "new Promise(r => setTimeout(() => r(location.hash), 500))")])
    assert not r.get("failed"), r
    assert r["values"]["settled"] == "#/photo"
    assert not problems(r), problems(r)
