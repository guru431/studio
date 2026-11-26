"""«Корзина» в «Моих файлах»: удалённый диалог — в группе «Чат», «Вернуть» возвращает его в
ленту чатов, «Очистить корзину» — только паролем учётки в окне подтверждения (неверный — словами в окне)."""
import time

import pytest

from conftest import browser  # noqa: F401 — фикстура браузера
from live_app import start_live_app
from test_ui_browser import API, READY, go, is_open, live_sql, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = start_live_app(tmp_path_factory.mktemp("studio-live-trash"))
    yield app
    app.stop()


def chat_row(live, uid: int, chat_id: str, title: str | None) -> None:
    now = time.time()
    live_sql(live, "INSERT INTO chats(id, user_id, title, model, created, updated) VALUES (?, ?, ?, 'm', ?, ?)",
             (chat_id, uid, title, now, now))


ITEMS = "[...document.querySelectorAll('#trash-list .trash-item')].map((li) => li.dataset.id)"
CHATS = f"(async () => (await {API}.api('GET', '/api/chats')).items.map((c) => c.id).sort())()"


@pytest.mark.parametrize("width", [320, 1280])
def test_trash_restores_and_empties_only_by_password(browser, live, width):
    login = f"trash{width}"
    uid = live.add_user(login)
    chat_row(live, uid, f"keep{width}", "Сохранить")
    chat_row(live, uid, f"drop{width}", "Выбросить")
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "deleted", f"""(async () => {{ const m = {API};
          await m.api('DELETE', '/api/chats/keep{width}'); await m.api('DELETE', '/api/chats/drop{width}');
          return true; }})()"""),
        ("click", "#myfiles-open"), ("wait", "document.querySelector('#myfiles-trash')"), ("click", "#myfiles-trash"),
        ("wait", "document.querySelectorAll('#trash-list .trash-item').length === 2"),
        ("eval", "title", "document.querySelector('#view h1').textContent"),
        ("eval", "group", "[...document.querySelectorAll('#trash-list .trash-group h2')].map((h) => h.textContent)"),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "before", CHATS),
        ("click", f".trash-item[data-id='keep{width}'] .trash-restore"),
        ("wait", "document.querySelectorAll('#trash-list .trash-item').length === 1"),
        ("eval", "left", ITEMS), ("eval", "restored", CHATS),
        ("eval", "said", "document.querySelector('#trash-status').textContent"),
        ("click", "#trash-empty"), ("wait", is_open("#trash-empty-dialog")),
        ("click", "#trash-empty-confirm"),
        ("wait", "document.querySelector('#trash-empty-dialog .error:not([data-for])').textContent !== ''"),
        ("eval", "blank", "document.querySelector('#trash-empty-dialog .error:not([data-for])').textContent"),
        ("type", "#trash-password", "wrong-password"), ("click", "#trash-empty-confirm"),
        ("wait", "document.querySelector('#trash-empty-dialog .error:not([data-for])').textContent.startsWith('Пароль')"),
        ("eval", "wrong", "document.querySelector('#trash-empty-dialog .error:not([data-for])').textContent"),
        ("eval", "kept", ITEMS),
        set_value("#trash-password", "password-123"), ("click", "#trash-empty-confirm"),
        ("wait", "document.querySelector('#trash-none') && !" + is_open("#trash-empty-dialog")),
        ("eval", "done", "document.querySelector('#trash-status').textContent"),
        ("eval", "emptyHidden", "document.querySelector('#trash-empty').hidden"),
        *go("settings/data"), ("wait", "document.querySelector('#storage-trash')?.textContent"),
        ("eval", "duplicate", "!!document.querySelector('#storage-trash a, #menu a[href=\"#/trash\"]')"),
        ("eval", "storage", "document.querySelector('#storage-trash').textContent")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["title"] == "Корзина" and v["group"] == ["Чат"] and not v["overflow"]
    assert v["before"] == []
    assert v["left"] == [f"drop{width}"] and v["restored"] == [f"keep{width}"]
    assert v["said"] == "Возвращено: Сохранить"
    assert v["blank"] == "Введите пароль учётки"
    assert v["wrong"] == "Пароль неверен" and v["kept"] == [f"drop{width}"]
    assert v["done"] == "Корзина очищена: 1" and v["emptyHidden"] is True
    assert v["storage"] == "Корзина пуста." and not v["duplicate"]
    # единственный отказ — неверный пароль, которого тест и ждал
    assert [line for line in problems(r) if "403" not in line or "/api/trash/empty" not in line] == []
    assert len(problems(r)) == 1


def test_trash_localizes_only_default_title_and_prompts_to_purge_one(browser, live):
    login = "trash-item-password"
    uid = live.add_user(login)
    chat_row(live, uid, "default", None)
    live_sql(live, "INSERT INTO messages(id, chat_id, role, content, status, via, created)"
             " VALUES ('trash-question', 'default', 'user', 'question', 'done', 'server', 1)")
    chat_row(live, uid, "custom", "Новый диалог")
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "deleted", f"""(async () => {{
          const m = {API};
          await m.api('DELETE', '/api/chats/default');
          await m.api('DELETE', '/api/chats/custom');
        }})()"""),
        *go("trash"), ("wait", "location.hash === '#/myfiles?trash=1' && document.querySelectorAll('.trash-item').length === 2"),
        ("eval", "titles", "Object.fromEntries([...document.querySelectorAll('.trash-item')]"
                          ".map(e => [e.dataset.id, e.querySelector('b').textContent]))"),
        ("click", ".trash-item[data-id='default'] .trash-forever"), ("wait", is_open("#trash-forever-dialog")),
        ("type", "#trash-item-password", "password-123"), ("click", "#trash-forever-confirm"),
        ("wait", "document.querySelectorAll('.trash-item').length === 1"), ("eval", "left", ITEMS)],
        init="localStorage.setItem('studio-language', 'en')")
    assert not r.get("failed"), r
    assert r["values"]["titles"] == {"default": "New conversation", "custom": "Новый диалог"}
    assert r["values"]["left"] == ["custom"]
    assert not problems(r), problems(r)
