"""Project navigation, chat transfer and upload in the actual Studio browser shell."""
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
          return {
            footer: Math.abs(footer.getBoundingClientRect().bottom - panel.getBoundingClientRect().bottom) < 2,
            left: !panel.querySelector('.chat-item') && !!main.querySelector('.project-history .code-chat-link'),
            toolbar: [...tools.children].slice(0, 3).map(a => a.textContent),
            plus: plus.parentElement.id === 'chat-tabbar' && !document.querySelector('#chat-new'),
          };
        })()"""),
        ("click", ".project-create"), ("type", "#chat-project-editor input[required]", "Каталог"),
        ("click", "#chat-project-editor .btn-accent"),
        ("wait", "document.querySelector('#chat-title')?.textContent === 'Каталог'"),
        ("wait", "document.querySelector('.project-history:not([hidden]) .chat-list-note')?.textContent === 'Диалогов пока нет'"),
        ("click", "#chat-tab-add"),
        ("wait", "location.hash.includes('new=1') && !document.querySelector('.project-conversation').hidden"),
        ("click", "#chat-project-history"),
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
        "footer": True, "left": True, "toolbar": ["Файлы", "Память", "Настройки"], "plus": True}
    assert result["values"]["sorted"] == ["Без проекта", "Зебра", "Каталог"]
    assert result["values"]["saved"] == "name"
    assert not problems(result), problems(result)
