"""«Файлы»: «нет такого файла» от моста — обычный отказ, а не закрытие компьютера: кэши раздела и метки
активности не сбрасываются."""
import httpx
import pytest

from fake_bridge import pid
from test_ui_browser import code_files_machine, code_online, live
from test_ui_chat_focus import focus_browser, pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_missing_file_does_not_clear_the_section_caches(focus_browser, live, width):
    if focus_browser.browser_type.name == "firefox":
        pytest.skip("сценарии «Агентов» — Chromium и WebKit")
    login = f"not-found-{focus_browser.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_files_machine()           # без FINDINGS.md: журнал проекта — «нет такого файла»
    aid, bridge = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    mobile = width < 760
    try:
        with focus_browser.new_context(viewport={"width": width, "height": 852}, is_mobile=mobile, has_touch=mobile,
                                       extra_http_headers={"Cookie": cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}?tab=files")
            page.locator("#code-files li[data-name='README.md']").wait_for()
            page.evaluate("window.cleared = 0; document.addEventListener('studio-code-clear', () => window.cleared++)")
            page.evaluate(f"location.hash = '#/code/{aid}/{pid('alpha')}?tab=files&journal=findings'")
            pw.expect(page.locator("#code-file-status")).to_contain_text("ет такого файла")
            assert page.evaluate("window.cleared") == 0
            assert any(q["op"] == "read" and q.get("path") == "FINDINGS.md" for q in machine.requests)
            assert not errors, errors
    finally:
        bridge.stop()
