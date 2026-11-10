"""Общий компонент меню (menu.js), реестр разделов на рельсе и переходы оболочки в Chromium и WebKit:
роли и клавиатура меню, Escape закрывает только верхнее меню, заголовок вкладки и фокус после смены раздела."""
import re

import pytest

from test_ui_browser import live
from test_ui_design_tokens import cookie, engine, shell
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

NEW_CHAT = """async () => {
  const { api } = await import('/static/js/api.js');
  const chat = await api('POST', '/api/chats', {});
  location.hash = '#/chat/' + chat.id;
  return chat.id;
}"""
FOCUS = "() => [document.activeElement.getAttribute('role'), document.activeElement.textContent.trim()]"


@pytest.mark.parametrize("width", [320, 393, 1280])
def test_chat_menu_keys_and_escape_closes_only_the_top_menu(engine, live, cookie, width):
    context, page = shell(engine, live, cookie, width=width, touch=width < 760)
    with context:
        chat = page.evaluate(NEW_CHAT)
        item = page.locator(f'.chat-item[data-id="{chat}"]')
        item.locator('[aria-current=page]').wait_for(state='attached')
        if width < 760:
            page.locator('#chats-toggle').click()
            pw.expect(page.locator('.chat')).to_have_class(re.compile('panel-open'))
        more = item.locator('.chat-more')
        assert more.get_attribute('aria-haspopup') == 'menu' and more.get_attribute('aria-expanded') == 'false'
        more.focus()
        page.keyboard.press('Enter')
        menu = item.locator('.chat-menu')
        pw.expect(menu).to_be_visible()
        assert menu.get_attribute('role') == 'menu' and more.get_attribute('aria-expanded') == 'true'
        assert page.evaluate(FOCUS) == ['menuitem', 'Поделиться чатом']
        page.keyboard.press('ArrowDown')
        assert page.evaluate(FOCUS)[1] == 'Переименовать'
        page.keyboard.press('End')
        assert page.evaluate(FOCUS)[1] == 'Удалить'
        page.keyboard.press('ArrowDown')                       # по кругу
        assert page.evaluate(FOCUS)[1] == 'Поделиться чатом'
        page.keyboard.press('ArrowUp')
        page.keyboard.press('Home')
        assert page.evaluate(FOCUS)[1] == 'Поделиться чатом'
        inside = menu.evaluate("m => { const b = m.getBoundingClientRect();"
                               " return b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight; }")
        heights = menu.evaluate("m => [...m.children].map(c => c.getBoundingClientRect().height)")
        page.keyboard.press('Escape')
        pw.expect(menu).to_be_hidden()
        assert page.evaluate("document.activeElement.classList.contains('chat-more')")
        assert more.get_attribute('aria-expanded') == 'false'
        if width < 760:
            # выдвижная панель осталась: Escape закрыл только меню, второй — панель
            pw.expect(page.locator('.chat')).to_have_class(re.compile('panel-open'))
            page.keyboard.press('Escape')
            pw.expect(page.locator('.chat')).not_to_have_class(re.compile('panel-open'))
    assert inside, width
    assert all(h >= (44 if width < 760 else 36) for h in heights), heights


@pytest.mark.parametrize("width", [393, 1280])
def test_model_menu_is_a_radio_menu(engine, live, cookie, width):
    context, page = shell(engine, live, cookie, width=width, touch=width < 760)
    with context:
        chip = page.locator('#model-chip')
        assert chip.get_attribute('aria-haspopup') == 'menu'
        chip.click()
        menu = page.locator('#model-menu')
        pw.expect(menu).to_be_visible()
        roles = menu.evaluate("m => [m.getAttribute('role'), [...m.querySelectorAll('.model-option')].map(o =>"
                              " [o.getAttribute('role'), o.getAttribute('aria-checked')])]")
        focused = page.evaluate("document.activeElement.getAttribute('aria-checked')")
        page.keyboard.press('Escape')
        pw.expect(menu).to_be_hidden()
        assert page.evaluate("document.activeElement.id") == 'model-chip'
    assert roles[0] == 'menu'
    if roles[1]:                                               # у учётки есть отмеченные модели
        assert all(role == 'menuitemradio' for role, _ in roles[1])
        assert [checked for _, checked in roles[1]].count('true') <= 1
        assert focused in ('true', 'false')


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [320, 1280])
def test_rail_groups_title_and_heading_focus(engine, live, cookie, width, scheme, tmp_path):
    context, page = shell(engine, live, cookie, width=width, touch=width < 760, scheme=scheme)
    with context:
        assert page.title() == 'Чат — Студия'
        assert page.locator('#view .view-loading').count() == 0
        tabs = page.evaluate("[...document.querySelectorAll('#rail a.tab')].map(t => [t.dataset.section,"
                             " t.getAttribute('aria-label').split(' · ')[0], t.classList.contains('group-start')])")
        separators = page.evaluate("[...document.querySelectorAll('#rail a.tab.group-start')]"
                                   ".map(t => getComputedStyle(t, '::before').display)")
        page.evaluate("location.hash = '#/speech'")
        pw.expect(page).to_have_title('Озвучка — Студия')
        if width >= 760:
            page.locator('#rail a[data-section=video]').click()
        else:
            page.evaluate("location.hash = '#/video'")
        pw.expect(page).to_have_title('Видео — Студия')
        pw.expect(page.locator('#view h1')).to_be_focused()
        heading = page.evaluate("document.activeElement.textContent")
        page.screenshot(path=str(tmp_path / f'rail-{width}-{scheme}.png'))
    assert tabs == [['chat', 'Чат', False], ['photo', 'Фото', True], ['video', 'Видео', False],
                    ['speech', 'Озвучка', True], ['transcribe', 'Расшифровка', False], ['ocr', 'Сканы', True],
                    ['translate', 'Перевод', False], ['code', 'Агенты', True]]
    assert all(display == ('none' if width < 760 else 'block') for display in separators), separators
    assert heading == 'Видео'
