"""History/worker/stream previews and isolated interaction on desktop and touch layouts."""
import json

import httpx
import pytest

from fake_bridge import FakeMachine, T0, pid
from test_ui_browser import CODE_CHAT_A, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]
SOURCE = 'C:/Users/owner/.codex/visualizations/2026/11/08/thread/dashboard.html'
MARKER = 'visualize' + json.dumps({'path': SOURCE, 'mode': 'wide', 'title': 'Панель'}) + ''
FRAGMENT = '''<button id="increment" onclick="this.textContent=Number(this.textContent)+1">0</button>
<i data-lucide="check" id="icon"></i>
<p id="isolation"></p><script>
try { parent.document.body.dataset.compromised = '1'; }
catch { document.querySelector('#isolation').textContent = 'isolated'; }
try { document.cookie; document.querySelector('#isolation').textContent = 'cookie access'; } catch {}
</script>'''


@pytest.mark.parametrize('width', [320, 1280])
def test_visualizations_in_history_and_stream_are_interactive_and_isolated(desktop_browser, live, width, tmp_path):
    login = f'visualization-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    text = MARKER + '\n\n[Открыть фотовизуализацию](' + SOURCE + ')\n\n```text\n' + MARKER + '\n```\n\n' + 'Paragraph.\n\n' * 800
    message = {'id': 'answer', 'role': 'assistant', 'text': text, 'steps': [], 'time': T0}
    session = code_session('Панель', messages=[message])
    machine = FakeMachine({'alpha': {'files': {SOURCE: FRAGMENT.encode()}, 'chats': {CODE_CHAT_A: session}}})
    machine.visualizations = True
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 320,
                                         extra_http_headers={'Cookie': cookie}) as context:
            if desktop_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            # Внешняя сеть недоступна сценарию: lucide должен прийти из /static/vendor студии.
            context.route(lambda url: not url.startswith(live.url) and not url.startswith(('data:', 'blob:', 'about:')),
                          lambda route: route.abort())
            page = context.new_page()
            page.set_default_timeout(10000)
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            opener = page.locator('.code-visualization-open')
            opener.wait_for()
            assert opener.count() == 1
            assert MARKER in page.locator('pre code').inner_text()
            opener.focus()
            opener.press('Enter')
            frame = page.frame_locator('#code-visualization iframe')
            frame.locator('#isolation').get_by_text('isolated', exact=True).wait_for()
            frame.locator('svg.lucide').wait_for()          # значок нарисован копией студии под CSP 'self'
            frame.locator('#increment').click()
            assert frame.locator('#increment').inner_text() == '1'
            assert page.locator('body').get_attribute('data-compromised') is None
            assert page.locator('#code-visualization iframe').get_attribute('sandbox') == 'allow-scripts'
            page.evaluate("document.documentElement.style.setProperty('--font-scale', '1.5')")
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            bounds = page.locator('#code-visualization').bounding_box()
            assert bounds['width'] <= width and bounds['height'] <= 850
            page.screenshot(path=str(tmp_path / f'visualization-{desktop_browser.browser_type.name}-{width}.png'))
            close = page.locator('#code-visualization').get_by_role('button', name='Закрыть')
            close.tap() if width == 320 else close.click()
            assert page.locator('#code-visualization').count() == 0
            page.locator('.code-visualization-link').click()
            frame.locator('#increment').wait_for()
            assert frame.locator('#increment').inner_text() == '0'
            close.click()
            machine.script = [{'k': 'text', 'text': MARKER[:30]}, {'k': 'text', 'text': MARKER[30:]}]
            page.locator('#code-text').fill('Покажи панель')
            page.locator('#code-send').click()
            page.wait_for_function("() => document.querySelectorAll('.code-visualization-open').length === 2")
            page.locator('.code-visualization-open').last.click()
            frame.locator('#increment').wait_for()
            page.evaluate("document.dispatchEvent(new Event('studio-code-clear'))")
            assert page.locator('#code-visualization').count() == 0
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.locator('.code-visualization-open').last.wait_for()
            assert not any('/Users/owner/' in request or 'dashboard.html' in request for request in requests)
            assert '/static/vendor/lucide.min.js' in ' '.join(requests)
            assert all(request.startswith((live.url, 'data:', 'blob:', 'about:')) for request in requests), requests
            assert len(context.pages) == 1 and not errors
    finally:
        channel.stop()
