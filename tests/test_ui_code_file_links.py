"""Local file links in assistant history and streamed output use the project viewer."""
import httpx
import pytest

from fake_bridge import FakeMachine, T0, pid
from test_media import png
from test_ui_browser import CODE_CHAT_A, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
def test_assistant_file_links_open_images_and_text_without_http_paths(desktop_browser, live, width):
    login = f'file-links-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    picture = '.superpowers/ui-review/превью экрана.png'
    links = [f'/C:/work/projects/alpha/{picture}', f'C:/work/projects/alpha/{picture}',
             f'file:///C:/work/projects/alpha/{picture}', picture,
             'C:/work/projects/alpha/README.md:12', '/C:/work/projects/beta/private.png',
             '/C:/work/projects/alpha/../beta/private.png', 'https://example.org/report']
    content = '\n\n'.join(f'[link-{index}](<{href}>)' for index, href in enumerate(links))
    messages = [{'id': 'answer', 'role': 'assistant', 'text': content + '\n\n' + 'Paragraph.\n\n' * 800,
                 'steps': [], 'time': T0}]
    machine = FakeMachine({'alpha': {'files': {picture: png(160, 100), 'README.md': b'# Preview text'},
                                    'chats': {CODE_CHAT_A: code_session('Files', messages=messages)}}})
    aid, bridge = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 800},
                                         extra_http_headers={'Cookie': cookie}) as context:
            if desktop_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            requests, errors = [], []
            page.on('request', lambda request: requests.append(request.url))
            page.on('pageerror', lambda error: errors.append(str(error)))
            route = f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}'
            for index in range(5):
                page.goto(route)
                page.locator('#code-chat[data-loaded="1"]').wait_for()
                page.get_by_role('link', name=f'link-{index}', exact=True).click()
                if index < 4:
                    image = page.locator('#code-file-image')
                    image.wait_for()
                    page.wait_for_function("() => document.querySelector('#code-file-image')?.naturalWidth === 160")
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                else:
                    page.locator('#code-md').wait_for()
                    assert page.locator('#code-md').inner_text() == 'Preview text'
                assert page.url.endswith('?tab=files') and picture not in page.url
            page.goto(route)
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            for index in (5, 6):
                assert page.get_by_text(f'link-{index}', exact=True).get_attribute('href') is None
            external = page.get_by_role('link', name='link-7', exact=True)
            assert external.get_attribute('href') == 'https://example.org/report'
            assert external.get_attribute('target') == '_blank'
            # The short live renderer and final renderer must both attach local navigation.
            machine.script = [{'k': 'text', 'text': f'[Streamed](</C:/work/projects/alpha/{picture}>)'}]
            page.locator('#code-text').fill('Preview')
            page.locator('#code-send').click()
            page.get_by_role('link', name='Streamed', exact=True).click()
            page.wait_for_function("() => document.querySelector('#code-file-image')?.naturalWidth === 160")
            assert not errors
            assert not any('/C:/' in url or 'private.png' in url or 'превью' in url for url in requests)
            assert len(context.pages) == 1
    finally:
        bridge.stop()

