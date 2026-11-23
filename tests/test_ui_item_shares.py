"""«Поделиться» записью раздела: окно владельца в «Расшифровке» («Обновить снимок», системное «Поделиться…»)
и страница получателя — реплики с собеседниками и временем, картинка по read-токену."""
import io
import json

import httpx
import pytest
from PIL import Image

import db
import media
from test_ui_browser import live  # noqa: F401 — модульная живая служба
from test_ui_chat_focus import focus_browser  # noqa: F401
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def seed(live):
    conn = db.connect(live.db_path)
    uid = conn.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    result = media.save(uid, json.dumps({"segments": [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "Добрый день"},
        {"start": 65.0, "end": 70.0, "speaker": "SPEAKER_01", "text": "Здравствуйте"}]}).encode(),
        "application/json", "result.json")
    audio = media.save(uid, b"mp3", "audio/mpeg", "meeting.mp3")
    conn.execute("DELETE FROM transcripts WHERE id = 'ui-t'")
    conn.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, result_media_id, duration, speakers,"
                 " state, detail, created, edits) VALUES ('ui-t', ?, 'whisperx', 'Встреча.mp3', ?, ?, 70, 2, 'done',"
                 " 'готово', 1, ?)", (uid, audio, result, json.dumps({"names": {"SPEAKER_00": "Анна"}})))
    png = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(png, "PNG")
    picture = media.save(uid, png.getvalue(), "image/png", "fox.png")
    conn.execute("DELETE FROM images WHERE id = 'ui-i'")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created)"
                 " VALUES ('ui-i', ?, 'generation', 'flux', 'рыжая лиса', '40x30', 1, ?, 'done', 'готово', 1)",
                 (uid, picture))
    conn.close()


@pytest.mark.parametrize('mobile,width', [(False, 1280), (True, 320)])
def test_record_share_refresh_and_reader_view(focus_browser, live, mobile, width):  # noqa: F811
    if mobile and focus_browser.browser_type.name == 'firefox':
        pytest.skip('Playwright не эмулирует мобильный Firefox')
    seed(live)
    signed = httpx.post(live.url + '/login', data={'login': 'owner', 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    client = httpx.Client(base_url=live.url, headers={'Cookie': cookie})
    client.headers['x-studio-csrf'] = client.get('/api/me').json()['csrf']
    options = {'viewport': {'width': width, 'height': 852}, 'is_mobile': mobile, 'has_touch': mobile}
    try:
        with focus_browser.new_context(**options, extra_http_headers={'Cookie': cookie}, service_workers='block') as context:
            if focus_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("navigator.share = async (data) => { window.sharedData = data; };")
            page.goto(live.url + '/#/transcribe?id=ui-t')
            page.locator('#tr-share').click()
            pw.expect(page.locator('#chat-share-dialog-title')).to_have_text('Поделиться расшифровкой')
            pw.expect(page.locator('#chat-share-source')).not_to_be_checked()
            pw.expect(page.locator('#chat-share-refresh')).to_be_hidden()
            pin = page.locator('#chat-share-pin')
            pw.expect(pin).to_be_enabled()
            pin.fill('0123')
            page.locator('#chat-share-create').click()
            link = page.locator('#chat-share-link')
            pw.expect(link).to_be_visible()
            url = link.input_value()
            page.locator('#chat-share-native').click()
            assert page.evaluate('window.sharedData.url') == url
            # «Обновить снимок» — тот же адрес и PIN
            page.locator('#chat-share-refresh').click()
            pw.expect(page.locator('#chat-share-facts')).to_contain_text('Снимок обновлён')
            pw.expect(link).to_have_value(url)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            image_url = client.post('/api/images/ui-i/share', json={'pin': '4567'}).json()['url']

            with focus_browser.new_context(**options, locale='ru-RU') as reader_context:
                reader = reader_context.new_page()
                reader.set_default_timeout(10000)
                reader.on('pageerror', lambda error: errors.append(str(error)))
                reader.goto(url)
                reader.locator('#share-pin').fill('0123')
                reader.locator('#share-open').click()
                pw.expect(reader.locator('#shared-title')).to_have_text('Встреча.mp3')
                pw.expect(reader.locator('#shared-kind')).to_have_text('Только чтение · расшифровка')
                pw.expect(reader.locator('.shared-segment')).to_have_text(['00:00 Анна: Добрый день',
                                                                           '01:05 SPEAKER_01: Здравствуйте'])
                assert reader.evaluate('document.documentElement.scrollWidth <= innerWidth')
                reader.goto(image_url)
                reader.locator('#share-pin').fill('4567')
                reader.locator('#share-open').click()
                picture = reader.locator('img.shared-picture')
                pw.expect(picture).to_be_visible()
                assert reader.evaluate("document.querySelector('img.shared-picture').naturalWidth") == 40
                pw.expect(reader.locator('.shared-item')).to_contain_text('рыжая лиса')
                assert reader.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert reader_context.cookies() == []
            assert not errors
    finally:
        client.delete('/api/transcripts/ui-t/share')
        client.close()
