"""Картинка истории возвращается после смены чата; данные и запоздавшие ответы не переживают уход."""
import base64

import httpx
import pytest

from fake_bridge import T0, pid
from fakes import PNG_REAL
from test_codex import CATALOG
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine, code_loaded,
                             code_online, code_send, code_session, go, live, problems, signin)
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
@pytest.mark.parametrize('cli', ['claude', 'codex'])
def test_image_is_restored_after_switching_chat_and_revoked_on_leave(desktop_browser, live, width, cli):
    browser = desktop_browser.browser_type.name
    login = f'img-{browser}-{cli}-{width}'
    live.add_user(login)
    message = {'id': 'picture-message', 'role': 'user', 'text': 'Посмотри на скриншот', 'steps': [], 'time': T0,
        'images': [{'index': 0, 'name': 'Изображение 1'}]}
    first = {**code_session('С изображением', messages=[message]), 'engine': cli,
        'image_data': {('picture-message', 0): {'mime': 'image/png', 'data': base64.b64encode(PNG_REAL).decode()}}}
    second = {**code_session('Другой чат', messages=[]), 'engine': cli}
    machine = code_free_machine({CODE_CHAT_A: first, CODE_CHAT_B: second})
    machine.history_images = True
    if cli == 'codex':
        machine.codex = CATALOG
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookies = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 320,
                is_mobile=width == 320 and browser != 'firefox', extra_http_headers={'Cookie': cookies}) as context:
            if browser == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            context.add_init_script("""(() => {
              window.revokedImages=[];
              const revoke=URL.revokeObjectURL.bind(URL);
              URL.revokeObjectURL=url => {window.revokedImages.push(url); revoke(url);};
            })()""")
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            path = f'#/code/{aid}/{pid("alpha")}/'
            page.goto(live.url + '/' + path + CODE_CHAT_A)
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            loaded = "() => document.querySelector('.code-sent-image')?.naturalWidth === 64"
            page.wait_for_function(loaded)
            source = page.locator('.code-sent-image').get_attribute('src')
            assert source.startswith('blob:')
            assert page.locator('.code-msg.user').inner_text() == 'Посмотри на скриншот'
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            assert page.locator('.code-sent-image').count() == 0
            assert source in page.evaluate('window.revokedImages')
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_A)
            page.wait_for_function(loaded)
            assert page.locator('.code-sent-image').get_attribute('src') != source
            calls = [row for row in machine.requests if row['op'] == 'history_image']
            assert len(calls) == 2
            assert all(row['chat'] == CODE_CHAT_A and row['message_id'] == 'picture-message' and row['index'] == 0 for row in calls)
            page.evaluate("""() => {
              const fetchOriginal=window.fetch;
              window.restoreImageFetch=() => {window.fetch=fetchOriginal;};
              window.fetch=async (url, options) => {
                if (new URL(String(url),location.href).pathname.endsWith('/image'))
                  await new Promise(resolve => {window.releaseImage=resolve;});
                return fetchOriginal(url,options);
              };
            }""")
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_A)
            page.wait_for_function("() => typeof window.releaseImage === 'function'")
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            page.evaluate('window.releaseImage()')
            page.evaluate('window.restoreImageFetch()')
            assert page.locator('.code-sent-image').count() == 0
            # Уточнение идёт по живому каналу, затем возвращается в снимке того же хода.
            machine.script = [{'k': 'text', 'text': 'Работа продолжается'}]
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_A)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_A}"][data-loaded="1"]').wait_for()
            page.locator('#code-text').fill('Начать проверку')
            page.locator('#code-send').click()
            page.get_by_text('Работа продолжается', exact=True).wait_for()
            page.locator('#code-image-input').set_input_files(
                {'name': 'screen.png', 'mimeType': 'image/png', 'buffer': PNG_REAL})
            page.locator('#code-images img').wait_for()
            page.locator('#code-text').fill('Уточнение со скриншотом')
            page.locator('#code-send').click()
            live_image = "() => document.querySelector('[data-followup] .code-sent-image')?.naturalWidth === 64"
            page.wait_for_function(live_image)
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_B)
            page.locator(f'#code-chat[data-chat="{CODE_CHAT_B}"][data-loaded="1"]').wait_for()
            page.evaluate('hash => location.hash=hash', path + CODE_CHAT_A)
            page.wait_for_function(live_image)
            assert page.locator('[data-followup] .code-sent-image').get_attribute('src').startswith('blob:')
            assert not errors, errors
    finally:
        channel.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_claude_previews_tool_images_inline_during_the_turn_and_after_reopening(browser, live, width):
    login = f"tool-image-{width}"
    live.add_user(login)
    path = r"C:\root\alpha\.tmp\overview.png"
    earlier = {"index": 0, "name": "overview.png", "message_id": "tool:earlier"}
    saved = {"index": 0, "name": "overview.png", "message_id": "tool:saved"}
    live_image = {"index": 0, "name": "detail.png", "message_id": "tool:live"}
    steps = [{"id": mid, "tool": "Read", "input": {"file_path": path}, "images": [image], "text": "[изображение]",
              "error": False} for mid, image in [("old", earlier), ("new", saved)]]
    first = code_session("Макеты", messages=[{"id": "answer", "role": "assistant", "text": "Общий план",
                                             "steps": steps, "time": T0}])
    png = {"mime": "image/png", "data": base64.b64encode(PNG_REAL).decode()}
    first["image_data"] = {(saved["message_id"], 0): png, (live_image["message_id"], 0): png}
    machine = code_free_machine({CODE_CHAT_A: first, CODE_CHAT_B: code_session("Другой чат")})
    machine.history_images = True
    machine.script = [{"k": "step", "id": "detail", "tool": "Read", "input": {"file_path": "detail.png"}},
                      {"k": "result", "id": "detail", "text": "[изображение]", "images": [live_image]},
                      {"k": "text", "text": "Фрагмент карты"}]
    aid, channel = code_online(live, login, machine)
    images = ".code-msg.assistant .code-result-images img"
    ready = lambda name: f"""document.querySelector('{images}[alt="{name}"]')?.naturalWidth === 64"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", ready("overview.png")),
            ("eval", "savedCount", f"document.querySelectorAll('{images}').length"),
            ("click", ".code-result-image"), ("wait", "document.querySelector('#code-result-preview[open] img')?.naturalWidth === 64"),
            ("click", "#code-result-preview .dialog-close"),
            *code_send("Покажи фрагмент"), ("wait", ready("detail.png")),
            *go(f"code/{aid}/{pid('alpha')}/{CODE_CHAT_B}"), code_loaded(CODE_CHAT_B),
            ("eval", "otherCount", f"document.querySelectorAll('{images}').length"),
            *go(f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"), code_loaded(CODE_CHAT_A), ("wait", ready("detail.png"))],
            width=width, mobile=width == 320)
    finally:
        channel.stop()
    assert not result.get("failed"), result
    assert result["values"]["savedCount"] == 1 and result["values"]["otherCount"] == 0
    requested = {frame["message_id"] for frame in machine.requests if frame["op"] == "history_image"}
    assert requested == {"tool:saved", "tool:live"}
    assert not problems(result), problems(result)
