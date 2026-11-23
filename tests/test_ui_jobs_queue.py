"""«В работе»: «Отменить» у идущих и ждущих строк; очередь расшифровок — «в очереди · N» в разделе
(Chromium 320/1280, WebKit касанием)."""
import time
from pathlib import Path

import httpx
import pytest

from browser import run_playwright

from test_ui_browser import (JOBS_POLLED, NO_OVERFLOW, READY, browser, go, is_open, live, live_sql,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def queued(live, uid, prefix):
    """Ждущие расшифровка и озвучка (раннер их не берёт: строки вставлены мимо API)."""
    now = time.time()
    live_sql(live, "INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                   " VALUES (?, ?, 'whisperx', 'Звонок.m4a', 'nope', 'queued', 'в очереди', ?)",
             (f"{prefix}-tr", uid, now - 30))
    live_sql(live, "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created)"
                   " VALUES (?, ?, 'qwen3-tts', 'v', 'Привет', 'queued', 'в очереди', ?)", (f"{prefix}-sp", uid, now - 20))


@pytest.mark.parametrize("width", [320, 1280])
def test_jobs_panel_cancels_a_queued_record(browser, live, width):
    login = f"jobsq{width}"
    uid = live.add_user(login)
    queued(live, uid, login)
    row = f"#jobs-running .jobs-row[data-id='{login}-sp']"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("wait", JOBS_POLLED),
        *go("transcribe"), ("wait", f"document.querySelector('.tr-item[data-id=\"{login}-tr\"]')"),
        ("eval", "trDetail", f"document.querySelector('.tr-item[data-id=\"{login}-tr\"]').textContent"),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("wait", f"document.querySelector(\"{row} .jobs-cancel\")"),
        ("eval", "detail", f"document.querySelector(\"{row} .jobs-detail\").textContent"),
        ("eval", "fits", f"[...document.querySelectorAll('#jobs-running .jobs-row')].every(r => r.scrollWidth <= r.clientWidth + 1) && {NO_OVERFLOW}"),
        ("click", f"{row} .jobs-cancel"),
        ("wait", f"!document.querySelector(\"{row}\") && document.querySelector(\"#jobs-finished .jobs-row[data-id='{login}-sp'][data-state=cancelled]\")"),
        ("eval", "noCancel", f"!document.querySelector(\"#jobs-finished .jobs-row[data-id='{login}-sp'] .jobs-cancel\")")],
        width=width, height=900, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert "в очереди · 1" in v["trDetail"] and v["detail"].startswith("в очереди · 1")
    assert v["fits"] and v["noCancel"]
    assert any(s == f"POST /api/speeches/chats/{login}-sp/cancel" for s in r["sent"])     # весь чат (C-I3)
    assert not problems(r), problems(r)


@pytest.fixture(scope="module")
def webkit():
    pw = pytest.importorskip("playwright.sync_api")
    with run_playwright(pw) as driver:
        if not Path(driver.webkit.executable_path).is_file():
            pytest.skip("нет WebKit: .venv/Scripts/python -m playwright install webkit")
        engine = driver.webkit.launch()
        yield engine
        engine.close()


def until(page, expression, sec=10.0):
    deadline = time.monotonic() + sec
    while not page.evaluate(expression):
        assert time.monotonic() < deadline, expression
        page.wait_for_timeout(50)


def test_jobs_panel_cancel_by_touch_in_webkit(webkit, live):
    login = "jobsq-wk"
    uid = live.add_user(login)
    queued(live, uid, login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with webkit.new_context(viewport={"width": 393, "height": 852}, is_mobile=True, has_touch=True,
                            device_scale_factor=3, extra_http_headers={"Cookie": cookie}) as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live.url + "/")
        until(page, READY)
        until(page, JOBS_POLLED)
        page.locator("#jobs").tap()
        row = f"#jobs-running .jobs-row[data-id='{login}-tr']"
        page.locator(f"{row} .jobs-cancel").tap()
        until(page, f"!document.querySelector(\"{row}\")")
        assert page.evaluate(NO_OVERFLOW) and not errors


@pytest.mark.parametrize("width", [320, 1280])
def test_jobs_panel_names_what_holds_the_card_and_how_long(browser, live, width):
    """00:I3: ждущий ролик в «В работе» — «карта занята: ролик · ещё примерно 9 мин» (идущий ролик, медиана недавних
    готовых — 600 с, прошло 60); строка влезает в панель."""
    login = f"jobscard{width}"
    uid = live.add_user(login)
    now, conn = time.time(), 900000 + width
    live_sql(live, "INSERT INTO connections(id, user_id, name, kind, base_url, token_enc, sections, created)"
                   " VALUES (?, ?, 'srvllm', 'srvllm', 'http://10.77.1.15:4000', x'00', '[\"video\"]', ?)", (conn, uid, now))
    for vid, state_, created, finished in ((f"{login}-old", "done", now - 4000, now - 3400),
                                           (f"{login}-run", "in_progress", now - 60, None),
                                           (f"{login}-wait", "queued", now - 30, None)):
        live_sql(live, "INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth,"
                       " state, progress, detail, created, finished) VALUES (?, ?, ?, 'nope', 'p', 5, 'standard', 1, ?, 0,"
                       " 'в очереди', ?, ?)", (vid, uid, conn, state_, created, finished))
    row = f"#jobs-running .jobs-row[data-id='{login}-wait']"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("wait", JOBS_POLLED),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("wait", f"document.querySelector(\"{row} .jobs-detail\")?.textContent.includes('карта занята')"),
        ("eval", "detail", f"document.querySelector(\"{row} .jobs-detail\").textContent"),
        ("eval", "busy", f"document.querySelector(\"#jobs-running .jobs-row[data-id='{login}-run'] .jobs-detail\").textContent"),
        ("eval", "fits", f"[...document.querySelectorAll('#jobs-running .jobs-row')].every(r => r.scrollWidth <= r.clientWidth + 1) && {NO_OVERFLOW}")],
        width=width, height=900, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["detail"].startswith("в очереди · карта занята: ролик · ещё примерно 9 мин · "), v
    assert "карта занята" not in v["busy"] and v["fits"]
    assert not problems(r), problems(r)
