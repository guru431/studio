"""Новые рабочие инструменты в браузере: клавиатура, очередь, ветвление и изоляция каталогов."""
import pytest

from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine, code_online,
                             code_session, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_partial_context_and_empty_passport_routes_keep_truthful_status(browser, live):
    result = browser.drive(live.url + '/login', [('eval', 'status', """(async () => {
        const {contextView} = await import('/static/js/code-context.js');
        const {passportBlock} = await import('/static/js/passport.js');
        const host = document.createElement('div'); document.body.append(host);
        const view = contextView(host, {engine: 'opencode', chat: 'test', project: 'demo', messages: () => 0});
        try {
            view.update({used: 25, limit: 100, usage: {total: 50}});
            view.button.click();
            view.update({used: null, limit: -1, percent: null, usage: {total: 60}, cost: {amount: 0.5}});
            const values = Object.fromEntries([...host.querySelectorAll('#code-context-dialog dt')]
                .map(dt => [dt.textContent, dt.nextElementSibling.textContent]));
            const passport = passportBlock({conn: {kind: 'srvllm', passport: {checked: 1,
                routes: [], models: [], profile: 'srvllm'}}, section: 'chat', models: [], label: x => x});
            return {values, passport: passport.textContent, used: view.button.dataset.used};
        } finally { view.unmount(); host.remove(); }
    })()""")])
    assert not result.get('failed'), result
    status = result['values']['status']
    assert status['used'] == '25'
    assert status['values']['Общий размер контекста, токенов'] == '100'
    assert status['values']['Заполнено'] == '25%'
    assert status['values']['Токены завершённых ходов Студии'] == '60'
    assert 'адрес отвечает' not in status['passport']
    assert 'не проверяется: проба занимала бы карту' in status['passport']


def test_subagent_list_polls_only_during_a_turn_or_with_the_window_open(browser, live):
    """Опрос подзадач не идёт без хода: старые мосты считают каждый запрос действием и не закрываются."""
    result = browser.drive(live.url + '/login', [('eval', 'counts', """(async () => {
        const delay = window.setTimeout;
        window.setTimeout = (fn, ms, ...args) => delay(fn, ms === 10000 || ms === 2000 ? 40 : ms, ...args);
        let calls = 0;
        window.fetch = async () => { calls += 1; return new Response('{"items":[]}',
            { status: 200, headers: { 'content-type': 'application/json' } }); };
        const { subagentView } = await import('/static/js/code-subagents.js');
        const pause = (ms) => new Promise((r) => delay(r, ms));
        const host = document.createElement('div'); document.body.append(host);
        let running = false;
        const view = subagentView(host, { endpoint: (path) => '/api/x' + path, polling: () => running });
        const counts = {};
        try {
            await pause(500); counts.idle = calls;
            running = true; view.refresh(); await pause(500); counts.running = calls - counts.idle;
            running = false; await pause(200); const stopped = calls; await pause(500);
            counts.after = calls - stopped;
            view.button.click(); await pause(500); counts.dialog = calls - stopped;
            host.querySelector('dialog').close(); await pause(200); const closed = calls; await pause(500);
            counts.closed = calls - closed;
            return counts;
        } finally { view.unmount(); host.remove(); window.setTimeout = delay; }
    })()""")])
    assert not result.get('failed'), result
    counts = result['values']['counts']
    assert counts['idle'] == 1                        # только чтение при открытии чата
    assert counts['running'] >= 3 and counts['dialog'] >= 3
    assert counts['after'] == 0 and counts['closed'] == 0


