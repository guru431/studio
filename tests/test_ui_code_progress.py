"""Хронология, команды, правки и восстановление живого хода в трёх браузерах."""
import httpx
import pytest

from fake_bridge import FakeMachine, T0, pid
from test_codex import CATALOG
from test_ui_browser import CODE_CHAT_A, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]
PATCH = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n-old = 1\n+new = 2\n+extra = 3\n"
CHILD = "01999999-2222-7333-8444-888888888888"


@pytest.mark.parametrize('width', [320, 1280])
def test_timeline_changes_agents_and_running_output_survive_reload(desktop_browser, live, width, tmp_path):
    login = f'progress-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    first, last = 'Проверю 🛠', 'Готово'
    message = {'id': 'answer', 'role': 'assistant', 'text': first + last, 'time': T0,
        'steps': [{'id': 'edit', 'tool': 'Edit', 'input': {}, 'text': '', 'status': 'completed',
                   'changes': [{'path': 'src/a.py', 'patch': PATCH}]}],
        'timeline': [{'kind': 'text', 'start': 0, 'end': 10, 'block': 'intro'}, {'kind': 'step', 'id': 'edit'},
                     {'kind': 'text', 'start': 10, 'end': 16, 'block': 'final'}],
        'progress': {'changes': {'diff': PATCH, 'files': [{'path': 'src/a.py', 'added': 2, 'removed': 1}]},
            'plan': [{'step': 'Проверить', 'status': 'completed'}],
            'agents': [{'id': CHILD, 'title': 'GPU backend', 'status': 'completed', 'task': 'Проверка GPU'}]}}
    session = code_session('Прогресс', messages=[message])
    session['engine'] = 'codex'
    session['children'] = {CHILD: {'title': 'GPU backend', 'messages': [
        {'role': 'assistant', 'text': 'Результат GPU', 'steps': []}]}}
    machine = FakeMachine({'alpha': {'files': {'src/a.py': b'new = 2\nextra = 3'}, 'chats': {CODE_CHAT_A: session}}})
    machine.codex, machine.subagents = CATALOG, True
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 320,
                                         extra_http_headers={'Cookie': cookie}) as context:
            if desktop_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))
            route = f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}'
            page.goto(route)
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.wait_for_function("() => document.querySelector('.code-timeline')?.textContent.includes('Готово')")
            parts = page.locator('.code-timeline').evaluate("node => [...node.children].map(e => e.classList.contains('code-step') ? e.dataset.id : e.textContent.trim())")
            assert parts == [first, 'edit', last]
            page.locator('.code-step .code-changes').wait_for()
            assert page.locator('.code-diff-split').first.is_visible() == (width == 1280)
            assert page.locator('.code-diff-unified').first.is_visible() == (width == 320)
            summary = page.locator('.code-progress-summary').first
            assert '+2' in summary.inner_text() and '−1' in summary.inner_text()
            summary.click()
            page.screenshot(path=str(tmp_path / f'reference-progress-{desktop_browser.browser_type.name}-{width}.png'))
            page.locator('.code-progress-agent').click()
            page.get_by_text('Результат GPU', exact=True).wait_for()
            page.locator('#code-subagents-dialog .dialog-actions button').click()
            page.locator('.code-progress-file').click()
            page.locator('#code-editor').wait_for()
            assert 'src/a.py' not in page.url
            page.goto(route)
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            machine.script = [
                {'k': 'text', 'text': 'Проверка началась 🛠', 'block': 'intro'},
                {'k': 'step', 'id': 'cmd', 'tool': 'Shell', 'input': {'command': 'pytest'}, 'status': 'running', 'process_id': '123'},
                {'k': 'step_update', 'id': 'cmd', 'text': '12 passed\n<img src=x onerror="window.unsafe=true">'},
                {'k': 'progress', 'plan': [{'step': 'Проверить тесты', 'status': 'inProgress'}]},
            ]
            page.locator('#code-text').fill('Запусти проверку')
            page.locator('#code-send').click()
            page.locator('.code-step[data-id="cmd"]').wait_for()
            page.locator('.code-step[data-id="cmd"] summary').click()
            page.get_by_text('12 passed', exact=False).wait_for()
            assert page.locator('.code-step[data-id="cmd"]').get_attribute('data-state') == 'running'
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.locator('.code-step[data-id="cmd"]').wait_for()
            page.locator('.code-step[data-id="cmd"] summary').click()
            page.get_by_text('12 passed', exact=False).wait_for()
            assert 'Выполняются команды: 1' in page.locator('.code-progress-summary').last.inner_text()
            assert page.evaluate('window.unsafe !== true')
            run = machine.running[CODE_CHAT_A]
            run['rest'] = [
                {'k': 'result', 'id': 'cmd', 'text': '1 failed', 'error': True, 'status': 'failed', 'duration_ms': 1220, 'exit_code': 1},
                {'k': 'text', 'text': 'Проверка завершена', 'block': 'final'},
                {'k': 'done', 'status': 'done'},
            ]
            frames = []
            machine._play(CODE_CHAT_A, frames)
            for frame in frames:
                channel._send(frame)
            page.wait_for_function("() => document.querySelector('#code-chat').dataset.running === '0'")
            page.wait_for_function("() => document.querySelector('#code-feed').textContent.includes('Проверка завершена')")
            command = page.locator('.code-step[data-id="cmd"]')
            assert command.get_attribute('data-state') == 'failed'
            assert '1,22 с' in command.inner_text() and 'код 1' in command.inner_text()
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.wait_for_function("() => document.querySelector('#code-feed').textContent.includes('Проверка завершена')")
            assert page.locator('.code-step[data-id="cmd"]').get_attribute('data-state') == 'failed'
            for theme in ['light', 'dark']:
                page.evaluate("theme => { const control = document.querySelector('#theme-mode'); control.value = theme; control.dispatchEvent(new Event('change', {bubbles: true})); document.documentElement.style.setProperty('--font-scale', '1.5'); }", theme)
                page.wait_for_function("theme => document.querySelector(theme === 'dark' ? '#hl-graphite' : '#hl-paper').media === 'all'", arg=theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(tmp_path / f'progress-{desktop_browser.browser_type.name}-{width}-{theme}.png'))
            assert not errors
            assert not any('src/a.py' in url or 'window.unsafe' in url for url in requests)
    finally:
        channel.stop()


