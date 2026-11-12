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
