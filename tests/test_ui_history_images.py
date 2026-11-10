"""Картинка истории возвращается после смены чата; данные и запоздавшие ответы не переживают уход."""
import base64

import httpx
import pytest

from fake_bridge import T0, pid
from fakes import PNG_REAL
from test_codex import CATALOG
from test_ui_browser import CODE_CHAT_A, CODE_CHAT_B, code_free_machine, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
@pytest.mark.parametrize('cli', ['claude', 'codex'])
def test_image_is_restored_after_switching_chat_and_revoked_on_leave(desktop_browser, live, width, cli):
    browser = desktop_browser.browser_type.name
    login = f'img-{browser}-{cli}-{width}'
    live.add_user(login)
    message = {'id': 'picture-message', 'role': 'user', 'text': 'Посмотри на скриншот', 'steps': [], 'time': T0,
        'images': [{'index': 0, 'name': 'Изображение 1'}]}
    first = {**code_session('С изображением', messages=[message]), 'engine': cli,
        'image_data': {('picture-message', 0): {'mime': 'image/png', 'data': base64.b64encode(PNG_REAL).decode()}}}
    second = {**code_session('Другой чат', messages=[]), 'engine': cli}
    machine = code_free_machine({CODE_CHAT_A: first, CODE_CHAT_B: second})
    machine.history_images = True
    if cli == 'codex':
        machine.codex = CATALOG
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookies = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 320,
                is_mobile=width == 320 and browser != 'firefox', extra_http_headers={'Cookie': cookies}) as context:
            if browser == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            context.add_init_script("""(() => {
              window.revokedImages=[];
              const revoke=URL.revokeObjectURL.bind(URL);
              URL.revokeObjectURL=url => {window.revokedImages.push(url); revoke(url);};
            })()""")
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            path = f'#/code/{aid}/{pid("alpha")}/'
            page.goto(live.url + '/' + path + CODE_CHAT_A)
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            loaded = "() => document.querySelector('.code-sent-image')?.naturalWidth === 64"
            page.wait_for_function(loaded)
            source = page.locator('.code-sent-image').get_attribute('src')
            assert source.startswith('blob:')
            assert page.locator('.code-msg.user').inner_text() == 'Посмотри на скриншот'
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            assert page.locator('.code-sent-image').count() == 0
            assert source in page.evaluate('window.revokedImages')
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_A)
            page.wait_for_function(loaded)
            assert page.locator('.code-sent-image').get_attribute('src') != source
            calls = [row for row in machine.requests if row['op'] == 'history_image']
            assert len(calls) == 2
            assert all(row['chat'] == CODE_CHAT_A and row['message_id'] == 'picture-message' and row['index'] == 0 for row in calls)
            page.evaluate("""() => {
              const fetchOriginal=window.fetch;
              window.fetch=async (url, options) => {
                if (new URL(String(url),location.href).pathname.endsWith('/image'))
                  await new Promise(resolve => {window.releaseImage=resolve;});
                return fetchOriginal(url,options);
              };
            }""")
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_A)
            page.wait_for_function("() => typeof window.releaseImage === 'function'")
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            page.evaluate('window.releaseImage()')
            assert page.locator('.code-sent-image').count() == 0
            assert not errors, errors
    finally:
        channel.stop()
