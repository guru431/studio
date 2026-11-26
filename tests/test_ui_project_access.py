"""Выдать доступ, открыть общий проект другой учёткой, отозвать: три движка и два экрана."""
import httpx
import pytest
from playwright.sync_api import expect

from fake_bridge import FakeMachine, pid
from test_ui_browser import CODE_CHAT_A, code_online, code_session, live
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
            list_badge = page.locator(f'li[data-pid="{pid("alpha")}"] .code-project-shared')
            header_badge = page.locator('#code-project-shared')
            expect(list_badge).to_have_attribute('hidden', '')
            expect(page.locator('#code-projects .code-project-pin')).to_have_count(2)
            page.locator(f'li[data-pid="{pid("alpha")}"] .code-project-name').click()
            button = page.locator('#code-project-access')
            access_count = button.locator('.code-access-count')
            button.wait_for()
            assert page.locator('.code-views > button').all_text_contents() == ['Git', 'Файлы', 'Зеркало', 'Доступ', 'Чат']
            initial_hash = page.url
            button.tap() if width == 320 and engine != 'firefox' else button.click()
            popup = page.locator('.project-members:popover-open')
            popup.get_by_text('Доступ есть только у владельца').wait_for()
            expect(header_badge).to_be_hidden()
            expect(access_count).to_be_hidden()
            if width == 320:
                assert page.evaluate('document.activeElement?.getAttribute("placeholder")') != 'Логин или email пользователя Студии'
            page.screenshot(path=str(tmp_path / f'access-{engine}-{width}.png'))
            box = popup.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            popup.get_by_label('Логин или email пользователя Студии').fill('missing@example.com')
            popup.get_by_role('button', name='Дать доступ', exact=True).click()
            popup.get_by_text('Укажите логин или email другого активного пользователя Студии', exact=True).wait_for()
            identity = ' ' + friend.upper() + '@EXAMPLE.COM ' if width == 320 else friend
            popup.get_by_label('Логин или email пользователя Студии').fill(identity)
            popup.get_by_role('button', name='Дать доступ', exact=True).click()
            popup.get_by_text('Доступ предоставлен', exact=True).wait_for()
            expect(header_badge).to_be_visible()
            expect(access_count).to_be_visible()
            expect(access_count).to_have_text('1')
            expect(button).to_have_accessible_name('Доступ к проекту · alpha · участников: 1')
            expect(list_badge).not_to_have_attribute('hidden', '')
            expect(page.locator(f'li[data-pid="{pid("private")}"] .code-project-shared')).to_have_attribute('hidden', '')
            assert page.url == initial_hash
            popup.get_by_role('button', name=f'Отозвать доступ · {friend}').wait_for()
            page.keyboard.press('Escape')
            assert button.evaluate('(node) => node === document.activeElement')
            page.reload()
            expect(header_badge).to_be_visible()
            expect(access_count).to_have_text('1')
            expect(list_badge).not_to_have_attribute('hidden', '')
            button.press('Enter')
            popup.get_by_role('button', name=f'Отозвать доступ · {friend}').wait_for()
            page.evaluate("document.documentElement.dataset.theme = 'dark'; document.documentElement.style.setProperty('--font-scale', '1.5')")
            assert page.evaluate('''() => {
                const buttons = [...document.querySelectorAll('.code-project-tools button')]
                    .map(button => button.getBoundingClientRect());
                const apart = (a, b) => a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top;
                return buttons.every((box, index) => box.left >= 0 && box.right <= innerWidth
                    && buttons.slice(0, index).every(other => apart(other, box))) && buttons[0].width >= 28;
            }''')
            assert page.evaluate('''() => {
                const badge = document.querySelector('#code-project-shared').getBoundingClientRect();
                const name = document.querySelector('.code-place-project').getBoundingClientRect();
                return badge.left >= 0 && badge.right <= innerWidth && name.width > 0
                    && document.documentElement.scrollWidth <= innerWidth;
            }''')
            page.screenshot(path=str(tmp_path / f'access-dark-scaled-{engine}-{width}.png'))
            box = popup.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            with context(desktop_browser, live, friend_cookie, width) as member_context:
                member_page = member_context.new_page()
                member_page.goto(live.url + '/#/code')
                member_page.locator(f'li[data-pid="{pid("alpha")}"]').wait_for()
                expect(member_page.locator('#code-projects .code-project-shared')).to_have_text('Общий доступ')
                assert member_page.locator('#code-projects > li').count() == 1
                assert member_page.locator('#code-project-access').count() == 0
                assert member_page.locator('.code-agent-info, .code-agent-desktop, .code-agent-update').count() == 0
                assert not member_page.locator('#code-project-create').is_visible()
                member_page.reload()
                member_page.locator(f'li[data-pid="{pid("alpha")}"]').wait_for()
                member_page.locator(f'li[data-pid="{pid("alpha")}"] .code-project-name').click()
                member_page.locator('#code-new-chat').click()
                member_page.locator('#code-chat[data-loaded="1"]').wait_for()
                expect(member_page.locator('#code-project-shared')).to_be_visible()
                assert not member_page.locator('#code-project-access').is_visible()
                assert not member_page.locator('#code-project-mirror').is_visible()
                # лимиты подписки — только владельцу: у участника нет кнопки с советом «обновить мост»
                assert member_page.locator('#code-usage').count() == 1
                assert not member_page.locator('#code-usage').is_visible()
                popup.get_by_role('button', name=f'Отозвать доступ · {friend}').click()
                popup.get_by_text('Доступ отозван', exact=True).wait_for()
                expect(header_badge).to_be_hidden()
                expect(access_count).to_be_hidden()
                expect(button).to_have_accessible_name('Доступ к проекту · alpha')
                expect(list_badge).to_have_attribute('hidden', '')
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


