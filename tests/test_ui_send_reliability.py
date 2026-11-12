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


@pytest.fixture(scope="module")
def driver():
    """Один Playwright на модуль: второй sync_playwright при живом первом — отказ «Sync API inside the asyncio loop»."""
    with pw.sync_playwright() as playwright:
        yield playwright


def launch(driver, name):
    if name == "chromium":
        executable = find_browser()
        if not executable:
            pytest.skip("нет Chromium/Edge/Chrome")
        return driver.chromium.launch(executable_path=executable)
    if not Path(driver.webkit.executable_path).is_file():
        pytest.skip("нет WebKit")
    return driver.webkit.launch()


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine(driver, request):
    browser = launch(driver, request.param)
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def chromium(driver):
    browser = launch(driver, "chromium")
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


def add_cloud(page, live):
    """Облако в «Чате» с отмеченной cloud-chat — прямое подключение: модель зовёт сам браузер."""
    page.goto(live.url + "/#/settings")
    page.locator(".settings-nav").wait_for()
    page.evaluate("""async ([url, token]) => { const m = await import('/static/js/api.js');
      const c = await m.api('POST', '/api/connections', { name: 'Облако', kind: 'openai', base_url: url, token,
        sections: ['chat'] });
      await m.api('PUT', '/api/sections/chat/links/' + c.id, { models: ['cloud-chat'] }); }""",
                  [live.cloud.base_url, live.cloud.TOKEN])
    page.goto(live.url + "/#/chat")
    page.reload()
    page.locator("#model-chip").click()
    page.locator('.model-option[data-model="cloud-chat"]').click()
    pw.expect(page.locator("#model-chip")).to_contain_text("cloud-chat")


LAST = "#messages .msg.assistant:last-child"


@pytest.mark.parametrize("width", [320, 1280])
def test_direct_answer_survives_a_lost_put_and_can_be_saved_again(chromium, live, width):
    """Прямое подключение: ответ на итоговый PUT потерялся — страница повторяет его (сервер отвечает тому же
    итогу 200), ответ сохранён. PUT отказал — «Ответ не сохранился» и «Сохранить ещё раз»: текст держит память
    вкладки и после ухода из диалога, кнопка сохраняет его."""
    login = f"put-lost-{width}"
    live.add_user(login)
    puts = []
    mode = {"put": "lose"}

    def put(route):
        if route.request.method != "PUT":
            route.continue_()
            return
        puts.append(mode["put"])
        if mode["put"] == "lose":
            mode["put"] = None
            route.fetch()
            route.abort()
        elif mode["put"] == "fail":
            route.fulfill(status=500, json={"detail": {"code": "http", "message": "сбой"}})
        else:
            route.continue_()

    last_saved = """async () => { const m = await import('/static/js/api.js');
      const last = (await m.api('GET', '/api/chats/' + location.hash.split('/')[2])).messages.at(-1);
      return [last.status, last.content]; }"""
    with opened(chromium, live, login, width) as page:
        add_cloud(page, live)
        page.route("**/api/chats/*/messages/*", put)
        answer = page.locator(LAST)
        page.locator("#chat-input").fill("Ты здесь?")
        page.locator("#send").click()
        until(page, lambda: len(puts) == 2)
        pw.expect(answer.locator(".md")).to_have_text("Облако на связи.")
        pw.expect(answer.locator(".msg-meta")).to_have_text("")
        assert page.evaluate(last_saved) == ["done", "Облако на связи."]

        mode["put"] = "fail"
        page.locator("#chat-input").fill("Ещё раз")
        page.locator("#send").click()
        pw.expect(answer.locator(".msg-meta")).to_contain_text("Ответ не сохранился")
        assert page.evaluate(last_saved) == ["streaming", ""]
        dialog = page.evaluate("location.hash")
        page.evaluate("() => { location.hash = '#/chat'; }")
        page.locator("#messages .msg").first.wait_for(state="detached")
        page.evaluate("hash => { location.hash = hash; }", dialog)
        pw.expect(answer.locator(".md")).to_have_text("Облако на связи.")
        mode["put"] = None
        answer.locator(".msg-save").click()
        pw.expect(answer.locator(".msg-save")).to_have_count(0)
        pw.expect(answer.locator(".msg-meta")).to_have_text("")
        assert page.evaluate(last_saved) == ["done", "Облако на связи."]


