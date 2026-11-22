"""Четвёртый помощник «Агентов» — DeepSeek Harness в браузере: новый чат и права, панель настроек профиля,
баланс, скрытые ветки и подагенты, заметка нечитаемой истории и отказ «чат открыт в DeepSeek Harness»."""

import pytest

from fake_bridge import Refused, pid
from test_deepseek import CATALOG, MODEL, USAGE
from test_ui_browser import (
    CODE_CHAT_A, NO_OVERFLOW, READY, browser, code_chat, code_free_machine, code_online, code_project,
    code_requests, code_send, code_session, go, known_refusals, live, problems, set_value, signin,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

OTHER = '["srvllm","gemma-4"]'
WRITER = "Чат открыт в DeepSeek Harness на компьютере — закройте DeepSeek Harness и повторите"
LOADED = "document.querySelector('#code-chat')?.dataset.loaded === '1'"
PANEL = "document.querySelector('#code-deepseek-options')?.textContent || ''"


def machine_with(chats=None):
    machine = code_free_machine(chats)
    machine.deepseek = CATALOG
    return machine


def deepseek_chat(title="Чат DeepSeek", messages=None):
    return {**code_session(title, messages=messages), "engine": "deepseek"}


def new_deepseek_chat(aid):
    return [*go("code"), ("wait", "document.querySelector('#code-projects li[data-pid]')"), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-new-deepseek')"),
            ("eval", "starter", "document.querySelector('#code-new-deepseek').textContent"),
            ("eval", "starters", """(() => {
              const row = document.querySelector('.code-chat-starters'), r = row.getBoundingClientRect();
              return [...row.children].every(b => { const x = b.getBoundingClientRect();
                return x.left >= r.left && x.right <= r.right + 1 && b.scrollWidth <= b.clientWidth + 1; });
            })()"""),
            ("click", "#code-new-deepseek"),
            ("wait", f"{LOADED} && document.querySelector('#code-chat').dataset.engine === 'deepseek'")]


@pytest.mark.parametrize("width", [320, 1280])
def test_new_deepseek_chat_turn_with_permission_card(browser, live, width, tmp_path):
    """Новый чат: права выбираются до первого хода, карточка разрешения, после хода права закреплены и второй
    ход шлёт то же значение, что ответил мост."""
    login = f"ui-deepseek-new-{width}"
    live.add_user(login)
    machine = machine_with()
    turns = [[{"k": "settings", "model": MODEL, "effort": "high", "access": "read-only", "access_fixed": True,
               "overlay": CATALOG["settings"]},
              {"k": "ask", "kind": "permission", "request_id": "p1", "tool": "Shell",
               "input": {"command": "git status"}, "always": True},
              {"k": "text", "text": "Готово DeepSeek"}, {"k": "done", "status": "done"}],
             [{"k": "text", "text": "Второй ответ"}, {"k": "done", "status": "done"}]]
    original = machine._op_turn

    def turn(req, events):
        machine.script = turns.pop(0) if turns else []
        return original(req, events)

    machine._op_turn = turn
    state = machine._op_state

    def fixed_state(req, events):       # как мост: у чата с сессией права закреплены (access_fixed)
        data = state(req, events)
        if machine.projects["alpha"]["chats"].get(req["chat"], {}).get("messages"):
            data["settings"]["access_fixed"] = True
        return data

    machine._op_state = fixed_state
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *new_deepseek_chat(aid),
            ("eval", "engine", "document.querySelector('.code-tab[aria-selected=true] .code-tab-engine').textContent"),
            ("eval", "access", "[...document.querySelector('#code-access').options].slice(1).map(o => [o.value, o.textContent])"),
            ("eval", "free", "!document.querySelector('#code-access').disabled"),
            ("eval", "set", "(() => { const e = document.querySelector('#code-access'); e.value = 'read-only';"
                            " e.dispatchEvent(new Event('change', {bubbles: true})); return e.value; })()"),
            *code_send("Проверь проект"), ("wait", "document.querySelector('.code-ask-yes')"),
            ("eval", "card", "document.querySelector('.code-ask-title').textContent"),
            ("screenshot", str(tmp_path / f"deepseek-ask-{width}.png")),
            ("click", ".code-ask-yes"),
            ("wait", "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Готово DeepSeek')"),
            ("wait", "document.querySelector('#code-access').disabled"),
            ("eval", "fixed", "[document.querySelector('#code-access').value, document.querySelector('#code-access-pick').disabled,"
                              " !document.querySelector('.code-deepseek-fixed').hidden,"
                              " document.querySelector('.code-deepseek-fixed').textContent]"),
            ("eval", "fits", NO_OVERFLOW),
            *code_send("Ещё"),
            ("wait", "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Второй ответ')")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["starter"] == "DeepSeek" and v["starters"] and v["engine"] == "DeepSeek Harness"
    assert v["access"] == [["default", "По настройкам DeepSeek Harness"], ["read-only", "Только чтение"],
                           ["workspace-write", "Правка в папке проекта"], ["danger-full-access", "Полный доступ"]]
    assert v["free"] and v["card"] == "DeepSeek Harness просит разрешения"
    assert v["fixed"] == ["read-only", True, True, "Права заданы при создании чата"] and v["fits"]
    first, second = code_requests(machine, "turn")
    assert (first["engine"], first["access"]) == ("deepseek", "read-only")
    assert (second["engine"], second["access"]) == ("deepseek", "read-only")
    assert code_requests(machine, "answer")[-1]["engine"] == "deepseek"
    assert not known_refusals(problems(r), 409), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_settings_panel_lists_not_applied_items(browser, live, width, tmp_path):
    """Сводка из hello, затем сводка state: профиль-источник и «Не применено:» с причинами словами."""
    login = f"ui-deepseek-panel-{width}"
    live.add_user(login)
    machine = machine_with()
    overlay = {"profile": "desktop", "applied": [],
               "skipped": [{"name": "llm-pi-ai:custom", "reason": "env_key"}, {"name": "model", "reason": "acp_own"},
                           {"name": "hooks", "reason": "unknown"}]}
    original = machine._op_state

    def state(req, events):
        data = original(req, events)
        data["settings"]["overlay"] = overlay
        return data

    machine._op_state = state
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *new_deepseek_chat(aid),
            ("wait", f"({PANEL}).includes('профиля Desktop')"),
            ("eval", "panel", PANEL),
            ("eval", "items", "[...document.querySelectorAll('.code-deepseek-skipped li')].map(li => li.textContent)"),
            ("eval", "open", "document.querySelector('#code-deepseek-options').open = true; true"),
            ("eval", "fits", NO_OVERFLOW),
            ("eval", "inside", "(() => { const p = document.querySelector('#code-deepseek-options').getBoundingClientRect();"
                               " return p.left >= 0 && p.right <= innerWidth; })()"),
            ("screenshot", str(tmp_path / f"deepseek-panel-{width}.png"))],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["panel"].startswith("Настройки DeepSeek Harness") and "Настройки взяты из профиля Desktop" in v["panel"]
    assert "Не применено:" in v["panel"]
    assert v["items"] == ["llm-pi-ai:custom — ключ из переменной окружения недоступен мосту",
                          "model — задано в профиле acp или общем патче DSH", "hooks — неизвестный вид настройки"]
    assert v["fits"] and v["inside"]
    assert not known_refusals(problems(r), 409), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_balance_panel_shows_currencies(browser, live, width, tmp_path):
    login = f"ui-deepseek-balance-{width}"
    live.add_user(login)
    machine = machine_with({CODE_CHAT_A: deepseek_chat()})
    machine.deepseek = {**CATALOG, "models": [*CATALOG["models"], {"id": OTHER, "label": "Gateway"}]}
    machine.subscription_usage = machine.deepseek_usage = True
    machine.usage_data = {**USAGE, "balance": {"available": True, "items": [
        {"currency": "CNY", "total": 10.5, "granted": 0.5, "topped_up": 10.0},
        {"currency": "USD", "total": 1.25, "granted": None, "topped_up": 1.25}]}}
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "document.querySelector('#code-usage-remaining')?.textContent !== '—'"),
            ("eval", "button", "[document.querySelector('#code-usage').textContent, document.querySelector('#code-usage').dataset.level]"),
            ("click", "#code-usage"),
            ("wait", "document.querySelector('#code-usage-dialog .code-balance')"),
            ("eval", "title", "document.querySelector('#code-usage-title').textContent"),
            ("eval", "rows", "[...document.querySelectorAll('#code-usage-dialog .code-balance .code-quota')].map(e => e.textContent)"),
            ("eval", "link", "document.querySelector('#code-usage-dialog a')?.textContent"),
            ("eval", "fits", "(() => { const d = document.querySelector('#code-usage-dialog');"
                             " return d.scrollWidth <= d.clientWidth && document.documentElement.scrollWidth <= innerWidth; })()"),
            ("screenshot", str(tmp_path / f"deepseek-balance-{width}.png")),
            ("eval", "close", "document.querySelector('#code-usage-dialog').close(); true"),
            ("eval", "label", "document.querySelector('#code-model').options[0].textContent"),
            set_value("#code-model", OTHER),
            ("wait", "performance.getEntriesByType('resource').some(e => e.name.includes("
                     f"'model=' + encodeURIComponent({OTHER!r})))")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["button"] == ["Баланс10,5 CNY", "green"]
    assert v["title"] == "Баланс DeepSeek" and v["link"] == "Баланс DeepSeek"
    assert v["rows"] == ["CNY10,5Подарено: 0,5 · пополнено: 10", "USD1,25Подарено: — · пополнено: 1,25"]
    assert v["fits"] and v["label"] == "DeepSeek Chat"
    usage = [q.get("model") for q in code_requests(machine, "usage") if q.get("engine") == "deepseek"]
    assert MODEL in usage and usage[-1] == OTHER
    assert not known_refusals(problems(r), 409), problems(r)


