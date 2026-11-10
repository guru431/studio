"""Раздел «Видео» в браузере: опрос идущего ролика и отмена (одна цепочка опросов), «Повторить» и
«Ещё варианты» (Chromium 320/1280, WebKit касаниями)."""
import io
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from fakes import video_object
from test_ui_browser import (CLIP, NO_OVERFLOW, READY, VIDEO_FEED_READY, add_gateway, browser, clips,  # noqa: F401
                             NEWEST_DONE, done_cards, go, is_open, live, make_images, photo_screen,
                             png_of, problems, put_file, ready_clips, set_value, video_screen)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

#: ответ опроса ролика приходит на 1 с позже, чем его прочёл сервер (устаревший ответ);
#: window.polls — сколько опросов в пути сейчас, максимум и всего
STALE_POLLS = """(() => { window.polls = { now: 0, max: 0, total: 0 }; const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    if ((opts.method || 'GET') !== 'GET' || !/^\\/api\\/videos\\/[0-9a-f]+$/.test(String(url))) return real(url, opts);
    window.polls.now += 1; window.polls.total += 1; window.polls.max = Math.max(window.polls.max, window.polls.now);
    try { const r = await real(url, opts); await new Promise(res => setTimeout(res, 1000)); return r; }
    finally { window.polls.now -= 1; }
  }; return true; })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_cancel_during_a_poll_ignores_the_stale_answer_and_keeps_one_chain(browser, live, width):
    """«Отменить», пока опрос в пути: его устаревший `in_progress` не возвращает карточке «Отменить»,
    и после отмены опросы не продолжаются (раньше — вторая цепочка таймеров)."""
    login = f"vstale{width}"
    live.add_user(login)
    running_id = f"video_{len(live.gw.video_ids) + 1:024x}"

    async def running(request):
        if request.method != "GET":
            return await live.gw.builtin(request)
        return httpx.Response(200, json=video_object(running_id, status="in_progress", progress=12, seed=7))

    live.gw.on[f"/slot/videos/{running_id}"] = running
    busy = f"{CLIP}[data-state=in_progress]"
    try:
        r = browser.drive(live.url + "/", [*video_screen(login), ("eval", "spy", STALE_POLLS),
            put_file("#video-file", png_of(48, 64, "teal")),
            ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
            ("click", "#video-go"),
            ("wait", f"document.querySelector('{busy} .card-cancel') && window.polls.now === 1"),
            ("click", f"{busy} .card-cancel"),
            ("wait", clips("cancelled", 1)),
            ("eval", "mark", "(window.mark = { at: Date.now(), total: window.polls.total }, true)"),
            # устаревший ответ пришёл, затем — дольше одного POLL_MS (3 с)
            ("wait", "window.polls.now === 0 && Date.now() - window.mark.at > 3600"),
            ("eval", "states", f"[...document.querySelectorAll('{CLIP}')].map(c => c.dataset.state)"),
            ("eval", "cancel", f"!!document.querySelector('{CLIP} .card-cancel')"),
            ("eval", "later", "window.polls.total - window.mark.total"),
            ("eval", "max", "window.polls.max")],
            width=width, height=900, mobile=width == 320, timeout=20000)
    finally:
        live.gw.on.pop(f"/slot/videos/{running_id}", None)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["states"] == ["cancelled"] and v["cancel"] is False
    assert v["later"] == 0 and v["max"] == 1
    assert not problems(r), problems(r)


#: роликов в ленте по состояниям — одной строкой для проверок
STATES = f"[...document.querySelectorAll('{CLIP}')].map(c => c.dataset.state)"


@pytest.mark.parametrize("width", [320, 1280])
def test_repeat_and_more_variants_of_a_clip(browser, live, ready_clips, width):
    """«Повторить» и «Ещё варианты» (2–4): тот же кадр, промпт и параметры, seed новый у каждого;
    кнопки помещаются в карточку на 320 px."""
    login = f"vrepeat{width}"
    live.add_user(login)
    v0 = len(live.gw.video_forms)
    r = browser.drive(live.url + "/", [*video_screen(login),
        put_file("#video-file", png_of(48, 64, "olive")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
        ("type", "#video-prompt", "медленный поворот"), ("click", "#video-go"),
        ("wait", clips("done", 1)),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "acts", f"[...document.querySelectorAll('{CLIP} .video-acts .btn')].map(b => b.textContent)"),
        ("click", f"{CLIP} .card-repeat"), ("wait", clips("done", 2)),
        ("click", f"{CLIP} .card-variants"), ("wait", is_open("#video-variants-dialog")),
        set_value("#video-variants-count", "3"), ("click", "#video-variants-go"),
        ("wait", f"{clips('done', 5)} && !{is_open('#video-variants-dialog')}"),
        ("eval", "states", STATES),
        ("eval", "error", "document.querySelector('#video-error').textContent"),
        ("eval", "fitsAfter", NO_OVERFLOW)],
        width=width, height=900, mobile=width == 320, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["fits"] and v["fitsAfter"] and v["error"] == ""
    assert v["acts"][-4:] == ["Скачать", "Повторить", "Ещё варианты", "Удалить"]
    forms = live.gw.video_forms[v0:]
    assert len(forms) == 5
    seeds = [f[0].pop("seed") for f in forms]
    assert len(set(seeds)) == 5                                     # seed — новый у каждого
    assert all(f[0] == forms[0][0] for f in forms) and forms[0][0]["prompt"] == "медленный поворот"
    assert all(f[1] == forms[0][1] for f in forms)                  # тот же кадр
    assert not problems(r), problems(r)


@pytest.fixture(scope="module")
def webkit():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        if not Path(driver.webkit.executable_path).is_file():
            pytest.skip("нет WebKit: .venv/Scripts/python -m playwright install webkit")
        engine = driver.webkit.launch()
        yield engine
        engine.close()


def until(page, expression, sec=10.0):
    """wait_for_function строкой WebKit не даёт выполнить (CSP без unsafe-eval) — опрос через evaluate."""
    deadline = time.monotonic() + sec
    while not page.evaluate(expression):
        assert time.monotonic() < deadline, expression
        page.wait_for_timeout(50)


def png_bytes(w, h, color):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("width", [320, 1280])
def test_photo_context_is_editable_bounded_and_cleared_with_a_new_source(browser, live, width):
    login = f"vcontext{width}"
    live.add_user(login)
    v0 = len(live.gw.video_forms)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
        ("eval", "photo", "(window.photoId = document.querySelector('.photo-card').dataset.id)"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-animate"),
        ("wait", "document.querySelector('#video-photo-prompt')?.value === 'кот 0'"),
        ("eval", "url", "location.hash"),
        ("click", "#video-photo-context summary"),
        ("eval", "long", "(document.querySelector('#video-photo-prompt').value = 'я'.repeat(4001), true)"),
        ("click", "#video-go"), ("wait", "document.querySelector('#video-error').textContent.includes('4000')"),
        ("eval", "bounded", NO_OVERFLOW),
        ("type", "#video-prompt", "медленный поворот"),
        # Внутри уже открытого видео снова переносится исходник; движение остаётся своим.
        ("eval", "again", "(location.hash = '#/video?photo=' + window.photoId, true)"),
        ("wait", "document.querySelector('#video-photo-prompt').value === 'кот 0'"),
        ("eval", "motion", "document.querySelector('#video-prompt').value"),
        ("click", "#video-source-clear"),
        ("eval", "cleared", "[document.querySelector('#video-photo-context').hidden, document.querySelector('#video-photo-prompt').value]"),
        put_file("#video-file", png_of(48, 64, "navy")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
        ("click", "#video-go"), ("wait", clips("queued", 1))],
        width=width, height=900, mobile=width == 320, timeout=20000)
    assert not r.get("failed"), r
    assert r["values"]["url"] == "#/video" and r["values"]["bounded"]
    assert r["values"]["cleared"] == [True, ""] and r["values"]["motion"] == "медленный поворот"
    assert len(live.gw.video_forms) - v0 == 1
    assert live.gw.video_forms[-1][0]["prompt"] == "медленный поворот"
    assert not problems(r), problems(r)


def test_a_late_photo_transfer_cannot_replace_a_new_device_source(browser, live):
    login = "vlatephoto"
    live.add_user(login)
    v0 = len(live.gw.video_forms)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
        ("eval", "photo", "(window.photoId = document.querySelector('.photo-card').dataset.id)"),
        *go("video"), ("wait", VIDEO_FEED_READY),
        ("eval", "hold", """(() => { const real = window.fetch;
          window.fetch = async (url, opts) => {
            const response = await real(url, opts);
            if (String(url) === '/api/images/' + window.photoId) {
              await new Promise(resolve => { window.releasePhoto = resolve; });
            }
            return response;
          }; return true; })()"""),
        ("eval", "transfer", "(location.hash = '#/video?photo=' + window.photoId, true)"),
        ("wait", "!!window.releasePhoto && document.querySelector('#video-go').disabled"),
        ("eval", "loadingFits", NO_OVERFLOW),
        put_file("#video-file", png_of(48, 64, "navy")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
        ("eval", "release", "(window.releasePhoto(), true)"), ("idle",),
        ("eval", "source", "[document.querySelector('#video-source img').naturalWidth, document.querySelector('#video-photo-context').hidden]"),
        ("type", "#video-prompt", "свой поворот"), ("click", "#video-go"), ("wait", clips("queued", 1))],
        width=320, height=900, mobile=True, timeout=20000)
    assert not r.get("failed"), r
    assert r["values"]["source"] == [48, True] and r["values"]["loadingFits"]
    assert len(live.gw.video_forms) - v0 == 1
    assert live.gw.video_forms[-1][0]["prompt"] == "свой поворот"
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width, height", [(320, 740), (393, 852)])
def test_photo_to_video_context_by_touch_in_webkit(webkit, live, ready_clips, width, height):
    login = f"vphoto-wk{width}"
    live.add_user(login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    v0 = len(live.gw.video_forms)
    with webkit.new_context(viewport={"width": width, "height": height}, is_mobile=True, has_touch=True,
                            device_scale_factor=3, extra_http_headers={"Cookie": cookie}) as context:
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live.url + "/#/settings")
        until(page, READY)
        page.evaluate(add_gateway()[2])
        page.evaluate(make_images(1)[2])
        page.evaluate("location.hash = '#/photo'")
        until(page, done_cards(1))
        page.locator(NEWEST_DONE).tap()
        page.locator("#viewer .act-animate").tap()
        until(page, "document.querySelector('#video-photo-prompt')?.value === 'кот 0'")
        assert page.evaluate("location.hash") == "#/video"
        assert page.evaluate("document.activeElement?.tagName") != "TEXTAREA"
        page.locator("#video-photo-context summary").tap()
        page.locator("#video-photo-prompt").fill("Кот у окна")
        page.locator("#video-prompt").fill("Поворачивает голову")
        assert page.evaluate(NO_OVERFLOW)
        page.locator("#video-go").tap()
        until(page, clips("done", 1))
        assert not errors
    assert len(live.gw.video_forms) - v0 == 1
    assert live.gw.video_forms[-1][0]["prompt"] == "Описание исходного фото:\nКот у окна\n\nДвижение в видео:\nПоворачивает голову"


@pytest.mark.parametrize("width, height", [(320, 740), (393, 852)])
def test_repeat_and_variants_by_touch_in_webkit(webkit, live, ready_clips, width, height):
    login = f"vrepeat-wk{width}"
    live.add_user(login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    # WebKit не шлёт Secure cookie по HTTP localhost — тестовый контекст получает заголовок своей сессии
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    v0 = len(live.gw.video_forms)
    with webkit.new_context(viewport={"width": width, "height": height}, is_mobile=True, has_touch=True,
                            device_scale_factor=3, extra_http_headers={"Cookie": cookie}) as context:
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live.url + "/#/settings")
        until(page, READY)
        page.evaluate(add_gateway()[2])
        page.evaluate("location.hash = '#/video'")
        until(page, VIDEO_FEED_READY)
        page.set_input_files("#video-file", files=[{"name": "p.png", "mimeType": "image/png",
                                                    "buffer": png_bytes(48, 64, "navy")}])
        until(page, "document.querySelector('#video-source img')?.naturalWidth === 48")
        page.locator("#video-go").tap()
        until(page, clips("done", 1))
        page.locator(f"{CLIP} .card-repeat").tap()
        until(page, clips("done", 2))
        page.locator(f"{CLIP} .card-variants").first.tap()
        page.locator("#video-variants-count").select_option("2")
        page.locator("#video-variants-go").tap()
        until(page, clips("done", 4))
        assert page.evaluate(NO_OVERFLOW)
        assert page.locator("#video-variants-dialog").count() == 0
        assert not errors
    assert len(live.gw.video_forms) - v0 == 4
