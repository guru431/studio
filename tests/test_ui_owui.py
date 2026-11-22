"""«Open WebUI» в браузере: панель учёток и дерево чатов, лента чата, поток, отправка и пауза. Студия — живая
(`live_app.py`), за ней поддельный Open WebUI (`fake_owui.py`: REST — через шлюз теста, сокет — в памяти).
Chromium (Edge/Chrome) и WebKit, 320 и 1280 px."""
import asyncio
import base64
import json
import re
import ssl
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

import owui
import owui_link
import state
import upstream
from browser import CHROMIUM_ARGS, find_browser, run_playwright
from fake_owui import FakeOwui
from test_owui_accounts import fingerprint, self_signed
from test_ui_browser import live, live_sql
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PASSWORD = "password-123"
HTTPS_ENTRY = "10.77.1.20:8443"
HTTPS_URL = f"https://{HTTPS_ENTRY}"
#: отказы, которые сценарий вызывает сам: шаг сертификата (409) и неверный пароль (422)
EXPECTED = ("409", "422")


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def owui_browser(request):
    with run_playwright(pw) as driver:
        if request.param == "chromium":
            executable = find_browser()
            if not executable:
                pytest.skip("нет Chromium/Edge/Chrome")
            browser = driver.chromium.launch(executable_path=executable, args=CHROMIUM_ARGS)
        else:
            if not Path(driver.webkit.executable_path).is_file():
                pytest.skip("нет WebKit")
            browser = driver.webkit.launch()
        yield browser
        browser.close()


def allow(live, entry):
    """Адрес в списке разрешённых живой студии (своим соединением с базой, как `live_sql`)."""
    import db
    conn = db.connect(live.db_path)
    try:
        listed = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'allowlist'").fetchone()["value"])
        if entry not in listed:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", (json.dumps([*listed, entry]),))
    finally:
        conn.close()


@pytest.fixture
def fake(live, monkeypatch):
    """Поддельный Open WebUI за живой студией: REST — на `FakeOwui.ENTRY` у шлюза, сокет — `owui_link.CONNECT`.
    Сокет живёт в цикле событий службы: тест шлёт события через `fake.later(fn, *args)` (call_soon_threadsafe)."""
    found = FakeOwui()
    loops = []

    async def connect(target, headers):
        loops.append(asyncio.get_running_loop())
        return await found.socket_connect(target, headers)

    found.later = lambda fn, *args: loops[-1].call_soon_threadsafe(fn, *args)
    live.gw.hosts[FakeOwui.ENTRY] = found.handler
    allow(live, FakeOwui.ENTRY)
    monkeypatch.setattr(owui_link, "CONNECT", connect)
    return found


