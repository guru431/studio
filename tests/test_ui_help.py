"""«Справка» в настоящем браузере: пункт меню аватара, руководство Studio безопасным Markdown, разделы кнопками."""
import pytest

from conftest import browser  # noqa: F401 — фикстура браузера
from live_app import start_live_app
from test_ui_browser import MENU_OPEN, READY, problems, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = start_live_app(tmp_path_factory.mktemp("studio-live-help"))
    yield app
    app.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_help_page_renders_the_guide_and_jumps_to_a_section(browser, live, width):
    login = f"help{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("click", "#avatar"), ("wait", MENU_OPEN), ("click", "#menu a[href='#/help']"),
        ("wait", "document.querySelectorAll('#help-text h2').length > 10"),
        ("wait", "document.querySelectorAll('#help-toc .chip').length > 10"),
        ("eval", "title", "document.querySelector('#view h1').textContent"),
        ("eval", "first", "document.querySelector('#help-text h2').textContent"),
        ("eval", "scripts", "document.querySelectorAll('#help-text script, #help-text style').length"),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "sources", "[...document.querySelectorAll('#help-source option')].map(o => o.textContent)"),
        ("click", "#help-toc summary"),
        ("click", "#help-toc .chip[data-help-section='s5']"),
        ("eval", "focused", "document.activeElement.id"),
        ("eval", "hash", "location.hash")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    values = r["values"]
    assert values["title"] == "Справка" and values["first"] == "Учётки и вход"
    assert values["scripts"] == 0 and not values["overflow"]
    assert values["sources"] == ["Studio"]
    assert values["focused"] == "help-s5" and values["hash"] == "#/help"
    assert not problems(r), problems(r)
