"""The iOS-style share menu sits in the chat status bar and downloads whole conversations."""
from pathlib import Path
import uuid

import pytest
from pypdf import PdfReader

import db
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
                'Поделиться ссылкой', 'Скачать PDF', 'Скачать выделенное в PDF', 'Скачать Markdown']
            pw.expect(menu.locator('[data-action="selection"]')).to_be_disabled()
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


@pytest.mark.parametrize('section', ['chat', 'code'])
@pytest.mark.parametrize('mobile,width,theme', [(False, 1280, 'light'), (True, 320, 'dark'), (True, 852, 'light')])
def test_selected_pdf_keeps_rendered_formatting_and_only_selected_text(
        focus_browser, live, section, mobile, width, theme, tmp_path):
    if mobile and focus_browser.browser_type.name == 'firefox':
        pytest.skip('Playwright не эмулирует мобильный Firefox')
    client, cookie, cookies = owner(live)
    text = '''Не экспортировать раньше

## Выбранный заголовок

**Жирный фрагмент** и *Курсивный фрагмент*, ~~удалённый~~ и `inline_code`.

> Цитата

4. Четвёртый
5. Пятый
   - Вложенный

| Колонка | Центр | По умолчанию | Значение |
|:---|:---:|---|---:|
| Тест | Середина | Слева | Работает |

[Документация](https://example.com/docs)

```python
if True:
    print("Сохранено")
```

Не экспортировать после'''
    bridge = None
    question = 'Не экспортировать вопрос\nВыбранный вопрос'
    if section == 'code':
        messages = [{'id': 'question', 'role': 'user', 'text': question, 'steps': [], 'time': 0},
                    {'id': 'answer', 'role': 'assistant', 'text': text, 'steps': [], 'time': 1}]
        machine = code_free_machine({CODE_CHAT_A: code_session('Выбранная часть', messages=messages)})
        aid, bridge = code_online(live, 'owner', machine)
        route = f'code/{aid}/{pid("alpha")}/{CODE_CHAT_A}'
        trigger_id, content_id = 'code-share-chat', 'code-chat-scroll'
    else:
        cid = client.post('/api/chats', json={}).json()['id']
        conn = db.connect(live.db_path)
        conn.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created)"
                     " VALUES(?,?,'user',?,'done','server',0)", (uuid.uuid4().hex, cid, question))
        conn.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created)"
                     " VALUES(?,?,'assistant',?,'done','server',1)", (uuid.uuid4().hex, cid, text))
        conn.close()
        route = 'chat/' + cid
        trigger_id, content_id = 'chat-share', 'messages'
    try:
        with focus_browser.new_context(viewport={'width': width, 'height': 900}, is_mobile=mobile,
                                       has_touch=mobile, extra_http_headers={'Cookie': cookie}) as context:
            if focus_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in cookies.items()])
            page = context.new_page()
            page.set_default_timeout(15000)
            page.goto(live.url + '/#/' + route)
            md = page.locator(f'#{content_id} .md').last
            pw.expect(md.locator('pre code')).to_contain_text('Сохранено')
            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
            assert md.locator('th, td').evaluate_all(
                "cells => cells.map(cell => getComputedStyle(cell).textAlign)") == [
                    'left', 'center', 'left', 'right'] * 2
            # A range wholly inside emphasis must retain that ancestor. Selecting the second list
            # item must retain its real number, and selecting inside code must retain whitespace.
            parts = page.evaluate('''async (id) => {
              const root = document.getElementById(id), md = root.querySelector('.md');
              const { selectedChatHTML } = await import('/static/js/chat-selection.js');
              const selection = getSelection(), out = [];
              for (const selector of ['strong', 'ol > li:nth-child(2)', 'pre code', 'td:last-child']) {
                const target = md.querySelector(selector), range = document.createRange();
                range.selectNodeContents(target);
                if (selector === 'strong') { range.setStart(target.firstChild, 1); range.setEnd(target.firstChild, 6); }
                selection.removeAllRanges(); selection.addRange(range);
                out.push(selectedChatHTML(root));
              }
              selection.removeAllRanges();
              return out;
            }''', content_id)
            assert '<strong>ирный</strong>' in parts[0]
            assert 'start="5"' in parts[1] and 'Четвёртый' not in parts[1]
            assert '<pre>' in parts[2] and '\n    ' in parts[2] and 'hljs-keyword' in parts[2]
            assert '<table>' in parts[3] and 'align="right"' in parts[3]
            crossing = page.evaluate('''async (id) => {
              const root = document.getElementById(id), user = root.querySelector('.bubble, .code-msg.user');
              const first = [...user.childNodes].find(n => n.nodeType === Node.TEXT_NODE && n.textContent.includes('Выбранный'));
              const strong = root.querySelector('.md strong'), range = document.createRange();
              range.setStart(first, first.textContent.indexOf('Выбранный'));
              range.setEnd(strong.firstChild, strong.textContent.length);
              getSelection().removeAllRanges(); getSelection().addRange(range);
              const { selectedChatHTML } = await import('/static/js/chat-selection.js');
              return selectedChatHTML(root);
            }''', content_id)
            assert 'chat-export-plain' in crossing and 'Выбранный вопрос' in crossing
            assert 'Не экспортировать вопрос' not in crossing and '<strong>Жирный фрагмент</strong>' in crossing
            # Select a continuous fragment crossing all block types; then use real pointer/touch
            # events to prove that focusing the trigger and its mobile overflow keeps the range.
            md.evaluate('''node => {
              const range = document.createRange();
              range.setStartBefore(node.querySelector('h2'));
              range.setEndAfter(node.querySelector('pre'));
              getSelection().removeAllRanges(); getSelection().addRange(range);
              document.dispatchEvent(new Event('selectionchange'));
            }''')
            trigger = page.locator('#' + trigger_id)
            if not trigger.is_visible():
                page.locator('#code-status-more').tap() if mobile else page.locator('#code-status-more').click()
            trigger.tap() if mobile else trigger.click()
            menu = page.locator('#' + trigger_id + '-menu')
            selected = menu.locator('[data-action="selection"]')
            pw.expect(selected).to_be_enabled()
            bounds = menu.bounding_box()
            assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= width + 1
            if not mobile:
                page.keyboard.press('ArrowDown')
                page.keyboard.press('ArrowDown')
                pw.expect(selected).to_be_focused()
            with page.expect_download(timeout=60000) as download, page.expect_request(
                    lambda r: '/export' in r.url and r.method == 'POST') as sent:
                if mobile:
                    selected.tap()
                else:
                    page.keyboard.press('Enter')
            fragment = sent.value.post_data_json['selection']
            for kept in ['<h2>', '<strong>', '<em>', '<del>', '<blockquote>', '<table>', '<pre>',
                         'hljs-keyword', 'start="4"', 'align="right"']:
                assert kept in fragment
            assert 'Не экспортировать' not in fragment and 'Копировать' not in fragment
            assert 'выделенное.pdf' in download.value.suggested_filename
            pdf_path = tmp_path / f'selected-{section}-{focus_browser.browser_type.name}-{width}.pdf'
            download.value.save_as(pdf_path)
            reader = PdfReader(pdf_path)
            extracted = '\n'.join(p.extract_text() for p in reader.pages)
            for kept in ['Выбранный заголовок', 'Жирный', 'Курсивный', 'Вложенный', 'Работает', 'Сохранено']:
                assert kept in extracted
            assert 'Не экспортировать' not in extracted
            links = [a.get_object().get('/A', {}).get('/URI') for p in reader.pages for a in p.get('/Annots', [])]
            assert 'https://example.com/docs' in links
            # Render the real PDF, not just the browser UI, for visual acceptance.
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(str(pdf_path))
            try:
                for n in range(len(doc)):
                    pdf_page = doc[n]
                    try:
                        bitmap = pdf_page.render(scale=1.5)
                        try:
                            bitmap.to_pil().save(pdf_path.with_name(pdf_path.stem + f'-{n + 1}.png'))
                        finally:
                            bitmap.close()
                    finally:
                        pdf_page.close()
            finally:
                doc.close()
            # Deselecting in the chat must disable the item instead of exporting a stale fragment.
            page.locator(f'#{content_id}').click(position={'x': 4, 'y': 4})
            page.evaluate('getSelection().removeAllRanges(); document.dispatchEvent(new Event("selectionchange"))')
            if not trigger.is_visible():
                page.locator('#code-status-more').click()
            trigger.click()
            pw.expect(selected).to_be_disabled()
    finally:
        if bridge:
            bridge.stop()
        client.close()
