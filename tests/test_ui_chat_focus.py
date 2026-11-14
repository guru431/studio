"""Фокус ввода при открытии чата на компьютере и отсутствие автофокуса на телефоне."""
from pathlib import Path

import httpx
import pytest

from browser import CHROMIUM_ARGS, find_browser
from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, code_free_machine, code_online,
                             code_session, live)
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.fixture(scope="module", params=["firefox", "chromium", "webkit"])
def focus_browser(request):
    with pw.sync_playwright() as driver:
        if request.param == "chromium":
            executable = find_browser()
            if not executable:
                pytest.skip("нет Chromium/Edge/Chrome")
            browser = driver.chromium.launch(executable_path=executable, args=CHROMIUM_ARGS)
        else:
            browser_type = getattr(driver, request.param)
            if not Path(browser_type.executable_path).is_file():
                pytest.skip(f"нет {request.param}")
            browser = browser_type.launch()
        yield browser
        browser.close()


@pytest.mark.parametrize("mobile,width", [(False, 1280), (False, 600), (True, 393)])
@pytest.mark.parametrize("section", ["chat", "code"])
def test_opening_chat_focuses_composer_only_on_desktop(focus_browser, live, section, mobile, width):
    if mobile and focus_browser.browser_type.name == "firefox":
        pytest.skip("Playwright не эмулирует мобильный Firefox")
    signed = httpx.post(live.url + "/login", data={"login": "owner", "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    bridge = None
    if section == "code":
        machine = code_free_machine({CODE_CHAT_A: code_session("Пустой чат"), CODE_CHAT_B: code_session(
            "История", messages=[{"id": "m-1", "role": "user", "text": "Сообщение\n\n" * 500,
                                  "steps": [], "time": 1}])})
        aid, bridge = code_online(live, "owner", machine)
        route = f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
        selector = "#code-text"
    else:
        route = "chat"
        selector = "#chat-input"
    try:
        with focus_browser.new_context(viewport={"width": width, "height": 900}, is_mobile=mobile,
                                has_touch=mobile, extra_http_headers={"Cookie": cookie}) as context:
            if focus_browser.browser_type.name == 'firefox':
                # Его service worker не наследует тестовый заголовок Cookie при reload.
                # Firefox принимает Secure cookie localhost; WebKit нужен заголовок выше.
                context.add_cookies([{'name': name, 'value': value, 'url': live.url,
                                     'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}
                                    for name, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def check_focus():
                field = page.locator(selector)
                try:
                    field.wait_for(state="visible")
                except pw.TimeoutError:
                    pytest.fail(str({'url': page.url, 'errors': errors, 'state': page.evaluate("""() => ({
                        ready:document.readyState, shrouded:document.documentElement.classList.contains('shrouded'),
                        dialogs:[...document.querySelectorAll('dialog[open]')].map(d=>d.id),
                        view:document.querySelector('#view')?.children.length,
                        controlled:!!navigator.serviceWorker.controller
                    })""")}), pytrace=False)
                if section == "code":
                    pw.expect(page.locator('#code-chat')).to_have_attribute('data-chat', route.rsplit('/', 1)[-1])
                    pw.expect(page.locator('#code-chat')).to_have_attribute('data-loaded', '1')
                elif route != "chat":
                    page.locator(f'.chat-item[data-id="{route.split("/")[1]}"] [aria-current="page"]').wait_for(state="attached")
                # Автофокус отложен до кадра, в котором оболочка снимет inert и visibility.
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                if mobile:
                    pw.expect(field).not_to_be_focused()
                else:
                    pw.expect(field).to_be_focused()
                    page.keyboard.insert_text("Можно писать сразу")
                    pw.expect(field).to_have_value("Можно писать сразу")
                    page.keyboard.press("ControlOrMeta+A")
                    page.keyboard.press("Backspace")

            page.goto(live.url + "/#/" + route)
            check_focus()
            if section == "code":
                page.locator('.code-tab[aria-selected="true"] .code-tab-title').click()
                check_focus()
                page.locator('#code-tab-chat').click()
                check_focus()
                if not mobile:
                    active_tab = page.locator('.code-tab[aria-selected="true"]')
                    active_tab.focus()
                    page.keyboard.press('Enter')
                    pw.expect(active_tab).to_be_focused()
                    active_tab.locator('.code-tab-title').click()
                    check_focus()
            if section == "chat":
                if width < 760:
                    page.locator('#chats-toggle').click()
                page.locator('#chat-new').click()
                check_focus()
                chat_id = page.evaluate("""async () => {
                    const {api} = await import('/static/js/api.js');
                    return (await api('POST', '/api/chats', {})).id;
                }""")
                route = f"chat/{chat_id}"
            else:
                route = f"code/{aid}/{pid('alpha')}/{CODE_CHAT_B}"
            page.evaluate("route => { location.hash = '#/' + route; }", route)
            check_focus()
            if section == "code":
                assert page.evaluate("""() => {
                    const view = document.querySelector('#view');
                    return view.scrollHeight - view.clientHeight - view.scrollTop < 2;
                }""")
                page.locator(f'.code-tab[data-chat="{CODE_CHAT_A}"] .code-tab-title').click()
                route = f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
                check_focus()
            page.reload()
            check_focus()
            if section == "code":
                page.locator('#code-tab-add').click()
                page.locator('#code-new-chat').click()
                pw.expect(page.locator('#code-chat')).to_have_attribute('data-loaded', '1')
                route = f"code/{aid}/{pid('alpha')}/" + page.locator('#code-chat').get_attribute('data-chat')
                check_focus()
    finally:
        if bridge:
            bridge.stop()
