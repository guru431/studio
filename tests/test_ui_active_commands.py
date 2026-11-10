"""Активные команды в статусе: счётчик, раскрытие и обновление без потери раскрытого шага."""
import httpx
import pytest

from fake_bridge import pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 393, 1280])
def test_active_commands_counter_and_expandable_live_list(desktop_browser, live, width, tmp_path):
    engine = desktop_browser.browser_type.name
    login = f'commands-{engine}-{width}'
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Команды', messages=[])})
    machine.chat_fork = machine.subagents = True
    machine.script = [
        {'k': 'step', 'id': 'a', 'tool': 'Shell', 'input': {'command': 'echo alpha'}, 'status': 'running'},
        {'k': 'step', 'id': 'read', 'tool': 'Read', 'input': {'file_path': 'README.md'}, 'status': 'running'},
        {'k': 'step', 'id': 'b', 'tool': 'Bash', 'input': {'command': 'echo beta'}, 'status': 'running'},
        {'k': 'step_update', 'id': 'a', 'text': 'Вывод первой команды'},
    ]
    aid, channel = code_online(live, login, machine)
    # Сервер фикстуры — локальный HTTP; загрузка системного TLS-хранилища Windows не требуется.
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'}, verify=False)
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width < 760,
                is_mobile=width < 760 and engine != 'firefox', extra_http_headers={'Cookie': cookie}) as context:
            if engine == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            button = page.locator('#code-active-commands')
            assert button.is_visible()
            assert button.locator('.code-active-count').inner_text() == '0'
            button.tap() if width < 760 and engine != 'firefox' else button.click()
            panel = page.locator('#code-command-menu:popover-open')
            panel.get_by_text('Нет выполняемых команд', exact=True).wait_for()
            page.keyboard.press('Escape')
            page.locator('#code-text').fill('Проверь')
            page.locator('#code-send').click()
            page.wait_for_function("() => document.querySelector('#code-active-commands .code-active-count').textContent==='2'")
            assert page.locator('.code-progress-processes').count() == 0
            assert page.locator('#code-feed').get_by_text('Выполняются команды', exact=False).count() == 0
            page.evaluate("document.documentElement.style.setProperty('--font-scale','1.5')")
            page.evaluate("() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
            assert button.evaluate("n=>{const r=n.getBoundingClientRect(),b=n.closest('.code-status-bar').getBoundingClientRect();"
                "return r.left>=b.left+b.width/2-1 && r.right<=b.right+1;}")
            button.tap() if width < 760 and engine != 'firefox' else button.click()
            panel = page.locator('#code-command-menu:popover-open')
            assert panel.locator('.code-step').count() == 2
            panel.locator('[data-id=a] summary').click()
            panel.locator('[data-id=b] summary').click()
            assert 'echo alpha' in panel.locator('[data-id=a] .code-step-input').inner_text()
            assert 'echo beta' in panel.locator('[data-id=b] .code-step-input').inner_text()

            def play(*events):
                machine.running[CODE_CHAT_A]['rest'] = list(events)
                frames = []
                machine._play(CODE_CHAT_A, frames)
                for frame in frames:
                    channel._send(frame)

            play({'k': 'step_update', 'id': 'b', 'text': 'Обновлённый вывод'})
            panel.locator('[data-id=b] .code-step-result').get_by_text('Обновлённый вывод', exact=True).wait_for()
            assert panel.locator('[data-id=b]').get_attribute('open') is not None
            play({'k': 'result', 'id': 'a', 'text': 'готово', 'status': 'completed'})
            page.wait_for_function("() => document.querySelector('#code-active-commands .code-active-count').textContent==='1'")
            assert panel.locator('[data-id=a]').count() == 0
            assert panel.locator('[data-id=b][open]').count() == 1
            assert page.locator('#code-feed .code-step[data-id=a]').count() == 1
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(tmp_path / f'active-commands-{engine}-{width}.png'))
            play({'k': 'result', 'id': 'b', 'text': 'готово', 'status': 'completed'},
                 {'k': 'result', 'id': 'read', 'text': 'готово', 'status': 'completed'},
                 {'k': 'done', 'status': 'done'})
            page.wait_for_function("() => document.querySelector('#code-active-commands .code-active-count').textContent==='0'")
            assert button.is_visible()
            panel.get_by_text('Нет выполняемых команд', exact=True).wait_for()
            assert panel.locator('.code-step').count() == 0
    finally:
        channel.stop()
