"""Счётчик доступных сбросов Codex в Firefox, Chromium и WebKit."""

import httpx
import pytest

from fake_bridge import pid
from test_codex import CATALOG
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_chat_focus import focus_browser, pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_opencode_weekly_limit_follows_selected_provider(focus_browser, live):
    login = f"opencode-usage-{focus_browser.browser_type.name}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: {**code_session("Лимиты OpenCode"), "engine": "opencode"}})
    machine.opencode = {"version": "2.2.4", "models": [
        {"id": "gateway/model", "label": "Gateway"}, {"id": "other/model", "label": "Other"}],
        "efforts": ["default"], "defaults": {"model": "gateway/model"}}
    machine.subscription_usage = machine.opencode_usage = True
    machine.usage_data = {"engine": "opencode", "available": True, "source": "OpenCode Go",
        "checked_at": 1800000000, "cache_ttl": 60, "windows": [{
            "id": "seven_day", "label": "Неделя", "available": True, "used_percent": 37,
            "remaining_percent": 63, "window_minutes": 10080}],
        "statistics": {"available": False, "source": "OpenCode", "summary": {}, "daily": []}}
    aid, bridge = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    try:
        with focus_browser.new_context(extra_http_headers={"Cookie": cookie}, service_workers="block") as context:
            if focus_browser.browser_type.name == "firefox":
                context.add_cookies([{"name": name, "value": value, "url": live.url,
                    "secure": True, "httpOnly": True, "sameSite": "Lax"} for name, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
            pw.expect(page.locator("#code-usage-remaining")).to_have_text("63%")
            assert any(r.get("op") == "usage" and r.get("model") == "gateway/model" for r in machine.requests)
            pw.expect(page.locator("#code-usage-refresh")).to_be_enabled()
            # Ответ прежнего провайдера задержан до смены модели: он не должен вернуть старый процент.
            held = []
            page.route("**/usage?*", lambda route: held.append(route), times=1)
            page.locator("#code-usage").click()
            pw.expect(page.locator("#code-usage-refresh")).to_be_disabled()
            page.locator("#code-usage-dialog").get_by_role("button", name="Закрыть", exact=True).click()
            page.locator("#code-model").select_option("other/model")
            pw.expect(page.locator("#code-usage-remaining")).to_have_text("—")
            assert len(held) == 1
            previous = dict(machine.usage_data)
            machine.usage_data.update(available=False, windows=[])
            held[0].fulfill(json=previous)
            pw.expect(page.locator("#code-usage-refresh")).to_be_enabled()
            pw.expect(page.locator("#code-usage-remaining")).to_have_text("—")
            assert any(r.get("op") == "usage" and r.get("model") == "other/model" for r in machine.requests)
            assert not errors
    finally:
        bridge.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_codex_subscription_shows_available_resets_and_unknown_count(focus_browser, live, width, tmp_path):
    login = f"codex-resets-{focus_browser.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: {**code_session("Сбросы Codex"), "engine": "codex"}})
    machine.codex = CATALOG
    machine.subscription_usage = True
    machine.usage_data = {"engine": "codex", "available": False, "source": "Codex app-server",
        "checked_at": 1800000000, "cache_ttl": 60, "windows": [],
        "statistics": {"available": True, "source": "Аккаунт Codex",
                       "summary": {"available_resets": 2}, "daily": [],
                       "reset_credits": [{"expires_at": 1767216645}, {"expires_at": 1767303045}]}}
    aid, bridge = code_online(live, login, machine)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    mobile = width == 320 and focus_browser.browser_type.name != "firefox"
    try:
        with focus_browser.new_context(viewport={"width": width, "height": 900}, is_mobile=mobile,
                has_touch=mobile, timezone_id="Asia/Tokyo", extra_http_headers={"Cookie": cookie}) as context:
            if focus_browser.browser_type.name == "firefox":
                context.add_cookies([{"name": name, "value": value, "url": live.url,
                                     "secure": True, "httpOnly": True, "sameSite": "Lax"}
                                    for name, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
            page.locator("#code-usage").click()
            dialog = page.locator("#code-usage-dialog")
            count = dialog.locator(".code-usage-summary dd").first
            pw.expect(dialog.locator("dt").first).to_have_text("Доступные сбросы лимита")
            pw.expect(count).to_have_text("2")
            expiry = dialog.locator(".code-reset-credits dd")
            # Account defaults to Moscow even when the browser is in Tokyo.
            pw.expect(expiry).to_have_text(["До 01.01.2026, 00:30:45", "До 02.01.2026, 00:30:45"])
            for theme in ("light", "dark"):
                page.evaluate("theme => { document.documentElement.dataset.theme = theme; }", theme)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
                page.screenshot(path=str(tmp_path / f"codex-resets-{width}-{theme}.png"))
            machine.usage_data["statistics"].update(summary={"available_resets": 4},
                reset_credits=[{"expires_at": 1767216645}, {"expires_at": None}])
            page.locator("#code-usage-refresh").click()
            pw.expect(expiry).to_have_text(["До 01.01.2026, 00:30:45", "Не сообщается"])
            pw.expect(dialog.locator(".code-reset-credits")).to_contain_text("Сроки указаны для 2 из 4 сбросов")
            machine.usage_data["statistics"].update(summary={"available_resets": 2}, reset_credits=None)
            page.locator("#code-usage-refresh").click()
            pw.expect(dialog.locator(".code-reset-credits")).to_contain_text("Срок действия не сообщается")
            pw.expect(page.locator("#code-usage-refresh")).to_be_enabled()
            for summary, expected in (({"available_resets": 0}, "0"), ({}, "Не сообщается"),
                                      ({"available_resets": None}, "Не сообщается")):
                machine.usage_data["statistics"].update(summary=summary, available=bool(summary), reset_credits=None)
                page.locator("#code-usage-refresh").click()
                pw.expect(count).to_have_text(expected)
                pw.expect(page.locator("#code-usage-refresh")).to_be_enabled()
                pw.expect(dialog.locator(".code-reset-credits")).to_have_count(0)
            assert not errors
    finally:
        bridge.stop()
