"""«Справка» в браузере: меню тем, одна статья, адрес и возврат к началу."""
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
def test_help_page_opens_a_topic_from_the_menu(browser, live, width):
    login = f"help{width}"
    live.add_user(login)
    mobile_menu = [("click", ".help-toggle")] if width == 320 else []
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("click", "#avatar"), ("wait", MENU_OPEN), ("click", "#menu a[href='#/help']"),
        ("wait", "document.querySelector('#help-start') && document.querySelectorAll('#help-toc a').length >= 10"),
        ("eval", "title", "document.querySelector('#view h1').textContent"),
        ("eval", "first", "document.querySelector('#help-text h2').textContent"),
        ("eval", "cards", "document.querySelectorAll('.help-card').length"),
        *mobile_menu,
        ("click", "#help-toc [data-help-section='security']"),
        ("wait", "document.activeElement.id === 'help-security'"),
        ("eval", "article", r"""[location.hash, document.querySelector('#help-toc [aria-current=page]').dataset.helpSection,
          document.querySelectorAll('#help-text h2').length, /docs\/|\.md\b/.test(document.querySelector('#help-text').textContent)]"""),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "back", "history.back()"),
        ("wait", "document.querySelector('#help-start') && location.hash === '#/help'")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    values = r["values"]
    assert values["title"] == "Справка" and values["first"] == "Начало работы"
    assert values["cards"] == 4 and not values["overflow"]
    assert values["article"] == ["#/help/security", "security", 1, False]
    assert not problems(r), problems(r)
