"""Оболочка страницы в настоящем браузере: статика после выкладки (stale.js), клавиши разделов, вход с адресом.

Студия — живая (`live_app.py`), браузер — свой на каждый тест (cookie localhost общие для портов).
"""
import json
import re

import pytest

from browser import launch
from live_app import start_live_app

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

READY = "document.documentElement.dataset.ready === '1'"


@pytest.fixture
def browser(tmp_path_factory):
    yield from launch(tmp_path_factory.mktemp("studio-ui-shell"))


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


HASH = "location.hash"


def alt(key, modifiers="Alt"):
    return ("press", key, modifiers)


def test_alt_digits_open_sections_in_rail_order(browser, live):
    """Alt+1…8 — разделы в порядке рельса, Alt+/ — «Поиск»; из поля ввода тоже. Ctrl+Alt (AltGr) и Shift+Alt, открытое
    окно — мимо. У кнопок рельса — aria-keyshortcuts и подсказка."""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "keys", "[...document.querySelectorAll('#rail a.tab')].map(a => [a.dataset.section,"
                         " a.getAttribute('aria-keyshortcuts'), a.title])"),
        ("eval", "searchKeys", "[document.querySelector('#search-open').getAttribute('aria-keyshortcuts'),"
                               " document.querySelector('#search-open').title]"),
        alt("3"), ("wait", "document.title === 'Видео — Студия'"), ("eval", "video", HASH),
        alt("8"), ("wait", "document.title === 'Агенты — Студия'"), ("eval", "code", HASH),
        alt("Slash"), ("wait", "document.querySelector('#search-q')"),
        ("eval", "search", HASH),
        ("eval", "focus", "document.querySelector('#search-q').focus(), document.activeElement.id"),
        alt("1"), ("wait", "document.title === 'Чат — Студия'"), ("eval", "fromField", HASH),
        alt("2", "Control+Alt"), ("eval", "altGr", HASH),
        alt("2", "Shift+Alt"), ("eval", "shifted", HASH),
        ("click", "#jobs"), ("wait", "document.querySelector('#jobs-panel').open"),
        alt("2"), ("eval", "inDialog", HASH),
        ("click", "#jobs-close"), ("wait", "!document.querySelector('#jobs-panel').open"),
        alt("2"), ("wait", "document.title === 'Фото — Студия'"), ("eval", "photo", HASH)])
    assert not r.get("failed"), r
    v = r["values"]
    order = ["chat", "photo", "video", "speech", "transcribe", "ocr", "translate", "code"]
    assert [row[0] for row in v["keys"]] == order
    assert [row[1] for row in v["keys"]] == [f"Alt+{n}" for n in range(1, 9)]
    assert v["keys"][0][2] == "Чат · Alt+1" and v["searchKeys"] == ["Alt+/", "Поиск · Alt+/"]
    assert [v["video"], v["code"], v["search"]] == ["#/video", "#/code", "#/search"]
    assert v["focus"] == "search-q" and v["fromField"] == "#/chat"
    assert v["altGr"] == v["shifted"] == v["inDialog"] == "#/chat"
    assert v["photo"] == "#/photo"
    assert not problems(r), problems(r)
