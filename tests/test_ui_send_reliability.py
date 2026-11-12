"""Надёжность отправки в браузере: обрыв связи после доставки хода «Агентов», обрыв итогового PUT прямого
подключения, Enter на сенсорном экране и черновики полей разделов (Playwright: Chromium/Edge, WebKit)."""
import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from browser import find_browser
from fake_bridge import pid
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live  # noqa: F401
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

DONE = {"k": "done", "status": "done"}


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine(request):
    with pw.sync_playwright() as driver:
        if request.param == "chromium":
            executable = find_browser()
            if not executable:
                pytest.skip("нет Chromium/Edge/Chrome")
            browser = driver.chromium.launch(executable_path=executable)
        else:
            if not Path(driver.webkit.executable_path).is_file():
                pytest.skip("нет WebKit")
            browser = driver.webkit.launch()
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def chromium():
    with pw.sync_playwright() as driver:
        executable = find_browser()
        if not executable:
            pytest.skip("нет Chromium/Edge/Chrome")
        browser = driver.chromium.launch(executable_path=executable)
        yield browser
        browser.close()


@contextmanager
def opened(browser, live, login, width, touch=False):
    """Вошедшая страница: cookie сессии заголовком (WebKit не шлёт Secure cookie по http://localhost)."""
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with browser.new_context(viewport={"width": width, "height": 900}, is_mobile=touch, has_touch=touch,
                             service_workers="block", extra_http_headers={"Cookie": cookie}) as context:
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        assert not errors, errors


def until(page, cond, ms=10000):
    """Условие на стороне теста; ожидание отдаёт цикл Playwright — обработчики route идут в нём."""
    for _ in range(ms // 50):
        if cond():
            return
        page.wait_for_timeout(50)
    assert cond()


def code_page(page, live, aid):
    page.goto(live.url + "/#/code")
    page.locator(f"#code-agents li[data-id='{aid}'] .code-agent[aria-current]").wait_for()
    page.locator("#code-projects li[data-pid]").first.wait_for()
    page.evaluate("route => { location.hash = route; }", f"#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
    pw.expect(page.locator("#code-chat")).to_have_attribute("data-loaded", "1")


@pytest.mark.parametrize("width", [320, 1280])
def test_agent_turn_lost_after_delivery_is_not_started_twice(chromium, live, width):
    """Ответ POST хода оборвался, когда мост ход уже принял. История подтвердила — поле пустое, ход один.
    История ещё не знает хода — ввод вернулся в поле с той же квитанцией: повторное «Отправить» отвечает
    «уже принято», второго хода на компьютере нет."""
    login = f"send-lost-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат")})
    aid, bridge = code_online(live, login, machine)
    sent = []
    state = {"cut": 0, "hide": False, "hide_next": False}

    def turn(route):
        body = json.loads(route.request.post_data)
        sent.append(body.get("request_id"))
        if state["cut"]:
            state["cut"] -= 1
            route.fetch()                   # сервер принял ход и ответил — до страницы ответ не дошёл
            state["hide"], state["hide_next"] = state["hide_next"], False
            route.abort()
        else:
            route.continue_()

    def history(route):
        if route.request.method != "GET" or not state["hide"]:
            route.continue_()
            return
        state["hide"] = False               # история ещё не записала задание (CLI пишет её чуть позже)
        reply = route.fetch()
        page_ = reply.json()
        page_["items"] = [m for m in page_["items"] if m["role"] != "user"]
        page_["total"] = len(page_["items"])
        route.fulfill(response=reply, json=page_)

    try:
        with opened(chromium, live, login, width) as page:
            page.route("**/chats/*/turn", turn)
            page.route(f"**/chats/{CODE_CHAT_A}", history)
            code_page(page, live, aid)
            text, status = page.locator("#code-text"), page.locator("#code-status")

            machine.script = [{"k": "text", "text": "Готово"}, DONE]
            state["cut"] = 1
            text.fill("Сделай раз")
            page.locator("#code-send").click()
            until(page, lambda: machine.turns == 1 and not state["cut"])
            pw.expect(page.locator("#code-feed .code-msg.assistant")).to_have_count(1)    # история и live
            pw.expect(page.locator("#code-feed .code-msg.user")).to_have_count(1)
            pw.expect(text).to_have_value("")
            pw.expect(status).not_to_contain_text("Связь прервалась")

            machine.script = [{"k": "text", "text": "Готово"}, DONE]
            state.update(cut=1, hide_next=True)
            text.fill("Сделай два")
            page.locator("#code-send").click()
            pw.expect(status).to_contain_text("повторная отправка не запустит его второй раз")
            pw.expect(text).to_have_value("Сделай два")
            assert machine.turns == 2
            page.wait_for_function("() => document.querySelector('#code-chat').dataset.running === '0'")
            page.locator("#code-send").click()
            pw.expect(status).to_contain_text("Это задание уже принято")
            pw.expect(text).to_have_value("")
            page.wait_for_timeout(300)
            assert machine.turns == 2
            assert len(sent) == 3 and sent[1] == sent[2] and sent[0] != sent[1] and all(sent)
            pw.expect(page.locator("#code-feed .code-msg.user")).to_have_count(2)
    finally:
        bridge.stop()
