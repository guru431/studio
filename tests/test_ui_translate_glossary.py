"""Глоссарий и тон перевода на странице (идея 05:I7): «Настройки → Перевод», блок `#translate-glossary` — строка без
«=» отказывается словами до запроса, сохранённые пары и тон приходят с сервера обратно в поле."""
import pytest

from test_ui_browser import NO_OVERFLOW, READY, add_gateway, browser, go, live, problems, set_value, signin  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.mark.parametrize("width", [320, 1280])
def test_glossary_and_tone_are_saved_from_settings(browser, live, width):
    login = f"glossary-{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings/translate"),
        ("wait", "document.querySelector('#translate-glossary-terms')"),
        set_value("#translate-glossary-terms", "Acme Corp"), ("click", "#translate-glossary-save"),
        ("wait", "document.querySelector('#translate-glossary-status').classList.contains('error')"),
        ("eval", "refused", "document.querySelector('#translate-glossary-status').textContent"),
        set_value("#translate-tone", "formal"),
        set_value("#translate-glossary-terms", "Acme  Corp → АО «Акме»\n\nдоговор=agreement"),
        ("click", "#translate-glossary-save"),
        ("wait", "document.querySelector('#translate-glossary-status').classList.contains('ok')"),
        ("eval", "shown", "document.querySelector('#translate-glossary-terms').value"),
        ("eval", "saved", """(async () => (await (await import('/static/js/api.js')).api('GET',
          '/api/translate/glossary')))()"""),
        ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width < 1000)
    assert not r.get("failed"), r
    assert "1" in r["values"]["refused"]
    assert r["values"]["shown"] == "Acme Corp = АО «Акме»\nдоговор = agreement"
    saved = r["values"]["saved"]
    assert saved["tone"] == "formal" and [p["term"] for p in saved["glossary"]] == ["Acme Corp", "договор"]
    assert r["values"]["fits"] is True
    assert not problems(r), r.get("log")
