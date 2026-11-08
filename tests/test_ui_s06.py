"""Personal gateway settings persist on desktop and narrow phones."""
import pytest

from fakes import TOKEN
from test_ui_browser import READY, browser, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_s06_settings(browser, live, width):
    login = f"s06-settings-{width}"
    live.add_user(login)
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "open", "location.hash = '#/settings/chat'"),
        ("wait", "document.querySelector('#link-add')"), ("click", "#link-add"),
        ("wait", "document.querySelector('#link-dialog[open]')"),
        set_value('#link-dialog [name=kind]', 's06'),
        set_value('#link-dialog [name=name]', 's06'),
        set_value('#link-dialog [name=base_url]', 'http://10.77.1.15:4000'),
        set_value('#link-dialog [name=token]', TOKEN), ("click", "#link-save"),
        ("wait", "document.querySelector('.model-check[value=\"qwen-27b\"]')?.checked"),
        ("click", ".link-menu"), ("click", ".link-edit"),
        ("wait", "document.querySelector('#conn-dialog[open]')"),
        ("eval", "kind", "document.querySelector('#conn-dialog [name=kind]').value"),
        ("eval", "secretHidden", "document.querySelector('#conn-dialog [name=token]').value === ''"),
        set_value('#conn-dialog [name=name]', 'My s06'), ("click", "#conn-save"),
        ("wait", "document.querySelector('.link-name')?.textContent === 'My s06'"),
        ("eval", "reload", "location.reload()"), ("wait", READY),
        ("wait", "document.querySelector('.link-name')?.textContent === 'My s06'"),
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
        ("eval", "models", "[...document.querySelectorAll('.model-check')].map(e => e.value)"),
        ("eval", "connection", "fetch('/api/connections').then(r => r.json()).then(r => ({s06:r.items[0].s06, token:r.items[0].token}))"),
    ], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["kind"] == "s06" and values["secretHidden"] and values["fits"]
    assert values["models"] == ["qwen-27b", "qwen-27b-fast"]
    assert values["connection"] == {"s06": True}
    assert not problems(result), problems(result)
