"""The iOS-style share menu sits in the chat status bar and downloads whole conversations."""
from pathlib import Path

import pytest
from pypdf import PdfReader

from fake_bridge import pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_chat_focus import focus_browser
from test_ui_chat_shares import owner
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize('mobile,width', [(False, 1280), (True, 320), (True, 852)])
def test_share_menu_location_keyboard_and_exported_files(focus_browser, live, mobile, width, tmp_path):
    if mobile and focus_browser.browser_type.name == 'firefox':
        pytest.skip('Playwright не эмулирует мобильный Firefox')
    client, cookie, cookies = owner(live)
    messages = [{'id': f'm-{n}', 'role': 'user' if n % 2 == 0 else 'assistant',
                 'text': f'Сообщение {n}', 'steps': [], 'time': n} for n in range(135)]
    messages[-1]['text'] = '**Последний ответ**\n\n| Колонка | Значение |\n|---|---|\n| Тест | Работает |'
    machine = code_free_machine({CODE_CHAT_A: code_session('Экспорт чата', messages=messages)})
    aid, bridge = code_online(live, 'owner', machine)
    try:
        with focus_browser.new_context(viewport={'width': width, 'height': 393 if width == 852 else 900}, is_mobile=mobile,
                                       has_touch=mobile, extra_http_headers={'Cookie': cookie}) as context:
            if focus_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in cookies.items()])
            page = context.new_page()
            page.set_default_timeout(15000)
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            trigger = page.locator('#code-share-chat')
            pw.expect(trigger).to_be_enabled()
            assert page.locator('.code-status-bar #code-share-chat, #code-status-menu #code-share-chat').count() == 1
            assert page.locator('.code-project-tools #code-share-chat').count() == 0
            overflow = not trigger.is_visible()
            button = (page.locator('#code-status-more') if overflow else trigger).bounding_box()
            status = page.locator('.code-status-bar').bounding_box()
            assert status['x'] <= button['x'] and button['x'] + button['width'] <= status['x'] + status['width'] + 1
            assert abs(button['y'] + button['height']/2 - status['y'] - status['height']/2) < 4
            repo = page.locator('#code-repo-status')
            pw.expect(repo).to_be_visible()
            status_box = repo.bounding_box()
            git_tab = page.locator('#code-tab-git').bounding_box()
            assert 0 <= git_tab['x'] - status_box['x'] - status_box['width'] <= 12
            assert abs(status_box['y'] + status_box['height']/2 - git_tab['y'] - git_tab['height']/2) < 4
            assert 'M12 15V3' in trigger.locator('path').get_attribute('d')
            def open_share():
                if not trigger.is_visible():
                    page.locator('#code-status-more').click()
                trigger.click()
            if overflow:
                page.locator('#code-status-more').click()
            if not mobile:
                trigger.focus()
                page.keyboard.press('ArrowDown')
                pw.expect(page.locator('[data-action="link"]')).to_be_focused()
                page.keyboard.press('ArrowDown')
                pw.expect(page.locator('[data-action="pdf"]')).to_be_focused()
                page.keyboard.press('End')
                pw.expect(page.locator('[data-action="md"]')).to_be_focused()
                page.keyboard.press('Escape')
                pw.expect(trigger).to_be_focused()
                pw.expect(trigger).to_have_attribute('aria-expanded', 'false')
            open_share()
            menu = page.locator('#code-share-chat-menu')
            pw.expect(menu).to_be_visible()
            box = menu.bounding_box()
            assert box['x'] >= 0 and box['y'] >= 0
            assert box['x'] + box['width'] <= width and box['y'] + box['height'] <= page.viewport_size['height']
            assert menu.locator('[role="menuitem"]').all_text_contents() == [
                'Поделиться ссылкой', 'Скачать PDF', 'Скачать Markdown']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if mobile:
                page.evaluate("document.documentElement.style.setProperty('--font-scale', '1.5')")
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.evaluate("document.documentElement.style.removeProperty('--font-scale')")
            page.screenshot(path=str(tmp_path / f'share-menu-{focus_browser.browser_type.name}-{width}.png'))
            with page.expect_download() as saved:
                menu.locator('[data-action="md"]').click()
            md = saved.value
            assert md.suggested_filename.endswith('.md')
            text = Path(md.path()).read_text(encoding='utf-8')
            assert 'Сообщение 0' in text and '**Последний ответ**' in text
            assert text.count('## Пользователь\n') + text.count('## Помощник\n') == 135
            pw.expect(trigger).to_be_enabled()
            open_share()
            with page.expect_download(timeout=60000) as saved_pdf:
                menu.locator('[data-action="pdf"]').click()
            pdf = saved_pdf.value
            assert pdf.suggested_filename.endswith('.pdf')
            reader = PdfReader(pdf.path())
            content = '\n'.join(p.extract_text() for p in reader.pages)
            assert 'Сообщение 0' in content and 'Последний ответ' in content and 'Работает' in content
            open_share()
            menu.locator('[data-action="link"]').click()
            pw.expect(page.locator('#chat-share-dialog')).to_be_visible()
            pw.expect(page.locator('#chat-share-pin')).to_have_value('')
            page.locator('#chat-share-dialog .dialog-close').click()
            open_share()
            pw.expect(menu).to_be_visible()
            page.locator('#code-tab-files').click()
            pw.expect(trigger).not_to_be_visible()
            pw.expect(menu).to_have_count(0)
    finally:
        bridge.stop()
        client.close()
