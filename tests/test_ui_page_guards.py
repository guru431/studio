"""Оболочка страницы: адрес раздела, блокировка, несохранённое и слова интерфейса в настоящем браузере."""
import pytest

from test_ui_browser import READY, browser, go, live, open_new_chat, problems, set_value, signin

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


def test_agent_drafts_leave_the_tab_with_the_emergency_exit(browser, live):
    login = "guard-drafts"
    live.add_user(login)
    stored = "sessionStorage.getItem('studio-agent-drafts')"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "saved", "import('/static/js/code-drafts.js').then(m => { m.saveDraft('1/a/b', 'неотправленное задание');"
                          f" return {stored}.includes('неотправленное'); }})"),
        *go("settings/security"), ("wait", "document.querySelector('#emergency-password')"),
        set_value("#emergency-password", "password-123"), ("click", "#emergency-start"), ("click", "#emergency-confirm"),
        ("wait", "location.pathname === '/login'"), ("eval", "after", stored)])
    assert not r.get("failed"), r
    assert r["values"]["saved"] is True and r["values"]["after"] is None


def test_english_connection_history_names_the_devices_in_english(browser, live):
    from test_ui_browser import live_sql
    import time
    login = "guard-devices"
    uid = live.add_user(login)
    for i, device in enumerate(["браузер a1b2c3", "телефон 0f9e8d", "мост", "аварийный выход", "участник", "Ноутбук"]):
        live_sql(live, "INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
                       " VALUES (?, 7, 'PC', ?, 'open', ?, NULL)", (uid, time.time() - 100 + i, device))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/code"),
        ("wait", "document.querySelectorAll('#agent-events li').length === 6"),
        ("eval", "devices", "[...document.querySelectorAll('#agent-events li')].map(li => li.textContent.split(' · ').at(-1))")],
        init="localStorage.setItem('studio-language', 'en')")
    assert not r.get("failed"), r
    assert r["values"]["devices"] == ["Ноутбук", "member", "emergency sign-out", "bridge", "phone 0f9e8d", "browser a1b2c3"]
