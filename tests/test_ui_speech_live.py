"""«Озвучка»: готовые части идущей озвучки слушаются до склейки (#sp-live) и проба голоса в настройках
(Chromium 320/1280, WebKit касанием)."""
import time
from pathlib import Path

import httpx
import pytest

from fakes import WAV_FAKE
from test_ui_browser import (NO_OVERFLOW, READY, add_gateway, add_voice, browser, go, is_open, live,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

TWO_PHRASES = "А" * 150 + ". " + "Б" * 150 + "."


@pytest.mark.parametrize("width", [320, 1280])
def test_ready_part_plays_while_the_speech_runs(browser, live, width):
    login = f"splive{width}"
    live.add_user(login)
    gw = live.gw
    gw.tts_replies = [(200, WAV_FAKE)] * (len(gw.tts_requests) + 1) + [(503, {"detail": "busy"})]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), add_voice(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
            *go("speech"), ("wait", "document.querySelector('#chip-voice option')"),
            ("eval", "fill", f"(() => {{ const t = document.querySelector('#speech-text'); t.value = {TWO_PHRASES!r};"
                             " t.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"),
            ("click", "#speech-go"),
            ("wait", "document.querySelector('#sp-live')?.getAttribute('src')"),
            ("eval", "src", "document.querySelector('#sp-live').getAttribute('src')"),
            ("eval", "part", "fetch(document.querySelector('#sp-live').src).then(r => [r.status, r.headers.get('content-type')])"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#sp-cancel"),
            ("wait", "document.querySelector('.sp-item[data-state=cancelled]') && !document.querySelector('#sp-live')")],
            width=width, height=900, mobile=width == 320, timeout=20000)
    finally:
        gw.tts_replies = [(200, WAV_FAKE)]
    assert not r.get("failed"), r
    v = r["values"]
    assert v["src"].endswith("/parts/0") and v["part"] == [200, "audio/wav"] and v["fits"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_voice_probe_plays_a_short_phrase(browser, live, width):
    login = f"probe{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), add_voice(),
        *go("settings/speech"), ("wait", "document.querySelector('.voice .voice-probe')"),
        ("click", ".voice .voice-probe"), ("wait", is_open("#voice-probe-dialog")),
        ("click", "#voice-probe-go"),
        ("wait", "!document.querySelector('#voice-probe-audio').hidden"),
        ("eval", "src", "document.querySelector('#voice-probe-audio').src.slice(0, 5)"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#voice-probe-dialog .dialog-close"), ("wait", f"!{is_open('#voice-probe-dialog')}")],
        width=width, height=900, mobile=width == 320)
    assert not r.get("failed"), r
    assert r["values"]["src"] == "blob:" and r["values"]["fits"]
    assert any(s.startswith("POST /api/voices/") and s.endswith("/probe") for s in r["sent"])
    assert not problems(r), problems(r)


@pytest.fixture(scope="module")
def webkit():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        if not Path(driver.webkit.executable_path).is_file():
            pytest.skip("нет WebKit: .venv/Scripts/python -m playwright install webkit")
        engine = driver.webkit.launch()
        yield engine
        engine.close()


def until(page, expression, sec=10.0):
    deadline = time.monotonic() + sec
    while not page.evaluate(expression):
        assert time.monotonic() < deadline, expression
        page.wait_for_timeout(50)


def test_voice_probe_by_touch_in_webkit(webkit, live):
    login = "probe-wk"
    live.add_user(login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with webkit.new_context(viewport={"width": 393, "height": 852}, is_mobile=True, has_touch=True,
                            device_scale_factor=3, extra_http_headers={"Cookie": cookie}) as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live.url + "/#/settings")
        until(page, READY)
        page.evaluate(add_gateway()[2])
        page.evaluate(add_voice()[2])
        page.evaluate("location.hash = '#/settings/speech'")
        page.locator(".voice .voice-probe").tap()
        page.locator("#voice-probe-go").tap()
        until(page, "!document.querySelector('#voice-probe-audio').hidden")
        assert page.evaluate(NO_OVERFLOW) and not errors
