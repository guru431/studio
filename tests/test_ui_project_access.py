"""Выдать доступ, открыть общий проект другой учёткой, отозвать: три движка и два экрана."""
import httpx
import pytest

from fake_bridge import FakeMachine, pid
from test_ui_browser import code_online, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def signed(live, login):
    response = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'}, verify=False)
    assert response.status_code == 303
    return response.cookies


def context(browser, live, cookies, width):
    engine = browser.browser_type.name
    ctx = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 320,
        is_mobile=width == 320 and engine != 'firefox',
        extra_http_headers={'Cookie': '; '.join(f'{k}={v}' for k, v in cookies.items())})
    if engine == 'firefox':
        ctx.add_cookies([{'name': k, 'value': v, 'url': live.url, 'secure': True,
            'httpOnly': True, 'sameSite': 'Lax'} for k, v in cookies.items()])
    return ctx


@pytest.mark.parametrize('width', [1280, 320])
def test_project_membership_workflow(desktop_browser, live, width, tmp_path):
    engine = desktop_browser.browser_type.name
    owner, friend = f'share-owner-{engine}-{width}', f'share-friend-{engine}-{width}'
    live.add_user(owner)
    live.add_user(friend)
    machine = FakeMachine({'alpha': {}, 'private': {}})
    machine.project_create = True
    aid, channel = code_online(live, owner, machine)
    owner_cookie, friend_cookie = signed(live, owner), signed(live, friend)
    try:
        with context(desktop_browser, live, owner_cookie, width) as owner_context:
            page = owner_context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(live.url + '/#/code')
            page.locator(f'li[data-pid="{pid("alpha")}"] .code-project-name').wait_for()
            assert page.locator('#code-projects button').count() == 0
            page.locator(f'li[data-pid="{pid("alpha")}"] .code-project-name').click()
            button = page.locator('#code-project-access')
            button.wait_for()
            assert page.locator('.code-views > button').all_text_contents() == ['Git', 'Файлы', 'Доступ', 'Чат']
            initial_hash = page.url
            button.tap() if width == 320 and engine != 'firefox' else button.click()
            popup = page.locator('.project-members:popover-open')
            popup.get_by_text('Доступ есть только у владельца').wait_for()
            if width == 320:
                assert page.evaluate('document.activeElement?.getAttribute("placeholder")') != 'Логин пользователя Студии'
            page.screenshot(path=str(tmp_path / f'access-{engine}-{width}.png'))
            box = popup.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            popup.get_by_label('Логин пользователя Студии').fill('missing-user')
            popup.get_by_role('button', name='Дать доступ', exact=True).click()
            popup.get_by_text('Укажите логин другого активного пользователя Студии', exact=True).wait_for()
            popup.get_by_label('Логин пользователя Студии').fill(friend)
            popup.get_by_role('button', name='Дать доступ', exact=True).click()
            popup.get_by_text('Доступ предоставлен', exact=True).wait_for()
            assert page.url == initial_hash
            popup.get_by_role('button', name=f'Отозвать доступ · {friend}').wait_for()
            page.keyboard.press('Escape')
            assert button.evaluate('(node) => node === document.activeElement')
            button.press('Enter')
            popup.get_by_role('button', name=f'Отозвать доступ · {friend}').wait_for()
            page.evaluate("document.documentElement.dataset.theme = 'dark'; document.documentElement.style.setProperty('--font-scale', '1.5')")
            assert page.evaluate('''() => {
                const buttons = [...document.querySelectorAll('.code-project-tools button')]
                    .map(button => button.getBoundingClientRect());
                return buttons.every((box, index) => box.left >= 0 && box.right <= innerWidth
                    && (!index || box.left >= buttons[index - 1].right)) && buttons[0].width >= 28;
            }''')
            page.screenshot(path=str(tmp_path / f'access-dark-scaled-{engine}-{width}.png'))
            box = popup.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            with context(desktop_browser, live, friend_cookie, width) as member_context:
                member_page = member_context.new_page()
                member_page.goto(live.url + '/#/code')
                member_page.locator(f'li[data-pid="{pid("alpha")}"]').wait_for()
                assert member_page.locator('#code-projects > li').count() == 1
                assert member_page.locator('#code-project-access').count() == 0
                assert member_page.locator('.code-agent-info, .code-agent-desktop, .code-agent-update').count() == 0
                assert not member_page.locator('#code-project-create').is_visible()
                member_page.reload()
                member_page.locator(f'li[data-pid="{pid("alpha")}"]').wait_for()
                member_page.locator(f'li[data-pid="{pid("alpha")}"] .code-project-name').click()
                member_page.locator('#code-new-chat').click()
                member_page.locator('#code-chat[data-loaded="1"]').wait_for()
                assert not member_page.locator('#code-project-access').is_visible()
                popup.get_by_role('button', name=f'Отозвать доступ · {friend}').click()
                popup.get_by_text('Доступ отозван', exact=True).wait_for()
                member_page.reload()
                member_page.locator('html[data-ready="1"]').wait_for(state='attached')
                assert member_page.locator('#code-projects > li').count() == 0
            page.keyboard.press('Escape')
            page.locator('#code-tab-files').click()
            page.locator('#code-files').wait_for()
            files_url = page.url
            button.click()
            popup.get_by_text('Доступ есть только у владельца').wait_for()
            assert page.url == files_url
            page.keyboard.press('Escape')
            if width == 320:
                page.locator('#code-panel-toggle').click()
            page.locator(f'li[data-pid="{pid("private")}"] .code-project-name').click()
            page.locator('#code-project-access').wait_for()
            page.locator('#code-project-access').click()
            page.get_by_role('dialog', name='Доступ к проекту private', exact=True).wait_for()
            assert page.locator('.project-members').count() == 1
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not errors, errors
    finally:
        channel.stop()