@pytest.mark.parametrize('width', [320, 1551])
def test_web_search_updates_in_place_and_long_chat_scrolls_above_composer(desktop_browser, live, width):
    login = f'search-scroll-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    session = code_session('Поиск', messages=[])
    session['engine'] = 'codex'
    machine = FakeMachine({'alpha': {'files': {}, 'chats': {CODE_CHAT_A: session}}})
    machine.codex = CATALOG
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850},
                                         extra_http_headers={'Cookie': cookie}) as context:
            if desktop_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            machine.script = [{'k': 'step', 'id': 'search', 'tool': 'WebSearch', 'input': {'query': ''}}]
            page.locator('#code-text').fill('Запрос')
            page.locator('#code-send').click()
            search = page.locator('.code-step[data-id="search"]')
            search.locator('summary').click()
            run = machine.running[CODE_CHAT_A]
            run['rest'] = [
                {'k': 'result', 'id': 'search', 'input': {'query': 'Codex caching'},
                 'text': 'Prompt caching\nhttps://example.com/caching'},
                {'k': 'text', 'text': 'Длинный ответ.\n\n' * 55 + 'Конец ответа.', 'block': 'final'},
                {'k': 'done', 'status': 'done'},
            ]
            frames = []
            machine._play(CODE_CHAT_A, frames)
            for frame in frames:
                channel._send(frame)
            page.get_by_text('Конец ответа.', exact=True).wait_for(state='attached')
            assert search.locator('.code-step-head').inner_text() == 'Codex caching'
            assert 'Codex caching' in search.locator('.code-step-input').inner_text()
            assert 'https://example.com/caching' in search.locator('.code-step-result').inner_text()
            assert page.evaluate('''() => {
              const scroll = document.querySelector('#code-chat-scroll');
              const composer = document.querySelector('.code-compose-area');
              const bottom = composer.getBoundingClientRect().bottom;
              scroll.scrollTop = 0;
              const scrolls = scroll.scrollHeight > scroll.clientHeight;
              const separated = scroll.getBoundingClientRect().bottom <= composer.getBoundingClientRect().top;
              scroll.scrollTop = scroll.scrollHeight;
              const end = document.querySelector('.code-timeline-text p:last-child').getBoundingClientRect();
              return scrolls && separated && composer.getBoundingClientRect().bottom === bottom
                && end.bottom <= scroll.getBoundingClientRect().bottom + 1 && bottom <= innerHeight;
            }''')
    finally:
        channel.stop()
