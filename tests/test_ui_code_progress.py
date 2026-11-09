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
def test_long_completed_timeline_keeps_all_text_and_toggles_commands(desktop_browser, live, width, tmp_path):
    login = f'long-timeline-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    session = code_session('Длинный ответ', messages=[])
    session['engine'] = 'codex'
    machine = FakeMachine({'alpha': {'files': {}, 'chats': {CODE_CHAT_A: session}}})
    machine.codex = CATALOG
    expected = [f'Пояснение {index} 🛠' for index in range(7)]
    machine.script = []
    for index, value in enumerate(expected):
        machine.script.extend([
            {'k': 'text', 'text': value, 'block': f'text-{index}'},
            {'k': 'step', 'id': f'cmd-{index}', 'tool': 'Shell',
             'input': {'command': 'pytest tests/очень_длинное_название_проверки.py ' * 3}},
            {'k': 'result', 'id': f'cmd-{index}', 'text': 'passed', 'status': 'completed'},
        ])
    machine.script.extend([{'k': 'text', 'text': 'Все проверки завершены', 'block': 'final'},
                           {'k': 'done', 'status': 'done'}])
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
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            toggle = page.locator('#code-toggle-steps')
            assert toggle.is_disabled()
            page.locator('#code-text').fill('Проверь проект')
            page.locator('#code-send').click()
            page.wait_for_function("() => document.querySelector('#code-chat').dataset.running === '0' && document.querySelectorAll('.code-step').length === 7")
            page.get_by_text('Все проверки завершены', exact=True).wait_for()
            expected.append('Все проверки завершены')
            assert [value.strip() for value in page.locator('.code-timeline-text').all_text_contents()] == expected
            assert page.locator('.code-step[open]').count() == 0
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            assert [value.strip() for value in page.locator('.code-timeline-text').all_text_contents()] == expected
            assert page.locator('.code-step').count() == 7
            assert page.locator('.code-step[open]').count() == 0
            assert page.locator('.code-step-input').count() == 0
            assert page.locator('.code-message-nav').evaluate('node => [...node.children].map(child => child.id)') == [
                'code-message-up', 'code-toggle-steps', 'code-message-down']
            navigation_fits = """node => {
              const boxes = [...node.children].map(child => child.getBoundingClientRect());
              const scroll = document.querySelector('#code-chat-scroll').getBoundingClientRect();
              const status = document.querySelector('.code-status-bar').getBoundingClientRect();
              return boxes.every(box => Math.abs(box.left - boxes[0].left) < 1
                && box.top >= scroll.top && box.bottom <= scroll.bottom && box.right <= scroll.right)
                && boxes[0].bottom < boxes[1].top && boxes[1].bottom < boxes[2].top
                && node.getBoundingClientRect().bottom <= status.top;
            }"""
            preview_fits = """node =>
              node.getBoundingClientRect().height <= 2 * parseFloat(getComputedStyle(node).lineHeight) + 1"""
            assert page.locator('.code-message-nav').evaluate(navigation_fits)
            assert page.locator('.code-step-head').first.evaluate(preview_fits)
            assert toggle.get_attribute('aria-label') == 'Развернуть все команды'
            if width == 320:
                toggle.tap()
            else:
                toggle.focus()
                toggle.press('Enter')
            page.wait_for_function("() => document.querySelectorAll('.code-step[open] .code-step-result').length === 7")
            assert toggle.get_attribute('aria-pressed') == 'true'
            assert toggle.get_attribute('aria-label') == 'Свернуть все команды'
            assert page.locator('.code-step-result').all_text_contents() == ['passed'] * 7
            page.locator('.code-step summary').first.click()
            page.wait_for_function("() => document.querySelector('#code-toggle-steps').getAttribute('aria-pressed') === 'false'")
            toggle.click()
            assert page.locator('.code-step[open]').count() == 7
            toggle.click()
            assert page.locator('.code-step[open]').count() == 0
            page.evaluate("document.documentElement.style.setProperty('--font-scale', '1.5')")
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.code-message-nav').evaluate(navigation_fits)
            assert page.locator('.code-step-head').first.evaluate(preview_fits)
            page.screenshot(path=str(tmp_path / f'commands-{desktop_browser.browser_type.name}-{width}.png'))
            toggle.click()
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            assert page.locator('.code-step[open]').count() == 0
            assert [value.strip() for value in page.locator('.code-timeline-text').all_text_contents()] == expected
            assert not errors
    finally:
        channel.stop()


