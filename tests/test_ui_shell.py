"""Оболочка страницы в настоящем браузере: статика после выкладки (stale.js), клавиши разделов, вход с адресом.

Студия — живая (`live_app.py`), у каждого теста свой браузерный контекст.
"""
import json
from pathlib import Path

import pytest

from browser import run_playwright
from live_app import start_live_app

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

READY = "document.documentElement.dataset.ready === '1'"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = start_live_app(tmp_path_factory.mktemp("studio-live-shell"))
    yield app
    app.stop()


def signin(login="owner", password="password-123"):
    return [("idle",), ("type", "#login", login), ("type", "#password", password), ("click", "#submit")]


def problems(r, *expected):
    """Ошибки журнала браузера, кроме ожидаемых сценарием (подменённые 404 модулей)."""
    return [line for line in r.get("log", []) if not any(part in str(line) for part in expected)]


#: ответ /health службы, уже выложенной с другой статикой
NEWER = json.dumps({"status": "ok", "version": "0.0.0", "static": "000000000000"})
NOTICE = "document.querySelector('#view .empty p')?.textContent || ''"


@pytest.mark.parametrize("width", [320, 1280])
def test_section_of_an_older_deployment_offers_a_reload(browser, live, width):
    """Страница открыта до выкладки: модуль раздела прежнего хеша — 404. Служба той же версии — прежние слова о связи
    без плашки; служба новее — плашка «Студия обновилась» в окне, «Обновить» перезагружает, и раздел открывается."""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("route", "/js/photo.js", 404, ""),
        ("click", "#rail a[data-section=photo]"),
        ("wait", f"document.title === 'Фото — Студия' && {NOTICE}"),
        ("eval", "same", NOTICE), ("eval", "sameBar", "!!document.querySelector('#studio-updated')"),
        ("click", "#rail a[data-section=chat]"), ("wait", "document.title === 'Чат — Студия'"),
        ("route", "/health", 200, NEWER),
        ("click", "#rail a[data-section=photo]"),
        ("wait", "document.querySelector('#studio-updated')"),
        ("eval", "notice", NOTICE),
        ("eval", "bar", "(() => { const b = document.querySelector('#studio-updated'); const r = b.getBoundingClientRect();"
                        " return { text: b.textContent, role: b.getAttribute('role'),"
                        " inside: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight }; })()"),
        ("unroute", "/js/photo.js"), ("unroute", "/health"),
        ("click", "#studio-update"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", "document.querySelector('#photo-status')"),
        ("eval", "after", "[location.hash, !!document.querySelector('#studio-updated')]")],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["same"] == "Раздел не загрузился. Проверьте связь и обновите страницу." and v["sameBar"] is False
    assert v["notice"] == "Студия обновилась — нажмите «Обновить» вверху страницы."
    assert v["bar"] == {"text": "Студия обновилась" + "Обновить", "role": "alert", "inside": True}
    assert v["after"] == ["#/photo", False]
    assert not problems(r, "photo.js"), problems(r, "photo.js")


def test_return_to_screen_finds_a_deployment_without_a_missing_module(browser, live):
    """Разделы уже загружены — 404 модуля не будет; выкладку находит сверка `/health` при возвращении на экран:
    плашка «Студия обновилась», в меню — версия, с которой открыта страница."""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "before", "document.querySelector('#avatar').click(), document.querySelector('#menu-version').textContent"),
        ("route", "/health", 200, NEWER),
        ("eval", "back", "document.dispatchEvent(new Event('visibilitychange'))"),
        ("wait", "document.querySelector('#studio-updated')"),
        ("eval", "after", "document.querySelector('#menu-version').textContent")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"].startswith("Studio ") and v["after"] == v["before"]


HASH = "location.hash"


def alt(key, modifiers="Alt"):
    return ("press", key, modifiers)


