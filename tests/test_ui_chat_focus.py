"""Фокус ввода при открытии чата на компьютере и отсутствие автофокуса на телефоне."""
import httpx
import pytest

from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, code_free_machine, code_online,
                             code_session, live)
from test_ui_webkit import pw, webkit

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("mobile,width", [(False, 1280), (False, 600), (True, 393)])
@pytest.mark.parametrize("section", ["chat", "code"])
def test_opening_chat_focuses_composer_only_on_desktop(webkit, live, section, mobile, width):
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
        with webkit.new_context(viewport={"width": width, "height": 900}, is_mobile=mobile,
                                has_touch=mobile, extra_http_headers={"Cookie": cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)

            def check_focus():
                field = page.locator(selector)
                field.wait_for(state="visible")
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
    finally:
        if bridge:
            bridge.stop()
