"""Автотекст образца: файл, микрофон, отказ ASR и запоздалые ответы в трёх движках."""
import base64
import hashlib
import io
import sys
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


def assert_audio_durations(page, expected):
    players = page.locator(".voice-sample audio")
    assert players.count() == len(expected)
    assert players.evaluate_all("as => as.every(a => a.controls && !a.hidden && a.src)")
    if sys.platform == "win32" and page.context.browser.browser_type.name == "webkit":
        # Probe independently: Windows WebKit can advertise WAV but lack the actual decoder.
        supported = page.evaluate("""async encoded => {
          const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
          const url = URL.createObjectURL(new Blob([bytes], {type: 'audio/wav'}));
          const audio = new Audio(url);
          try { await audio.play(); return true; }
          catch (e) { if (e.name === 'NotSupportedError') return false; throw e; }
          finally { audio.pause(); audio.removeAttribute('src'); audio.load(); URL.revokeObjectURL(url); }
        }""", base64.b64encode(wav_of(0.1)).decode())
        if not supported:
            return  # Editor/persistence still run; Chromium and Firefox exercise native playback.
    page.wait_for_function("() => [...document.querySelectorAll('.voice-sample audio')].every(a => a.readyState >= 1)")
    assert sorted(round(n) for n in players.evaluate_all("as => as.map(a => a.duration)")) == sorted(expected)
    players.first.evaluate("a => a.play()")
    page.wait_for_function("() => document.querySelector('.voice-sample audio').currentTime > 0")
    players.first.evaluate("a => a.pause()")


def test_file_is_transcribed_corrected_and_saved(page, live):
    page.locator("#voice-dialog [name=name]").fill("Голос с ASR")
    upload(page, seconds=20.0)
    assert_audio_durations(page, [20])
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
    assert abs(saved["duration"] - 20) < 0.05
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
    page.evaluate(put_audio(".voice-sample input[type=file]", wav_of(6), "second.wav")[2])
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
    page.evaluate("window.recordingTime = 0; window.actualNow = Date.now; Date.now = () => window.recordingTime")
    activate(page, "#voice-record")
    page.wait_for_function("() => document.querySelector('#voice-record').textContent === 'Остановить'")
    assert page.locator("#voice-save").is_disabled()
    page.evaluate("window.recordingTime = 16000")
    page.wait_for_function("() => document.querySelector('#voice-dialog').textContent.includes('0:16')")
    assert page.locator("#voice-record").text_content() == "Остановить"
    page.evaluate("window.recordingTime = 60000")
    page.wait_for_function("() => document.querySelector('#voice-dialog [name=text]').value === 'распознано'")
    page.evaluate("Date.now = window.actualNow")
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


def read_voice(page):
    return page.evaluate("""async () => {
      const items = (await (await fetch('/api/voices')).json()).items;
      return (await fetch('/api/voices/' + items[0].id)).json();
    }""")


def test_multiple_samples_can_be_edited_replaced_and_selected(page):
    page.locator("#voice-dialog [name=name]").fill("Несколько образцов")
    page.locator("#voice-files").set_input_files([
        {"name": "minute.wav", "mimeType": "audio/wav", "buffer": wav_of(60)},
        {"name": "second.wav", "mimeType": "audio/wav", "buffer": wav_of(6)}])
    page.wait_for_function("() => document.querySelectorAll('.voice-sample').length === 2 && !document.querySelector('#voice-save').disabled")
    page.locator(".voice-sample [name=text]").nth(0).fill("Первый")
    page.locator(".voice-sample [name=text]").nth(1).fill("Второй")
    activate(page, "#voice-save")
    page.locator("#voice-dialog").wait_for(state="detached")
    saved = read_voice(page)
    assert saved["sample_count"] == 2 and abs(saved["duration"] - 60) < 0.05
    activate(page, ".voice-edit")
    page.locator("#voice-dialog[open]").wait_for()
    assert_audio_durations(page, [6, 60])
    page.locator("#voice-dialog [name=name]").fill("Исправленный голос")
    page.locator(".voice-sample [name=text]").nth(1).fill("Исправленный второй")
    page.locator(".voice-sample input[type=radio]").nth(1).check()
    page.locator(".voice-sample input[type=file]").nth(0).set_input_files(
        {"name": "replacement.wav", "mimeType": "audio/wav", "buffer": wav_of(12)})
    page.wait_for_function("() => !document.querySelector('#voice-save').disabled")
    page.locator("#voice-files").set_input_files(
        {"name": "third.wav", "mimeType": "audio/wav", "buffer": wav_of(6)})
    page.wait_for_function("() => document.querySelectorAll('.voice-sample').length === 3 && !document.querySelector('#voice-save').disabled")
    assert page.evaluate("document.querySelector('#voice-dialog').scrollWidth <= document.querySelector('#voice-dialog').clientWidth")
    activate(page, "#voice-save")
    page.locator("#voice-dialog").wait_for(state="detached")
    saved = read_voice(page)
    assert saved["name"] == "Исправленный голос" and saved["sample_count"] == 3
    assert saved["text"] == "Исправленный второй"
    assert sorted(round(s["duration"]) for s in saved["samples"]) == [6, 6, 12]
    activate(page, ".voice-edit")
    page.locator("#voice-dialog[open]").wait_for()
    page.locator(".voice-sample .btn-danger").nth(0).click()
    activate(page, "#voice-save")
    page.locator("#voice-dialog").wait_for(state="detached")
    assert read_voice(page)["sample_count"] == 2


