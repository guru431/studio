"""Чаты озвучки в Firefox и WebKit: продолжение, правка, повторное открытие."""
import httpx
import pytest

from test_ui_browser import NO_OVERFLOW, READY, add_gateway, add_voice
from test_ui_email_accounts import live as secure_live  # noqa: F401
from test_ui_voice_asr import activate, engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize('engine', ['firefox', 'webkit'], indirect=True)
def test_speech_chat_continues_and_edits_after_reload(engine, secure_live):
    name, browser = engine
    login = f'speech-{name}'
    secure_live.add_user(login)
    signed = httpx.post(secure_live.url + '/login', data={'login': login, 'password': 'password-123'}, verify=False)
    assert signed.status_code == 303
    with browser.new_context(viewport={'width': 320, 'height': 900}, has_touch=name == 'webkit',
                             ignore_https_errors=True, service_workers='block') as context:
        context.add_cookies([{'name': k, 'value': v, 'url': secure_live.url, 'secure': True,
                             'httpOnly': True, 'sameSite': 'Lax'} for k, v in signed.cookies.items()])
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(secure_live.url + '/#/settings')
        page.wait_for_function(f'() => {READY}')
        page.evaluate(add_gateway()[2])
        page.evaluate(add_voice()[2])
        page.evaluate("location.hash = '#/speech'")
        page.locator('#chip-voice option').wait_for(state='attached')
        page.locator('#speech-text').fill('Первый текст.')
        activate(page, '#speech-go')
        page.locator('.sp-item[data-state=done]').wait_for(state='attached')
        page.locator('.sp-record audio[src^="/api/media/"]').wait_for(state='attached')
        page.evaluate("window.original = document.querySelector('.sp-record audio')")
        page.wait_for_function("() => !document.querySelector('#speech-go').disabled")
        page.locator('#speech-text').fill('Второй текст.')
        activate(page, '#speech-go')
        page.wait_for_function("() => document.querySelectorAll('.sp-record audio[src^=\"/api/media/\"]').length === 2")
        assert page.locator('.sp-item').count() == 1
        assert page.evaluate("window.original === document.querySelector('.sp-record audio')")
        activate(page, '.sp-record:last-child .sp-edit')
        page.wait_for_function("() => !document.querySelector('#speech-edit-cancel').hidden")
        assert page.locator('#speech-text').input_value() == 'Второй текст.'
        page.locator('#speech-text').fill('Исправленный второй.')
        activate(page, '#speech-go')
        page.wait_for_function("() => document.querySelectorAll('.sp-record audio').length === 2 && document.querySelectorAll('.sp-text')[1].textContent === 'Исправленный второй.'")
        page.reload()
        activate(page, '.split-toggle')
        activate(page, '.sp-item')
        page.wait_for_function("() => document.querySelectorAll('.sp-record audio').length === 2")
        assert page.locator('.sp-text').all_text_contents() == ['Первый текст.', 'Исправленный второй.']
        assert page.locator('.sp-voice-name').all_text_contents() == ['Голос: Мой голос'] * 2
        assert page.evaluate(NO_OVERFLOW) and not errors
