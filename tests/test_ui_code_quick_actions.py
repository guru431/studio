"""Быстрые действия нового чата и расположение панели задания."""
import json

import pytest

import code_actions
from test_ui_browser import (API, CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine,
                             code_online, code_session, code_tab_click, go, known_refusals, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_empty_chat_composer_and_status_toolbar_stay_at_bottom(browser, live, width, tmp_path):
    login = f"chat-bottom-{width}"
    live.add_user(login)
    messages = [{"id": str(i), "role": "user" if i % 2 == 0 else "assistant",
                 "text": "Длинная история.\n\n" * 30, "steps": [], "time": i + 1} for i in range(20)]
    machine = code_free_machine({CODE_CHAT_A: code_session("Пустой чат"),
                                 CODE_CHAT_B: code_session("История", messages=messages)})
    machine.chat_fork = machine.subagents = machine.queue_edit = True
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, server = code_online(live, login, machine)
    shape = """(() => {
      const composer = document.querySelector('#code-composer').getBoundingClientRect();
      const view = document.querySelector('#view'), rect = view.getBoundingClientRect();
      const padding = parseFloat(getComputedStyle(view).paddingBottom);
      const toolbar = document.querySelector('.code-status-bar');
      const buttons = ['#code-fork-chat', '#code-subagents', '#code-context', '#code-usage'].map(s => document.querySelector(s));
      return { bottom: Math.abs(composer.bottom - rect.bottom + padding) < 20,
        navigationLast: toolbar.lastElementChild === document.querySelector('.code-message-nav'),
        toolbar: !!toolbar && buttons.every(b => toolbar.contains(b)) && !toolbar.closest('form'),
        icons: buttons.slice(0, 2).every(b => b.querySelector('svg') && b.getAttribute('aria-label')),
        order: buttons.every((b,i) => !i || b.getBoundingClientRect().left >= buttons[i-1].getBoundingClientRect().right),
        fits: document.documentElement.scrollWidth <= innerWidth };
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "empty", shape), ("screenshot", str(tmp_path / f"empty-chat-{width}.png")),
            set_value("#code-text", "Начать работу"), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "delivery", """(() => {
              const rights = document.querySelector('#code-access').closest('label').getBoundingClientRect();
              const delivery = document.querySelector('#code-delivery').getBoundingClientRect();
              return delivery.left >= rights.right && Math.abs(delivery.top - rights.top) < 2
                && !!document.querySelector('#code-composer #code-delivery');
            })()"""), ("eval", "working", shape),
            ("screenshot", str(tmp_path / f"working-chat-{width}.png")),
            ("click", "#code-stop"), ("wait", "document.querySelector('#code-chat').dataset.running === '0'"),
            *code_chat(aid, CODE_CHAT_B),
            ("eval", "oldBottom", "(() => { const v = document.querySelector('#view'); return v.scrollHeight-v.clientHeight-v.scrollTop < 2; })()"),
            ("eval", "scrollUp", "document.querySelector('#view').scrollTop=0; true"),
            ("eval", "scrolled", shape),
            *code_tab_click(CODE_CHAT_A), *code_tab_click(CODE_CHAT_B),
            ("eval", "reopenedBottom", "(() => { const v = document.querySelector('#view'); return v.scrollHeight-v.clientHeight-v.scrollTop < 2; })()")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert all(values["empty"].values()), values
    assert all(values["working"].values()) and all(values["scrolled"].values()), values
    assert values["delivery"] and values["oldBottom"] and values["reopenedBottom"], values
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_quick_actions_settings_and_exact_first_message(browser, live, width, tmp_path):
    login = f"quick-actions-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Новый чат"), CODE_CHAT_B: code_session("Другой чат")})
    machine.script = [{"k": "text", "text": "Выполняю"}]
    aid, server = code_online(live, login, machine)
    prompt = "  Точный текст.\n\nБез сокращений и правок.  "
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/code"),
            ("wait", "document.querySelectorAll('.code-quick-action-row').length === 3"),
            ("eval", "defaults", "[...document.querySelectorAll('.code-quick-action-row textarea')].map(n=>n.value)"),
            set_value(".code-quick-action-row input", "Моё действие " + "с длинным названием " * 3),
            set_value(".code-quick-action-row textarea", prompt),
            ("click", ".code-quick-action-row:nth-child(3) [aria-label='Удалить действие']"),
            ("click", "#code-quick-actions-add"),
            set_value(".code-quick-action-row:last-child input", "Добавленное действие"),
            set_value(".code-quick-action-row:last-child textarea", "Добавленный текст"),
            ("click", "#code-quick-actions-save"),
            ("wait", "document.querySelector('#code-quick-actions-status').textContent.includes('сохранены')"),
            ("eval", "reload", "location.reload(); true"), ("wait", READY),
            ("wait", "document.querySelector('.code-quick-action-row textarea')?.value === " + json.dumps(prompt)),
            *code_chat(aid, CODE_CHAT_A), ("wait", "!document.querySelector('#code-quick-actions').hidden"),
            ("eval", "titles", """[...document.querySelectorAll('.code-quick-action')].every(b => {
              const s=getComputedStyle(b); return s.whiteSpace==='nowrap' && b.getBoundingClientRect().right <= innerWidth;
            })"""), ("screenshot", str(tmp_path / f"quick-actions-{width}.png")),
            ("click", ".code-quick-action"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "hidden", "document.querySelector('#code-quick-actions').hidden"),
            ("click", "#code-stop"), ("wait", "document.querySelector('#code-chat').dataset.running === '0'"),
            *code_chat(aid, CODE_CHAT_B), *code_tab_click(CODE_CHAT_A),
            ("eval", "stillHidden", "document.querySelector('#code-quick-actions').hidden"),
            ("eval", "stored", f"(async () => (await {API}.api('GET','/api/me')).code_quick_actions)()")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["defaults"] == [a["text"] for a in code_actions.DEFAULTS]
    assert values["titles"] and values["hidden"] and values["stillHidden"]
    assert values["stored"][0]["text"] == prompt and values["stored"][-1]["text"] == "Добавленный текст"
    turns = [r for r in machine.requests if r["op"] == "turn"]
    assert len(turns) == 1 and turns[0]["text"] == prompt
    assert not problems(result), problems(result)


def test_quick_action_rejected_before_start_restores_text_and_can_be_retried(browser, live):
    login = "quick-action-retry"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Новый чат")})
    machine.script = [{"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    reject = """(() => {
      const real = window.fetch;
      window.rejectAction = true;
      window.fetch = (url, options) => {
        if (window.rejectAction && String(url).endsWith('/turn')) {
          window.rejectAction = false;
          return new Promise(resolve => { window.rejectTurn = () => resolve(new Response(
            JSON.stringify({detail:{code:'busy',message:'Повторите отправку'}}),
            {status:409,headers:{'content-type':'application/json'}})); });
        }
        return real(url, options);
      }; return true;
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "reject", reject), ("click", ".code-quick-action"),
            ("wait", "!!window.rejectTurn"),
            ("eval", "pendingHidden", "document.querySelector('#code-quick-actions').hidden"),
            ("eval", "release", "window.rejectTurn(); true"),
            ("wait", "!document.querySelector('#code-quick-actions').hidden && !document.querySelector('#code-send').disabled"),
            ("eval", "restored", "document.querySelector('#code-text').value"),
            ("click", ".code-quick-action"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '0' && document.querySelector('.code-msg.assistant .md')?.textContent.includes('Готово')"),
            ("eval", "hidden", "document.querySelector('#code-quick-actions').hidden"),
            *go("settings/code"), ("wait", "document.querySelectorAll('.code-quick-action-row').length === 3"),
            ("click", ".code-quick-action-row [aria-label='Удалить действие']"),
            ("click", ".code-quick-action-row [aria-label='Удалить действие']"),
            ("click", ".code-quick-action-row [aria-label='Удалить действие']"),
            ("click", "#code-quick-actions-save"),
            ("wait", "document.querySelector('#code-quick-actions-status').textContent.includes('сохранены')"),
            *go("code"), ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-new-chat')"),
            ("click", "#code-new-chat"), ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "disabled", "document.querySelector('#code-quick-actions').hidden && document.querySelectorAll('.code-quick-action').length===0")])
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["restored"] == code_actions.DEFAULTS[0]["text"]
    assert result["values"]["pendingHidden"] and result["values"]["hidden"] and result["values"]["disabled"]
    turns = [r for r in machine.requests if r["op"] == "turn"]
    assert len(turns) == 1 and turns[0]["text"] == code_actions.DEFAULTS[0]["text"]
    assert not known_refusals(problems(result), 409), problems(result)
