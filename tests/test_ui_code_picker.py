"""Отдельные параметры хода: клавиатура, касания и геометрия при размере текста и масштабе."""
import httpx
import pytest

from fake_bridge import pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_chat_focus import focus_browser, pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

GEOMETRY = """menuId => {
    const menu = document.querySelector('#' + menuId).getBoundingClientRect();
    const viewport = visualViewport;
    const buttons = [...document.querySelectorAll('#code-composer .code-pick-trigger, #code-send')];
    const rects = buttons.filter(e => e.getClientRects().length).map(e => e.getBoundingClientRect());
    return {menu:menu.toJSON(), viewport:{left:viewport.offsetLeft,top:viewport.offsetTop,width:viewport.width,height:viewport.height},
      menuFits:menu.left >= viewport.offsetLeft && menu.right <= viewport.offsetLeft + viewport.width
        && menu.top >= viewport.offsetTop && menu.bottom <= viewport.offsetTop + viewport.height,
      controlsFit:rects.every((r,i) => r.left >= 0 && r.right <= innerWidth && (!i || r.left >= rects[i-1].right - 1)),
      pageFits:document.documentElement.scrollWidth <= innerWidth, viewportScale:viewport.scale};
}"""


def open_chat(focus_browser, live, width, login):
    engine = focus_browser.browser_type.name
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: {**code_session("Параметры"), "engine": "codex"}})
    machine.codex = {"version": "test", "efforts": ["low", "high", "xhigh"],
        "models": [{"id": "gpt-6.1-sol", "label": "gpt-6.1-sol"},
                   {"id": "long-model", "label": "Модель с очень длинным названием " * 2}]}
    aid, bridge = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    mobile = width < 760 and engine != "firefox"
    context = focus_browser.new_context(viewport={"width": width, "height": 852},
        is_mobile=mobile, has_touch=mobile, extra_http_headers={"Cookie": cookie})
    if engine == "firefox":
        context.add_cookies([{"name": key, "value": value, "url": live.url,
            "secure": True, "httpOnly": True, "sameSite": "Lax"} for key, value in signed.cookies.items()])
    page = context.new_page()
    page.set_default_timeout(10000)
    page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
    pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
    return bridge, context, page, mobile


def styles(page, scale, font, theme):
    page.evaluate("""({scale,font,theme}) => {
        const root = document.documentElement;
        root.dataset.theme = theme;
        root.style.zoom = scale;
        root.style.setProperty('--ui-scale', scale);
        root.style.setProperty('--font-scale', font);
    }""", {"scale": scale, "font": font, "theme": theme})


@pytest.mark.parametrize("width", [320, 393, 760, 1280])
def test_choices_fit_and_support_keyboard_touch_and_lifecycle(focus_browser, live, width, tmp_path):
    engine = focus_browser.browser_type.name
    bridge, context, page, mobile = open_chat(focus_browser, live, width, f"choice-{engine}-{width}")
    try:
        with context:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            pw.expect(page.locator("#code-effort-pick")).to_be_enabled()
            pw.expect(page.locator("#code-settings-pick")).to_have_count(0)
            for name in ("model", "effort", "access"):
                pw.expect(page.locator(f"#code-{name}-pick")).to_be_visible()
            for scale, font in [(1, 1), (.8, 1.5), (.5, .85)]:
                for theme in ("light", "dark"):
                    styles(page, scale, font, theme)
                    trigger = page.locator("#code-effort-pick")
                    trigger.tap() if mobile else trigger.click()
                    menu = page.locator("#code-effort-menu")
                    pw.expect(menu).to_be_visible()
                    geometry = page.evaluate(GEOMETRY, "code-effort-menu")
                    if scale == 1:
                        page.screenshot(path=str(tmp_path / f"choices-{theme}.png"))
                    assert geometry["menuFits"] and geometry["controlsFit"] and geometry["pageFits"] and geometry["viewportScale"] == 1, geometry
                    menu.get_by_role("menuitemradio", name="Очень высокое xhigh", exact=True).click()
                    pw.expect(page.locator("#code-effort")).to_have_value("xhigh")
                    pw.expect(trigger).to_contain_text("xhigh")
                    assert page.evaluate("document.activeElement.id !== 'code-text'")
            trigger.focus()
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Home")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            pw.expect(page.locator("#code-effort")).to_have_value("low")
            page.locator("#code-model-pick").click()
            page.keyboard.press("End")
            page.keyboard.press("Enter")
            pw.expect(page.locator("#code-model")).to_have_value("long-model")
            page.locator("#code-access-pick").click()
            page.locator("#code-access-menu").get_by_role("menuitemradio", name="Проект", exact=True).click()
            pw.expect(page.locator("#code-access")).to_have_value("workspace-write")
            page.locator("#code-access-pick").click()
            page.keyboard.press("Escape")
            pw.expect(page.locator("#code-access-menu")).not_to_be_visible()
            pw.expect(page.locator("#code-access-pick")).to_be_focused()
            page.locator("#code-model-pick").click()
            page.mouse.click(width - 2, 2)
            pw.expect(page.locator("#code-model-menu")).not_to_be_visible()
            page.locator("#code-effort-pick").click()
            page.locator("#code-tab-add").click()
            pw.expect(page.locator("#code-effort-menu")).to_have_count(0)
            assert not errors, errors
    finally:
        bridge.stop()
