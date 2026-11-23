"""Лист «Поделиться» установленной студии (share_target манифеста): настоящий sw.js принимает POST /receive, студия
спрашивает раздел по типу принятого и кладёт его в поле раздела; принятое переживает вход."""
import base64
import io
import time

import pytest
from PIL import Image

from browser import CHROMIUM_ARGS, find_browser, run_playwright
from test_ui_browser import live  # noqa: F401 — модульная живая служба
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

#: форма, как её шлёт системный лист: POST /receive, multipart, поля title/text/url и files
SHARE = """async ({ png, title, text, url }) => {
  const form = document.createElement('form');
  form.method = 'post';
  form.enctype = 'multipart/form-data';
  form.action = '/receive';
  for (const [name, value] of Object.entries({ title, text, url })) {
    const input = document.createElement('input');
    input.name = name;
    input.value = value;
    form.append(input);
  }
  if (png) {
    const bytes = Uint8Array.from(atob(png), (c) => c.charCodeAt(0));
    const files = new DataTransfer();
    files.items.add(new File([bytes], 'документ.png', { type: 'image/png' }));
    const input = document.createElement('input');
    input.type = 'file';
    input.name = 'files';
    input.files = files.files;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
}"""

#: счёт загрузок «Сканов» (POST /api/ocr XHR) без отправки
OCR_POSTS = """() => {
  window.ocrPosts = 0;
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.ocrPost = method === 'POST' && url === '/api/ocr';
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (this.ocrPost) window.ocrPosts += 1;
    else send.call(this, body);
  };
}"""


@pytest.fixture(scope="module")
def chromium():
    with run_playwright(pw) as driver:
        executable = find_browser()
        if not executable:
            pytest.skip("нет Chromium/Edge/Chrome")
        browser = driver.chromium.launch(executable_path=executable, args=CHROMIUM_ARGS)
        yield browser
        browser.close()


def png():
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (20, 120, 200)).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def until(page, expression, sec=10.0):
    deadline = time.monotonic() + sec
    while not page.evaluate(expression):
        assert time.monotonic() < deadline, expression
        page.wait_for_timeout(50)


def choices(page):
    return page.locator("#received [data-section]").evaluate_all(
        "(nodes) => nodes.map((n) => [n.dataset.section, n.textContent])")


@pytest.mark.parametrize("mobile,width", [(False, 1280), (True, 320)])
def test_shared_photo_and_text_land_in_the_chosen_section(chromium, live, mobile, width):  # noqa: F811
    with chromium.new_context(viewport={"width": width, "height": 800}, is_mobile=mobile, has_touch=mobile) as context:
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(live.url + "/robots.txt")
        page.evaluate("""async () => {
          await navigator.serviceWorker.register('/sw.js');
          if (!navigator.serviceWorker.controller) {
            await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, {once:true}));
          }
        }""")
        # без сессии: лист открыл студию, сервер увёл на вход — принятое ждёт в кэше устройства
        page.evaluate(SHARE, {"png": png(), "title": "", "text": "", "url": ""})
        page.wait_for_url("**/login")
        page.fill("#login", "owner")
        page.fill("#password", "password-123")
        page.click("#submit")
        pw.expect(page.locator("#received")).to_be_visible()
        pw.expect(page.locator("#received .received-what")).to_have_text("документ.png")
        assert choices(page) == [["ocr", "Сканы"], ["photo", "Фото"], ["chat", "Чат"]]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator('#received [data-section="photo"]').click()
        page.wait_for_url("**/#/photo")
        pw.expect(page.locator("#photo-source")).to_be_visible()     # фото стало исходным, как выбранное скрепкой
        assert page.evaluate("caches.has('studio-received')") is False

        # «Сканы» загружают выбранный файл сразу: принятое (его мог прислать и чужой сайт) — только явной кнопкой.
        # Загрузка (XHR) до службы не доходит: page.route запросы страницы под служебным работником не ловит
        for upload in (False, True):
            page.goto(live.url + "/robots.txt")
            page.evaluate(SHARE, {"png": png(), "title": "", "text": "", "url": ""})
            pw.expect(page.locator("#received")).to_contain_text("из другого приложения или сайта")
            page.evaluate(OCR_POSTS)
            page.locator('#received [data-section="ocr"]').click()
            pw.expect(page.locator("#received-confirm")).to_be_visible()
            pw.expect(page.locator("#received-confirm .received-what")).to_have_text("документ.png")
            pw.expect(page.locator("#received-upload")).to_have_text("Загрузить скан")
            page.locator("#received-upload" if upload else "#received-confirm .dialog-close").click()
            pw.expect(page.locator("#received-confirm")).to_have_count(0)
            if upload:
                page.wait_for_function("window.ocrPosts === 1")
            else:
                assert page.evaluate("window.ocrPosts") == 0

        # текст и ссылка — «Чат» или «Перевод»; ссылка, уже вписанная в текст, не повторяется
        page.goto(live.url + "/robots.txt")
        page.evaluate(SHARE, {"png": "", "title": "Статья", "text": "Прочти https://example.com/a",
                              "url": "https://example.com/a"})
        pw.expect(page.locator("#received")).to_be_visible()
        assert choices(page) == [["chat", "Чат"], ["translate", "Перевод"]]
        page.locator('#received [data-section="chat"]').click()
        pw.expect(page.locator("#chat-input")).to_have_value("Статья\nПрочти https://example.com/a")

        # «Не отправлять» — принятое забыто, при следующем запуске окна нет
        page.goto(live.url + "/robots.txt")
        page.evaluate(SHARE, {"png": "", "title": "", "text": "черновик", "url": ""})
        pw.expect(page.locator("#received")).to_be_visible()
        page.locator("#received .dialog-close").click()
        pw.expect(page.locator("#received")).to_have_count(0)
        assert page.evaluate("caches.has('studio-received')") is False
        page.reload()
        page.wait_for_selector("html[data-ready='1']")
        assert page.locator("#received").count() == 0
        assert errors == []


