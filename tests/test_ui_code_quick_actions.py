"""Быстрые действия нового чата и расположение панели задания. Быстрые действия — навыки с отметкой «Кнопкой в
пустом чате» («Настройки → Навыки», skills.js)."""
import pytest

import code_actions
from test_ui_browser import (API, CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine,  # noqa: F401
                             code_online, code_session, code_tab_click, go, is_open, known_refusals, live, problems,
                             set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

SKILLS = "[...document.querySelectorAll('#skills-settings .skill-item-name')].map(e => e.textContent)"


def _item(name: str) -> str:
    return (f"[...document.querySelectorAll('#skills-settings .skill-item')]"
            f".find(e => e.querySelector('.skill-item-name').textContent === {name!r})")


def edit(name: str) -> list:
    return [("eval", "edit", f"{_item(name)}.querySelector('.skill-edit').click()"), ("wait", is_open("#skill-dialog"))]


def remove(name: str) -> list:
    return [("eval", "remove", f"{_item(name)}.querySelector('.skill-delete').click()"), ("wait", is_open("#skill-remove")),
            ("click", "#skill-remove-confirm"), ("wait", f"!{is_open('#skill-remove')} && !{_item(name)}")]


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
      const history = document.querySelector('.code-chat-history');
      const buttons = ['#code-subagents', '#code-cache', '#code-context', '#code-usage'].map(s => document.querySelector(s));
      return { bottom: Math.abs(composer.bottom - rect.bottom + padding) < 20,
        navigationLast: history.lastElementChild === document.querySelector('.code-message-nav'),
        toolbar: !!toolbar && buttons.every(b => toolbar.contains(b)) && !toolbar.closest('form'),
        icons: !!buttons[0].querySelector('svg') && !!buttons[0].getAttribute('aria-label'),
        order: buttons.every((b,i) => !i || b.getBoundingClientRect().left >= buttons[i-1].getBoundingClientRect().right),
        fits: document.documentElement.scrollWidth <= innerWidth };
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "empty", shape), ("screenshot", str(tmp_path / f"empty-chat-{width}.png")),
            set_value("#code-text", "Начать работу"), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "delivery", """(() => {
              const rights = document.querySelector('#code-access').closest('.pick').getBoundingClientRect();
              const delivery = document.querySelector('#code-delivery').getBoundingClientRect();
              return delivery.left >= rights.right && Math.abs(delivery.top - rights.top) < 2
                && !!document.querySelector('#code-composer #code-delivery');
            })()"""), ("eval", "working", shape),
            ("screenshot", str(tmp_path / f"working-chat-{width}.png")),
            ("click", "#code-stop"), ("wait", "document.querySelector('#code-chat').dataset.running === '0'"),
            *code_chat(aid, CODE_CHAT_B),
            ("eval", "oldBottom", "(() => { const v = document.querySelector('#code-chat-scroll'); return v.scrollTop > 1000 && v.scrollHeight-v.clientHeight-v.scrollTop < 2; })()"),
            ("eval", "scrollUp", "document.querySelector('#code-chat-scroll').scrollTop=0; true"),
            ("wait", "document.querySelector('#code-chat-scroll').scrollTop === 0"),
            ("eval", "scrolled", shape),
            *code_tab_click(CODE_CHAT_A), *code_tab_click(CODE_CHAT_B),
            ("eval", "reopenedBottom", "(() => { const v = document.querySelector('#code-chat-scroll'); return v.scrollTop > 1000 && v.scrollHeight-v.clientHeight-v.scrollTop < 2; })()")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert all(values["empty"].values()), str(values)
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
    title = "Моё действие " + "с длинным названием " * 3
    try:
        # быстрые действия — навыки с отметкой «Кнопкой в пустом чате» на странице «Настройки → Навыки»
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/skills"),
            ("wait", f"{SKILLS}.length === 9"),
            ("eval", "defaults", f"(async () => (await {API}.api('GET','/api/me')).code_quick_actions.map(a => a.text))()"),
            *edit(code_actions.DEFAULTS[0]["title"]), set_value("#skill-name", title), set_value("#skill-text", prompt),
            ("click", "#skill-save"), ("wait", f"!{is_open('#skill-dialog')} && {SKILLS}[4] === {title.strip()!r}"),
            *remove(code_actions.DEFAULTS[2]["title"]),
            ("click", "#skills-settings-add"), ("wait", is_open("#skill-dialog")),
            set_value("#skill-name", "Добавленное действие"), set_value("#skill-text", "Добавленный текст"),
            set_value("#skill-scope", "agents"), ("click", "#skill-quick"), ("click", "#skill-save"),
            ("wait", f"!{is_open('#skill-dialog')} && {SKILLS}.at(-1) === 'Добавленное действие'"),
            ("eval", "reload", "location.reload(); true"), ("wait", READY), ("wait", f"{SKILLS}.length === 9"),
            ("eval", "kept", f"""(async () => {{ const m = {API};
              const skill = (await m.api('GET', '/api/skills?scope=agents')).items.find(s => s.name === {title.strip()!r});
              return (await m.api('GET', '/api/skills/' + skill.id)).text; }})()"""),
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
    assert values["kept"] == prompt
    assert values["titles"] and values["hidden"] and values["stillHidden"]
    assert values["stored"][0]["text"] == prompt and values["stored"][-1]["text"] == "Добавленный текст"
    turns = [r for r in machine.requests if r["op"] == "turn"]
    assert len(turns) == 1 and turns[0]["text"] == prompt
    assert not problems(result), problems(result)


def test_quick_action_keeps_the_typed_draft_and_can_be_retried(browser, live):
    """Быстрое действие уходит мимо поля: набранный черновик остаётся и при отказе, и после хода; отказ до начала —
    кнопка снова доступна, повтор отправляет то же действие."""
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
            ("eval", "reject", reject), set_value("#code-text", "Мой черновик"), ("click", ".code-quick-action"),
            ("wait", "!!window.rejectTurn"),
            ("eval", "pendingHidden", "document.querySelector('#code-quick-actions').hidden"),
            ("eval", "release", "window.rejectTurn(); true"),
            ("wait", "!document.querySelector('#code-quick-actions').hidden && !document.querySelector('#code-send').disabled"),
            ("eval", "restored", "document.querySelector('#code-text').value"),
            ("click", ".code-quick-action"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '0' && document.querySelector('.code-msg.assistant .md')?.textContent.includes('Готово')"),
            ("eval", "hidden", "document.querySelector('#code-quick-actions').hidden"),
            ("eval", "kept", "document.querySelector('#code-text').value"),
            *go("settings/skills"), ("wait", f"{SKILLS}.length === 9"),
            *[step for action in code_actions.DEFAULTS for step in remove(action["title"])],
            ("wait", f"{SKILLS}.length === 6"),
            *go("code"), ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-new-chat')"),
            ("click", "#code-new-chat"), ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "disabled", "document.querySelector('#code-quick-actions').hidden && document.querySelectorAll('.code-quick-action').length===0")])
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["restored"] == result["values"]["kept"] == "Мой черновик"
    assert result["values"]["pendingHidden"] and result["values"]["hidden"] and result["values"]["disabled"]
    turns = [r for r in machine.requests if r["op"] == "turn"]
    assert len(turns) == 1 and turns[0]["text"] == code_actions.DEFAULTS[0]["text"]
    assert not known_refusals(problems(result), 409), problems(result)