@pytest.mark.parametrize('width', [320, 1280])
def test_timeline_changes_agents_and_running_output_survive_reload(desktop_browser, live, width, tmp_path):
    login = f'progress-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    first, last = 'Проверю 🛠', 'Готово'
    message = {'id': 'answer', 'role': 'assistant', 'text': first + last, 'time': T0,
        'steps': [{'id': 'edit', 'tool': 'Edit', 'input': {'file_path': 'src/a.py'}, 'text': '', 'status': 'completed',
                   'changes': [{'path': 'src/a.py', 'patch': PATCH},
                               {'path': 'src/b.py', 'before': 'old', 'after': 'new'},
                               {'path': 'src/a.py', 'before': 'extra = 2', 'after': 'extra = 3'}]}],
        'timeline': [{'kind': 'text', 'start': 0, 'end': 10, 'block': 'intro'}, {'kind': 'step', 'id': 'edit'},
                     {'kind': 'text', 'start': 10, 'end': 16, 'block': 'final'}],
        'progress': {'changes': {'diff': PATCH, 'files': [{'path': 'src/a.py', 'added': 2, 'removed': 1},
                                                       {'path': 'src/without-diff.py'}]},
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
            assert page.locator('.code-step[open]').count() == 0
            assert page.locator('.code-step-head').inner_text() == 'Файлов: 2'
            assert page.locator('.code-step-file').all_text_contents() == ['src/a.py', 'src/b.py']
            page.locator('.code-step summary').click()
            page.locator('.code-step .code-changes').wait_for()
            assert page.locator('.code-diff-split').first.is_visible() == (width == 1280)
            assert page.locator('.code-diff-unified').first.is_visible() == (width == 320)
            summary = page.locator('.code-progress-summary').first
            assert 'Файлов:' not in summary.inner_text()
            summary.click()
            page.screenshot(path=str(tmp_path / f'reference-progress-{desktop_browser.browser_type.name}-{width}.png'))
            page.locator('.code-progress-agent').click()
            page.get_by_text('Результат GPU', exact=True).wait_for()
            page.locator('#code-subagents-dialog .dialog-actions button').click()
            changed = page.locator('#code-changed-files')
            assert changed.locator('.code-changed-count').inner_text() == '2'
            assert changed.evaluate("node => node.closest('.code-status-bar').contains(document.querySelector('#code-cache'))")
            assert page.locator('.code-progress-file').count() == 0
            for theme in ['light', 'dark']:
                page.evaluate("theme => { document.documentElement.dataset.theme = theme; document.documentElement.style.setProperty('--font-scale', '1.5'); }", theme)
                if width == 320:
                    changed.tap()
                else:
                    changed.focus()
                    changed.press('Enter')
                dialog = page.locator('#code-changed-dialog')
                assert dialog.locator('.code-changed-file').count() == 2
                assert dialog.locator('.code-changes').count() == 0
                dialog.locator('.code-changed-file').nth(1).click()
                assert 'Помощник не передал изменения' in dialog.inner_text()
                dialog.locator('.code-changed-back').click()
                dialog.locator('.code-changed-file').first.click()
                assert '+2' in dialog.inner_text() and '−1' in dialog.inner_text()
                assert 'old = 1' in dialog.inner_text() and 'new = 2' in dialog.inner_text()
                assert dialog.locator('.code-diff-split').is_visible() == (width == 1280)
                assert dialog.locator('.code-diff-unified').is_visible() == (width == 320)
                assert dialog.evaluate('node => node.scrollWidth <= node.clientWidth')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert changed.evaluate('node => node.getBoundingClientRect().right <= innerWidth')
                page.screenshot(path=str(tmp_path / f'changed-files-{desktop_browser.browser_type.name}-{width}-{theme}.png'))
                if theme == 'light':
                    page.keyboard.press('Escape')
                    dialog.wait_for(state='detached')
                    assert changed.get_attribute('aria-expanded') == 'false'
            page.locator('.code-changed-open').click()
            page.locator('#code-editor').wait_for()
            assert page.locator('#code-changed-dialog').count() == 0
            assert 'src/a.py' not in page.url
            page.goto(route)
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            assert changed.locator('.code-changed-count').inner_text() == '2'
            machine.script = [
                {'k': 'text', 'text': 'Проверка началась 🛠', 'block': 'intro'},
                {'k': 'step', 'id': 'cmd', 'tool': 'Shell', 'input': {'command': 'pytest'}, 'status': 'running', 'process_id': '123'},
                {'k': 'step_update', 'id': 'cmd', 'text': '12 passed\n<img src=x onerror="window.unsafe=true">'},
                {'k': 'progress', 'plan': [{'step': 'Проверить тесты', 'status': 'inProgress'}]},
            ]
            page.locator('#code-text').fill('Запусти проверку')
            page.locator('#code-send').click()
            page.locator('.code-step[data-id="cmd"]').wait_for()
            assert changed.locator('.code-changed-count').inner_text() == '0'
            changed.click()
            assert 'Изменённых файлов нет' in page.locator('#code-changed-dialog').inner_text()
            page.locator('#code-changed-dialog .dialog-close').click()
            page.locator('#code-toggle-steps').click()
            assert page.locator('.code-step[open]').count() == 2
            page.get_by_text('12 passed', exact=False).wait_for()
            assert page.locator('.code-step[data-id="cmd"]').get_attribute('data-state') == 'running'
            changed.click()
            run = machine.running[CODE_CHAT_A]
            run['rest'] = [{'k': 'step', 'id': 'next-cmd', 'tool': 'Shell', 'input': {'command': 'check'},
                            'status': 'running'},
                           {'k': 'progress', 'changes': {'diff': PATCH, 'files': [{'path': 'src/a.py'}], 'truncated': True}}]
            frames = []
            machine._play(CODE_CHAT_A, frames)
            for frame in frames:
                channel._send(frame)
            page.wait_for_function("() => document.querySelector('#code-changed-files').textContent === '≥1'")
            page.locator('.code-changed-file').click()
            assert 'показана часть' in page.locator('#code-changed-dialog').inner_text()
            page.keyboard.press('Escape')
            page.locator('#code-changed-dialog').wait_for(state='detached')
            page.locator('.code-step[data-id="next-cmd"][open] .code-step-input').wait_for()
            page.locator('#code-toggle-steps').click()
            assert page.locator('.code-step[open]').count() == 0
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            assert changed.locator('.code-changed-count').inner_text() == '≥1'
            page.locator('.code-step[data-id="cmd"]').wait_for()
            assert page.locator('.code-step[open]').count() == 0
            page.locator('.code-step[data-id="cmd"] summary').click()
            page.get_by_text('12 passed', exact=False).wait_for()
            assert 'Выполняются команды: 2' in page.locator('.code-progress-summary').last.inner_text()
            assert page.evaluate('window.unsafe !== true')
            run = machine.running[CODE_CHAT_A]
            run['rest'] = [
                {'k': 'result', 'id': 'cmd', 'text': '1 failed', 'error': True, 'status': 'failed', 'duration_ms': 1220, 'exit_code': 1},
                {'k': 'result', 'id': 'next-cmd', 'text': 'passed', 'status': 'completed'},
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
                page.evaluate("theme => { const control = document.querySelector('#menu-theme-' + theme); control.checked = true; control.dispatchEvent(new Event('change', {bubbles: true})); document.documentElement.style.setProperty('--font-scale', '1.5'); }", theme)
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


@pytest.mark.parametrize('width', [320, 1280])
def test_followups_stay_between_commands_and_split_streamed_text_after_reload(desktop_browser, live, width):
    login = f'followup-order-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    session = {**code_session('Уточнения'), 'engine': 'codex'}
    machine = FakeMachine({'alpha': {'chats': {CODE_CHAT_A: session}}})
    machine.codex = CATALOG
    machine.script = [{'k': 'text', 'text': 'До 🛠', 'block': 'intro'},
                      {'k': 'step', 'id': 'before', 'tool': 'Shell', 'input': {'command': 'first'}, 'status': 'running'}]
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
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.locator('#code-text').fill('Начни')
            page.locator('#code-send').click()
            page.locator('.code-step[data-id="before"]').wait_for()

            def play(*events):
                machine.running[CODE_CHAT_A]['rest'] = list(events)
                frames = []
                machine._play(CODE_CHAT_A, frames)
                for frame in frames:
                    channel._send(frame)

            def clarify(text):
                page.locator('#code-text').fill(text)
                page.locator('#code-send').click()
                page.get_by_text(text, exact=True).wait_for()

            clarify('Уточнение №1')
            play({'k': 'text', 'text': 'После первого 🛠', 'block': 'reply'})
            page.get_by_text('После первого 🛠', exact=True).wait_for()
            clarify('Уточнение №2')
            play({'k': 'text', 'text': 'После второго', 'block': 'reply'},
                 {'k': 'result', 'id': 'before', 'text': 'done', 'status': 'completed'},
                 {'k': 'step', 'id': 'after', 'tool': 'Shell', 'input': {'command': 'last'}, 'status': 'running'})
            page.locator('.code-step[data-id="after"]').wait_for()
            order = """() => [...document.querySelectorAll('.code-timeline > *')].map(node =>
                node.classList.contains('code-step') ? node.dataset.id : node.textContent.trim()).filter(Boolean)"""
            expected = ['До 🛠', 'before', 'Уточнение №1', 'После первого 🛠', 'Уточнение №2', 'После второго', 'after']
            page.wait_for_function("() => document.querySelector('#code-feed').textContent.includes('После второго')")
            assert page.evaluate(order) == expected
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.locator('.code-step[data-id="after"]').wait_for()
            page.get_by_text('После первого 🛠', exact=True).wait_for()
            page.get_by_text('После второго', exact=True).wait_for()
            assert page.evaluate(order) == expected
            assert page.locator('[data-followup]').count() == 2
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
    finally:
        channel.stop()
