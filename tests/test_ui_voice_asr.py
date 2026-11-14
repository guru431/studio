"""Автотекст образца: файл, микрофон, отказ ASR и запоздалые ответы в трёх движках."""
import hashlib
import io
import wave
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

from browser import find_browser
from test_ui_browser import FAKE_MIC, READY, add_gateway, live, put_audio, wav_of

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def activate(page, selector):
    if page.context.browser.browser_type.name == "webkit":
        page.locator(selector).tap()
    else:
        page.locator(selector).click()


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def engine(request):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        kind = getattr(driver, request.param)
        executable = find_browser() if request.param == "chromium" else kind.executable_path
        if not executable or not Path(executable).is_file():
            pytest.skip(f"нет {request.param}")
        browser = kind.launch(executable_path=executable)
        yield request.param, browser
        browser.close()


@pytest.fixture
def page(engine, live, request):
    name, browser = engine
    login = "asr-" + hashlib.sha256(request.node.name.encode()).hexdigest()[:16]
    live.add_user(login, theme="dark" if name == "webkit" else "light")
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with browser.new_context(viewport={"width": 320 if name == "webkit" else 1280, "height": 900},
                             has_touch=name == "webkit", extra_http_headers={"Cookie": cookie},
                             service_workers="block") as context:
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live.url + "/#/settings")
        page.wait_for_function(f"() => {READY}")
        page.evaluate(add_gateway()[2])
        page.evaluate("location.hash = '#/settings/speech'")
        activate(page, "#voice-add")
        page.locator("#voice-dialog[open]").wait_for()
        yield page
        assert not errors


def upload(page, name="sample.wav", seconds=6.0):
    page.evaluate(put_audio("#voice-dialog input[type=file]", wav_of(seconds), name)[2])


def test_file_is_transcribed_corrected_and_saved(page, live):
    page.locator("#voice-dialog [name=name]").fill("Голос с ASR")
    upload(page, seconds=20.0)
    page.wait_for_function("() => document.querySelector('#voice-dialog [name=text]').value === 'распознано'")
    page.locator("#voice-dialog [name=text]").fill("Исправленный текст")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#voice-save").focus()
    page.keyboard.press("Enter")
    page.locator("#voice-dialog").wait_for(state="detached")
    page.locator(".voice").wait_for()
    saved = page.evaluate("""async () => {
      const items = (await (await fetch('/api/voices')).json()).items;
      return (await fetch('/api/voices/' + items[0].id)).json();
    }""")
    assert saved["name"] == "Голос с ASR" and saved["text"] == "Исправленный текст"
    sent = live.gw.asr_requests[-1]
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {sent.headers['content-type']}\r\n\r\n".encode() + sent.content)
    sample = next(part.get_payload(decode=True) for part in message.iter_parts() if part.get_filename())
    with wave.open(io.BytesIO(sample)) as wav:
        assert wav.getframerate() == 24000 and wav.getnchannels() == 1
        assert abs(wav.getnframes() / wav.getframerate() - saved["duration"]) < 0.05
    assert saved["duration"] <= 15.5
    page.reload()
    assert page.locator(".voice").text_content().startswith("Голос с ASR")


def test_replacing_sample_and_closing_ignore_late_answers(page):
    # Подмена только ASR: даже ответ fetch, проигнорировавший AbortSignal, не должен затереть новый текст.
    page.evaluate("""() => {
      const original = window.fetch;
      window.asr = [];
      window.fetch = (url, opts) => String(url) === '/api/voices/transcribe'
        ? new Promise(resolve => window.asr.push({signal: opts.signal, resolve: text => resolve(
            new Response(JSON.stringify({text}), {headers: {'content-type': 'application/json'}}))}))
        : original(url, opts);
    }""")
    upload(page, "first.wav")
    assert page.locator("#voice-save").is_disabled()
    upload(page, "second.wav")
    assert page.evaluate("window.asr[0].signal.aborted")
    page.evaluate("window.asr[1].resolve('Второй образец')")
    page.wait_for_function("() => document.querySelector('#voice-dialog [name=text]').value === 'Второй образец'")
    page.evaluate("window.asr[0].resolve('Первый образец')")
    page.locator("#voice-dialog [name=text]").fill("Моя правка")
    assert page.locator("#voice-dialog [name=text]").input_value() == "Моя правка"
    activate(page, "#voice-transcribe")
    page.keyboard.press("Escape")
    page.locator("#voice-dialog").wait_for(state="detached")
    assert page.evaluate("window.asr[2].signal.aborted")
    page.evaluate("window.asr[2].resolve('Закрытый образец')")
    activate(page, "#voice-add")
    assert page.locator("#voice-dialog [name=text]").input_value() == ""


def test_microphone_transcribes_and_failure_allows_manual_text(page, fake_audio):
    fake_audio.duration = 8.0
    page.evaluate("""() => {
      if (!navigator.mediaDevices) Object.defineProperty(navigator, 'mediaDevices', {value: {}});
    }""")
    page.evaluate(FAKE_MIC)
    activate(page, "#voice-record")
    page.wait_for_function("() => document.querySelector('#voice-record').textContent === 'Остановить'")
    assert page.locator("#voice-save").is_disabled()
    activate(page, "#voice-record")
    page.wait_for_function("() => document.querySelector('#voice-dialog [name=text]').value === 'распознано'")
    page.route("**/api/voices/transcribe", lambda route: route.fulfill(status=503,
               content_type="application/json", body='{"detail":{"code":"busy","message":"ASR busy"}}'))
    activate(page, "#voice-transcribe")
    page.wait_for_function("() => document.querySelector('#voice-asr-status').textContent.includes('не удалось')")
    assert page.locator("#voice-dialog [name=text]").input_value() == ""
    assert page.locator("#voice-save").is_enabled()
    page.locator("#voice-dialog [name=text]").fill("Текст вручную")
    page.unroute("**/api/voices/transcribe")
    activate(page, "#voice-transcribe")
    page.wait_for_function("() => document.querySelector('#voice-dialog [name=text]').value === 'распознано'")
