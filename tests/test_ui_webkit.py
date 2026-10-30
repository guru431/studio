"""Раздел «Код» в WebKit: касания и геометрия iPhone, не только узкое окно Chromium."""

import hashlib
from pathlib import Path

import httpx
import pytest

from fake_bridge import pid
from test_ui_browser import CODE_CHAT_A, CODE_CHAT_B, code_online, code_three, live

pw = pytest.importorskip("playwright.sync_api")
pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.fixture(scope="module")
def webkit():
    with pw.sync_playwright() as driver:
        if not Path(driver.webkit.executable_path).is_file():
            pytest.skip("нет WebKit: .venv/Scripts/python -m playwright install webkit")
        browser = driver.webkit.launch()
        yield browser
        browser.close()


@pytest.fixture
def iphone(webkit, live, request):
    width, height = request.param
    login = "webkit-" + hashlib.sha256(request.node.name.encode()).hexdigest()[:12]
    live.add_user(login)
    machine = code_three()
    machine.projects["alpha"]["files"] = {"README.md": b"# Hello\n\nPreview text", "src/app.py": b"print(1)\n"}
    machine.projects.update({f"project-{n:02}": {} for n in range(40)})
    machine.projects["zulu"] = {}
    aid, bridge = code_online(live, login, machine)
    try:
        signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
        assert signed.status_code == 303
        # WebKit не отправляет Secure cookie по HTTP localhost. Только тестовый контекст
        # получает заголовок своей действительной сессии; защита cookie приложения прежняя.
        cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
        with webkit.new_context(viewport={"width": width, "height": height}, is_mobile=True,
                                has_touch=True, device_scale_factor=3,
                                extra_http_headers={"Cookie": cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(live.url + "/#/code")
            page.locator("#code-projects li[data-pid]").first.wait_for()
            yield page, aid
            assert not errors
    finally:
        bridge.stop()


def fits(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("iphone", [(320, 740), (393, 852)], ids=["320", "iphone15pro"], indirect=True)
def test_code_phone_projects_fill_the_panel_and_accept_taps(iphone, tmp_path):
    page, aid = iphone
    panel = page.locator("#code-panel")
    box = panel.bounding_box()
    rail = page.locator("#rail").bounding_box()
    assert abs(box["height"] - rail["y"]) <= 1, (box, rail)
    # Проверка попадания касания: наличие строк в DOM не означает, что они видны в обрезанной панели.
    alpha = page.locator(f"#code-projects li[data-pid='{pid('alpha')}'] a")
    alpha.tap(trial=True)
    assert alpha.evaluate("""e => {
        const r = e.getBoundingClientRect();
        return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }""")
    page.screenshot(path=str(tmp_path / "projects.png"))
    alpha.tap()
    panel.wait_for(state="hidden")
    page.locator("#code-chats li[data-chat]").first.wait_for()
    page.locator("#code-panel-toggle").tap()
    panel.wait_for(state="visible")
    page.locator(f"#code-projects li[data-pid='{pid('beta')}'] a").tap()
    panel.wait_for(state="hidden")
    pw.expect(page.locator(".code-place-project")).to_have_text("beta")
    assert page.url.endswith(f"#/code/{aid}/{pid('beta')}")
    page.locator("#code-panel-toggle").tap()
    page.locator(f"#code-projects li[data-pid='{pid('zulu')}'] a").tap()
    panel.wait_for(state="hidden")
    pw.expect(page.locator(".code-place-project")).to_have_text("zulu")
    fits(page)


@pytest.mark.parametrize("iphone", [(393, 852), (852, 393)], ids=["portrait", "landscape"], indirect=True)
def test_code_chat_tabs_files_and_git_show_their_content(iphone, tmp_path):
    page, aid = iphone
    page.locator(f"#code-projects li[data-pid='{pid('alpha')}'] a").tap()
    if page.viewport_size["width"] <= 760:
        page.locator("#code-panel").wait_for(state="hidden")
    page.locator(f"#code-chats li[data-chat='{CODE_CHAT_A}'] button").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    pw.expect(page.locator("#code-feed")).to_be_in_viewport()
    page.locator("#code-tab-add").tap()
    page.locator(f"#code-tab-menu li[data-chat='{CODE_CHAT_B}'] button").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про B")
    page.locator(f"#code-tabbar .code-tab[data-chat='{CODE_CHAT_A}'] .code-tab-title").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    page.screenshot(path=str(tmp_path / "chat.png"))
    fits(page)
    page.locator("#code-tab-files").tap()
    page.locator("#code-files li[data-name='README.md'] button").tap()
    pw.expect(page.locator("#code-editor")).to_have_value("# Hello\n\nPreview text")
    page.locator("#code-md-view").get_by_role("button", name="Вид", exact=True).tap()
    pw.expect(page.locator("#code-md")).to_contain_text("Hello")
    pw.expect(page.locator("#code-md")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "files.png"))
    fits(page)
    page.locator("#code-tab-git").tap()
    pw.expect(page.locator("#code-git")).to_contain_text("main")
    pw.expect(page.locator("#code-git")).to_contain_text("Изменений нет")
    pw.expect(page.locator("#code-git")).to_be_in_viewport()
    page.locator("#code-tab-chat").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    page.reload()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    fits(page)
