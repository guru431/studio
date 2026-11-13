"""Новый проект: выбор папки, ошибки, клавиатура и телефон в трёх браузерах."""
import pytest
from playwright.sync_api import expect

from fake_bridge import pid
from test_project_creation import creation_machine
from test_ui_browser import code_online, live
from test_ui_desktop import desktop_browser
from test_ui_project_access import context, signed

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [1280, 320])
@pytest.mark.parametrize('count', [1, 2])
def test_create_project_in_chosen_folder(desktop_browser, live, width, count, tmp_path):
    engine = desktop_browser.browser_type.name
    login = f'create-{engine}-{width}-{count}'
    live.add_user(login)
    machine = creation_machine(count)
    machine.roots[-1]['path'] = 'C:\\Projects\\' + 'Очень длинная папка\\' * 12
    aid, channel = code_online(live, login, machine)
    try:
        with context(desktop_browser, live, signed(live, login), width) as ctx:
            page = ctx.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(live.url + '/#/code')
            trigger = page.locator('#code-project-create')
            trigger.wait_for()
            trigger.tap() if width == 320 else trigger.press('Enter')
            dialog = page.locator('#code-project-create-dialog')
            name = dialog.get_by_label('Название проекта')
            folder = dialog.get_by_label('Папка проектов')
            submit = dialog.get_by_role('button', name='Создать', exact=True)
            expect(submit).to_be_enabled()
            if width == 320:
                assert not name.evaluate('(node) => node === document.activeElement')
            page.keyboard.press('Escape')
            expect(dialog).to_have_count(0)
            expect(trigger).to_be_focused()
            trigger.click()
            expect(submit).to_be_enabled()
            expect(folder).to_have_value('' if count > 1 else machine.roots[0]['id'])
            name.fill('alpha')
            if count > 1:
                submit.click()
                assert not any(req['op'] == 'project_create' for req in machine.requests)
                folder.select_option(machine.roots[-1]['id'])
            else:
                expect(folder).to_be_disabled()
            submit.click()
            expect(dialog.get_by_role('alert')).to_contain_text('уже существует')
            expect(name).to_have_value('alpha')
            expect(folder).to_have_value(machine.roots[-1]['id'])
            name.fill('../outside')
            name.press('Enter')
            expect(dialog.get_by_role('alert')).to_contain_text('без разделителей пути')
            # Длинный путь и увеличенный шрифт не расширяют окно телефона.
            page.evaluate("document.documentElement.dataset.theme = 'dark'; document.documentElement.style.setProperty('--font-scale', '1.5')")
            page.screenshot(path=str(tmp_path / f'create-{engine}-{width}-{count}.png'))
            assert page.evaluate('''() => {
                const dialog = document.querySelector('#code-project-create-dialog'), box = dialog.getBoundingClientRect();
                return box.left >= 0 && box.right <= innerWidth + 1
                    && dialog.scrollWidth <= dialog.clientWidth + 1 && document.documentElement.scrollWidth <= innerWidth;
            }''')
            name.fill('Новый проект')
            name.press('Enter')
            expect(dialog).to_have_count(0)
            expect(page).to_have_url(live.url + f'/#/code/{aid}/{pid("Новый проект")}')
            expect(page.locator('.code-place-project')).to_have_text('Новый проект')
            created = [req for req in machine.requests if req['op'] == 'project_create']
            assert len(created) == 2 and created[-1]['root'] == machine.roots[-1]['id']
            assert not errors, errors
            # Список и выбранный проект доступны и после перезагрузки страницы.
            page.reload()
            expect(page.locator('.code-place-project')).to_have_text('Новый проект')
            if width == 320:
                page.locator('#code-panel-toggle').click()
            page.locator(f'li[data-pid="{pid("Новый проект")}"] .code-project-name').wait_for()
    finally:
        channel.stop()


def test_expired_computer_pin_closes_creation_dialog(desktop_browser, live):
    login = 'create-pin-' + desktop_browser.browser_type.name
    live.add_user(login)
    machine = creation_machine(1, pin='2468')
    aid, channel = code_online(live, login, machine)
    try:
        with context(desktop_browser, live, signed(live, login), 1280) as ctx:
            page = ctx.new_page()
            page.goto(live.url + '/#/code')
            page.locator('#code-pin-input').fill('2468')
            page.locator('#code-pin-open').click()
            page.locator('#code-project-create').click()
            dialog = page.locator('#code-project-create-dialog')
            expect(dialog.get_by_role('button', name='Создать', exact=True)).to_be_enabled()
            dialog.get_by_label('Название проекта').fill('new')
            machine.open_sessions.clear()
            dialog.get_by_role('button', name='Создать', exact=True).click()
            expect(dialog).to_have_count(0)
            expect(page.locator('#code-pin-input')).to_be_visible()
            assert 'new' not in machine.projects
    finally:
        channel.stop()
