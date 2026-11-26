"""Project navigation, chat transfer and upload in the actual Studio browser shell."""
import json

import pytest

from live_app import start_live_app
from test_ui_browser import API, READY, live_sql, problems, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = start_live_app(tmp_path_factory.mktemp("studio-live-projects"))
    yield app
    app.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_create_a_project_move_a_chat_and_upload_multiple_files(browser, live, width):
    mobile = width == 320
    login = f"projects-{width}"
    uid = live.add_user(login)
    cid = f"a{width}"
    live_sql(live, "INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, 'Старый диалог', 1, 1)", (cid, uid))
    drawer = [("click", "#chats-toggle")] if mobile else []
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *drawer,
        ("wait", "document.querySelector('.project-list li:first-child[data-inbox] > .project-link')"
         " && document.querySelector('.project-history:not([hidden]) .chat-item')"),
        ("eval", "layout", """(() => {
          const panel = document.querySelector('#chats');
          const footer = panel.querySelector('.code-panel-footer');
          const main = document.querySelector('.project-workspace .code-main');
          const tools = document.querySelector('.code-head .project-actions');
          const plus = document.querySelector('#chat-tab-add');
          const create = footer.querySelector('.project-create');
          return {
            footer: Math.abs(footer.getBoundingClientRect().bottom - panel.getBoundingClientRect().bottom) < 2,
            left: !panel.querySelector('.chat-item') && !!main.querySelector('.project-history .code-chat-link'),
            toolbar: [...tools.children].map(a => a.textContent),
            plus: plus.parentElement.id === 'chat-tabbar' && !!main.querySelector('.project-history .code-new-chat'),
            create_label: getComputedStyle(create.querySelector('.code-project-create-label')).display !== 'none',
            create_icon: getComputedStyle(create.querySelector('.ico')).display !== 'none',
            create_name: create.getAttribute('aria-label'),
          };
        })()"""),
        ("click", ".project-create"), ("type", "#chat-project-editor input[required]", "Каталог"),
        ("click", "#chat-project-editor .btn-accent"),
        ("wait", "document.querySelector('#chat-title')?.textContent === 'Каталог'"),
        ("wait", "document.querySelector('.project-history:not([hidden]) .chat-list-note')?.textContent === 'Диалогов пока нет'"),
        ("click", "#chat-tab-add"),
        ("wait", "location.hash.includes('history=1') && !document.querySelector('.project-history').hidden"),
        ("click", "#chat-new"),
        ("wait", "location.hash.includes('new=1') && !document.querySelector('.project-conversation').hidden"),
        ("click", "#chat-tab-add"),
        ("wait", "!document.querySelector('.project-history').hidden"),
        *drawer, ("click", ".project-list [data-inbox] > a"),
        ("wait", "document.querySelector('.project-list [data-inbox] [aria-current=page]')"
         " && document.querySelector('.project-history:not([hidden]) .chat-item')"),
        ("click", f".project-history .chat-item[data-id='{cid}'] .chat-link"),
        ("wait", "document.querySelector('#chat-title')?.textContent === 'Старый диалог'"),
        ("eval", "reveal_actions", """(() => {
          if (document.querySelector('#chat-title-actions').closest('#chat-status-menu:not(:popover-open)')) {
            document.querySelector('#chat-status-more').click();
          }
          return true;
        })()"""),
        ("click", "#chat-title-actions"), ("click", "#chat-title-move-project"), ("click", "#choose-chat-project .btn-accent"),
        ("wait", "!document.querySelector('#choose-chat-project[open]')"
         " && document.querySelector('.project-list [aria-current=page] .code-project-name')?.textContent === 'Каталог'"),
        ("click", ".code-head .project-actions a[href^='#/myfiles']"),
        ("wait", "document.querySelector('#myfiles-tabs [aria-selected=true]')?.textContent === 'Каталог'"),
        ("eval", "upload", """(() => {
          const input = document.querySelector('#project-files-picker');
          const picked = new DataTransfer();
          for (const name of ['one.txt', 'two.txt']) picked.items.add(new File([name], name, {type:'text/plain'}));
          input.files = picked.files;
          const count = input.files.length;
          input.dispatchEvent(new Event('change'));
          return count;
        })()"""),
        ("wait", "document.querySelectorAll('#myfiles-list .myfiles-item').length === 2"),
        ("eval", "add_for_sort", f"(async () => (await {API}.api('POST', '/api/chat-projects', {{name: 'Зебра'}})).id)()"),
        ("eval", "projects", "location.hash = '#/chat?history=1'"),
        ("wait", "document.querySelectorAll('.project-list .project-link').length === 3"),
        *drawer, ("click", "#chat-project-sort"),
        ("wait", "document.querySelector('.code-project-sort-menu:popover-open')"),
        ("click", ".code-project-sort-menu label:has(input[value=name])"),
        ("eval", "sorted", "[...document.querySelectorAll('.project-list .code-project-name')].map(n => n.textContent)"),
        ("eval", "saved", "Object.entries(localStorage).find(([key]) => key.startsWith('studio-chat-project-sort:'))?.[1]"),
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth")], width=width, mobile=mobile)
    assert not result.get("failed"), (result.get("failed"), result.get("where"), result.get("log"))
    assert result["values"]["upload"] == 2 and result["values"]["fits"], result
    assert result["values"]["layout"] == {
        "footer": True, "left": True, "toolbar": ["Файлы", "Память", "Настройки"], "plus": True,
        "create_label": not mobile, "create_icon": mobile, "create_name": "Новый проект",
    }
    assert result["values"]["sorted"] == ["Без проекта", "Зебра", "Каталог"]
    assert result["values"]["saved"] == "name"
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_each_project_restores_its_own_tabs_and_last_selection(browser, live, width):
    mobile = width == 320
    login = f"project-tabs-{width}"
    uid = live.add_user(login)
    inbox, other = f"inbox-{uid}", f"other-{uid}"
    live_sql(live, "INSERT INTO chat_projects(id, user_id, name, created) VALUES (?, ?, 'Другой проект', 1)", (other, uid))
    for cid, project in [("a1", inbox), ("a2", inbox), ("a3", inbox), ("b1", other)]:
        live_sql(live, "INSERT INTO chats(id, user_id, title, created, updated, project_id) VALUES (?, ?, ?, 1, 1, ?)",
                 (f"{cid}-{uid}", uid, cid, project))
    a1, a2, b1 = (f"{cid}-{uid}" for cid in ("a1", "a2", "b1"))
    old = json.dumps({"user": uid, "ids": [a1, b1, a2]})
    init = f"""if (location.pathname === '/' && !sessionStorage.getItem('legacy-tabs-seeded')) {{
      sessionStorage.setItem('legacy-tabs-seeded', '1');
      localStorage.setItem('studio-chat-tabs', {json.dumps(old)});
    }}"""
    drawer = [("click", "#chats-toggle")] if mobile else []
    tab = "#chat-tabbar .code-tab"
    active = f"document.querySelector('{tab}[aria-selected=true]')?.dataset.chat"
    restored = f"[...document.querySelectorAll('{tab}')].map(node => node.dataset.chat)"
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("wait", f"JSON.stringify({restored}) === JSON.stringify({json.dumps([a1, a2])})"),
        ("eval", "open_counts", """[...document.querySelectorAll('.project-list .code-project-count')].map(el => el.textContent)"""),
        ("click", f"{tab}[data-chat='{a1}']"), ("wait", f"{active} === '{a1}'"),
        ("click", "#chat-tab-add"),
        ("wait", "document.querySelector('.project-history:not([hidden]) .chat-item')"
         " && document.querySelector('#chat-tab-add[aria-pressed=true]')"),
        ("eval", "picker", "document.querySelector('.project-history #chat-new').textContent"),
        *drawer, ("click", f".project-list li[data-pid='{other}'] a"), ("wait", f"{active} === '{b1}'"),
        ("eval", "other_tabs", restored),
        *drawer, ("click", f".project-list li[data-pid='{inbox}'] a"), ("wait", f"{active} === '{a1}'"),
        ("eval", "inbox_tabs", restored),
        ("eval", "reload_project", f"history.replaceState(history.state, '', '#/chat?p={inbox}'); location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"
         f" && location.hash === '#/chat/{a1}' && {active} === '{a1}'"),
        *drawer, ("click", f".project-list li[data-pid='{other}'] a"), ("wait", f"{active} === '{b1}'"),
        ("click", f"{tab}[data-chat='{b1}'] .code-tab-close"),
        ("wait", "document.querySelector('.project-history:not([hidden]) .chat-item')"
         f" && !document.querySelector(\".project-list li[data-pid='{other}'] .code-project-count\")"),
        ("eval", "after_close", restored),
    ], width=width, mobile=mobile, init=init)
    assert not result.get("failed"), (result.get("failed"), result.get("where"), result.get("log"))
    assert result["values"]["open_counts"] == ["2", "1"]
    assert result["values"]["picker"] == "Создать новый чат"
    assert result["values"]["other_tabs"] == [b1]
    assert result["values"]["inbox_tabs"] == [a1, a2]
    assert result["values"]["after_close"] == []
    assert not problems(result), problems(result)