def test_partial_batch_failure_can_be_retried_without_duplicate_samples(page):
    page.locator("#voice-dialog [name=name]").fill("Повтор загрузки")
    page.locator("#voice-files").set_input_files([
        {"name": "first.wav", "mimeType": "audio/wav", "buffer": wav_of(6)},
        {"name": "second.wav", "mimeType": "audio/wav", "buffer": wav_of(6)}])
    page.wait_for_function("() => !document.querySelector('#voice-save').disabled")
    page.route("**/api/voices/*/samples", lambda route: route.fulfill(status=503,
               content_type="application/json", body='{"detail":{"code":"busy","message":"Try again"}}'))
    activate(page, "#voice-save")
    page.wait_for_function("() => document.querySelector('#voice-dialog [data-for=form]').textContent === 'Try again'")
    assert read_voice(page)["sample_count"] == 1
    page.unroute("**/api/voices/*/samples")
    activate(page, "#voice-save")
    page.locator("#voice-dialog").wait_for(state="detached")
    assert read_voice(page)["sample_count"] == 2


def test_preview_sources_are_released_when_replaced_removed_or_closed(page):
    page.evaluate("""() => {
      const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      window.previewUrls = new Set();
      URL.createObjectURL = blob => { const url = create(blob); window.previewUrls.add(url); return url; };
      URL.revokeObjectURL = url => { window.previewUrls.delete(url); revoke(url); };
    }""")
    upload(page)
    page.wait_for_function("() => !document.querySelector('#voice-save').disabled")
    first = page.locator(".voice-sample audio").get_attribute("src")
    page.locator(".voice-sample input[type=file]").set_input_files(
        {"name": "replacement.wav", "mimeType": "audio/wav", "buffer": wav_of(8)})
    page.wait_for_function("() => !document.querySelector('#voice-save').disabled")
    assert page.locator(".voice-sample audio").get_attribute("src") != first
    assert page.evaluate("url => !window.previewUrls.has(url)", first)
    # Re-selecting the same filename adds an independent reference too.
    upload(page)
    page.wait_for_function("() => !document.querySelector('#voice-save').disabled")
    assert page.locator(".voice-sample audio").count() == 2
    page.locator(".voice-sample .btn-danger").last.click()
    assert page.evaluate("window.previewUrls.size") == 1
    page.keyboard.press("Escape")
    page.locator("#voice-dialog").wait_for(state="detached")
    assert page.evaluate("window.previewUrls.size") == 0


def test_voice_order_matches_in_settings_and_speech(page):
    page.evaluate("""async encoded => {
      const {api} = await import('/static/js/api.js');
      const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      const blob = new Blob([bytes], {type: 'audio/wav'});
      for (const name of ['Яна', 'Анна', 'борис']) {
        const body = new FormData();
        body.append('name', name); body.append('text', 'Образец'); body.append('audio', blob, 'sample.wav');
        await api('POST', '/api/voices', body);
      }
    }""", base64.b64encode(wav_of(6)).decode())
    page.keyboard.press("Escape")
    page.locator("#voice-dialog").wait_for(state="detached")
    page.reload()
    page.wait_for_function("() => document.querySelectorAll('.voice').length === 3")
    assert page.locator(".voice b").all_text_contents() == ["Анна", "борис", "Яна"]
    page.evaluate("location.hash = '#/speech'")
    page.wait_for_function("() => document.querySelectorAll('#chip-voice option').length === 3")
    assert page.locator("#chip-voice option").all_text_contents() == ["Анна", "борис", "Яна"]
