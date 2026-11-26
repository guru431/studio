"""Project navigation, chat transfer and upload in the actual Studio browser shell."""
import pytest

from live_app import start_live_app
from test_ui_browser import READY, live_sql, problems, signin

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
        ("wait", "document.querySelector('.project-list .project-link:first-child[data-inbox]')"),
        ("click", ".project-create"), ("type", "#chat-project-editor input[required]", "Каталог"),
        ("click", "#chat-project-editor .btn-accent"),
        ("wait", "document.querySelector('#chat-title')?.textContent === 'Каталог'"),
        *drawer, ("click", ".project-list [data-inbox]"),
        ("wait", "document.querySelector('.project-list [data-inbox][aria-current=page]')"),
        *drawer, ("click", f".chat-item[data-id='{cid}'] .chat-link"),
        ("wait", "document.querySelector('#chat-title')?.textContent === 'Старый диалог'"),
        ("eval", "reveal_actions", """(() => {
          if (document.querySelector('#chat-title-actions').closest('#chat-status-menu:not(:popover-open)')) {
            document.querySelector('#chat-status-more').click();
          }
          return true;
        })()"""),
        ("click", "#chat-title-actions"), ("click", "#chat-title-move-project"), ("click", "#choose-chat-project .btn-accent"),
        ("wait", "document.querySelector('.project-list [aria-current=page]')?.textContent === 'Каталог'"),
        *drawer, ("click", ".project-actions a"),
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
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth")], width=width, mobile=mobile)
    assert not result.get("failed"), result
    assert result["values"]["upload"] == 2 and result["values"]["fits"], result
    assert not problems(result), problems(result)