def session(live, login):
    live.add_user(login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": PASSWORD})
    assert signed.status_code == 303
    return "; ".join(f"{key}={value}" for key, value in signed.cookies.items())


def call(live, cookie, method, path, **kw):
    headers = {"Cookie": cookie}
    if method != "GET":
        headers["x-studio-csrf"] = httpx.get(live.url + "/api/me", headers=headers).json()["csrf"]
    return httpx.request(method, live.url + path, headers=headers, timeout=15, **kw)


def connected(live, cookie, label="s06"):
    r = call(live, cookie, "POST", "/api/owui/accounts", json={"label": label, "url": FakeOwui.URL,
                                                                "email": FakeOwui.EMAIL, "password": FakeOwui.PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@contextmanager
def opened(browser, live, cookie, width, path, **options):
    """`options` — параметры контекста (`service_workers="block"` — для `page.route` в WebKit)."""
    mobile = width < 760
    with browser.new_context(viewport={"width": width, "height": 800}, is_mobile=mobile, has_touch=mobile,
                             extra_http_headers={"Cookie": cookie}, **options) as context:
        page = context.new_page()
        page.set_default_timeout(10000)
        page.problems = []
        page.events = []
        page.on("pageerror", lambda error: page.problems.append(str(error)))
        page.on("console", lambda message: page.problems.append(message.text) if message.type == "error"
                and not ("Failed to load resource" in message.text and any(c in message.text for c in EXPECTED))
                else None)
        page.on("request", lambda request: page.events.append(request.url) if request.url.endswith("/events") else None)
        page.asked = []                                                  # GET страницы к API Open WebUI (пути)
        page.on("request", lambda request: page.asked.append(request.url.split("?")[0].replace(live.url, ""))
                if request.method == "GET" and "/api/owui/" in request.url else None)
        page.goto(live.url + "/#/" + path)
        page.locator("html[data-ready='1']").wait_for(state="attached")
        yield page


def show_panel(page):
    """Телефон: панель выезжает по ☰ (на компьютере она видна всегда)."""
    if page.viewport_size["width"] < 760 and not page.locator("#owui-panel").is_visible():
        page.locator("#owui-panel-toggle").click()
        pw.expect(page.locator("#owui-panel")).to_be_visible()


def until(page, check, seconds=5):
    """Условие на стороне теста (запросы страницы) — ждать, отдавая цикл событий драйверу."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "не дождались"
        page.wait_for_timeout(50)


def fits(page):
    return page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def no_password_manager(box):
    """Пароль Open WebUI — не для менеджера паролей браузера (как PIN компьютера, ui.js computerPin): поле не password,
    вне <form>, без autocomplete входа; email рядом — тоже без него. Иначе браузер сохранил бы пароль под адресом
    Студии и предложил бы пароль Студии для чужого сервера."""
    password = box.locator("[name=password]")
    pw.expect(password).to_have_count(1)
    assert box.evaluate("""box => {
      const p = box.querySelector('[name=password]');
      const login = [...box.querySelectorAll('input')].filter((i) => /username|password/.test(i.autocomplete));
      return p.type === 'text' && p.autocomplete === 'off' && p.classList.contains('computer-pin') && !p.closest('form')
        && !box.querySelector('input[type=password]') && login.length === 0;
    }""")


@pytest.mark.parametrize("width", [320, 1280])
def test_connect_account_and_see_tree(owui_browser, live, fake, width):
    work = fake.add_folder("Work")
    fake.add_chat("In folder", folder_id=work, updated_at=500)
    fake.add_chat("Pinned talk", pinned=True, updated_at=900)
    for n in range(65):
        fake.add_chat(f"Root {n:02}", updated_at=10_000 - n)
    fake.add_chat("Busy", updated_at=20_000, active=True)
    fake.add_chat("Waiting", updated_at=19_000, pending_approval=True)
    cookie = session(live, f"owui-tree-{owui_browser.browser_type.name}-{width}")
    with opened(owui_browser, live, cookie, width, "owui") as page:
        pw.expect(page.locator("#owui-empty")).to_contain_text("Пароль Studio не сохраняет")
        page.locator("#owui-connect-first").click()
        dialog = page.locator("#owui-connect-dialog")
        no_password_manager(dialog)
        dialog.locator("[name=label]").fill("s06 · рабочая")
        dialog.locator("[name=url]").fill(FakeOwui.URL)
        dialog.locator("[name=email]").fill(FakeOwui.EMAIL)
        dialog.locator("[name=password]").fill(FakeOwui.PASSWORD)
        dialog.locator("[name=password]").press("Enter")                     # Enter в поле — как «Подключить»
        pw.expect(dialog).to_have_count(0)
        (account,) = call(live, cookie, "GET", "/api/owui/accounts").json()
        aid = account["id"]
        pw.expect(page).to_have_url(re.compile(rf"#/owui/{aid}$"))
        pw.expect(page.locator("#owui-panel")).to_be_visible()              # телефон: чата нет — панель выехала сама
        row = page.locator(".owui-account-row")
        pw.expect(row).to_have_count(1)
        pw.expect(row.locator(".owui-account-label")).to_have_text("s06 · рабочая")
        pw.expect(row.locator(".owui-account-host")).to_have_text(FakeOwui.ENTRY)
        pw.expect(row.locator(".owui-state")).to_have_class(re.compile(r"\bon\b"))
        assert "Пароль" not in json.dumps(account) and FakeOwui.PASSWORD not in json.dumps(account)

        pw.expect(page.locator("#owui-pinned .owui-chat")).to_have_text(["Pinned talk"])
        pw.expect(page.locator("#owui-folders .owui-folder")).to_have_text(["Work"])
        chats = page.locator("#owui-chat-list .owui-chat")
        pw.expect(chats).to_have_count(60)
        busy, waiting = chats.filter(has_text="Busy"), chats.filter(has_text="Waiting")
        pw.expect(busy.locator(".owui-chat-active")).to_have_count(1)
        pw.expect(waiting.locator(".owui-chat-pending")).to_have_text("ждёт решения")
        page.locator("#owui-tree").evaluate("tree => { tree.scrollTop = tree.scrollHeight; }")
        pw.expect(chats).to_have_count(67)                                   # вторая страница — прокруткой
        pw.expect(page.locator("#owui-chats-more")).to_be_hidden()

        page.locator("#owui-folders .owui-folder").click()
        pw.expect(page.locator("#owui-folders .owui-folder-chats .owui-chat")).to_have_text(["In folder"])

        page.locator("#owui-search").fill("Root 6")
        pw.expect(page.locator("#owui-results .owui-chat")).to_have_count(5)
        pw.expect(page.locator("#owui-chats")).to_be_hidden()
        search = [r for r in fake.requests if r.url.path == "/api/v1/chats/search"]
        assert search and search[-1].url.params["text"] == "Root 6"
        page.locator("#owui-search").fill("")
        pw.expect(page.locator("#owui-results")).to_be_hidden()
        pw.expect(page.locator("#owui-chats")).to_be_visible()

        target = chats.filter(has_text="Root 03")
        chat_id = target.get_attribute("data-chat")
        page.locator("#owui-tree").evaluate("tree => { tree.scrollTop = 0; }")
        target.click()
        pw.expect(page).to_have_url(re.compile(rf"#/owui/{aid}/{chat_id}$"))
        pw.expect(target).to_have_attribute("aria-current", "page")
        if width < 760:
            pw.expect(page.locator("#owui-panel")).to_be_hidden()            # чат выбран — панель ушла
        assert fits(page)
        assert len(page.events) == 1, page.events                             # одна подписка SSE на учётку
        place = json.loads(page.evaluate("localStorage.getItem('studio-owui-place')"))
        assert (place["aid"], place["chat"]) == (aid, chat_id)

        page.evaluate("location.hash = '#/chat'")
        page.locator("#rail .tab[data-section=chat][aria-current=page]").wait_for(state="attached")
        page.evaluate("location.hash = '#/owui'")
        pw.expect(page).to_have_url(re.compile(rf"#/owui/{aid}/{chat_id}$"))  # последнее место

        page.evaluate("location.hash = '#/search'")                          # «Поиск» чаты Open WebUI не охватывает
        pw.expect(page.locator(".search-chips .chip[data-section=chat]")).to_have_count(1)
        pw.expect(page.locator(".search-chips .chip[data-section=owui]")).to_have_count(0)
        pw.expect(page.locator(".search-chips .chip[data-section=code]")).to_have_count(0)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_certificate_step_shows_fingerprint(owui_browser, live, fake, monkeypatch, width):
    der = self_signed()

    async def untrusted(request):
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")
        except ssl.SSLCertVerificationError as e:
            raise httpx.ConnectError("certificate verify failed", request=request) from e

    async def fetch(conn, url):
        assert url == HTTPS_URL
        return der

    live.gw.hosts[HTTPS_ENTRY] = untrusted
    allow(live, HTTPS_ENTRY)
    monkeypatch.setattr(upstream, "fetch_certificate", fetch)
    monkeypatch.setattr(state, "pinned_http", lambda pem: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    cookie = session(live, f"owui-cert-{owui_browser.browser_type.name}-{width}")
    with opened(owui_browser, live, cookie, width, "owui") as page:
        page.locator("#owui-connect-first").click()
        dialog = page.locator("#owui-connect-dialog")
        dialog.locator("[name=label]").fill("Сервер с IP")
        dialog.locator("[name=url]").fill(HTTPS_URL)
        dialog.locator("[name=email]").fill(FakeOwui.EMAIL)
        dialog.locator("[name=password]").fill(FakeOwui.PASSWORD)
        page.locator("#owui-connect-submit").click()
        pw.expect(page.locator("#owui-cert")).to_be_visible()
        pw.expect(page.locator("#owui-cert-sha256")).to_have_text(fingerprint(der))
        pw.expect(page.locator("#owui-cert")).to_contain_text("owui.example.com")
        assert call(live, cookie, "GET", "/api/owui/accounts").json() == []
        assert page.evaluate("document.querySelector('#owui-connect-dialog').scrollWidth"
                             " <= document.querySelector('#owui-connect-dialog').clientWidth")
        page.locator("#owui-cert-trust").click()
        pw.expect(dialog).to_have_count(0)
        pw.expect(page.locator(".owui-account-row .owui-account-host")).to_have_text(HTTPS_ENTRY)
        (account,) = call(live, cookie, "GET", "/api/owui/accounts").json()
        assert account["cert"] == fingerprint(der) and account["url"] == HTTPS_URL
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_relogin_state_offers_password(owui_browser, live, fake, width):
    cookie = session(live, f"owui-relogin-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie, label="Работа")
    fake.add_chat("After relogin")
    live_sql(live, "UPDATE owui_accounts SET state = 'relogin' WHERE id = ?", (aid,))
    with opened(owui_browser, live, cookie, width, f"owui/{aid}") as page:
        show_panel(page)
        row = page.locator(".owui-account-row")
        pw.expect(row.locator(".owui-state")).to_have_class(re.compile(r"\brelogin\b"))
        pw.expect(row.locator(".owui-state .sr")).to_have_text("войдите заново")
        pw.expect(page.locator("#owui-relogin")).to_be_visible()
        pw.expect(page.locator("#owui-search")).to_be_hidden()
        pw.expect(page.locator("#owui-relogin-cancel")).to_have_count(0)
        assert page.events == []                                             # учётке без входа поток не нужен
        no_password_manager(page.locator("#owui-relogin"))
        page.locator("#owui-relogin-password").fill("wrong")
        page.locator("#owui-relogin-submit").click()
        pw.expect(page.locator("#owui-relogin .error[data-for=password]")).to_have_text(
            "Неверный email или пароль Open WebUI")
        page.locator("#owui-relogin-password").fill(FakeOwui.PASSWORD)
        page.locator("#owui-relogin-password").press("Enter")
        pw.expect(page.locator("#owui-relogin")).to_have_count(0)
        pw.expect(row.locator(".owui-state")).to_have_class(re.compile(r"\bon\b"))
        pw.expect(page.locator("#owui-chat-list .owui-chat")).to_have_text(["After relogin"])
        assert call(live, cookie, "GET", "/api/owui/accounts").json()[0]["state"] == "ok"
        until(page, lambda: len(page.events) == 1)                          # вошли — поток учётки открылся

        # «⋯»: переименовать, переподключить (с «Отменой»), удалить с подтверждением
        page.locator(".owui-account-more").click()
        page.locator(".owui-rename").click()
        page.locator("#owui-rename-dialog [name=label]").fill("Работа · s06")
        page.locator("#owui-rename-save").click()
        pw.expect(row.locator(".owui-account-label")).to_have_text("Работа · s06")
        page.locator(".owui-account-more").click()
        page.locator(".owui-relogin-item").click()
        pw.expect(page.locator("#owui-relogin")).to_be_visible()
        page.locator("#owui-relogin-cancel").click()
        pw.expect(page.locator("#owui-chat-list .owui-chat")).to_have_text(["After relogin"])
        page.locator(".owui-account-more").click()
        page.locator(".owui-delete").click()
        pw.expect(page.locator("#owui-delete-dialog")).to_contain_text("«Работа · s06»")
        page.locator("#owui-delete-confirm").click()
        pw.expect(page.locator("#owui-connect-first")).to_be_visible()
        assert call(live, cookie, "GET", "/api/owui/accounts").json() == []
        assert fake.revoked                                                  # сессия Open WebUI отозвана
        pw.expect(page).to_have_url(re.compile(r"#/owui$"))
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_events_update_the_tree(owui_browser, live, fake, width):
    """События сокета Open WebUI через SSE студии: идёт ответ, пауза, новое название, конец, новый чат; отзыв сессии
    в самом Open WebUI — `link relogin` и поле пароля, после входа — новый поток."""
    cookie = session(live, f"owui-events-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    calm = fake.add_chat("Calm", updated_at=300)
    fake.add_chat("Other", updated_at=200)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}") as page:
        show_panel(page)
        row = page.locator(f".owui-chat[data-chat='{calm}']")
        pw.expect(row).to_be_visible()
        until(page, lambda: fake.socket is not None and fake.socket.user is not None)
        fake.later(fake.emit_event, calm, None, {"type": "chat:active", "data": {"active": True}})
        pw.expect(row.locator(".owui-chat-active")).to_have_count(1)
        # пауза 0.13.4: снимок с вызовом в паузе, затем `active: false` — метка «ждёт решения» остаётся
        fake.later(fake.emit_event, calm, "m1", {"type": "chat:completion", "data": {"output": [
            {"type": "function_call", "call_id": "c1", "name": "delete_file", "status": "pending"}]}})
        pw.expect(row.locator(".owui-chat-pending")).to_have_text("ждёт решения")
        pw.expect(row.locator(".owui-chat-active")).to_have_count(0)
        fake.later(fake.emit_event, calm, None, {"type": "chat:active", "data": {"active": False}})
        fake.later(fake.emit_event, calm, None, {"type": "chat:title", "data": {"title": "Renamed by model"}})
        pw.expect(row.locator(".owui-chat-title")).to_have_text("Renamed by model")
        pw.expect(row.locator(".owui-chat-pending")).to_have_text("ждёт решения")
        # форма пробы (`pending_approval` в `chat:active`) — тоже пауза
        fake.later(fake.emit_event, calm, "m1", {"type": "chat:active", "data": {"active": True, "pending_approval": True}})
        pw.expect(row.locator(".owui-chat-pending")).to_have_text("ждёт решения")
        # решение принято: ответ продолжился, затем кончился
        fake.later(fake.emit_event, calm, None, {"type": "chat:active", "data": {"active": True}})
        pw.expect(row.locator(".owui-chat-active")).to_have_count(1)
        pw.expect(row.locator(".owui-chat-pending")).to_have_count(0)
        fake.later(fake.emit_event, calm, None, {"type": "chat:active", "data": {"active": False}})
        pw.expect(row.locator(".owui-chat-pending, .owui-chat-active")).to_have_count(0)
        fresh = fake.add_chat("Fresh", updated_at=10_000)
        fake.later(fake.emit_event, None, None, {"type": "chat:list", "data": {"chat_id": fresh}})
        pw.expect(page.locator(f".owui-chat[data-chat='{fresh}']")).to_be_visible()

        fake.revoke_all()                                                    # сессию «Studio» отозвали в Open WebUI
        fake.expire_tokens()
        fake.later(fake.drop)
        pw.expect(page.locator("#owui-relogin")).to_be_visible()
        pw.expect(page.locator(".owui-state")).to_have_class(re.compile(r"\brelogin\b"))
        page.locator("#owui-relogin-password").fill(FakeOwui.PASSWORD)
        page.locator("#owui-relogin-submit").click()
        pw.expect(page.locator(f".owui-chat[data-chat='{fresh}']")).to_be_visible()
        until(page, lambda: len(page.events) == 2)                          # после входа — новый поток
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_phone_panel_slides_and_hides(owui_browser, live, fake, width):
    cookie = session(live, f"owui-phone-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    one = fake.add_chat("One", updated_at=200)
    two = fake.add_chat("Two", updated_at=100)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{one}") as page:
        panel, toggle = page.locator("#owui-panel"), page.locator("#owui-panel-toggle")
        two_link = page.locator(f".owui-chat[data-chat='{two}']")
        if width >= 760:                                                     # компьютер: панель всегда на месте
            pw.expect(toggle).to_be_hidden()
            pw.expect(panel).to_be_visible()
            two_link.click()
            pw.expect(page).to_have_url(re.compile(rf"#/owui/{aid}/{two}$"))
            pw.expect(panel).to_be_visible()
            assert fits(page) and not page.problems, page.problems
            return
        pw.expect(panel).to_be_hidden()                                      # чат открыт — панель спрятана
        toggle.click()
        pw.expect(panel).to_be_visible()
        pw.expect(toggle).to_have_attribute("aria-expanded", "true")
        page.locator(".owui-backdrop").click(position={"x": width - 4, "y": 300})
        pw.expect(panel).to_be_hidden()
        toggle.click()
        pw.expect(panel).to_be_visible()
        page.keyboard.press("Escape")
        pw.expect(panel).to_be_hidden()
        toggle.click()
        two_link.click()
        pw.expect(page).to_have_url(re.compile(rf"#/owui/{aid}/{two}$"))
        pw.expect(panel).to_be_hidden()
        pw.expect(toggle).to_have_attribute("aria-expanded", "false")
        page.evaluate(f"location.hash = '#/owui/{aid}'")                    # без чата — панель выезжает сама
        pw.expect(panel).to_be_visible()
        assert fits(page)
        assert not page.problems, page.problems


# ---- лента чата, поток, отправка, пауза -------------------------------------------------------------------------------

#: PNG 1×1
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def hide_panel(page):
    """Телефон: без открытого чата панель выезжает сама — убрать её, чтобы писать в поле."""
    if page.viewport_size["width"] < 760:
        pw.expect(page.locator("#owui-panel")).to_be_visible()
        page.keyboard.press("Escape")
        pw.expect(page.locator("#owui-panel")).to_be_hidden()


def socket_ready(page, fake):
    until(page, lambda: fake.socket is not None and fake.socket.user is not None)


def emit(fake, chat, message, kind, data):
    fake.later(fake.emit_event, chat, message, {"type": kind, "data": data})


def delta(fake, chat, message, text, kind="output_text"):
    emit(fake, chat, message, "response:completion", {"type": f"response.{kind}.delta", "delta": text})


def finished(fake, chat, message):
    emit(fake, chat, message, "chat:completion", {"done": True})


def talk(answer: dict, question="Hi") -> list[dict]:
    """Ветка: вопрос пользователя `u1` и ответ `answer` (id и поля сохранённого сообщения Open WebUI)."""
    return [{"id": "u1", "parentId": None, "role": "user", "content": question, "timestamp": 1},
            {"parentId": "u1", "role": "assistant", "model": "m1", "timestamp": 2, **answer}]


def loaded_image(page, selector):
    page.wait_for_function("s => [...document.querySelectorAll(s)].some(img => img.complete && img.naturalWidth > 0)",
                           arg=selector)


@pytest.mark.parametrize("width", [320, 1280])
def test_open_chat_renders_text_reasoning_and_tools(owui_browser, live, fake, width):
    fake.add_model("m1", "Model One")
    fake.add_file("img-1", PNG, "image/png")
    chat = fake.add_chat("Rendered", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "", "output": [
        {"type": "reasoning", "content": [{"type": "output_text", "text": "Thinking about boats"}], "duration": 3},
        {"type": "function_call", "call_id": "c1", "name": "web_search", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "Found: a red boat"}],
         "files": [{"type": "image", "id": "img-1", "name": "boat.png", "content_type": "image/png"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "A **red** boat\n\n- one\n- two"}]},
        {"type": "mystery"}]}, question="Find the boat"))
    cookie = session(live, f"owui-feed-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        pw.expect(page.locator(".owui-feed-title")).to_have_text("Rendered")
        pw.expect(page.locator(".owui-msg.user .bubble")).to_have_text("Find the boat")
        answer = page.locator(".owui-msg.assistant[data-id='a1']")
        pw.expect(answer.locator(".md strong")).to_have_text("red")
        pw.expect(answer.locator(".md li")).to_have_count(2)
        think = answer.locator("details.owui-think")
        pw.expect(think.locator("summary")).to_have_text("💭 Рассуждала 3 с")
        pw.expect(think.locator(".think-text")).to_be_hidden()                # рассуждение свёрнуто
        think.locator("summary").click()
        pw.expect(think.locator(".think-text")).to_have_text("Thinking about boats")
        tool = answer.locator(".owui-tool")
        pw.expect(tool.locator(".owui-tool-name")).to_have_text("web_search")
        pw.expect(tool.locator(".owui-tool-result")).to_be_hidden()
        tool.locator("summary").click()
        pw.expect(tool.locator(".owui-tool-result")).to_have_text("Found: a red boat")
        pw.expect(answer.locator(".owui-other")).to_have_text("элемент mystery")
        image = answer.locator(".msg-files img")
        pw.expect(image).to_have_attribute("src", f"/api/owui/accounts/{aid}/files/img-1")
        loaded_image(page, ".owui-msg.assistant .msg-files img")              # картинка — через сервер Студии
        pw.expect(page.locator("#owui-model-pick")).to_have_attribute("title", re.compile("Model One"))
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_new_chat_streams_answer_and_appears_in_tree(owui_browser, live, fake, width):
    fake.add_model("m1", "Model One")
    cookie = session(live, f"owui-new-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}") as page:
        hide_panel(page)
        pw.expect(page.locator(".owui-feed-title")).to_have_text("Новый чат")
        pw.expect(page.locator("#owui-model-pick")).to_have_attribute("title", re.compile("Model One"))
        socket_ready(page, fake)
        page.locator("#owui-input").fill("Hello model")
        page.locator("#owui-send").click()
        until(page, lambda: fake.completions)
        body = fake.completions[-1]
        (chat,) = fake.chats
        message = body["id"]
        assert body["model"] == "m1" and "chat_id" not in body and body["user_message"]["content"] == "Hello model"
        pw.expect(page).to_have_url(re.compile(rf"#/owui/{aid}/{chat}$"))
        pw.expect(page.locator(".owui-msg.user .bubble")).to_have_text("Hello model")
        pw.expect(page.locator("#owui-input")).to_have_value("")
        answer = page.locator(f".owui-msg.assistant[data-id='{message}']")
        pw.expect(answer.locator(".msg-meta")).to_have_text("ответ идёт…")
        pw.expect(page.locator("#owui-stop")).to_be_visible()
        delta(fake, chat, message, "Pondering", kind="reasoning_text")
        delta(fake, chat, message, "Hello")
        delta(fake, chat, message, " world")
        pw.expect(answer.locator(".owui-stream")).to_have_text("Hello world")
        pw.expect(answer.locator("details.owui-think .think-text")).to_have_text("Pondering", use_inner_text=False)
        emit(fake, chat, message, "response:completion", {"type": "response.output_item.added", "item": {
            "type": "function_call", "call_id": "c9", "name": "calculator", "status": "in_progress"}})
        pw.expect(answer.locator(".owui-tool .owui-tool-name")).to_have_text("calculator")

        fake.chats[chat]["title"] = "Greeting"
        emit(fake, None, None, "chat:list", {"chat_id": chat})
        emit(fake, chat, None, "chat:title", "Greeting")
        row = page.locator(f"#owui-chat-list .owui-chat[data-chat='{chat}']")
        pw.expect(row).to_contain_text("Greeting")
        pw.expect(row).to_have_attribute("aria-current", "page")
        pw.expect(page.locator(".owui-feed-title")).to_have_text("Greeting")

        fake.finish(chat, message, "Hello **world**")
        finished(fake, chat, message)
        pw.expect(answer.locator(".md strong")).to_have_text("world")
        pw.expect(page.locator("#owui-stop")).to_be_hidden()
        pw.expect(answer.locator(".msg-meta")).to_have_count(0)
        assert len(page.events) == 1, page.events                             # одна подписка: учётка панели и ленты
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_continue_with_other_model(owui_browser, live, fake, width):
    fake.add_model("m1", "Model One")
    fake.add_model("m2", "Model Two")
    chat = fake.add_chat("Talk", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "First answer"}))
    cookie = session(live, f"owui-more-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        pw.expect(page.locator(".owui-msg.assistant[data-id='a1'] .md")).to_have_text("First answer")
        pick = page.locator("#owui-model-pick")
        pw.expect(pick).to_have_attribute("title", re.compile("Model One"))       # модель чата
        pick.click()
        page.locator("#owui-model-menu .pick-option", has_text="Model Two").click()
        pw.expect(pick).to_have_attribute("title", re.compile("Model Two"))
        page.locator("#owui-input").fill("And now?")
        if width >= 760:
            page.locator("#owui-input").press("Enter")
        else:
            page.locator("#owui-send").click()
        until(page, lambda: fake.completions)
        body = fake.completions[-1]
        assert (body["model"], body["chat_id"], body["parent_id"]) == ("m2", chat, "a1")
        assert body["user_message"]["content"] == "And now?"
        pw.expect(page.locator(".owui-msg.user .bubble")).to_have_text(["Hi", "And now?"])
        pw.expect(page.locator(f".owui-msg.assistant[data-id='{body['id']}'] .msg-meta")).to_have_text("ответ идёт…")
        pw.expect(pick).to_have_attribute("title", re.compile("Model Two"))       # перечитывание выбор не сбросило
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_attach_image(owui_browser, live, fake, width):
    fake.add_model("m1", "Model One")
    chat = fake.add_chat("Pictures", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "Send it"}))
    cookie = session(live, f"owui-attach-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        pw.expect(page.locator(".owui-msg.assistant .md")).to_have_text("Send it")
        page.set_input_files("#owui-attach", files=[{"name": "boat.png", "mimeType": "image/png", "buffer": PNG}])
        chip = page.locator(".owui-att")
        pw.expect(chip).to_have_count(1)
        pw.expect(chip).to_have_attribute("data-state", "ready")
        pw.expect(chip.locator(".att-name")).to_have_text("boat.png")
        fake.processing = ["failed"]                                         # Open WebUI не обработал документ
        page.set_input_files("#owui-attach", files=[{"name": "notes.pdf", "mimeType": "application/pdf",
                                                     "buffer": b"%PDF-1.4 broken"}])
        pw.expect(chip).to_have_count(2)
        pw.expect(chip.nth(1)).to_have_attribute("data-state", "failed")
        pw.expect(chip.nth(1).locator(".att-state")).to_have_text(
            "Open WebUI не обработал файл: модель может его не увидеть")
        page.locator("#owui-input").fill("Look")
        page.locator("#owui-send").click()
        until(page, lambda: fake.completions)
        sent = fake.completions[-1]["user_message"]["files"]
        image_id = next(fid for fid, name in fake.file_names.items() if name == "boat.png")
        assert [(f["name"], f["content_type"]) for f in sent] == [("boat.png", "image/png"),
                                                                    ("notes.pdf", "application/pdf")]
        assert sent[0]["id"] == image_id
        pw.expect(chip).to_have_count(0)
        mine = page.locator(".owui-msg.user").last
        pw.expect(mine.locator(".bubble")).to_have_text("Look")
        pw.expect(mine.locator("img")).to_have_attribute("src", f"/api/owui/accounts/{aid}/files/{image_id}")
        loaded_image(page, ".owui-msg.user img")
        pw.expect(mine.locator(".msg-file-link")).to_contain_text("notes.pdf")
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_pending_question_card_answers(owui_browser, live, fake, width):
    fake.add_model("m1", "Model One")
    questions = json.dumps({"allow_other": True, "questions": [
        {"id": "q1", "header": "Color", "question": "Which color?", "allow_other": False,
         "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cold"}]},
        {"id": "q2", "question": "Anything else?", "options": [{"label": "No", "description": ""}]}]})
    ask = fake.add_chat("Ask", models=["m1"], pending_approval=True, messages=talk({
        "id": "a1", "done": False, "content": "",
        "output": [{"type": "function_call", "call_id": "c1", "name": "ask_user", "arguments": questions,
                    "status": "pending"}]}))
    approve = fake.add_chat("Approve", models=["m1"], pending_approval=True, messages=talk({
        "id": "a2", "done": False, "content": "",
        "output": [{"type": "function_call", "call_id": "c2", "name": "delete_file",
                    "arguments": '{"path": "x.txt"}', "status": "pending"}]}))
    cookie = session(live, f"owui-pending-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{ask}") as page:
        card = page.locator(".owui-pending[data-kind='question']")
        pw.expect(card).to_be_visible()
        pw.expect(card.locator(".owui-question")).to_have_count(2)
        first, second = card.locator(".owui-question").nth(0), card.locator(".owui-question").nth(1)
        pw.expect(first).to_contain_text("Which color?")
        pw.expect(first.locator(".owui-option")).to_have_count(2)
        pw.expect(first.locator(".owui-option").nth(1)).to_contain_text("cold")
        pw.expect(first.locator(".pending-text")).to_have_count(0)            # свой текст не разрешён
        reply = card.locator(".pending-reply")
        pw.expect(reply).to_be_disabled()
        socket_ready(page, fake)
        first.locator(".owui-option", has_text="Blue").click()
        pw.expect(first.locator(".owui-option", has_text="Blue")).to_have_attribute("aria-pressed", "true")
        pw.expect(reply).to_be_disabled()                                    # второй вопрос ещё без ответа
        reads = lambda: sum(r.url.path == f"/api/v1/chats/{ask}" for r in fake.requests)  # noqa: E731
        before = reads()
        emit(fake, ask, "a1", "chat:active", {"active": True, "pending_approval": True})
        until(page, lambda: reads() > before)                                # лента перечитала чат
        page.wait_for_timeout(100)
        pw.expect(first.locator(".owui-option", has_text="Blue")).to_have_attribute("aria-pressed", "true")  # выбор цел
        second.locator(".pending-text").fill("Make it shiny")
        pw.expect(reply).to_be_enabled()
        reply.click()
        until(page, lambda: fake.resolved)
        assert fake.resolved[-1] == (ask, "a1", {"call_id": "c1", "action": "answer", "answers": {
            "q1": {"type": "option", "option_index": 1, "label": "Blue", "description": "cold"},
            "q2": {"type": "other", "text": "Make it shiny"}}})
        pw.expect(card).to_have_count(0)
        pw.expect(page.locator(".owui-msg.assistant[data-id='a1'] .msg-meta")).to_have_text("ответ идёт…")

        page.evaluate(f"location.hash = '#/owui/{aid}/{approve}'")
        card = page.locator(".owui-pending[data-kind='approval']")
        pw.expect(card.locator(".msg-pending-tool")).to_have_text("delete_file")
        pw.expect(card.locator(".owui-args-text")).to_be_hidden()           # аргументы свёрнуты
        card.locator(".owui-args summary").click()
        pw.expect(card.locator(".owui-args-text")).to_have_text('{"path": "x.txt"}')
        pw.expect(card.locator(".pending-run")).to_have_text("Разрешить")
        pw.expect(card.locator(".pending-refuse")).to_have_text("Отклонить")
        # решили в другом окне: вызов получил результат, Open WebUI продолжил ответ — карточка уходит
        output = fake.chats[approve]["messages"]["a2"]["output"]
        output[0]["status"] = "completed"
        output.append({"type": "function_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "ok"}]})
        socket_ready(page, fake)
        emit(fake, approve, "a2", "chat:active", {"active": True})
        pw.expect(card).to_have_count(0)
        assert not [r for r in fake.resolved if r[0] == approve]
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_stop_ends_answer(owui_browser, live, fake, width):
    fake.add_model("m1", "Model One")
    chat = fake.add_chat("Long", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "Ready"}))
    cookie = session(live, f"owui-stop-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        pw.expect(page.locator(".owui-msg.assistant .md")).to_have_text("Ready")
        pw.expect(page.locator("#owui-stop")).to_be_hidden()
        socket_ready(page, fake)
        page.locator("#owui-input").fill("Write a long story")
        page.locator("#owui-send").click()
        until(page, lambda: fake.completions)
        message = fake.completions[-1]["id"]
        delta(fake, chat, message, "Once upon")
        answer = page.locator(f".owui-msg.assistant[data-id='{message}']")
        pw.expect(answer.locator(".owui-stream")).to_have_text("Once upon")

        fake.queue_next = True                                               # пока идёт ответ — в очередь
        page.locator("#owui-input").fill("Then a poem")
        page.locator("#owui-send").click()
        pw.expect(page.locator(".owui-msg-queued .bubble")).to_have_text("Then a poem")
        pw.expect(page.locator(".owui-msg-queued")).to_contain_text("в очереди")
        pw.expect(page.locator("#owui-queued")).to_be_visible()
        fake.queue_next = False

        stop = page.locator("#owui-stop")
        pw.expect(stop).to_be_visible()
        stop.click()
        until(page, lambda: fake.stopped == [chat])
        pw.expect(stop).to_be_hidden()
        pw.expect(page.locator("#owui-send")).to_be_enabled()
        fake.finish(chat, message, "Once upon")
        finished(fake, chat, message)
        pw.expect(answer.locator(".md")).to_have_text("Once upon")
        pw.expect(answer.locator(".msg-meta")).to_have_count(0)
        assert fits(page)
        assert not page.problems, page.problems


# ---- поток после блокировки, две подписки, вход заново с email, прокрутка -------------------------------------------

def chat_reads(page, aid, chat):
    """Сколько раз страница читала чат (GET …/chats/{chat})."""
    return page.asked.count(f"/api/owui/accounts/{aid}/chats/{chat}")


def watchers(aid):
    link = owui_link.LINKS.get(aid)
    return len(link.watchers) if link else 0


def at_bottom(page):
    page.wait_for_function("""() => { const s = document.querySelector('#owui-messages');
      return s.scrollHeight > s.clientHeight && s.scrollHeight - s.scrollTop - s.clientHeight < 80; }""", timeout=5000)


@pytest.mark.parametrize("width", [320, 1280])
def test_stream_comes_back_after_the_screen_lock(owui_browser, live, fake, monkeypatch, width):
    """Блокировка экрана: сервер закрывает поток (`closed`), повтор ждёт отпирания (423 — api.js), после отпирания поток
    учётки снова открыт, куски и `done` доходят до ленты."""
    monkeypatch.setattr(owui, "CHECK_SEC", 0)                             # блокировку поток видит на первом событии
    fake.add_model("m1", "Model One")
    chat = fake.add_chat("Locked", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "Ready"}))
    cookie = session(live, f"owui-lock-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        pw.expect(page.locator(".owui-msg.assistant[data-id='a1'] .md")).to_have_text("Ready")
        socket_ready(page, fake)
        page.locator("#owui-input").fill("Write a story")
        page.locator("#owui-send").click()
        until(page, lambda: fake.completions)
        message = fake.completions[-1]["id"]
        answer = page.locator(f".owui-msg.assistant[data-id='{message}']")
        delta(fake, chat, message, "Once")
        pw.expect(answer.locator(".owui-stream")).to_have_text("Once")
        page.evaluate("""async () => { const m = await import('/static/js/api.js');
          await m.api('POST', '/api/me/screen-lock', { enabled: true, timeout: 600 });
          await m.api('POST', '/api/lock/now'); }""")
        delta(fake, chat, message, " lost")                               # вместо куска — `closed`
        until(page, lambda: watchers(aid) == 0)
        page.locator("#lock[open]").wait_for()
        if page.locator("#lock-use-password").is_visible():
            page.locator("#lock-use-password").click()
        page.locator("#lock-password").fill(PASSWORD)
        page.locator("#lock-password-go").click()
        page.locator("#lock[open]").wait_for(state="detached")
        until(page, lambda: watchers(aid) == 1)                         # поток учётки открылся снова
        socket_ready(page, fake)
        delta(fake, chat, message, " upon")
        pw.expect(answer.locator(".owui-stream")).to_contain_text("upon")
        fake.finish(chat, message, "Once upon a time")
        finished(fake, chat, message)
        pw.expect(answer.locator(".md")).to_have_text("Once upon a time")
        pw.expect(page.locator("#owui-stop")).to_be_hidden()
        assert fits(page)
        problems = [p for p in page.problems if "423" not in p]
        assert not problems, problems


@pytest.mark.parametrize("width", [320, 1280])
def test_panel_and_feed_of_different_accounts_keep_two_streams(owui_browser, live, fake, width):
    """Учётка панели ≠ учётке ленты: два потока; нажатие на учётку меняет только панель; лишний поток закрывается;
    `relogin` учётки ленты видно в панели, хоть она и не показана."""
    fake.add_model("m1", "Model One")
    chat = fake.add_chat("Mine", models=["m1"], messages=talk({"id": "a1", "done": False, "content": "Part"}))
    cookie = session(live, f"owui-two-{owui_browser.browser_type.name}-{width}")
    first = connected(live, cookie, label="First")
    fake.user_id = "owui-user-2"                                         # вторая учётка того же сервера
    second = connected(live, cookie, label="Second")
    with opened(owui_browser, live, cookie, width, f"owui/{first}/{chat}") as page:
        stream = page.locator(".owui-msg.assistant[data-id='a1'] .owui-stream")
        pw.expect(stream).to_have_text("Part")
        show_panel(page)
        rows = {aid: page.locator(f".owui-account-row[data-id='{aid}']") for aid in (first, second)}
        rows[second].locator(".owui-account").click()
        pw.expect(rows[second].locator(".owui-account")).to_have_attribute("aria-current", "true")
        pw.expect(page).to_have_url(re.compile(rf"#/owui/{first}/{chat}$"))     # лента осталась
        pw.expect(page.locator("#owui-feed")).to_have_attribute("data-account", str(first))
        until(page, lambda: watchers(first) == 1 and watchers(second) == 1)
        until(page, lambda: len([s for s in fake.sockets if not s.closed and s.user]) == 2)
        delta(fake, chat, "a1", " more")
        pw.expect(stream).to_have_text("Part more")                       # поток учётки ленты жив

        rows[first].locator(".owui-account").click()                     # панель и лента — одна учётка
        until(page, lambda: watchers(second) == 0 and watchers(first) == 1)
        rows[second].locator(".owui-account").click()
        until(page, lambda: watchers(first) == 1 and watchers(second) == 1)

        fake.revoke_all()                                                # обе сессии отозваны в Open WebUI
        fake.expire_tokens()
        for sock in [s for s in fake.sockets if not s.closed]:
            fake.later(sock.close_from_server)
        for aid in (first, second):
            pw.expect(rows[aid].locator(".owui-state")).to_have_class(re.compile(r"\brelogin\b"))
        pw.expect(page.locator("#owui-relogin")).to_be_visible()
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_relogin_asks_email_when_needed(owui_browser, live, fake, width):
    """Вход заново: 422 `owui_email` — появляется поле email (с известным email), повтор шлёт его; учётка `reenter`
    (данные не читаются после смены ключа сервера) — поле email сразу и пустое."""
    cookie = session(live, f"owui-email-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie, label="Работа")
    live_sql(live, "UPDATE owui_accounts SET state = 'relogin' WHERE id = ?", (aid,))
    sent = []

    def login(route):
        sent.append(json.loads(route.request.post_data))
        if len(sent) == 1:
            route.fulfill(status=422, content_type="application/json", body=json.dumps(
                {"detail": {"code": "owui_email", "message": "укажите email учётки Open WebUI"}}))
        else:
            route.continue_()

    with opened(owui_browser, live, cookie, width, f"owui/{aid}", service_workers="block") as page:
        page.route(f"**/api/owui/accounts/{aid}/login", login)
        show_panel(page)
        email = page.locator("#owui-relogin-email")
        pw.expect(email).to_be_hidden()
        pw.expect(page.locator(".owui-relogin-who")).to_have_text(FakeOwui.EMAIL)
        page.locator("#owui-relogin-password").fill(FakeOwui.PASSWORD)
        page.locator("#owui-relogin-submit").click()
        pw.expect(email).to_be_visible()
        pw.expect(email).to_have_value(FakeOwui.EMAIL)
        pw.expect(page.locator("#owui-relogin .error[data-for=email]")).not_to_be_empty()
        pw.expect(page.locator(".owui-relogin-who")).to_have_count(0)
        page.locator("#owui-relogin-submit").click()
        pw.expect(page.locator("#owui-relogin")).to_have_count(0)
        assert sent == [{"password": FakeOwui.PASSWORD}, {"password": FakeOwui.PASSWORD, "email": FakeOwui.EMAIL}]

        page.unroute(f"**/api/owui/accounts/{aid}/login")
        live_sql(live, "UPDATE owui_accounts SET state = 'relogin' WHERE id = ?", (aid,))

        def listing(route):
            response = route.fetch()
            route.fulfill(response=response, json=[{**a, "reenter": True} for a in response.json()])

        page.route(re.compile(r".*/api/owui/accounts$"), listing)
        page.reload()
        page.locator("html[data-ready='1']").wait_for(state="attached")
        pw.expect(page.locator("#owui-panel")).to_be_visible()             # чата нет: телефон выдвигает панель сам
        pw.expect(email).to_be_visible()
        pw.expect(email).to_have_value("")
        pw.expect(page.locator(".owui-relogin-who")).to_have_count(0)
        no_password_manager(page.locator("#owui-relogin"))
        assert fits(page)
        assert not page.problems, page.problems


@pytest.mark.parametrize("width", [320, 1280])
def test_long_chat_opens_at_the_end_and_rereads_after_reconnect(owui_browser, live, fake, width):
    """Длинный чат открывается у последнего сообщения и после разметки (ответ длиннее 8000 знаков — воркер); картинка
    Open WebUI в разметке — через сервер Студии; `link up` новой подписки чат не перечитывает, после обрыва — да."""
    fake.add_model("m1", "Model One")
    fake.add_file("img-1", PNG, "image/png")
    messages, parent = [], None
    for n in range(12):
        messages += [{"id": f"u{n}", "parentId": parent, "role": "user", "content": f"Question {n}", "timestamp": 2 * n},
                     {"id": f"a{n}", "parentId": f"u{n}", "role": "assistant", "model": "m1", "done": True,
                      "timestamp": 2 * n + 1,
                      "content": "\n\n".join(f"Paragraph {k} of **answer {n}**" for k in range(8))}]
        parent = f"a{n}"
    messages[-1]["content"] = "Last ![boat](/api/v1/files/img-1/content)\n\n" + "word " * 2000 + "\n\n**The end**"
    chat = fake.add_chat("Long read", models=["m1"], messages=messages)
    cookie = session(live, f"owui-scroll-{owui_browser.browser_type.name}-{width}")
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        last = page.locator(".owui-msg.assistant[data-id='a11']")
        pw.expect(last.locator(".md strong").last).to_have_text("The end")
        pw.expect(last.locator(".md img")).to_have_attribute("src", f"{live.url}/api/owui/accounts/{aid}/files/img-1")
        loaded_image(page, ".owui-msg.assistant[data-id='a11'] .md img")
        at_bottom(page)
        socket_ready(page, fake)
        page.wait_for_timeout(300)
        assert chat_reads(page, aid, chat) == 1                          # `link up` подписки — без второго чтения
        fake.later(fake.drop)                                            # обрыв сокета: down → up
        until(page, lambda: chat_reads(page, aid, chat) == 2)
        pw.expect(last.locator(".md strong").last).to_have_text("The end")
        at_bottom(page)
        assert fits(page)
        assert not page.problems, page.problems