def test_alt_digits_open_sections_in_rail_order(browser, live):
    """Alt+1…9 — прежние разделы, Alt+0 — «Очистка», Alt+/ — «Поиск». Ctrl+Alt (AltGr) и Shift+Alt, открытое
    окно — мимо. У кнопок рельса — aria-keyshortcuts и подсказка."""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "keys", "[...document.querySelectorAll('#rail a.tab')].map(a => [a.dataset.section,"
                         " a.getAttribute('aria-keyshortcuts'), a.title])"),
        ("eval", "searchKeys", "[document.querySelector('#search-open').getAttribute('aria-keyshortcuts'),"
                               " document.querySelector('#search-open').title]"),
        alt("3"), ("wait", "document.title === 'Видео — Студия'"), ("eval", "video", HASH),
        alt("8"), ("wait", "document.title === 'Open WebUI — Студия'"), ("eval", "owui", HASH),
        alt("9"), ("wait", "document.title === 'Агенты — Студия'"), ("eval", "code", HASH),
        alt("0"), ("wait", "document.title === 'Очистка — Студия'"), ("eval", "cleaner", HASH),
        alt("Slash"), ("wait", "document.querySelector('#search-q')"),
        ("eval", "search", HASH),
        ("eval", "focus", "document.querySelector('#search-q').focus(), document.activeElement.id"),
        alt("1"), ("wait", "document.title === 'Проекты — Студия'"), ("eval", "fromField", HASH),
        alt("2", "Control+Alt"), ("eval", "altGr", HASH),
        alt("2", "Shift+Alt"), ("eval", "shifted", HASH),
        ("click", "#jobs"), ("wait", "document.querySelector('#jobs-panel').open"),
        alt("2"), ("eval", "inDialog", HASH),
        ("click", "#jobs-close"), ("wait", "!document.querySelector('#jobs-panel').open"),
        alt("2"), ("wait", "document.title === 'Фото — Студия'"), ("eval", "photo", HASH)])
    assert not r.get("failed"), r
    v = r["values"]
    order = ["chat", "photo", "video", "speech", "transcribe", "ocr", "translate", "cleaner", "owui", "code"]
    assert [row[0] for row in v["keys"]] == order
    assert [row[1] for row in v["keys"]] == [f"Alt+{n}" for n in (1, 2, 3, 4, 5, 6, 7, 0, 8, 9)]
    assert v["keys"][0][2] == "Проекты · Alt+1" and v["searchKeys"] == ["Alt+/", "Поиск · Alt+/"]
    assert [v["video"], v["owui"], v["code"], v["cleaner"], v["search"]] == [
        "#/video", "#/owui", "#/code", "#/cleaner", "#/search",
    ]
    assert v["focus"] == "search-q" and v["fromField"] == "#/chat"
    assert v["altGr"] == v["shifted"] == v["inDialog"] == "#/chat"
    assert v["photo"] == "#/photo"
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_login_returns_to_the_address_it_came_from(browser, live, width):
    """Ссылка без сессии (нажатие на push) — `/` уводит на вход с тем же фрагментом; неверный пароль его не теряет,
    после верного студия открывает этот адрес, а не «Чат». Логин после отказа уже набран (`auth._login_page`) —
    вводится только пароль."""
    login = f"back{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/#/settings/security", [("idle",),
        ("eval", "page", "[location.pathname, location.hash, document.querySelector('#next').value]"),
        ("type", "#login", login), ("type", "#password", "wrong-password"), ("click", "#submit"),
        ("wait", "document.querySelector('.error').textContent.trim() !== ''"), ("idle",),
        ("eval", "again", "[location.pathname, location.hash, document.querySelector('#next').value,"
                          " document.querySelector('#login').value]"),
        ("type", "#password", "password-123"), ("click", "#submit"),
        ("wait", READY), ("eval", "after", "[location.pathname, location.hash, document.title]")],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["page"] == ["/login", "#/settings/security", "#/settings/security"]
    assert v["again"] == ["/login", "#/settings/security", "#/settings/security", login]
    assert v["after"] == ["/", "#/settings/security", "Настройки — Студия"]
    assert not problems(r, "status of 401"), problems(r, "status of 401")


