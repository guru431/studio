"""Служебные сообщения Claude читаются как задачи, без XML и кнопки ветвления."""
import httpx
import pytest

from fake_bridge import FakeMachine, T0, pid
from test_ui_browser import CODE_CHAT_A, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
def test_claude_task_cards_preserve_output_and_are_safe(desktop_browser, live, width, tmp_path):
    login = f'claude-tasks-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    notice = '''<task-notification><task-id>monitor-1</task-id>
<summary>Запуск модели: сборка ядер</summary>
<event>Building module: fp4_gemm\n&lt;img src=x onerror="window.unsafe=true"&gt;</event>
If this event is something the user would act on now, send a PushNotification.
</task-notification>'''
    stopped = '''<task-notification><task-id>shell-1</task-id><task-id>__orphan_summary__shell</task-id>
<status>stopped</status><summary>Фоновая команда остановлена</summary>
<note>Internal scanner explanation</note></task-notification>'''
    messages = [{'id': 'ask', 'role': 'user', 'text': 'Какой статус?', 'time': T0, 'steps': []},
                {'id': 'notice', 'role': 'user', 'text': notice, 'time': T0, 'steps': []},
                {'id': 'stopped', 'role': 'user', 'text': stopped, 'time': T0, 'steps': []}]
    machine = FakeMachine({'alpha': {'chats': {CODE_CHAT_A: code_session('Задачи', messages=messages)}}})
    machine.chat_fork = True
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
            cards = page.locator('.code-task-notice')
            assert cards.count() == 2
            assert cards.locator('.code-fork').count() == 0
            assert page.locator('.code-msg.user').count() == 1
            assert 'Обновление' in cards.first.inner_text() and 'Остановлена' in cards.last.inner_text()
            assert 'Building module' in cards.first.inner_text()
            assert '<task-notification>' not in cards.all_text_contents()[0]
            assert 'PushNotification' not in cards.first.inner_text() and '__orphan_summary' not in cards.last.inner_text()
            assert cards.locator('img').count() == 0 and page.evaluate('window.unsafe !== true')
            output = cards.first.locator('details')
            output.locator('summary').focus()
            output.locator('summary').press('Enter')
            assert not output.evaluate('node => node.open')
            output.locator('summary').press('Enter')
            for theme in ('light', 'dark'):
                page.evaluate("theme => {document.documentElement.dataset.theme=theme; document.documentElement.style.setProperty('--font-scale','1.5')}", theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert cards.first.evaluate('node => node.scrollWidth <= node.clientWidth')
                page.screenshot(path=str(tmp_path / f'claude-tasks-{desktop_browser.browser_type.name}-{width}-{theme}.png'))
            page.reload()
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            assert cards.count() == 2 and not errors
    finally:
        channel.stop()
