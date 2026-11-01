"""Кнопки Git выполняются в текущем чате; цвета и черновики переживают выполнение."""
import pytest

from fake_bridge import T0, pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine, code_online,
                             code_requests, code_session, code_tab_click, go, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("action, prefix", [("commit", "Закоммить"), ("push", "Запушь"),
                                           ("push-all", "Запушь"), ("deploy", "Запушь")])
@pytest.mark.parametrize("width", [320, 1280])
def test_git_action_creates_chat_once_without_an_active_tab(browser, live, action, prefix, width):
    login = f"git-new-{action}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_B: code_session('Прошлый чат')})
    machine.script = [{"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    gate = """(() => { const real = fetch; window.newChats = 0;
      window.fetch = async (url, options) => {
        if (options?.method === 'POST' && String(url).endsWith('/chats')) {
          window.newChats++;
          await new Promise(resolve => window.releaseNewChat = resolve);
        }
        return real(url, options);
      }; return true; })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go('code'),
            ("wait", "document.querySelector('#code-projects li[data-pid]')"),
            *go(f"code/{aid}/{pid('alpha')}?tab=git"),
            ("wait", "document.querySelector('#code-git-commit') && !document.querySelector('#code-git-commit').disabled"),
            ("eval", "gate", gate), ("click", f"#code-git-{action}"),
            ("wait", "typeof window.releaseNewChat === 'function'"),
            ("eval", "pending", "[...document.querySelectorAll('.code-git-actions button')].every(b => b.disabled)"),
            ("eval", "doubleClick", f"document.querySelector('#code-git-{action}').click(); window.newChats"),
            ("eval", "release", "window.releaseNewChat(); true"),
            ("wait", "document.querySelector('#code-chat')?.dataset.running === '0' && document.querySelector('#code-feed .code-msg.user')"),
            ("eval", "chat", "document.querySelector('#code-chat').dataset.chat"),
            ("eval", "tabs", "document.querySelectorAll('.code-tab').length"),
            ("eval", "created", "window.newChats")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["pending"] and values["doubleClick"] == values["created"] == values["tabs"] == 1
    [turn] = code_requests(machine, "turn")
    assert turn["chat"] == values["chat"] and turn["project"] == pid("alpha")
    assert turn["text"].startswith(prefix)
    assert ("затем задеплой" in turn["text"]) == (action == "deploy")
    assert not problems(result), problems(result)


@pytest.mark.parametrize('outcome', ['retry', 'leave'])
def test_git_chat_creation_handles_failure_and_leaving_the_project(browser, live, outcome):
    login = f'git-new-{outcome}'
    live.add_user(login)
    machine = code_free_machine()
    machine.projects['beta'] = {}
    machine.script = [{"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    gate = """(() => { const real = fetch; window.newChats = 0;
      window.fetch = async (url, options) => {
        if (options?.method === 'POST' && String(url).endsWith('/chats')) {
          window.newChats++;
          if (window.failNewChat) throw new Error('test network failure');
          await new Promise(resolve => window.releaseNewChat = resolve);
          const response = await real(url, options);
          window.chatCreated = true;
          return response;
        }
        return real(url, options);
      }; return true; })()"""
    try:
        actions = [*signin(login), ('wait', READY), *go('code'),
            ('wait', "document.querySelector('#code-projects li[data-pid]')"),
            *go(f"code/{aid}/{pid('alpha')}?tab=git"),
            ('wait', "document.querySelector('#code-git-commit') && !document.querySelector('#code-git-commit').disabled"),
            ('eval', 'gate', gate)]
        if outcome == 'retry':
            actions += [('eval', 'fail', 'window.failNewChat = true'), ('click', '#code-git-commit'),
                ('wait', "document.querySelector('#code-git-error')?.textContent.includes('Нет связи') && !document.querySelector('#code-git-commit').disabled"),
                ('eval', 'stillGit', "location.hash.endsWith('?tab=git') && !document.querySelector('#code-chat')"),
                ('eval', 'allow', 'window.failNewChat = false')]
        actions += [('click', '#code-git-commit'), ('wait', "typeof window.releaseNewChat === 'function'")]
        if outcome == 'leave':
            actions += [*go(f"code/{aid}/{pid('beta')}"),
                ('wait', "document.querySelector('.code-place-project')?.textContent === 'beta'")]
        actions += [('eval', 'release', 'window.releaseNewChat(); true'), ('wait', 'window.chatCreated')]
        if outcome == 'retry':
            actions += [('wait', "document.querySelector('#code-chat')?.dataset.running === '0' && document.querySelector('#code-feed .code-msg.user')")]
        actions += [('eval', 'path', 'location.hash'), ('eval', 'created', 'window.newChats')]
        result = browser.drive(live.url + '/', actions)
    finally:
        server.stop()
    assert not result.get('failed'), result
    values = result['values']
    if outcome == 'retry':
        assert values['stillGit'] and values['created'] == 2
        [turn] = code_requests(machine, 'turn')
        assert turn['project'] == pid('alpha') and turn['chat'] in values['path']
    else:
        assert values['created'] == 1 and values['path'].endswith(pid('beta'))
        assert not code_requests(machine, 'turn')
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_git_actions_color_and_preserve_draft_in_current_chat(browser, live, width, tmp_path):
    login = f"git-actions-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Git"), CODE_CHAT_B: code_session("Другой чат")})
    machine.project_summary = True
    for name, changed, unpushed in [("alpha", 1, 2), ("beta", 0, 2), ("gamma", 0, 0)]:
        project = machine.projects.setdefault(name, {})
        project["summary"] = {"git": "ok", "changed": changed, "unpushed": unpushed,
            "findings": 0, "ideas": 0, "modified": T0, "checked_at": T0}
    machine.projects["alpha"]["git"] = {"branch": "main", "upstream": "origin/main", "ahead": 2,
        "behind": 0, "files": [{"path": "a.py", "status": "M"}]}
    original = machine._op_turn

    def turn(req, events):
        machine.script = [{"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
        project = machine.projects["alpha"]
        project["git"]["files"] = []
        project["git"]["ahead"] = 2 if machine.turns == 0 else 0
        project["summary"].update(changed=0, unpushed=project["git"]["ahead"])
        return original(req, events)

    machine._op_turn = turn
    aid, server = code_online(live, login, machine)
    gate = """(() => { const real = fetch; window.holdGit = true;
      window.fetch = async (url, options) => {
        if (window.holdGit && String(url).endsWith('/turn'))
          await new Promise(resolve => window.releaseGit = resolve);
        return real(url, options);
      }; return true; })()"""
    try:
        actions = [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_B), *code_chat(aid, CODE_CHAT_A),
            ("eval", "chatHasNoActions", "!document.querySelector('.code-git-actions')"),
            ("wait", "document.querySelectorAll('.code-project-git[data-git-level]:not([data-git-level=unknown])').length === 3"),
            ("eval", "colors", "[...document.querySelectorAll('.code-project-git')].map(n=>n.dataset.gitLevel)"),
            ("eval", "red", "document.querySelector('#code-repo-status').dataset.gitLevel"),
            set_value("#code-text", "Мой черновик"), ("eval", "gate", gate),
            ("click", "#code-repo-status"), ("wait", "!document.querySelector('#code-git-commit').disabled"),
            ("eval", "toolbar", """(() => {
              const toolbar = document.querySelector('.code-git-actions'), content = document.querySelector('#code-git');
              const buttons = [...toolbar.querySelectorAll('button')].map(b=>b.getBoundingClientRect());
              const r = toolbar.getBoundingClientRect();
              return { oneRow: buttons.every(b=>Math.abs(b.top-buttons[0].top)<1),
                aboveStatus: r.bottom <= content.getBoundingClientRect().top,
                inside: toolbar.parentElement === content.parentElement && !toolbar.closest('.code-top'),
                fits: document.documentElement.scrollWidth <= innerWidth,
                scrollable: toolbar.scrollWidth > toolbar.clientWidth };
            })()"""),
            ("screenshot", str(tmp_path / f"git-actions-{width}.png")),
            ("click", "#code-git-commit"), ("wait", "typeof window.releaseGit === 'function'"),
            ("eval", "busy", "document.querySelector('#code-send').disabled && !document.querySelector('.code-git-actions')?.getClientRects().length"),
            ("eval", "release", "window.holdGit=false; window.releaseGit(); true"),
            ("wait", "document.querySelector('#code-repo-status').dataset.gitLevel === 'yellow' && !document.querySelector('#code-send').disabled"),
            ("eval", "yellow", "getComputedStyle(document.querySelector('#code-repo-status')).color")]
        for count, action in enumerate(["push", "push-all", "deploy"], 2):
            actions += [("click", "#code-repo-status"), ("wait", "!document.querySelector('#code-git-commit').disabled"),
                ("click", f"#code-git-{action}"),
                ("wait", f"document.querySelectorAll('#code-feed .code-msg.user').length === {count} && !document.querySelector('#code-send').disabled && document.querySelector('#code-chat').dataset.running === '0'")]
        actions += [("wait", "document.querySelector('#code-repo-status').dataset.gitLevel === 'green'"),
            ("eval", "draft", "document.querySelector('#code-text').value"),
            *code_tab_click(CODE_CHAT_B), *code_tab_click(CODE_CHAT_A),
            ("eval", "draftAfterReturn", "document.querySelector('#code-text').value"),
            ("eval", "green", "getComputedStyle(document.querySelector('#code-repo-status')).color"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth")]
        result = browser.drive(live.url + "/", actions, width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["chatHasNoActions"]
    assert all(values["toolbar"][key] for key in ("oneRow", "aboveStatus", "inside", "fits")), values
    assert not values["toolbar"]["scrollable"]
    assert values["colors"] == ["red", "yellow", "green"] and values["red"] == "red"
    assert values["yellow"] != values["green"] and values["busy"] and values["fits"]
    assert values["draft"] == values["draftAfterReturn"] == "Мой черновик"
    turns = code_requests(machine, "turn")
    assert len(turns) == 4 and all(t["chat"] == CODE_CHAT_A and t["project"] == pid("alpha") for t in turns)
    assert len({t["text"] for t in turns}) == 4
    assert turns[0]["text"].startswith("Закоммить") and "Не пушь" in turns[0]["text"]
    assert "в основной удалённый репозиторий" in turns[1]["text"]
    assert "во все предназначенные для push" in turns[2]["text"] and "Не создавай коммит" in turns[2]["text"]
    assert "затем задеплой" in turns[3]["text"] and "не начинай деплой" in turns[3]["text"]
    assert not problems(result), problems(result)
