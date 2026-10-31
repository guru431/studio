"""Размеры, вложения, статусы проектов и подписка в настоящем браузере."""
import base64

import pytest

from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine,
                             code_online, code_project, code_requests, code_session, code_tab_click, code_tab_ready,
                             go, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_session_picker_uses_cached_titles_and_search_while_refreshing(browser, live, width, tmp_path):
    login = f"session-cache-{width}"
    live.add_user(login)
    title = "Длинное название чата про ёлку " * 12
    machine = code_free_machine({CODE_CHAT_A: code_session("Открытый чат"), CODE_CHAT_B: code_session(title)})
    aid, server = code_online(live, login, machine)
    gate = """(() => { const real = window.fetch; window.chatReads = 0;
      window.fetch = async (url, options) => {
        if (String(url).endsWith('/chats') && (!options?.method || options.method === 'GET')) {
          window.chatReads++;
          if (window.holdChats) await new Promise(resolve => { window.releaseChats = resolve; });
        }
        return real(url, options);
      }; return true; })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("eval", "gate", gate), *code_chat(aid, CODE_CHAT_A),
            ("eval", "hold", "window.holdChats = true"), ("click", "#code-tab-add"),
            ("wait", "!!window.releaseChats && !!document.querySelector('#code-chat-search')"),
            ("eval", "cached", f"!!document.querySelector('#code-chats li[data-chat=\"{CODE_CHAT_B}\"]')"),
            ("eval", "height", """(() => { const n = document.querySelector('.code-chat-title');
              return n.getBoundingClientRect().height <= 2 * parseFloat(getComputedStyle(n).lineHeight) + 1; })()"""),
            ("click", f".code-tab[data-chat='{CODE_CHAT_A}'] .code-tab-title"),
            ("wait", f"document.querySelector('#code-chat')?.dataset.chat === '{CODE_CHAT_A}'"),
            ("click", "#code-tab-add"),
            ("wait", "!!document.querySelector('#code-chat-search')"),
            ("eval", "joined", "window.chatReads"),
            set_value("#code-chat-search", "ЕЛКУ длинное"),
            ("eval", "found", "document.querySelectorAll('#code-chats li[data-chat]').length"),
            set_value("#code-chat-search", "нет такого чата"),
            ("eval", "empty", "document.querySelector('#code-chats').textContent.includes('Ничего не найдено')"),
            ("eval", "refresh", "window.holdChats = false; window.releaseChats(); true"),
            ("wait", "document.querySelector('#code-chats').getAttribute('aria-busy') === 'false'"),
            ("eval", "kept", "[document.querySelector('#code-chat-search').value, document.querySelectorAll('#code-chats li[data-chat]').length]"),
            set_value("#code-chat-search", "елку"),
            ("screenshot", str(tmp_path / f"session-search-{width}.png")),
            ("click", f"#code-chats li[data-chat='{CODE_CHAT_B}'] button"), code_tab_ready(CODE_CHAT_B),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("eval", "reads", "window.chatReads")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["cached"] and values["height"] and values["fits"]
    assert values["found"] == 1 and values["empty"]
    assert values["kept"] == ["нет такого чата", 0]
    assert values["joined"] == values["reads"] == 2
    assert not problems(result), problems(result)


def test_locked_reload_needs_only_one_lock_state_request(browser, live):
    from test_ui_browser import LOCK_OPEN, PIN_SHOWN, PHONE, known_refusals, phone_with_pin, pin_locked, tap

    login = "pin-start-once"
    live.add_user(login)
    gate = """(() => { const real = window.fetch; window.lockReads = 0;
      window.fetch = (url, options) => {
        if (String(url) === '/api/lock' && ++window.lockReads > 1
          && performance.getEntriesByType('navigation')[0]?.type === 'reload') return new Promise(() => {});
        return real(url, options);
      }; return true; })()"""
    result = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(),
        ("eval", "reload", "location.reload(); true"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {LOCK_OPEN} && {PIN_SHOWN}"),
        ("eval", "reads", "window.lockReads"), *tap("2580"), ("click", "#lock-pin-ok"),
        ("wait", f"!{LOCK_OPEN} && {READY}")], init=gate, **PHONE)
    assert not result.get("failed"), result
    assert result["values"]["reads"] == 1
    assert not known_refusals(problems(result), 423), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_new_tab_picker_fills_workspace_and_preserves_chat_draft(browser, live, width, tmp_path):
    login = f"agent-picker-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A"), CODE_CHAT_B: code_session("Чат B")})
    aid, server = code_online(live, login, machine)
    shape = """(() => {
      const picker = document.querySelector('#code-tab-menu'), rect = picker.getBoundingClientRect();
      const body = document.querySelector('.code-body'), area = body.getBoundingClientRect();
      const style = getComputedStyle(body), selects = document.querySelectorAll('.code-starts');
      return { inline: picker.parentElement.classList.contains('code-pane') && getComputedStyle(picker).position === 'static',
        fullWidth: Math.abs(rect.width - area.width + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)) < 2,
        noHeaderButtons: selects.length === 0, noChat: !document.querySelector('#code-chat'),
        fits: document.documentElement.scrollWidth <= innerWidth };
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "!!document.querySelector('#code-projects li[data-pid]')"), *code_project(aid, "alpha"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 2"),
            ("eval", "initial", shape), ("screenshot", str(tmp_path / f"picker-empty-{width}.png")),
            ("click", f"#code-chats li[data-chat='{CODE_CHAT_A}'] button"), code_tab_ready(CODE_CHAT_A),
            set_value("#code-text", "Черновик задания"),
            ("eval", "fonts", "[...document.querySelectorAll('.code-pick select')].map(s => getComputedStyle(s).fontSize)"),
            ("click", "#code-tab-add"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 1"),
            ("eval", "opened", shape),
            ("eval", "rows", "[...document.querySelectorAll('#code-tab-menu li[data-chat]')].map(n => n.dataset.chat)"),
            ("click", "#code-place"), ("press", "Escape"),
            ("eval", "stillOpen", "!!document.querySelector('#code-tab-menu #code-new-chat')"),
            ("screenshot", str(tmp_path / f"picker-open-{width}.png")),
            *code_tab_click(CODE_CHAT_A), ("eval", "draft", "document.querySelector('#code-text').value"),
            ("click", "#code-tab-add"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 1"),
            ("click", ".code-tab-close"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 2"),
            ("eval", "lastClosed", "!!document.querySelector('#code-new-chat') && !document.querySelector('.code-tab')")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert all(values["initial"].values()) and all(values["opened"].values()), values
    assert values["fonts"] == ["12px"] * 3
    assert values["rows"] == [CODE_CHAT_B] and values["stillOpen"] and values["lastClosed"]
    assert values["draft"] == "Черновик задания"
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [390, 1280])
def test_font_size_and_zoom_persist_without_losing_form(browser, live, width, tmp_path):
    login = f"appearance-{width}"
    live.add_user(login)
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("click", "#avatar"), ("wait", "!document.querySelector('#menu').hidden"),
        ("eval", "initial", "[document.querySelector('#appearance-scale').value, document.documentElement.style.zoom]"),
        ("eval", "options", "[...document.querySelector('#appearance-scale').options].map(o => Number(o.value))"),
        ("eval", "chosenSize", """(() => {
          const f = document.querySelector('#appearance-font'); f.value = '125'; f.dispatchEvent(new Event('input'));
          const s = document.querySelector('#appearance-scale'); s.value = '80'; s.dispatchEvent(new Event('change'));
          return [getComputedStyle(document.body).fontSize, document.documentElement.style.zoom]; })()"""),
        ("screenshot", str(tmp_path / f"appearance-{width}.png")),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "reload", "location.reload(); true"),
        ("wait", "performance.getEntriesByType('navigation')[0].type === 'reload' && " + READY),
        ("eval", "saved", "[document.querySelector('#appearance-font').value, document.querySelector('#appearance-scale').value]"),
        *go("settings/code"), ("wait", "!!document.querySelector('[data-settings=code]') || !!document.querySelector('.code-defaults-block')"),
        ("eval", "names", "[document.querySelector('#rail [data-section=code]').textContent.trim(), document.querySelector('a[href=\"#/settings/code\"]').textContent]"),
        ("click", "#avatar"),
        ("eval", "largest", """(() => { const f = document.querySelector('#appearance-font'); f.value='150';
          f.dispatchEvent(new Event('input')); const s = document.querySelector('#appearance-scale'); s.value='100';
          s.dispatchEvent(new Event('change')); const r = document.querySelector('#menu').getBoundingClientRect();
          return [document.documentElement.scrollWidth <= innerWidth, r.left >= 0, r.right <= innerWidth,
            document.body.getBoundingClientRect().height <= innerHeight + 1]; })()"""),
        ("screenshot", str(tmp_path / f"appearance-largest-{width}.png")),
        set_value('#appearance-scale', '50'),
        ("eval", "reloadSmallest", "location.reload(); true"),
        ("wait", "performance.getEntriesByType('navigation')[0].type === 'reload' && " + READY),
        ("eval", "smallest", "[document.querySelector('#appearance-scale').value, document.documentElement.style.zoom, document.documentElement.scrollWidth <= innerWidth]")],
        width=width, mobile=width == 390,
        init="if (!localStorage.getItem('studio-ui-scale')) localStorage.setItem('studio-ui-scale', '150');")
    assert not result.get("failed"), result
    assert result["values"]["initial"] == ["100", "1"]
    assert result["values"]["options"] == list(range(50, 101, 5))
    assert result["values"]["chosenSize"] == ["18.75px", "0.8"]
    assert result["values"]["saved"] == ["125", "80"]
    assert result["values"]["names"] == ["Агенты", "Агенты"]
    assert not result["values"]["overflow"]
    assert result["values"]["largest"] == [True, True, True, True]
    assert result["values"]["smallest"] == ["50", "0.5", True]
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [390, 1280])
def test_agent_attachments_context_project_status_and_subscription(browser, live, width, tmp_path):
    login = f"agent-controls-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Вложения"), CODE_CHAT_B: code_session("В работе")})
    machine.projects["beta"] = {"chats": {}}
    machine.attachments = machine.project_activity = machine.subscription_usage = True
    machine.context = {"used": 85000, "limit": 100000, "percent": 85}
    machine.running = {CODE_CHAT_B: {"project": pid("alpha"), "asks": {}},
                       "c3333333-3333-4333-8333-333333333333": {"project": pid("beta"), "asks": {"r": {}}}}
    machine.usage_data = {"engine": "claude", "available": True, "source": "test", "checked_at": 1800000000,
        "cache_ttl": 60, "plan": "max", "windows": [
            {"id": "five_hour", "label": "5 часов", "available": True, "used_percent": 25,
             "remaining_percent": 75, "window_minutes": 300, "resets_at": 1800010000},
            {"id": "weekly", "label": "Неделя", "available": True, "used_percent": 40,
             "remaining_percent": 60, "window_minutes": 10080, "resets_at": 1800500000},
            {"id": "monthly", "label": "Месяц", "available": False, "reason": "Не сообщается"}],
        "statistics": {"available": True, "source": "CLI", "summary": {"lifetime_tokens": 12345},
                       "daily": [{"date": "2026-10-30", "tokens": 456}]}}
    machine.script = [{"k": "text", "text": "Получил файлы"}, {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    pick = """(() => { const dt = new DataTransfer();
      dt.items.add(new File(['%PDF-sample'], 'документ.pdf', {type:'application/pdf'}));
      dt.items.add(new File([new Uint8Array([0, 255, 7])], 'archive.zip', {type:'application/zip'}));
      const input = document.querySelector('#code-image-input'); input.files = dt.files;
      input.dispatchEvent(new Event('change')); return true; })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "[...document.querySelectorAll('.code-project-state:not([hidden])')].length === 2"),
            ("eval", "statuses", "[...document.querySelectorAll('.code-project-state:not([hidden])')].map(n=>n.getAttribute('aria-label'))"),
            ("eval", "context", "[document.querySelector('#code-context-text').textContent, document.querySelector('#code-context').dataset.level]"),
            ("eval", "keepTab", "window.firstAgentTab = document.querySelector('.code-tab'); import('/static/js/code-workspace.js').then(w=>w.act('panel',{aid:" + str(aid) + "}))"),
            ("idle",), ("eval", "sameTab", "window.firstAgentTab === document.querySelector('.code-tab')"),
            ("eval", "files", pick), ("wait", "document.querySelectorAll('#code-images .code-file').length === 2"),
            ("eval", "accept", "document.querySelector('#code-image-input').hasAttribute('accept')"),
            ("click", "#code-usage"), ("wait", "!!document.querySelector('[data-window=five_hour]')"),
            ("eval", "usage", "document.querySelector('#code-usage-dialog').textContent"),
            ("screenshot", str(tmp_path / f"agent-usage-{width}.png")), ("click", "#code-usage-dialog .dialog-actions .btn:last-child"),
            ("screenshot", str(tmp_path / f"agent-controls-{width}.png")),
            ("click", "#code-send"), ("wait", "document.querySelector('#code-feed').textContent.includes('Получил файлы')"),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth")], width=width, mobile=width == 390)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["context"] == ["85%", "orange"]
    assert result["values"]["statuses"] == ["В работе: 1", "Требуют вашего ответа: 1"]
    assert result["values"]["sameTab"] and not result["values"]["accept"]
    assert "Осталось 75%" in result["values"]["usage"] and "Нет данных" in result["values"]["usage"]
    assert "null" not in result["values"]["usage"]
    assert "12" in result["values"]["usage"] and "345" in result["values"]["usage"]
    turn = code_requests(machine, "turn")[-1]
    assert turn["text"] == "" and [item["name"] for item in turn["attachments"]] == ["документ.pdf", "archive.zip"]
    assert base64.b64decode(turn["attachments"][1]["data"]) == bytes([0, 255, 7])
    assert not result["values"]["overflow"]
    assert not problems(result), problems(result)
