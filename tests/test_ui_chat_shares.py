"""Owner sharing and fresh anonymous PIN entry in Firefox, Chromium and mobile WebKit."""
import uuid

import httpx
import pytest

import db
from fake_bridge import pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_chat_focus import focus_browser
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def owner(live):
    signed = httpx.post(live.url + '/login', data={'login': 'owner', 'password': 'password-123'})
    assert signed.status_code == 303
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    client = httpx.Client(base_url=live.url, headers={'Cookie': cookie})
    client.headers['x-studio-csrf'] = client.get('/api/me').json()['csrf']
    return client, cookie, signed.cookies


@pytest.mark.parametrize('mobile,width', [(False, 1280), (True, 320)])
def test_owner_shares_and_anonymous_reader_must_enter_pin_on_each_open(focus_browser, live, mobile, width, tmp_path):
    if mobile and focus_browser.browser_type.name == 'firefox':
        pytest.skip('Playwright не эмулирует мобильный Firefox')
    client, cookie, cookies = owner(live)
    cid = client.post('/api/chats', json={}).json()['id']
    conn = db.connect(live.db_path)
    text = 'Ответ для получателя **с выделением** <script>window.shareXss=1</script>'
    tools = '[{"kind":"search","query":"погода","results":[{"title":"Пример","url":"https://example.org/a"}],"text":"x"}]'
    conn.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created,tools)"
                 " VALUES(?,?,'assistant',?,'done','server',1,?)", (uuid.uuid4().hex, cid, text, tools))
    conn.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created,error)"
                 " VALUES(?,?,'assistant','','error','server',2,'шлюз не ответил')", (uuid.uuid4().hex, cid))
    conn.execute('UPDATE chats SET title=? WHERE id=?', ('Чат для друга', cid))
    conn.close()
    options = {'viewport': {'width': width, 'height': 852}, 'is_mobile': mobile, 'has_touch': mobile}
    try:
        with focus_browser.new_context(**options, extra_http_headers={'Cookie': cookie}) as context:
            if focus_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copiedShare = text}}})")
            page.goto(live.url + '/#/chat/' + cid)
            page.locator('#chat-share').click()
            page.locator('#chat-share-menu [data-action="link"]').click()
            pin = page.locator('#chat-share-pin')
            pw.expect(pin).to_be_enabled()
            if mobile:
                pw.expect(pin).not_to_be_focused()
            # срок — неделя по умолчанию, рассуждение модели — только явным выбором
            pw.expect(page.locator('#chat-share-days')).to_have_value('7')
            pw.expect(page.locator('#chat-share-reasoning')).not_to_be_checked()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            pin.fill('123')
            page.locator('#chat-share-create').click()
            pw.expect(page.locator('#chat-share-link')).not_to_be_visible()
            pin.fill('0123')
            pin.press('Enter')
            link = page.locator('#chat-share-link')
            pw.expect(link).to_be_visible()
            url = link.input_value()
            assert '/share#' in url
            pw.expect(page.locator('#chat-share-facts')).to_contain_text('действует до')
            pw.expect(page.locator('#chat-share-facts')).to_contain_text('открыта 0 раз')
            page.locator('#chat-share-copy').click()
            assert page.evaluate('window.copiedShare') == url
            page.locator('#chat-share-dialog .dialog-close').click()
            page.locator('#chat-share').click()
            page.locator('#chat-share-menu [data-action="link"]').click()
            pw.expect(link).to_have_value(url)
            pw.expect(page.locator('#chat-share-pin')).to_have_value('')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

            # получатель без выбора языка видит /share на языке браузера — сценарий проверяет русский
            with focus_browser.new_context(**options, locale='ru-RU') as reader_context:
                reader = reader_context.new_page()
                reader.set_default_timeout(10000)
                requests = []
                reader.on('request', lambda req: requests.append(req.url))
                reader.goto(url.split('#')[0])                       # адрес без секрета — сразу, до PIN
                pw.expect(reader.locator('#share-error')).to_contain_text('Ссылка недействительна')
                pw.expect(reader.locator('#share-open')).to_be_disabled()
                reader.goto(url)
                pw.expect(reader.locator('#share-unlock')).to_be_visible()
                pw.expect(reader.locator('#shared-chat')).not_to_be_visible()
                assert 'Ответ для получателя' not in reader.content()
                reader.locator('#share-pin').fill('9999')
                reader.locator('#share-open').click()
                pw.expect(reader.locator('#share-error')).to_contain_text('неверный PIN')
                reader.locator('#share-pin').fill('0123')
                reader.locator('#share-open').click()
                pw.expect(reader.locator('#shared-title')).to_have_text('Чат для друга')
                pw.expect(reader.locator('#shared-messages strong')).to_have_text('с выделением')
                pw.expect(reader.locator('.shared-sources summary')).to_have_text('Источники (1)')
                pw.expect(reader.locator('.shared-sources a')).to_have_attribute('href', 'https://example.org/a')
                pw.expect(reader.locator('#shared-messages p.error')).to_have_text('шлюз не ответил')
                assert reader.locator('textarea').count() == 0
                assert not reader.evaluate('window.shareXss || false')
                assert reader.evaluate('document.documentElement.scrollWidth <= innerWidth')
                reader.screenshot(path=str(tmp_path / f'share-{focus_browser.browser_type.name}-{width}.png'), full_page=True)
                reader.reload()
                pw.expect(reader.locator('#share-unlock')).to_be_visible()
                pw.expect(reader.locator('#shared-chat')).not_to_be_visible()
                pw.expect(reader.locator('#share-pin')).to_have_value('')
                reader.locator('#share-pin').fill('0123')
                reader.locator('#share-pin').press('Enter')
                pw.expect(reader.locator('#shared-chat')).to_be_visible()
                reader.goto(live.url + '/health')
                reader.go_back()
                pw.expect(reader.locator('#share-unlock')).to_be_visible()
                pw.expect(reader.locator('#shared-chat')).not_to_be_visible()
                pw.expect(reader.locator('#share-pin')).to_have_value('')
                reader.locator('#share-pin').fill('0123')
                reader.locator('#share-open').click()
                pw.expect(reader.locator('#shared-chat')).to_be_visible()
                # владелец видит открытия и неверные PIN
                page.locator('#chat-share-dialog .dialog-close').click()
                page.locator('#chat-share').click()
                page.locator('#chat-share-menu [data-action="link"]').click()
                facts = page.locator('#chat-share-facts')
                pw.expect(facts).to_contain_text('открыта 3 раза, последний')
                pw.expect(facts).to_contain_text('неверных PIN: 1')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Revocation is checked immediately on returning to the tab.
                page.locator('#chat-share-revoke').click()
                pw.expect(page.locator('#chat-share-link')).not_to_be_visible()
                reader.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                pw.expect(reader.locator('#share-error')).to_contain_text('отключена')
                assert 'Ответ для получателя' not in reader.content()
                assert all(url.split('#')[1] not in request and '0123' not in request for request in requests)
                assert reader_context.cookies() == []
            assert not errors
    finally:
        client.delete('/api/chats/' + cid)
        client.close()


