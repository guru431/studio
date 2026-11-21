"""Роли диалога на странице (roles.js, спека 2026-11-21-studio-source-foundation-design §3): чип `#role-chip` в поле
чата, библиотека в «Настройки → Чат» и умолчания в панели администратора."""
import pytest

from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, add_gateway, browser, go, is_open, live, open_new_chat,  # noqa: F401
                             problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

CHIP = "document.querySelector('#role-chip')"
MENU_OPEN = "document.querySelector('#role-menu')?.matches(':popover-open')"
NAMES = "[...document.querySelectorAll('#chat-roles-settings .role-item-name')].map(e => e.textContent)"


def pick(name: str) -> tuple:
    return ("eval", "pick", f"[...document.querySelectorAll('#role-menu [role=menuitemradio]')]"
                            f".find(e => e.textContent === {name!r}).click()")


def item(scope: str, name: str) -> str:
    return (f"[...document.querySelectorAll('{scope} .role-item')]"
            f".find(e => e.querySelector('.role-item-name').textContent === {name!r})")


def edit(scope: str, name: str, new_name: str, text: str) -> list:
    return [("wait", item(scope, name)), ("eval", "edit", f"{item(scope, name)}.querySelector('.role-edit').click()"),
            ("wait", is_open("#role-dialog")), set_value("#role-name", new_name), set_value("#role-text", text),
            ("click", "#role-save"), ("wait", f"!{is_open('#role-dialog')} && {item(scope, new_name)}")]


@pytest.mark.parametrize("width", [320, 1280])
def test_role_is_seeded_chosen_kept_and_edited_in_settings(browser, live, width):
    login = f"roles-{width}"
    live.add_user(login)
    mobile = width == 320
    steps = [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("eval", "before", f"[{CHIP}.textContent, {CHIP}.getAttribute('aria-pressed')]"),
        # первое открытие чата засеяло библиотеку умолчаниями языка страницы
        ("click", "#role-chip"), ("wait", f"{MENU_OPEN} && document.querySelectorAll('#role-menu .role-option').length"),
        ("eval", "menu", "[...document.querySelectorAll('#role-menu .menu-option')].map(e => e.textContent)"),
        ("eval", "menuFits", "(() => { const r = document.querySelector('#role-menu').getBoundingClientRect();"
                             " return r.left >= 0 && r.right <= innerWidth; })()"),
        pick("Юрист"), ("wait", f"{CHIP}.textContent === 'Юрист'"),
        ("eval", "composerFits", NO_OVERFLOW),
        # новый диалог: роль — черновик, уходит в диалог после его создания
        ("type", "#chat-input", "Привет"), ("press", "Enter"), ("wait", ANSWERED),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"
                 f" && document.querySelector('#messages .msg') && {CHIP}?.textContent === 'Юрист'"),
        ("eval", "after", f"""(async () => {{ const m = {API};
          const id = location.hash.split('/')[2];
          return [(await m.api('GET', '/api/chats/' + id)).chat.role.name, {CHIP}.getAttribute('aria-pressed')]; }})()"""),
        # «Без роли» снимает роль с открытого диалога
        ("click", "#role-chip"), ("wait", MENU_OPEN), ("click", "#role-menu .role-none"),
        ("wait", f"{CHIP}.textContent === 'Роль'"),
        ("eval", "cleared", f"""(async () => {{ const m = {API};
          return (await m.api('GET', '/api/chats/' + location.hash.split('/')[2])).chat.role; }})()"""),
        # «Настроить роли» ведёт в «Настройки → Чат»: создать и изменить роль
        ("click", "#role-chip"), ("wait", MENU_OPEN), ("click", "#role-menu .role-configure"),
        ("wait", f"location.hash === '#/settings/chat' && {NAMES}.length === 6"),
        ("eval", "seeded", NAMES),
        ("click", "#chat-roles-settings-add"), ("wait", is_open("#role-dialog")),
        ("type", "#role-name", "Тестировщик"), ("type", "#role-text", "Ищи ошибки в присланном коде."),
        ("click", "#role-save"), ("wait", f"!{is_open('#role-dialog')} && {item('#chat-roles-settings', 'Тестировщик')}"),
        *edit("#chat-roles-settings", "Тестировщик", "Ревьюер", "Проверяй код и называй риски."),
        ("eval", "settingsFits", NO_OVERFLOW),
        ("eval", "library", f"""(async () => {{ const m = {API};
          return (await m.api('GET', '/api/roles?lang=ru')).items.map(r => [r.name, r.text]); }})()""")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=mobile)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["before"] == ["Роль", "false"]
    assert values["menu"] == ["Юрист", "Медик", "Переводчик", "Редактор", "Программист", "Аналитик", "Без роли",
                              "Настроить роли"]
    assert values["menuFits"] is True and values["composerFits"] is True and values["settingsFits"] is True
    assert values["after"] == ["Юрист", "true"]
    assert values["cleared"] is None
    assert values["seeded"] == ["Юрист", "Медик", "Переводчик", "Редактор", "Программист", "Аналитик"]
    assert values["library"][-1] == ["Ревьюер", "Проверяй код и называй риски."]
    assert len(values["library"]) == 7
    # роль дошла до модели в системном сообщении первого хода
    system = [r["messages"][0]["content"] for r in live.gw.chat_requests if r["messages"][0]["role"] == "system"]
    assert any("Роль в этом диалоге — «Юрист»" in s for s in system)
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_edits_a_default_role(browser, live, width):
    text = f"Помогай с правом, вариант {width}."
    steps = [*signin(), ("wait", READY), *go("admin"),
        ("wait", f"{item('#admin-role-defaults', 'Юрист')}"),
        *edit("#admin-role-defaults", "Юрист", "Юрист", text),
        # английские умолчания — отдельный список
        set_value("#role-default-lang", "en"), ("wait", item("#admin-role-defaults", "Lawyer")),
        ("click", "#admin-role-defaults-add"), ("wait", is_open("#role-dialog")),
        ("type", "#role-name", f"Reviewer {width}"), ("type", "#role-text", "Review the code."),
        ("click", "#role-save"), ("wait", f"!{is_open('#role-dialog')} && {item('#admin-role-defaults', f'Reviewer {width}')}"),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "server", f"""(async () => {{ const m = {API};
          const ru = (await m.api('GET', '/api/admin/roles?lang=ru')).items;
          const en = (await m.api('GET', '/api/admin/roles?lang=en')).items;
          return [ru.find(r => r.name === 'Юрист').text, en.some(r => r.name === 'Reviewer {width}'),
                  ru.some(r => r.name === 'Reviewer {width}')]; }})()""")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["fits"] is True
    assert values["server"] == [text, True, False]
    assert not problems(result), result.get("log")
