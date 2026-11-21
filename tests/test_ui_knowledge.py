"""Базы знаний в настоящем браузере (knowledge.js, спека 2026-11-21-studio-source-capabilities-design, п. 15):
«Настройки» → «Чат» — создать базу и увидеть её документ; чип «Знания» поля ввода (`.ctl-chip`) открывает панель
флажков баз (menu.js), выбор сохраняется у диалога, и ход уносит модели найденное блоком `<knowledge>`."""
import pytest

from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, add_gateway, browser, go, live,  # noqa: F401
                             open_new_chat, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

PANEL = "document.querySelector('#kb-panel')"
INSIDE = (f"(() => {{ const b = {PANEL}.getBoundingClientRect();"
          " return b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight; })()")


def upload_doc(text: str):
    """Документ в первую базу учётки через API (тело — файл, имя — X-Studio-Name, как у страницы)."""
    return ("eval", "uploaded", f"""(async () => {{ const m = {API};
      const kb = (await m.api('GET', '/api/knowledge')).items[0];
      const file = new File([{text!r}], 'правила.txt', {{ type: 'text/plain' }});
      await m.api('POST', `/api/knowledge/${{kb.id}}/documents`, file,
        {{ headers: {{ 'content-type': 'text/plain', 'x-studio-name': encodeURIComponent(file.name) }} }});
      return kb.id; }})()""")


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_knowledge_base_in_settings_and_chip_in_chat(browser, live, width):
    login = f"kb-{width}"
    live.add_user(login)
    mobile = width < 760
    live.gw.chat_script = [["Пароль — бирюза."]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings/chat"),
            ("wait", "document.querySelector('#knowledge-settings #kb-name')"),
            ("wait", "document.querySelector('#kb-list').textContent !== ''"),
            ("eval", "none", "document.querySelector('#kb-list').textContent"),
            set_value("#kb-name", "Регламенты"), ("click", "#kb-name-create"),
            ("wait", "document.querySelector('#kb-list .kb-base')"),
            upload_doc("Кодовое слово отдела — бирюза."),
            *go("settings/data"), ("wait", "location.hash === '#/settings/data'"), *go("settings/chat"),
            ("wait", "document.querySelector('#kb-list .kb-doc')"),
            ("eval", "doc", "document.querySelector('#kb-list .kb-doc').textContent"),
            ("eval", "fits", NO_OVERFLOW),
            *open_new_chat(mobile),
            ("wait", "document.querySelector('#kb-chip') && !document.querySelector('#kb-chip').hidden"),
            ("eval", "chip", "[document.querySelector('#kb-chip').textContent, document.querySelector('#kb-chip').className]"),
            ("click", "#kb-chip"), ("wait", f"{PANEL}.matches(':popover-open')"),
            ("eval", "options", f"[...{PANEL}.querySelectorAll('.kb-option')].map((o) => o.textContent)"),
            ("eval", "inside", INSIDE),
            ("click", "#kb-panel input[type=checkbox]"),
            ("wait", "document.querySelector('#kb-chip').textContent === 'Знания (1)'"),
            ("press", "Escape"), ("wait", f"!{PANEL}.matches(':popover-open')"),
            ("type", "#chat-input", "Какое кодовое слово?"), ("press", "Enter"),
            ("wait", ANSWERED),
            ("eval", "saved", f"""(async () => {{ const m = {API};
              const id = location.hash.split('/')[2];
              return (await m.api('GET', `/api/chats/${{id}}`)).knowledge.length; }})()"""),
            ("eval", "chipAfter", "document.querySelector('#kb-chip').textContent"),
            ("eval", "fitsChat", NO_OVERFLOW)],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["none"] == "Баз знаний пока нет"
    assert v["doc"].startswith("правила.txt") and "готов" in v["doc"]
    assert v["fits"] is True and v["fitsChat"] is True
    assert v["chip"][0] == "Знания" and "ctl-chip" in v["chip"][1]
    assert v["options"] == ["Регламенты"] and v["inside"] is True
    assert v["saved"] == 1 and v["chipAfter"] == "Знания (1)"
    user = [m for m in live.gw.chat_requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert user.startswith("<knowledge>") and "бирюза" in user
    assert not problems(r), r.get("log")


def test_admin_source_and_shared_base(browser, live):
    """Панель: источник — только общие подключения (без них — «поиск по словам»), общая база создаётся формой."""
    live.add_user("kb-admin", is_admin=True)
    r = browser.drive(live.url + "/", [*signin("kb-admin"), ("wait", READY), *go("admin"),
        ("wait", "document.querySelector('#admin-knowledge #kb-source-connection option')"),
        ("eval", "options", "[...document.querySelectorAll('#kb-source-connection option')].map((o) => o.textContent)"),
        set_value("#kb-shared-name", "Общие правила"), ("click", "#kb-shared-name-create"),
        ("wait", "document.querySelector('#kb-shared-list .kb-base .kb-access')"),
        ("eval", "base", "document.querySelector('#kb-shared-list .kb-base b').textContent"),
        ("click", "#kb-source-save"),
        ("wait", "document.querySelector('#kb-source-status').textContent !== ''"),
        ("eval", "saved", "document.querySelector('#kb-source-status').textContent"),
        ("eval", "fits", NO_OVERFLOW)], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["options"] == ["Нет — только поиск по словам"] and v["base"] == "Общие правила"
    assert v["saved"] == "Сохранено" and v["fits"] is True
    assert not problems(r), r.get("log")