@pytest.mark.parametrize('mobile,width', [(False, 1280), (True, 320)])
def test_agent_chat_sharing_uses_the_same_pin_flow(focus_browser, live, mobile, width):
    if mobile and focus_browser.browser_type.name == 'firefox':
        pytest.skip('Playwright не эмулирует мобильный Firefox')
    client, cookie, cookies = owner(live)
    machine = code_free_machine({CODE_CHAT_A: code_session('Обсуждение', messages=[
        {'id': 'u', 'role': 'user', 'text': 'Вопрос к агенту', 'steps': [], 'time': 1,
         'images': [{'index': 0, 'name': 'shot.png'}]},
        # FINDINGS 07:F4: правка — диффом, порядок текста и шагов — по timeline, картинка без байтов — заглушкой
        {'id': 'e', 'role': 'assistant', 'text': 'СначалаПотом', 'time': 2, 'steps': [
            {'id': 'edit', 'tool': 'Edit', 'input': {'file_path': 'a.py'}, 'text': 'ok',
             'changes': [{'path': 'a.py', 'before': 'old line', 'after': 'new line'}]}],
         'timeline': [{'kind': 'text', 'start': 0, 'end': 7}, {'kind': 'step', 'id': 'edit'},
                      {'kind': 'text', 'start': 7, 'end': 12}]},
        {'id': 'a', 'role': 'assistant', 'text': 'Ответ агента', 'steps': [
            {'id': 'cmd', 'tool': 'Bash', 'input': {'command': 'echo test'}, 'text': 'test'}], 'time': 2}])})
    aid, bridge = code_online(live, 'owner', machine)
    try:
        with focus_browser.new_context(viewport={'width': width, 'height': 900}, is_mobile=mobile, has_touch=mobile,
                                       extra_http_headers={'Cookie': cookie}) as context:
            if focus_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in cookies.items()])
            page = context.new_page()
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            if not page.locator('#code-share-chat').is_visible():
                page.locator('#code-status-more').click()
            page.locator('#code-share-chat').click()
            page.locator('#code-share-chat-menu [data-action="link"]').click()
            pin = page.locator('#chat-share-pin')
            pw.expect(pin).to_be_enabled()
            assert page.locator('#chat-share-reasoning').count() == 0     # у агентских чатов рассуждения нет
            pw.expect(page.locator('#chat-share-days')).to_have_value('7')
            pin.fill('4567')
            page.locator('#chat-share-create').click()
            pw.expect(page.locator('#chat-share-link')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            url = page.locator('#chat-share-link').input_value()
            with focus_browser.new_context(locale='ru-RU') as reader_context:
                reader = reader_context.new_page()
                reader.goto(url)
                reader.locator('#share-pin').fill('4567')
                reader.locator('#share-open').click()
                pw.expect(reader.locator('#shared-messages')).to_contain_text('Ответ агента')
                reader.locator('#shared-messages summary', has_text='Bash').click()
                pw.expect(reader.locator('#shared-messages details pre').last).to_have_text('test')
                pw.expect(reader.locator('#shared-messages')).to_contain_text('Изображение «shot.png» в ссылку не вошло')
                edited = reader.locator('#shared-messages article').nth(1)
                assert edited.evaluate("node => [...node.children].slice(1, -1).map((child) => child.tagName"
                                       " === 'DETAILS' ? 'step' : child.textContent.trim())") == ['Сначала', 'step', 'Потом']
                edited.locator('summary').click()
                pw.expect(edited.locator('.code-change')).to_contain_text('new line')
                assert reader.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('#chat-share-revoke').click()
            pw.expect(page.locator('#chat-share-link')).not_to_be_visible()
    finally:
        bridge.stop()
        client.close()