#: выход и вход другой учётки в этом же браузере — запросами, как сделала бы соседняя вкладка
SWAP = ("fetch('/logout', { method: 'POST' }).then(() => fetch('/login', { method: 'POST',"
        " headers: { 'content-type': 'application/x-www-form-urlencoded' },"
        " body: 'login={0}&password=password-123' })).then((r) => r.ok)")


def test_sign_out_in_another_tab_sends_this_one_to_login(browser, live):
    """Две вкладки одного браузера: в одной выход (её страница входа шлёт `studio-session`) — вторая уходит на вход
    сразу, со своим адресом, а не при следующем опросе (опросы API здесь отвечают успехом — увести может только канал)."""
    live.add_user("tabs-a")
    r = browser.drive(live.url + "/#/settings", [*signin("tabs-a"), ("wait", READY),
        ("eval", "out", "fetch('/logout', { method: 'POST' }).then((r) => r.ok)"),
        ("route", "/api/", 200, "{}"),
        ("eval", "button", "(() => { const b = document.createElement('button'); b.id = 'other-tab';"
                           " b.onclick = () => { window.open('/login', '_blank'); }; document.body.append(b); })()"),
        ("click", "#other-tab"),
        ("wait", "location.pathname === '/login'"),
        ("eval", "after", "location.hash")])
    assert not r.get("failed"), r
    assert r["values"]["out"] is True and r["values"]["after"] == "#/settings"


def test_profile_of_another_account_reloads_the_tab(browser, live):
    """Вкладка проспала выход и чужой вход: перечитанный при возвращении профиль другой учётки не подменяет прежний —
    страница перезагружается и открывается уже как новая учётка."""
    live.add_user("swap-a")
    live.add_user("swap-b")
    r = browser.drive(live.url + "/", [*signin("swap-a"), ("wait", READY),
        ("eval", "swapped", SWAP.replace("{0}", "swap-b")),
        ("eval", "back", "document.dispatchEvent(new Event('visibilitychange'))"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("eval", "who", "document.querySelector('#menu-login').textContent")])
    assert not r.get("failed"), r
    assert r["values"]["swapped"] is True and r["values"]["who"] == "swap-b"


@pytest.mark.parametrize("engine", ["webkit", "firefox"])
def test_login_keeps_the_address_in_webkit_and_firefox(live, engine):
    """Перенос фрагмента через 303 `/` → `/login` и через POST формы зависит от движка: WebKit и Firefox ведут себя
    как Chromium. Secure-cookie по http://localhost WebKit не хранит, поэтому после входа проверяется ответ POST."""
    pw = pytest.importorskip("playwright.sync_api")
    login = f"back-{engine}"
    live.add_user(login)
    with run_playwright(pw) as driver:
        kind = getattr(driver, engine)
        if not Path(kind.executable_path).is_file():
            pytest.skip(f"нет {engine}: .venv/Scripts/python -m playwright install {engine}")
        browser = kind.launch()
        try:
            page = browser.new_page()
            page.set_default_timeout(15000)
            page.goto(live.url + "/#/settings/security")
            page.wait_for_url("**/login#/settings/security")
            assert page.locator("#next").input_value() == "#/settings/security"
            page.fill("#login", login)
            page.fill("#password", "wrong-password")
            page.click("#submit")
            page.wait_for_selector(".error:not(:empty)")
            assert page.evaluate("[location.pathname, location.hash]") == ["/login", "#/settings/security"]
            page.fill("#login", login)
            page.fill("#password", "password-123")
            posted = lambda r: r.request.method == "POST" and r.url.startswith(live.url + "/login")  # noqa: E731
            with page.expect_response(posted) as got:
                page.click("#submit")
            assert got.value.status == 303 and got.value.headers["location"] == "/#/settings/security"
        finally:
            browser.close()
