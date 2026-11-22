"""«Open WebUI» в браузере: панель учёток и дерево чатов. Студия — живая (`live_app.py`), за ней поддельный Open WebUI
(`fake_owui.py`: REST — через шлюз теста, сокет — в памяти). Chromium (Edge/Chrome) и WebKit, 320 и 1280 px."""
import asyncio
import json
import re
import ssl
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

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
def opened(browser, live, cookie, width, path):
    mobile = width < 760
    with browser.new_context(viewport={"width": width, "height": 800}, is_mobile=mobile, has_touch=mobile,
                             extra_http_headers={"Cookie": cookie}) as context:
        page = context.new_page()
        page.set_default_timeout(10000)
        page.problems = []
        page.events = []
        page.on("pageerror", lambda error: page.problems.append(str(error)))
        page.on("console", lambda message: page.problems.append(message.text) if message.type == "error"
                and not ("Failed to load resource" in message.text and any(c in message.text for c in EXPECTED))
                else None)
        page.on("request", lambda request: page.events.append(request.url) if request.url.endswith("/events") else None)
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
        dialog.locator("[name=label]").fill("s06 · рабочая")
        dialog.locator("[name=url]").fill(FakeOwui.URL)
        dialog.locator("[name=email]").fill(FakeOwui.EMAIL)
        dialog.locator("[name=password]").fill(FakeOwui.PASSWORD)
        page.locator("#owui-connect-submit").click()
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
        page.locator("#owui-relogin-password").fill("wrong")
        page.locator("#owui-relogin-submit").click()
        pw.expect(page.locator("#owui-relogin .error[data-for=password]")).to_have_text(
            "Неверный email или пароль Open WebUI")
        page.locator("#owui-relogin-password").fill(FakeOwui.PASSWORD)
        page.locator("#owui-relogin-submit").click()
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
        fake.later(fake.emit_event, calm, "m1", {"type": "chat:active", "data": {"active": True, "pending_approval": True}})
        pw.expect(row.locator(".owui-chat-pending")).to_have_text("ждёт решения")
        pw.expect(row.locator(".owui-chat-active")).to_have_count(0)
        fake.later(fake.emit_event, calm, None, {"type": "chat:title", "data": {"title": "Renamed by model"}})
        pw.expect(row.locator(".owui-chat-title")).to_have_text("Renamed by model")
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