def test_fork_subagents_and_steer_hidden_for_deepseek(browser, live):
    """Ветки и подагенты не предлагаются, уточнение во время хода — только «Отправить после ответа»."""
    login = "ui-deepseek-queue"
    live.add_user(login)
    messages = [{"id": "m-0", "role": "user", "text": "Первое задание", "steps": [], "time": 1},
                {"id": "m-1", "role": "assistant", "text": "Ответ", "steps": [], "time": 2}]
    machine = machine_with({CODE_CHAT_A: deepseek_chat(messages=messages)})
    machine.chat_fork = machine.subagents = machine.queue_edit = True
    machine.script = [{"k": "text", "text": "Работаю"}]           # ход остаётся идущим
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "branches", "[document.querySelectorAll('.code-fork').length, !!document.querySelector('#code-fork-chat')]"),
            ("eval", "subagents", "document.querySelector('#code-subagents').hidden"),
            *code_send("Проверь проект"), ("wait", "!document.querySelector('#code-stop').hidden"),
            ("wait", "document.querySelector('#code-send .go-label').textContent === 'Отправить после ответа'"),
            ("eval", "delivery", "!document.querySelector('#code-delivery-pick').getClientRects().length"),
            ("eval", "fits", NO_OVERFLOW),
            *code_send("И ещё тесты"),
            ("wait", "document.querySelector('#code-status').textContent.includes('после текущего ответа')")],
            width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["branches"] == [0, False] and v["subagents"] is True and v["delivery"] is True and v["fits"]
    [steer] = code_requests(machine, "steer")
    assert steer["engine"] == "deepseek" and "mode" not in steer
    assert not code_requests(machine, "subagents") and not code_requests(machine, "chat_fork")
    assert not known_refusals(problems(r), 409), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_history_notice_for_unsupported_format(browser, live, width):
    login = f"ui-deepseek-notice-{width}"
    live.add_user(login)
    machine = machine_with({CODE_CHAT_A: deepseek_chat()})
    machine._op_history = lambda req, events: {"items": [], "start": 0, "total": 0, "notice": "unsupported_format"}
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "notice", "(() => { const n = document.querySelector('#code-history-notice');"
                               " return [n.hidden, n.textContent, n.getClientRects().length > 0]; })()"),
            ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["notice"] == [False, "Историю этой версии DeepSeek Harness студия пока не читает", True] and v["fits"]
    assert not known_refusals(problems(r), 409), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_writer_active_message_is_shown(browser, live, width, tmp_path):
    """Отказ state (409 writer_active) — плашка со словами моста и «Проверить снова»; отказ хода — та же плашка,
    задание возвращено в поле."""
    login = f"ui-deepseek-writer-{width}"
    live.add_user(login)
    machine = machine_with({CODE_CHAT_A: deepseek_chat()})
    original = machine._op_state
    refusals = [Refused("writer_active", WRITER)]

    def state(req, events):
        if refusals:
            raise refusals.pop()
        return original(req, events)

    machine._op_state = state

    def refused_turn(req, events):      # как мост: сессию держит DSH Web — ход не начат, задание не принято
        events.append({"t": "ev", "project": req["project"], "chat": req["chat"], "turn": "rejected",
                       "e": {"k": "done", "status": "error", "message": WRITER, "code": "writer_active",
                             "not_started": True}})
        return {"turn": "rejected"}

    machine._op_turn = refused_turn
    aid, sb = code_online(live, login, machine)
    notice = "(() => { const n = document.querySelector('#code-writer-notice'); return n.hidden ? null : n.textContent; })()"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "!document.querySelector('#code-writer-notice').hidden"),
            ("eval", "state", notice), ("eval", "fits", NO_OVERFLOW),
            ("screenshot", str(tmp_path / f"deepseek-writer-{width}.png")),
            ("click", "#code-writer-retry"),
            ("wait", "document.querySelector('#code-writer-notice').hidden && !document.querySelector('#code-send').disabled"),
            *code_send("Продолжи"),
            ("wait", "!document.querySelector('#code-writer-notice').hidden && document.querySelector('#code-stop').hidden"),
            ("eval", "turn", notice), ("eval", "text", "document.querySelector('#code-text').value")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["state"] == v["turn"] == WRITER + "Проверить снова"
    assert v["text"] == "Продолжи" and v["fits"]
    assert code_requests(machine, "turn")[0]["engine"] == "deepseek"
    assert not known_refusals(problems(r), 409), problems(r)