def test_shown_but_unsent_share_does_not_outlive_the_session(chromium, live):  # noqa: F811
    """Показанное, но не отправленное принятое стирают явный выход и страница входа после конца сессии: следующей
    учётке устройства оно не достаётся (принятое без сессии ждёт входа — первый сценарий)."""
    with chromium.new_context(viewport={"width": 1280, "height": 800}) as context:
        page = context.new_page()
        page.set_default_timeout(15000)
        page.goto(live.url + "/robots.txt")
        page.evaluate("""async () => {
          await navigator.serviceWorker.register('/sw.js');
          if (!navigator.serviceWorker.controller) {
            await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, {once:true}));
          }
        }""")
        context.request.post(live.url + "/login", form={"login": "owner", "password": "password-123"})
        has = "caches.has('studio-received')"
        # явный выход: окно не закрыто (приложение свернули) — принятое уходит вместе с сессией
        page.evaluate(SHARE, {"png": "", "title": "", "text": "чужое", "url": ""})
        pw.expect(page.locator("#received")).to_be_visible()
        assert page.evaluate(has) is True
        page.evaluate("document.querySelector('#menu-logout').form.requestSubmit()")
        page.wait_for_url("**/login")
        until(page, f"{has}.then((x) => !x)")
        # сессия кончилась (завершена с другого устройства): страница входа стирает уже показанное
        context.request.post(live.url + "/login", form={"login": "owner", "password": "password-123"})
        page.evaluate(SHARE, {"png": "", "title": "", "text": "чужое", "url": ""})
        pw.expect(page.locator("#received")).to_be_visible()
        context.clear_cookies()
        page.goto(live.url + "/login")
        until(page, f"{has}.then((x) => !x)")


def test_app_badge_counts_unseen_finished_and_clears_with_the_panel(chromium, live):  # noqa: F811
    """Число на значке приложения: непросмотренное готовое (здесь — картинка за минуту до входа); открытие панели
    «В работе» его снимает. Само API значка подменено: у неустановленной страницы его эффекта не видно."""

    import db
    import media

    conn = db.connect(live.db_path)
    uid = conn.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    picture = media.save(uid, base64.b64decode(png()), "image/png", "badge.png")
    conn.execute("DELETE FROM images WHERE id = 'ui-badge'")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created,"
                 " finished) VALUES ('ui-badge', ?, 'generation', 'flux', 'лиса', '40x30', 1, ?, 'done', 'готово', ?, ?)",
                 (uid, picture, time.time() - 90, time.time() - 60))
    conn.close()
    try:
        with chromium.new_context(viewport={"width": 1280, "height": 800}) as context:
            context.request.post(live.url + "/login", form={"login": "owner", "password": "password-123"})
            page = context.new_page()
            page.set_default_timeout(15000)
            page.add_init_script("""window.badges = [];
              navigator.setAppBadge = async (n) => { window.badges.push(n); };
              navigator.clearAppBadge = async () => { window.badges.push(0); };""")
            page.goto(live.url + "/#/chat")
            page.wait_for_selector("#jobs[data-polled='1']")
            assert page.evaluate("window.badges") == [1]
            assert page.evaluate("caches.open('studio-badge').then((c) => c.match('/badge')).then((r) => r.text())") == "1"
            page.click("#jobs")
            page.wait_for_function("window.badges.at(-1) === 0")
    finally:
        conn = db.connect(live.db_path)
        conn.execute("DELETE FROM images WHERE id = 'ui-badge'")
        conn.close()
