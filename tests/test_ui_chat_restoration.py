"""Статус, слежение за ходом и живые/завершённые подзадачи на телефоне и компьютере."""
import httpx
import pytest

from fake_bridge import T0, pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
def test_status_budget_following_and_subagent_history(desktop_browser, live, width, tmp_path):
    engine = desktop_browser.browser_type.name
    login = f'restore-chat-{engine}-{width}'
    live.add_user(login)
    messages = [{'id': str(i), 'role': 'user' if i % 2 == 0 else 'assistant',
                 'text': 'История.\n\n' * 25, 'steps': [], 'time': T0 + i} for i in range(18)]
    session = code_session('Рабочий чат', messages=messages)
    session['children'] = {
        'first': {'title': 'Первая проверка', 'status': 'running', 'messages': []},
        'old': {'title': 'Вчерашняя проверка', 'status': 'completed', 'messages': [
            {'role': 'assistant', 'text': 'Сохранённый результат', 'steps': []}]},
    }
    machine = code_free_machine({CODE_CHAT_A: session})
    machine.subagents = machine.chat_fork = True
    machine._op_subagents = lambda req, events: {'items': [
        {'id': key, 'title': row['title'], 'status': row['status']}
        for key, row in session['children'].items()]}
    machine.script = [{'k': 'text', 'text': 'Начало выполнения.\n\n' * 80, 'block': 'intro'}]
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850},
                has_touch=width == 320, is_mobile=width == 320 and engine != 'firefox',
                extra_http_headers={'Cookie': cookie}) as context:
            if engine == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda err: errors.append(str(err)))
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.wait_for_function("() => document.querySelector('.code-subagents-count').textContent === '1'")
            geometry = """() => {
              const bar = document.querySelector('.code-status-bar').getBoundingClientRect();
              const text = document.querySelector('#code-activity').getBoundingClientRect();
              const controls = document.querySelector('.code-status-controls').getBoundingClientRect();
              const pinned = [...document.querySelectorAll('.code-status-fixed > button')]
                .filter(node => !node.hidden).map(node => node.getBoundingClientRect());
              return {text: text.width >= bar.width / 2 - 1, controls: controls.width <= bar.width / 2 + 1,
                right: pinned.length >= 4 && pinned.every((r, i) => r.width > 0 && r.right <= bar.right + 1
                  && r.left >= text.right - 1 && (!i || r.left >= pinned[i - 1].right - 1)),
                fits: document.documentElement.scrollWidth <= innerWidth};
            }"""
            for theme in ('light', 'dark'):
                page.evaluate("theme => {document.documentElement.dataset.theme=theme; document.documentElement.style.setProperty('--font-scale','1.5');}", theme)
                page.wait_for_function("() => !document.querySelector('#code-status-more').hidden || innerWidth > 600")
                assert all(page.evaluate(geometry).values()), page.evaluate("() => JSON.stringify([...document.querySelectorAll('.code-status-bar, .code-status-controls, .code-status-fixed > button, #code-status-more, #code-activity')].map(n=>[n.id||n.className,n.hidden,n.getBoundingClientRect().toJSON()]))")
            if width == 320:
                more = page.locator('#code-status-more')
                more.tap()
                page.locator('#code-status-menu:popover-open').wait_for()
                page.locator('#code-share-chat').tap()
                page.locator('#code-share-chat-menu:popover-open').wait_for()
                page.keyboard.press('Escape')
                page.keyboard.press('Escape')
            else:
                page.locator('#code-share-chat').click()
                page.locator('#code-share-chat-menu:popover-open').wait_for()
                page.keyboard.press('Escape')
            page.locator('#code-text').fill('Начать')
            page.locator('#code-send').click()
            page.wait_for_function("() => document.querySelector('#code-feed').textContent.includes('Начало выполнения.')")
            at_end = "() => { const s=document.querySelector('#code-chat-scroll'); return s.scrollHeight-s.clientHeight-s.scrollTop < 3; }"
            page.wait_for_function(at_end)

            def play(*events):
                machine.running[CODE_CHAT_A]['rest'] = list(events)
                frames = []
                machine._play(CODE_CHAT_A, frames)
                for frame in frames:
                    channel._send(frame)

            # Прежний порог 80px бросал слежение уже при небольшом сдвиге.
            page.locator('#code-chat-scroll').evaluate('node => {node.scrollTop -= 120;}')
            play({'k': 'text', 'text': '\n\nНовый комментарий рядом с концом.', 'block': 'near'})
            page.wait_for_function(at_end)
            # Крупная отложенная разметка не должна оставлять конец за экраном.
            page.evaluate("document.querySelector('.code-timeline').append(Object.assign(document.createElement('div'), {textContent:'Разметка'})); document.querySelector('.code-timeline').lastChild.style.height='600px'")
            page.wait_for_function(at_end)
            scroll = page.locator('#code-chat-scroll')
            scroll.evaluate('node => {node.scrollTop=0;}')
            page.wait_for_function("() => document.querySelector('#code-chat-scroll').scrollTop === 0")
            play({'k': 'text', 'text': '\n\nКомментарий при чтении начала.', 'block': 'far'})
            page.wait_for_function("() => document.querySelector('#code-feed').textContent.includes('Комментарий при чтении начала.')")
            assert scroll.evaluate('node => node.scrollTop') < 5

            session['children']['first']['status'] = 'completed'
            play({'k': 'progress', 'agents': [
                {'id': 'first', 'title': 'Первая проверка', 'status': 'completed'},
                {'id': 'second', 'title': 'Вторая проверка', 'status': 'running'}]})
            page.wait_for_function("() => document.querySelector('#code-subagents').getAttribute('aria-label').includes('всего: 3')")
            assert page.locator('.code-subagents-count').inner_text() == '1'
            play({'k': 'progress', 'agents': [{'id': 'second', 'title': 'Вторая проверка', 'status': 'completed'}]})
            page.wait_for_function("() => document.querySelector('.code-subagents-count').textContent === '0'")
            subagents = page.locator('#code-subagents')
            subagents.tap() if width == 320 else subagents.click()
            page.locator('.code-subagent-row').filter(has_text='Вчерашняя проверка').click()
            page.get_by_text('Сохранённый результат', exact=True).wait_for()
            assert page.locator('.code-subagent-row').count() == 3
            assert all(page.evaluate(geometry).values()), page.evaluate(geometry)
            page.screenshot(path=str(tmp_path / f'restored-chat-{engine}-{width}.png'))
            assert not errors, errors
    finally:
        channel.stop()
