"""Оценка времени кэша: настоящий UI, управляемые часы, повторное открытие и неизвестные данные."""
import httpx
import pytest

from fake_bridge import pid
from test_codex import CATALOG
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_chat_focus import focus_browser, pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]
NOW = 1800000000


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_cache_countdown_survives_reload_and_handles_unknown_data(focus_browser, live, engine, width, tmp_path):
    login = f"cache-{engine}-{focus_browser.browser_type.name}-{width}"
    live.add_user(login)
    model = "gpt-6.1-sol" if engine == "codex" else "opus"
    machine = code_free_machine({CODE_CHAT_A: {**code_session("Кэш"), "engine": engine}})
    machine.codex = {**CATALOG, "models": [{"id": model, "label": "Тестовая модель"}]}
    machine.context = {"used": 85000, "limit": 100000, "usage": {"cached": 12345},
                       "cache": {"updated_at": NOW - 120, "model": model, "ttl_seconds": 3600}}
    aid, bridge = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    mobile = width == 320 and focus_browser.browser_type.name != "firefox"
    try:
        with focus_browser.new_context(viewport={"width": width, "height": 900}, is_mobile=mobile,
                has_touch=mobile, extra_http_headers={"Cookie": cookie}) as context:
            if focus_browser.browser_type.name == "firefox":
                context.add_cookies([{"name": key, "value": value, "url": live.url,
                    "secure": True, "httpOnly": True, "sameSite": "Lax"} for key, value in signed.cookies.items()])
            context.add_init_script(f"Date.now = () => Number(sessionStorage.getItem('cache-test-now') || '{NOW * 1000}');")
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            reloading = False
            def page_error(error):
                # WebKit иногда сообщает об отменённом SSE старого документа во время reload.
                if not (reloading and focus_browser.browser_type.name == "webkit"
                        and f"/chats/{CODE_CHAT_A}/live" in str(error)
                        and str(error).endswith(" due to access control checks.")):
                    errors.append(str(error))
            page.on("pageerror", page_error)
            def reload_chat():
                nonlocal reloading
                reloading = True
                page.reload()
                pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
                reloading = False
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
            timer = page.locator("#code-cache-text")
            pw.expect(timer).to_have_text("≈28:00" if engine == "codex" else "≈58:00")
            assert page.evaluate("""() => {
                const cache = document.querySelector('#code-cache'), context = document.querySelector('#code-context');
                return cache.nextElementSibling === context && cache.getBoundingClientRect().right <= context.getBoundingClientRect().left
                    && !!cache.getAttribute('aria-label') && document.documentElement.scrollWidth <= innerWidth;
            }""")
            for theme in ("light", "dark"):
                page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                page.screenshot(path=str(tmp_path / f"cache-{engine}-{width}-{theme}.png"))
            page.evaluate(f"sessionStorage.setItem('cache-test-now', '{(NOW + 62) * 1000}')")
            expected = "≈26:58" if engine == "codex" else "≈56:58"
            pw.expect(timer).to_have_text(expected)
            page.locator('.code-tab[aria-selected="true"] .code-tab-title').click()
            pw.expect(timer).to_have_text(expected)
            reload_chat()
            pw.expect(timer).to_have_text(expected)
            page.locator("#code-cache").click()
            dialog = page.locator("#code-cache-dialog")
            pw.expect(dialog).to_be_visible()
            assert dialog.locator('dt').all_text_contents() == ['Из кэша', 'Запись в кэш', 'Кэш: оценка оставшегося времени']
            pw.expect(dialog).to_contain_text("Из кэша")
            pw.expect(dialog).to_contain_text("12\u00a0345")
            pw.expect(dialog).to_contain_text("Открытие чата отсчёт не продлевает")
            assert page.evaluate("""() => {
                const first = document.querySelector('#code-cache-dialog dt');
                document.dispatchEvent(new Event('visibilitychange'));
                return first === document.querySelector('#code-cache-dialog dt')
                    && document.querySelector('#code-cache-dialog dd:last-child').getAttribute('aria-live') === 'off';
            }""")
            page.locator("#code-cache-dialog .btn").click()
            page.locator("#code-context").click()
            details = page.locator("#code-context-dialog")
            pw.expect(details).to_contain_text('Общий размер контекста')
            assert 'Из кэша' not in details.inner_text() and 'Таймер кэша' not in details.inner_text()
            page.locator("#code-context-dialog .btn").click()
            if engine == "claude":
                machine.context["cache"]["ttl_seconds"] = 300
                reload_chat()
                pw.expect(timer).to_have_text("≈1:58")
            page.evaluate(f"sessionStorage.setItem('cache-test-now', '{(NOW + 4000) * 1000}'); document.dispatchEvent(new Event('visibilitychange'))")
            pw.expect(timer).to_have_text("≈0:00")
            pw.expect(page.locator("#code-cache")).to_have_attribute("data-state", "elapsed")
            assert "неизвестно" in page.locator("#code-cache").get_attribute("title")
            machine.context["cache"].pop("updated_at")
            reload_chat()
            pw.expect(timer).to_have_text("—")
            pw.expect(page.locator("#code-cache")).to_have_attribute("data-state", "unknown")
            assert not errors
    finally:
        bridge.stop()
