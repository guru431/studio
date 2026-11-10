"""Третий CLI в «Коде»: каталог, умолчания, разрешения и переходы в браузере."""

import pytest

from test_ui_browser import (
    API, NO_OVERFLOW, READY, browser, live, code_machine, code_online, code_open,
    code_project, code_requests, code_send, go, known_refusals, live_sql, problems, set_value, signin,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

MODEL = "local-provider/model-with-project-settings"
CATALOG = {"version": "2.2.4", "models": [{"id": MODEL, "label": "OpenCode model"}],
           "efforts": ["default", "adaptive"],
           "defaults": {"model": MODEL, "effort": "default", "access": "default"}}


@pytest.mark.parametrize("width", [320, 1160, 1280])
def test_code_composer_icons_and_controls_stay_in_one_row(browser, live, tmp_path, width):
    login = f"ui-composer-{width}"
    live.add_user(login)
    live_sql(live, "UPDATE users SET theme = 'light', code_defaults = ? WHERE login = ?",
             ('{"codex":{"model":"gpt-6.1-sol","effort":"xhigh","access":"danger-full-access"}}', login))
    machine = code_machine()
    machine.codex = {"version": "1", "models": [{"id": "gpt-6.1-sol", "label": "gpt-6.1-sol"}], "efforts": ["xhigh"]}
    machine.context = {"used": 36700, "limit": 100000, "percent": 36.7}
    machine.script = [{"k": "ask", "kind": "permission", "request_id": "p1", "tool": "Shell",
                       "input": {"command": "git status"}, "always": True}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    geometry = """(() => {
      const rect = (s) => document.querySelector(s).getBoundingClientRect();
      const selectors = ['#code-model-pick', '#code-effort-pick', '#code-access-pick', '#code-stop', '#code-send'];
      const boxes = selectors.map(s => document.querySelector(s)).filter(e => e.getClientRects().length)
        .map(e => e.getBoundingClientRect()), bar = rect('#code-composer .composer-bar');
      const attach = document.querySelector('#code-attach'), mic = document.querySelector('#code-composer .mic');
      return { row: boxes.every(b => b.top >= bar.top - 1 && b.bottom <= bar.bottom + 1),
        locked: [...document.querySelectorAll('.code-pick-trigger')].filter(b => b.getClientRects().length).every(b => b.disabled),
        fits: boxes.every(b => b.left >= bar.left - 1 && b.right <= bar.right + 1),
        controls: boxes.every(b => b.width >= 24),
        adjacent: attach.nextElementSibling === mic && Math.abs(rect('#code-attach').left - rect('#code-composer .mic').left) < 1,
        icon: !!attach.querySelector('svg') && !attach.textContent.trim(),
        always: !attach.hidden && !!attach.getAttribute('aria-label'),
        emptyInput: document.querySelector('#code-text').scrollHeight <= document.querySelector('#code-text').clientHeight + 1 };
    })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid),
            *code_project(aid, "alpha"), ("wait", "!!document.querySelector('#code-new-codex')"), ("click", "#code-new-codex"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "compact", "(() => { const r = s => document.querySelector(s).getBoundingClientRect(); "
                "return "
                "r('#code-effort').left - r('#code-model').right <= 12 && "
                "r('#code-access').left - r('#code-effort').right <= 12; })()"),
            ("eval", "idleStatus", "document.querySelector('#code-activity').textContent === 'Нет активной задачи'"),
            ("screenshot", str(tmp_path / f"composer-idle-{width}.png"), "#code-composer"),
            *code_send("Проверь проект"), ("wait", "!!document.querySelector('.code-ask-yes')"),
            ("eval", "geometry", geometry), ("eval", "fits", NO_OVERFLOW),
            ("screenshot", str(tmp_path / f"composer-{width}.png"), "#code-composer"),
            ("click", ".code-ask-yes"), ("wait", "document.querySelector('#code-stop').hidden"),
            ("eval", "emptyAttach", "!document.querySelector('#code-attach').hidden && document.querySelector('#code-text').value === ''")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert all(r["values"]["geometry"].values()), r["values"]["geometry"]
    assert r["values"]["fits"] and r["values"]["emptyAttach"]
    assert r["values"]["compact"] and r["values"]["idleStatus"], r["values"]
    assert not known_refusals(problems(r), 409), problems(r)


def test_codex_activity_thinking_tools_reply_reconnect_and_completion(browser, live):
    from fake_bridge import FakeMachine, pid
    from test_ui_browser import CODE_PIN

    class StatusMachine(FakeMachine):
        stages = [
            {"k": "activity", "state": "tool", "tool": "Shell"},
            {"k": "activity", "state": "responding"},
            {"k": "done", "status": "done"},
        ]

        def _op_read(self, req, events):
            result = super()._op_read(req, events)
            if self.running and self.stages:
                chat = next(iter(self.running))
                event = self.stages.pop(0)
                if event["k"] == "done":
                    self._finish(chat, events, event)
                else:
                    self.running[chat]["activity"] = {k: v for k, v in event.items() if k != "k"}
                    events.append(self._ev(chat, event))
            return result

    login = "ui-codex-activity"
    live.add_user(login)
    machine = StatusMachine(projects={"alpha": {"files": {"advance.txt": b"ok"}}, "beta": {}}, pin=CODE_PIN)
    machine.codex = {"version": "1", "models": [{"id": "gpt-test", "label": "GPT"}], "efforts": ["high"]}
    machine.script = []  # ход остаётся активным; стадии выдаём на явные запросы чтения тестового файла
    aid, sb = code_online(live, login, machine)
    advance = f"(async () => {API}.api('POST', '/api/code/agents/{aid}/projects/{pid('alpha')}/read', {{path:'advance.txt'}}))()"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-new-codex')"), ("click", "#code-new-codex"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            *code_send("Проверь проект"),
            ("wait", "document.querySelector('#code-activity')?.dataset.state === 'thinking'"),
            ("eval", "thinking", "document.querySelector('#code-activity').textContent"),
            ("eval", "advanceTool", advance),
            ("wait", "document.querySelector('#code-activity')?.dataset.state === 'tool'"),
            ("eval", "tool", "document.querySelector('#code-activity').textContent"),
            ("eval", "advanceReply", advance),
            ("wait", "document.querySelector('#code-activity')?.dataset.state === 'responding'"),
            ("eval", "reply", "document.querySelector('#code-activity').textContent"),
            *go("chat"), *go("code"),
            ("wait", "document.querySelector('#code-activity')?.dataset.state === 'responding'"),
            ("eval", "restored", "document.querySelector('#code-activity').textContent"),
            ("eval", "advanceDone", advance),
            ("wait", "document.querySelector('#code-activity')?.dataset.state === 'done'"),
            ("eval", "done", "[document.querySelector('#code-activity').textContent, document.querySelector('#code-stop').hidden]")],
            width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["thinking"] == "Думает…"
    assert r["values"]["tool"] == "Выполняет: Shell"
    assert r["values"]["reply"] == r["values"]["restored"] == "Пишет ответ…"
    assert r["values"]["done"] == ["Задача завершена", True]
    assert not known_refusals(problems(r), 409), problems(r)


def test_opencode_defaults_new_chat_permissions_and_session_menu(browser, live):
    """Настройки после перезагрузки и запросы чата остаются у OpenCode, вкладка Claude независима."""
    login = "ui-opencode"
    live.add_user(login)
    machine = code_machine()
    machine.opencode = CATALOG
    machine.script = [{"k": "ask", "kind": "permission", "request_id": "p1", "tool": "Shell",
                       "input": {"command": "git status"}, "always": True},
                      {"k": "text", "text": "Готово OpenCode"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/code"),
            ("wait", "!!document.querySelector('#code-defaults-save')"),
            ("eval", "settingsModels", "[...document.querySelector('#code-defaults-opencode-model').options].map(o => o.value)"),
            set_value("#code-defaults-opencode-model", MODEL),
            set_value("#code-defaults-opencode-effort", "adaptive"),
            set_value("#code-defaults-opencode-access", "default"),
            ("click", "#code-defaults-save"),
            ("wait", "document.querySelector('#code-defaults-status').textContent.includes('Умолчания сохранены')"),
            ("eval", "reload", "location.reload()"), ("wait", READY),
            ("wait", f"document.querySelector('#code-defaults-opencode-model')?.value === '{MODEL}'"),
            ("eval", "saved", f"(async () => (await {API}.api('GET', '/api/me')).code_defaults.opencode)()"),
            *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-new-opencode')"),
            ("click", "#code-new-opencode"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "chat", "location.hash.split('/').at(-1)"),
            ("eval", "engine", "document.querySelector('.code-tab[aria-selected=true] .code-tab-engine').textContent"),
            ("eval", "permanent", "[...document.querySelectorAll('.code-status-fixed > button')].filter(b=>!b.hidden).map(b=>b.id)"),
            ("eval", "unsupported", "document.querySelector('#code-subagents').disabled && document.querySelector('#code-cache').disabled"),
            ("eval", "picked", "[document.querySelector('#code-model').value, document.querySelector('#code-effort').value, document.querySelector('#code-access').value]"),
            *code_send("Проверь проект"), ("wait", "document.querySelector('.code-ask-yes')"),
            ("eval", "card", "document.querySelector('.code-ask-title').textContent"),
            ("click", ".code-ask-yes"),
            ("wait", "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Готово OpenCode')"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#code-tab-add"),
            ("wait", "!!document.querySelector('#code-tab-menu #code-new-opencode')"),
            ("eval", "menu", "document.querySelector('#code-tab-menu #code-new-opencode').textContent"),
            ("click", "#code-tab-menu #code-new-chat"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1' && document.querySelector('#code-chat')?.dataset.engine === 'claude'"),
            ("eval", "claude", "[...document.querySelector('#code-model').options].map(o => o.value)"),
            ("eval", "tabs", "[...document.querySelectorAll('#code-tabbar .code-tab-engine')].map(e => e.textContent)")],
            width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["settingsModels"] == ["", MODEL]
    assert v["saved"] == {"model": MODEL, "effort": "adaptive", "access": "default"}
    assert v["engine"] == "OpenCode" and v["picked"] == [MODEL, "adaptive", "default"]
    assert v['permanent'] == ['code-changed-files', 'code-active-commands', 'code-subagents', 'code-cache', 'code-context', 'code-usage']
    assert v['unsupported'] and not code_requests(machine, 'subagents')
    assert v["card"] == "OpenCode просит разрешения" and v["menu"] == "OpenCode"
    assert v["claude"] == ["", "opus", "sonnet"] and v["tabs"] == ["OpenCode", "Claude Code"]
    assert v["fits"]
    [turn] = code_requests(machine, "turn")
    assert (turn["engine"], turn["model"], turn["effort"], turn["access"]) == ("opencode", MODEL, "adaptive", "default")
    assert code_requests(machine, "answer")[-1]["engine"] == "opencode"
    assert all(req.get("engine") == "opencode" for req in code_requests(machine, "history") if req["chat"] == v["chat"])
    assert not known_refusals(problems(r), 409), problems(r)


def test_opencode_creation_is_disabled_without_the_machine_catalog(browser, live):
    login = "ui-opencode-unavailable"
    live.add_user(login)
    machine = code_machine()
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-new-chat')"),
            ("eval", "unavailable", "!document.querySelector('#code-new-opencode')"),
            ("click", "#code-new-chat"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("click", "#code-tab-add"),
            ("wait", "!!document.querySelector('#code-tab-menu #code-new-chat')"),
            ("eval", "menu", "!!document.querySelector('#code-tab-menu #code-new-opencode')"),
            ("eval", "fits", NO_OVERFLOW)], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["unavailable"] is True and v["menu"] is False and v["fits"] is True
    assert not known_refusals(problems(r), 409), problems(r)


def test_opencode_session_reopens_and_pages_its_own_history(browser, live):
    from fake_bridge import T0, pid
    login = "ui-opencode-history"
    live.add_user(login)
    chat = "55555555-5555-4555-8555-555555555555"
    messages = [{"id": f"m-{i}", "role": "user" if i % 2 == 0 else "assistant", "text": f"сообщение {i}",
                 "steps": [], "time": T0 + i} for i in range(110)]
    machine = code_machine({chat: {"engine": "opencode", "title": "OpenCode history", "created": T0,
                                  "modified": T0 + 109, "tag": None, "messages": messages}})
    machine.opencode = CATALOG
    aid, sb = code_online(live, login, machine)
    path = f"/api/code/agents/{aid}/projects/{pid('alpha')}/chats/{chat}"
    close = f"(async () => {{ await {API}.api('PATCH', '{path}?engine=opencode', {{ state: 'closed' }}); return true; }})()"
    count = "document.querySelectorAll('#code-feed .code-msg').length"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid),
            ("eval", "close", close), *code_project(aid, "alpha"),
            ("wait", f"!!document.querySelector('#code-chats li[data-chat=\"{chat}\"]')"),
            ("eval", "badge", "document.querySelector('#code-chats .code-engine').textContent"),
            ("click", f"#code-chats li[data-chat=\"{chat}\"] .code-chat-link"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "n", count),
            *[step for total in (42, 72, 102, 110) for step in
              (("click", "#code-more"), ("wait", f"{count} === {total}"))],
            ("eval", "first", "document.querySelector('#code-feed .code-msg').textContent.trim()"),
            ("eval", "reopened", f"(async () => (await {API}.api('GET', '{path.rsplit('/', 1)[0]}')).items.find(c => c.chat === '{chat}').state)()")],
            width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["badge"] == "OpenCode" and v["n"] == 12 and v["first"] == "сообщение 0" and v["reopened"] == "open"
    history = code_requests(machine, "history")
    assert all(q["engine"] == "opencode" for q in history)
    assert [q['before'] for q in history if q['before'] is not None] == [98, 68, 38, 8]
    assert any(q['before'] is None and q['limit'] == 12 for q in history)
    assert not known_refusals(problems(r), 409), problems(r)