@pytest.mark.parametrize('width', [1280, 320])
def test_viewer_role_and_term(desktop_browser, live, width):
    """Выдача наблюдателю на неделю: дата в окне «Доступ»; у наблюдателя чат без поля ввода и без «Нового чата»,
    файл — только для чтения; владелец делает его соавтором, не пересоздавая выдачу."""
    engine = desktop_browser.browser_type.name
    owner, viewer = f'view-owner-{engine}-{width}', f'view-viewer-{engine}-{width}'
    live.add_user(owner)
    live.add_user(viewer)
    history = [{"id": "m-1", "role": "assistant", "text": "Отчёт готов.", "steps": [], "time": 0}]
    machine = FakeMachine({'alpha': {'files': {'a.txt': b'hello'},
                                     'chats': {CODE_CHAT_A: code_session('Отчёт', messages=history)}}})
    machine.file_operations = machine.file_upload = machine.file_search = True
    aid, channel = code_online(live, owner, machine)
    owner_cookie, viewer_cookie = signed(live, owner), signed(live, viewer)
    try:
        with context(desktop_browser, live, owner_cookie, width) as owner_context:
            page = owner_context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(live.url + f'/#/code/{aid}/{pid("alpha")}')
            button = page.locator('#code-project-access')
            button.wait_for()
            button.click()
            popup = page.locator('.project-members:popover-open')
            popup.get_by_text('Доступ есть только у владельца').wait_for()
            popup.get_by_label('Логин или email пользователя Студии').fill(viewer)
            popup.get_by_label('Роль участника').select_option('viewer')
            popup.get_by_label('Срок доступа').select_option('week')
            popup.get_by_role('button', name='Дать доступ', exact=True).click()
            popup.get_by_text('Доступ предоставлен', exact=True).wait_for()
            expect(popup.get_by_label(f'Роль · {viewer}')).to_have_value('viewer')
            expect(popup.locator('.project-member-term')).to_contain_text('до ')
            box = popup.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            with context(desktop_browser, live, viewer_cookie, width) as viewer_context:
                member = viewer_context.new_page()
                member.set_default_timeout(10000)
                member.on('pageerror', lambda error: errors.append(str(error)))
                member.goto(live.url + f'/#/code/{aid}/{pid("alpha")}')
                member.locator('#code-chats li[data-chat]').first.wait_for()
                assert member.locator('#code-new-chat').count() == 0
                assert member.locator('#code-chats .code-chat-state').count() == 0
                member.goto(live.url + f'/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
                member.locator('#code-chat[data-loaded="1"]').wait_for()
                expect(member.locator('#code-readonly-note')).to_be_visible()
                expect(member.locator('#code-composer')).to_be_hidden()
                answer = member.locator('#code-feed .code-msg.assistant .md')
                expect(answer).to_have_text('Отчёт готов.')
                answer.evaluate("""node => {
                  document.getSelection().selectAllChildren(node);
                  document.dispatchEvent(new Event('selectionchange'));
                  node.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
                }""")
                expect(member.locator('#quote-float')).to_have_count(0)
                expect(member.get_by_role('menuitem', name='Копировать')).to_be_visible()
                expect(member.get_by_role('menuitem', name='Цитировать')).to_have_count(0)
                member.goto(live.url + f'/#/code/{aid}/{pid("alpha")}?tab=files')
                member.locator('#code-files').get_by_text('a.txt').click()
                editor = member.locator('#code-editor')
                expect(editor).to_have_value('hello')
                assert editor.evaluate('(node) => node.readOnly')
                assert member.locator('#code-save').count() == 0
                # «Загрузить файл» наблюдателю скрыт, поиск по содержимому и «Скачать» — есть
                expect(member.locator('#code-upload')).to_be_hidden()
                expect(member.locator('#code-files-search')).to_be_visible()
                member.locator('#code-open-file-actions').click()
                actions = member.locator('#code-file-actions [data-action]')
                expect(actions).to_have_count(1)
                expect(actions.first).to_have_attribute('data-action', 'download')
                member.locator('#code-file-actions .dialog-close').click()
                popup.get_by_label(f'Роль · {viewer}').select_option('editor')
                popup.get_by_text('Роль изменена', exact=True).wait_for()
                member.goto(live.url + f'/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
                member.reload()
                member.locator('#code-chat[data-loaded="1"]').wait_for()
                expect(member.locator('#code-composer')).to_be_visible()
                assert member.locator('#code-readonly-note').count() == 0
            assert not errors, errors
    finally:
        channel.stop()