@pytest.mark.parametrize("touch,width", [(True, 320), (False, 1280)])
def test_enter_breaks_the_line_on_touch_screens_and_sends_on_desktop(engine, live, touch, width):
    """Экранной клавиатуре нечем набрать Shift+Enter: на сенсорном экране Enter в «Чате» и «Агентах» — перенос
    строки (`enterkeyhint="enter"`), отправка — кнопкой; на компьютере Enter отправляет (`enterkeyhint="send"`)."""
    login = f"enter-{engine.browser_type.name}-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат")})
    aid, bridge = code_online(live, login, machine)
    try:
        with opened(engine, live, login, width, touch=touch) as page:
            add_cloud(page, live)
            field = page.locator("#chat-input")
            pw.expect(field).to_have_attribute("enterkeyhint", "enter" if touch else "send")
            field.click()
            page.keyboard.type("первая")
            page.keyboard.press("Enter")
            if touch:
                page.keyboard.type("вторая")
                pw.expect(field).to_have_value("первая\nвторая")
                pw.expect(page.locator("#messages .msg")).to_have_count(0)
                page.locator("#send").click()
                pw.expect(page.locator("#messages .msg.user")).to_have_text("первая\nвторая")
            else:
                pw.expect(page.locator("#messages .msg.user")).to_have_text("первая")
                pw.expect(field).to_have_value("")

            machine.script = [DONE]
            code_page(page, live, aid)
            text = page.locator("#code-text")
            pw.expect(text).to_have_attribute("enterkeyhint", "enter" if touch else "send")
            text.click()
            page.keyboard.type("задание")
            page.keyboard.press("Enter")
            if touch:
                page.keyboard.type("дальше")
                pw.expect(text).to_have_value("задание\nдальше")
                page.wait_for_timeout(300)
                assert machine.turns == 0
            else:
                until(page, lambda: machine.turns == 1)
                pw.expect(text).to_have_value("")
    finally:
        bridge.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_section_drafts_survive_switching_and_go_on_send_and_lock(chromium, live, width):
    """Набранное в «Чате» и «Переводе» возвращается после перехода в другой раздел (память вкладки); отправленное —
    нет; блокировка экрана стирает черновики разделов, которых нет на экране."""
    login = f"drafts-{width}"
    live.add_user(login)
    chat, translate = "#chat-input", "#tl-input"

    def visit(page, section, selector):
        page.evaluate("s => { location.hash = '#/' + s; }", section)
        page.locator(selector).wait_for()

    with opened(chromium, live, login, width) as page:
        add_cloud(page, live)
        page.locator(chat).fill("Длинное сообщение")
        visit(page, "translate", translate)
        page.locator(translate).fill("Текст на потом")
        visit(page, "chat", chat)
        pw.expect(page.locator(chat)).to_have_value("Длинное сообщение")
        visit(page, "translate", translate)
        pw.expect(page.locator(translate)).to_have_value("Текст на потом")

        visit(page, "chat", chat)
        page.locator("#send").click()
        pw.expect(page.locator("#messages .msg.assistant .md")).to_have_text("Облако на связи.")
        visit(page, "translate", translate)
        visit(page, "chat", chat)
        pw.expect(page.locator(chat)).to_have_value("")

        page.locator(chat).fill("Секрет до блокировки")
        visit(page, "translate", translate)
        page.evaluate("""async () => { const m = await import('/static/js/api.js');
          await m.api('POST', '/api/me/screen-lock', { enabled: true, timeout: 600 });
          await m.api('POST', '/api/lock/now');
          m.api('GET', '/api/me'); }""")                 # 423 — экран блокировки, запрос ждёт отпирания
        page.locator("#lock[open]").wait_for()
        if page.locator("#lock-use-password").is_visible():
            page.locator("#lock-use-password").click()
        page.locator("#lock-password").fill("password-123")
        page.locator("#lock-password-go").click()
        page.locator("#lock[open]").wait_for(state="detached")
        visit(page, "chat", chat)
        pw.expect(page.locator(chat)).to_have_value("")