@pytest.mark.parametrize("width", [320, 1280])
def test_suggestions_diffs_content_search_and_editable_queue(browser, live, width, tmp_path):
    login = f"workspace-tools-{width}"
    live.add_user(login)
    messages = [{"id": "source", "role": "user", "text": "Первый вопрос", "steps": [], "time": 1},
                {"id": "answer", "role": "assistant", "text": "Старое решение с маркером Needle", "time": 2,
                 "steps": [{"id": "edit", "tool": "Edit", "input": {"file_path": "src/util.py"}, "text": "ok",
                            "error": False, "changes": [{"path": "src/util.py", "before": "old", "after": "new"}]}]}]
    machine = code_free_machine({CODE_CHAT_A: code_session("Основной чат", messages=messages)})
    machine.file_suggestions = machine.chat_search = machine.queue_edit = True
    machine.projects["alpha"]["files"] = {"src/util.py": b"new"}
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "diff", "document.querySelector('.code-step').open=true; true"),
            ("eval", "diffText", "document.querySelector('.code-changes').textContent"),
            ("wait", "document.querySelector('#code-changed-files').textContent === '1'"),
            ("click", "#code-changed-files"), ("click", ".code-changed-file"),
            ("eval", "fileDiff", "document.querySelector('#code-changed-dialog').textContent"),
            ("click", "#code-changed-dialog .dialog-close"),
            ("click", "#code-text"), set_value("#code-text", "@util"), ("wait", "document.querySelector('#code-file-options button')"),
            ("press", "Enter"), ("eval", "inserted", "document.querySelector('#code-text').value"),
            ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-search-content')"),
            ("click", "#code-search-content"), set_value("#code-chat-search", "Needle"),
            ("wait", "!!document.querySelector('.code-search-snippet')"),
            ("eval", "snippet", "document.querySelector('.code-search-snippet').textContent"),
            ("click", f"#code-chats li[data-chat='{CODE_CHAT_A}'] button"),
            ("wait", f"document.querySelector('#code-chat')?.dataset.chat === '{CODE_CHAT_A}' && document.querySelector('#code-chat').dataset.loaded === '1'"),
            set_value("#code-text", "Основная задача"), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            set_value("#code-delivery", "queue"), set_value("#code-text", "После задачи"), ("click", "#code-send"),
            ("wait", "document.querySelector('.code-queue-preview')?.textContent === 'После задачи'"),
            ("click", "button[aria-label='Изменить ожидающее задание']"),
            ("wait", "!!document.querySelector('.code-queue-text')"),
            set_value(".code-queue-text", "Исправленное задание"),
            ("eval", "paused", "document.querySelector('#code-queue').textContent.includes('на паузе') && document.querySelector('#code-send').disabled"),
            ("screenshot", str(tmp_path / f"queue-edit-{width}.png")),
            ("click", ".code-queue-actions button"),
            ("wait", "document.querySelector('.code-queue-preview')?.textContent === 'Исправленное задание'"),
            ("click", "button[aria-label='Удалить ожидающее задание']"),
            ("wait", "document.querySelector('#code-queue').hidden"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("click", "#code-stop"), ("wait", "document.querySelector('#code-chat').dataset.running === '0'")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["inserted"] == "src/util.py "
    assert "old" in result["values"]["diffText"] and "new" in result["values"]["diffText"]
    assert "old" in result["values"]["fileDiff"] and "new" in result["values"]["fileDiff"]
    assert "Needle" in result["values"]["snippet"]
    assert result["values"]["paused"] and result["values"]["fits"]
    assert not problems(result), problems(result)
    assert not any("Needle" in url for url in result.get("sent", []))
    assert len([row for row in machine.requests if row["op"] == "turn"]) == 1


@pytest.mark.parametrize("width", [320, 1280])
def test_fork_subagents_and_worktree_browser_flows(browser, live, width, tmp_path):
    login = f"branch-tools-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Основной чат", messages=[
        {"id": "first", "role": "user", "text": "Измени постановку", "steps": [], "time": 1},
        {"id": "answer", "role": "assistant", "text": "Исходный ответ", "steps": [], "time": 2}])})
    machine.chat_fork = machine.subagents = machine.worktrees = True
    machine.projects["alpha"]["chats"][CODE_CHAT_A]["children"] = {
        "reviewer": {"title": "Проверка кода", "status": "running", "started_at": 1767268800,
                     "duration_ms": 3000, "total_tokens": 1500,
                     "messages": [{"role": "assistant", "text": "Результат подзадачи", "steps": []}]}}
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("click", "#code-subagents"), ("wait", "!!document.querySelector('.code-subagent-row')"),
            ("eval", "metrics", "document.querySelector('.code-subagent-metrics').textContent"),
            ("eval", "active", "document.querySelector('.code-subagent-row').dataset.status === 'running' && getComputedStyle(document.querySelector('.code-subagent-row')).boxShadow !== 'none'"),
            ("click", ".code-subagent-row"), ("wait", "document.querySelector('.code-subagent-history').textContent.includes('Результат подзадачи')"),
            ("eval", "child", "document.querySelector('.code-subagent-history').textContent"),
            ("click", "#code-subagents-dialog .dialog-actions button"),
            ("click", ".code-msg.user .code-fork"),
            ("wait", f"document.querySelector('#code-chat')?.dataset.loaded === '1' && document.querySelector('#code-chat').dataset.chat !== '{CODE_CHAT_A}'"),
            ("eval", "draft", "document.querySelector('#code-text').value"),
            ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-worktrees')"),
            ("click", "#code-worktrees"), ("wait", "!!document.querySelector('.code-worktree-row')"),
            set_value("#code-worktree-name", "task"), ("click", ".code-worktree-form button"),
            ("wait", f"location.hash.includes('/{pid('alpha--task')}') && !!document.querySelector('#code-new-chat')"),
            ("wait", "document.querySelector('#code-place .code-place-project')?.textContent === 'alpha--task'"),
            ("eval", "copy", "location.hash"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("screenshot", str(tmp_path / f"worktree-{width}.png"))], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["draft"] == "Измени постановку"
    assert result["values"]["active"]
    assert all(word in result["values"]["metrics"] for word in ("Запуск:", "Длительность:", "Токены:", "1\u00a0500"))
    assert result["values"]["fits"] and "Результат подзадачи" in result["values"]["child"]
    assert len(machine.projects["alpha"]["chats"][CODE_CHAT_A]["messages"]) == 2
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_second_turn_in_a_project_warns_and_runs_in_a_separate_worktree(browser, live, width, tmp_path):
    """Другой чат проекта работает: отправка сначала предупреждает; 1280 — «Запустить в отдельной копии» (worktree,
    новый чат копии, задание уходит туда), 320 — «Отправить здесь» (обычный ход в этом чате)."""
    login = f"busy-project-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Основной чат"), CODE_CHAT_B: code_session("Второй чат")})
    machine.project_activity = machine.worktrees = True
    machine.running[CODE_CHAT_B] = {"turn": "t-b", "project": pid("alpha"), "rest": [], "text": "", "steps": [],
                                    "asks": {}, "followups": []}
    machine.script = [{"k": "text", "text": "Принято"}]
    aid, server = code_online(live, login, machine)
    action = "#code-busy-copy" if width == 1280 else "#code-busy-send"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "poll", "import('/static/js/code-activity.js').then((m) => { m.refreshCodeActivity(); return true; })"),
            ("wait", f"import('/static/js/code-activity.js').then((m) => m.codeActivity({aid}).some((i) => i.running))"),
            set_value("#code-text", "Почини сборку"), ("click", "#code-send"),
            ("wait", "!document.querySelector('#code-busy-notice').hidden"),
            ("eval", "notice", "document.querySelector('#code-busy-notice').textContent"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("screenshot", str(tmp_path / f"busy-project-{width}.png")),
            ("click", action),
            ("wait", "document.querySelector('#code-feed')?.textContent.includes('Почини сборку')"
                     " && document.querySelector('#code-busy-notice').hidden"),
            ("eval", "hash", "location.hash")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert "В проекте идёт ещё заданий: 1" in result["values"]["notice"] and result["values"]["fits"]
    turns = [row for row in machine.requests if row["op"] == "turn"]
    assert [row["text"] for row in turns] == ["Почини сборку"]
    if width == 1280:
        copy = next(name for name in machine.projects if name.startswith("alpha--task-"))
        assert turns[0]["project"] == pid(copy) and pid(copy) in result["values"]["hash"]
        assert [row["query"] for row in machine.requests if row["op"] == "worktree_create"] == [copy[len("alpha--"):]]
    else:
        assert turns[0]["project"] == pid("alpha") and turns[0]["chat"] == CODE_CHAT_A
        assert not any(row["op"] == "worktree_create" for row in machine.requests)
    assert not problems(result), problems(result)
