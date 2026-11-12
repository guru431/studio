"""Читаемость строк проектов и переходы в Chromium, Firefox и WebKit."""

import httpx
import pytest

from fake_bridge import T0, pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_requests, code_session, live
from test_ui_desktop import desktop_browser
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_project_names_dates_and_navigation(desktop_browser, live, width, tmp_path):
    login = f"projects-{desktop_browser.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Сессия")})
    machine.project_summary = True
    for name in ("alpha", "beta", "long-project-name-that-does-not-fit-the-panel"):
        machine.projects.setdefault(name, {"chats": {}})["summary"] = {
            "git": "ok", "changed": 0, "unpushed": 0, "findings": 0, "ideas": 0, "modified": T0, "checked_at": T0}
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={"width": width, "height": 900}, has_touch=width == 320,
                                         extra_http_headers={"Cookie": cookie}) as context:
            if desktop_browser.browser_type.name == "firefox":
                context.add_cookies([{"name": name, "value": value, "url": live.url, "secure": True,
                                     "httpOnly": True, "sameSite": "Lax"} for name, value in signed.cookies.items()])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
            for theme in ("light", "dark"):
                page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                if width == 320:
                    page.locator("#code-panel-toggle").click()
                page.locator("#code-project-refresh").click()
                pw.expect(page.locator("#code-project-refresh")).to_have_attribute("aria-busy", "false")
                row = page.locator(f"#code-projects li[data-pid='{pid('alpha')}']")
                pw.expect(row.locator(".code-project-modified")).not_to_have_text("—")
                layout = row.evaluate("""li => {
                    const name=li.querySelector('.code-project-name'), metrics=li.querySelector('.code-project-metrics');
                    const git=li.querySelector('.code-project-git').getBoundingClientRect();
                    const date=li.querySelector('.code-project-modified').getBoundingClientRect();
                    const current=li.getBoundingClientRect(), next=li.nextElementSibling.getBoundingClientRect();
                    return {weight:+getComputedStyle(name).fontWeight, metrics:+getComputedStyle(metrics).fontWeight,
                        dateGap:date.left-git.right, baseline:Math.abs(date.top-git.top),
                        rowGap:next.top-current.bottom, width:document.documentElement.scrollWidth, viewport:innerWidth};
                }""")
                assert layout["weight"] > layout["metrics"], layout
                assert 0 <= layout["dateGap"] < 30 and layout["baseline"] < 2, layout
                assert layout["rowGap"] >= 3 and layout["width"] <= layout["viewport"], layout
                long_name = page.locator(".code-project-name", has_text="long-project-name")
                assert long_name.get_attribute("title") == "long-project-name-that-does-not-fit-the-panel"
                page.screenshot(path=str(tmp_path / f"projects-{desktop_browser.browser_type.name}-{width}-{theme}.png"))
                beta = page.locator(f"#code-projects li[data-pid='{pid('beta')}'] .code-project-name")
                if width == 320:
                    beta.tap()
                else:
                    beta.focus()
                    beta.press("Enter")
                pw.expect(page.locator(".code-place-project")).to_have_text("beta")
                if theme == "light":
                    page.reload()
                    pw.expect(page.locator(".code-place-project")).to_have_text("beta")
            assert not errors
    finally:
        channel.stop()


def test_large_attachment_limits_and_delivery(desktop_browser, live):
    login = f"attachment-limits-{desktop_browser.browser_type.name}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Вложения")})
    machine.attachments = machine.large_attachments = True
    machine.script = [{"k": "text", "text": "Получил 30 МиБ"}, {"k": "done", "status": "done"}]
    aid, channel = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(extra_http_headers={"Cookie": cookie}) as context:
            if desktop_browser.browser_type.name == "firefox":
                context.add_cookies([{"name": name, "value": value, "url": live.url, "secure": True,
                                     "httpOnly": True, "sameSite": "Lax"} for name, value in signed.cookies.items()])
            page = context.new_page()
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")

            def pick(sizes):
                page.evaluate("""sizes => {
                    const dt = new DataTransfer();
                    sizes.forEach((size, i) => dt.items.add(new File([new Uint8Array(size)], `file-${i}.bin`)));
                    const input = document.querySelector('#code-image-input'); input.files = dt.files;
                    input.dispatchEvent(new Event('change'));
                }""", sizes)

            pick([10 * 1024 * 1024 + 1])
            pw.expect(page.locator("#code-status")).to_contain_text("до 10 МиБ")
            pw.expect(page.locator("#code-images .code-file")).to_have_count(0)
            pick([10 * 1024 * 1024] * 3)
            pw.expect(page.locator("#code-images .code-file")).to_have_count(3)
            pick([1])
            pw.expect(page.locator("#code-status")).to_contain_text("до 30 МиБ")
            pw.expect(page.locator("#code-images .code-file")).to_have_count(3)
            page.locator("#code-send").click()
            pw.expect(page.locator("#code-feed")).to_contain_text("Получил 30 МиБ", timeout=20000)
            [request] = code_requests(machine, "turn")
            assert len(request["attachments"]) == 3
            assert all(len(item["data"]) == (10 * 1024 * 1024 + 2) // 3 * 4 for item in request["attachments"])
    finally:
        channel.stop()
