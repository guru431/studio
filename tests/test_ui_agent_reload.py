"""Чат «Агентов»: первая загрузка истории, которую обогнали или которая упала, не оставляет «Отправить» и выбор
модели выключенными навсегда; «Повторить» в строке статуса; возврат компьютера — сразу, событием `online`."""
import re
import time

import httpx
import pytest

from fake_bridge import SocketBridge, pid
from test_ui_browser import CODE_CHAT_A, CODE_WAITING, code_free_machine, code_session, enroll_live, live, wait_for
from test_ui_chat_focus import focus_browser, pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]
HISTORY = re.compile(rf".*/chats/{CODE_CHAT_A}(\?.*)?$")


class KeptSessions(set):
    """Открытые сессии машины переживают переподключение моста (мост ≥ 0.7.1 сохраняет срок PIN)."""

    def clear(self):
        pass


def online(live, login, machine):
    for project in machine.projects.values():
        project.setdefault("git", {"branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0, "files": []})
    aid, key = enroll_live(live, login, "Рабочий ПК")
    import bridge
    sb = SocketBridge(live.url, key, machine).start()
    wait_for(lambda: aid in bridge.LINKS)
    return aid, key, sb


def open_page(browser, live, login, width):
    if browser.browser_type.name == "firefox":
        pytest.skip("сценарии «Агентов» — Chromium и WebKit")
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    mobile = width < 760
    # Запросы под service worker WebKit не отдаёт page.route — сценарии подменяют историю, поэтому без него.
    context = browser.new_context(viewport={"width": width, "height": 852}, is_mobile=mobile, has_touch=mobile,
                                  extra_http_headers={"Cookie": cookie}, service_workers="block")
    page = context.new_page()
    page.set_default_timeout(10000)
    return context, page


def ready(page):
    pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
    pw.expect(page.locator("#code-send")).to_be_enabled()
    for name in ("model", "effort", "access"):
        pw.expect(page.locator(f"#code-{name}-pick")).to_be_enabled()


def fits(page):
    return page.evaluate("""() => {
        const bar = document.querySelector('.code-status-bar');
        return document.documentElement.scrollWidth <= innerWidth && bar.getBoundingClientRect().right <= innerWidth + 1
          && [...bar.children].every(e => !e.getClientRects().length || e.getBoundingClientRect().right <= innerWidth + 1);
    }""")


@pytest.mark.parametrize("width", [320, 1280])
def test_first_history_overtaken_by_return_to_the_screen_still_enables_sending(focus_browser, live, width):
    """Первая загрузка истории задержана, страница «вернулась на экран» (открытие из push) — вторая загрузка её
    обгоняет. Раньше состояние сессии не запрашивал никто: «Отправить» и выбор модели оставались выключенными."""
    login = f"reload-overtaken-{focus_browser.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Обгон")})
    aid, _, bridge = online(live, login, machine)
    held = []

    def hold(route):
        if route.request.method == "GET" and not held:
            held.append(route)
            return
        route.continue_()
    try:
        context, page = open_page(focus_browser, live, login, width)
        with context:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(HISTORY, hold)
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            deadline = time.monotonic() + 10
            while not held and time.monotonic() < deadline:
                page.wait_for_timeout(25)
            assert held
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")
            held[0].continue_()
            ready(page)
            pw.expect(page.locator("#code-reload")).to_be_hidden()
            assert [q["op"] for q in machine.requests].count("state") >= 1
            assert not errors, errors
    finally:
        bridge.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_failed_first_history_offers_retry_in_the_status_bar(focus_browser, live, width, tmp_path):
    """Первая загрузка истории упала (502 моста): слова и «Повторить» в строке статуса, повтор сам через 5 с;
    повтор загружает чат, включает «Отправить» и выбор модели, кнопка уходит. Строка статуса не шире экрана в
    обеих темах."""
    login = f"reload-failed-{focus_browser.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Сбой")})
    aid, _, bridge = online(live, login, machine)
    failed = []
    failing = [True]

    def fail_once(route):
        if route.request.method == "GET" and failing[0]:
            failed.append(route.request.url)
            route.fulfill(status=502, content_type="application/json",
                          body='{"detail": {"code": "bridge", "message": "мост ответил непонятно"}}')
            return
        route.continue_()
    try:
        context, page = open_page(focus_browser, live, login, width)
        with context:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(HISTORY, fail_once)
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            retry = page.locator("#code-reload")
            pw.expect(retry).to_be_visible()
            pw.expect(page.locator("#code-status")).to_contain_text("ответил непонятно")
            pw.expect(page.locator("#code-send")).to_be_disabled()
            for theme in ("light", "dark"):
                page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                assert fits(page)
                page.screenshot(path=str(tmp_path / f"reload-{width}-{theme}.png"))
            deadline = time.monotonic() + 15                 # сбой моста — повтор сам, через RETRY_MS
            while len(failed) < 2 and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert len(failed) >= 2
            pw.expect(retry).to_be_visible()
            failing[0] = False
            retry.tap() if width < 760 else retry.click()
            ready(page)
            pw.expect(retry).to_be_hidden()
            pw.expect(page.locator("#code-status")).to_have_text("")
            assert not errors, errors
    finally:
        bridge.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_returning_computer_resumes_the_chat_at_once(focus_browser, live, width):
    """Компьютер пропал: история не загрузилась — «жду его», live ждёт потоком. Мост вернулся — студия шлёт
    `online`, и страница сразу (раньше повтора через 5 с) загружает чат заново."""
    login = f"reload-online-{focus_browser.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Возврат")})
    machine.open_sessions = KeptSessions()
    aid, key, bridge = online(live, login, machine)
    import bridge as studio_bridge
    try:
        context, page = open_page(focus_browser, live, login, width)
        with context:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
            ready(page)
            bridge.stop()
            wait_for(lambda: aid not in studio_bridge.LINKS)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            pw.expect(page.locator("#code-status")).to_have_text(CODE_WAITING)
            pw.expect(page.locator("#code-activity")).to_have_attribute("data-state", "offline")
            wait_for(lambda: any(chat == CODE_CHAT_A for aid_, chat in studio_bridge.WATCHERS if aid_ == aid))
            before = len(machine.requests)
            started = time.monotonic()
            bridge = SocketBridge(live.url, key, machine).start()
            pw.expect(page.locator("#code-status")).to_have_text("", timeout=3000)
            wait_for(lambda: {"history", "watch"} <= {q["op"] for q in machine.requests[before:]}, 3)
            assert time.monotonic() - started < 4
            pw.expect(page.locator("#code-activity")).not_to_have_attribute("data-state", "offline")
            pw.expect(page.locator("#code-reload")).to_be_hidden()
            ready(page)
            assert not errors, errors
    finally:
        bridge.stop()
