"""Опрос идущих заданий не пересоздаёт карточки и строки: фокус на «Отменить» карточки «Фото» и «Видео»
и на строке идущей записи «Расшифровки» и «Озвучки» переживает несколько опросов, нажатие доходит."""
import asyncio
import json
import time

import httpx
import pytest

from fakes import video_object
from test_ui_browser import (CLIP, LISTS, READY, add_gateway, browser, go, known_refusals, live,  # noqa: F401
                             photo_screen, png_of, problems, put_file, seed_list, signin, video_screen)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

#: пауза опроса раздела (ms) — 50 мс; window.polls — сколько GET по адресу pattern ушло
FAST_POLLS = """((ms, pattern) => { window.polls = 0; const wait = window.setTimeout.bind(window); const real = window.fetch;
  window.setTimeout = (f, t, ...a) => wait(f, t === ms ? 50 : t, ...a);
  window.fetch = (url, opts = {}) => {
    if ((opts.method || 'GET') === 'GET' && new RegExp(pattern).test(String(url))) window.polls += 1;
    return real(url, opts); };
  return true; })"""


def fast_polls(ms, pattern):
    return ("eval", "spy", f"{FAST_POLLS}({ms}, {json.dumps(pattern)})")


def hold(selector):
    """Фокус на узел selector; window.held — он, window.mark — опросов к этому мигу."""
    return ("eval", "focused", f"""(() => {{ const b = document.querySelector({json.dumps(selector)}); b.focus();
      window.held = b; window.mark = window.polls; return document.activeElement === b; }})()""")


AFTER_POLLS = "window.polls >= window.mark + 4"
KEPT = "[document.activeElement === window.held, window.held.isConnected]"


@pytest.mark.parametrize("width", [320, 1280])
def test_photo_cancel_keeps_focus_through_polls(browser, live, width):
    import images
    login = f"pfocus{width}"
    live.add_user(login)

    async def held(request):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(j.state == "cancelling" for j in images.JOBS.values()):
            await asyncio.sleep(0.01)
        return await live.gw.builtin(request)

    running = "#photo-feed .photo-card[data-state=working]"
    live.gw.on["/slot/images/generations"] = held
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), fast_polls(1500, r"^/api/images/[0-9a-f]+$"),
            ("type", "#photo-prompt", "долгий рассвет"), ("click", "#photo-go"),
            ("wait", f"document.querySelector('{running} .card-cancel')"),
            hold(f"{running} .card-cancel"), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
            ("click", f"{running} .card-cancel"),
            ("wait", "document.querySelector('#photo-feed .photo-card[data-state=cancelled]')")],
            width=width, mobile=width == 320, timeout=20000)
    finally:
        live.gw.on.pop("/slot/images/generations", None)
    assert not r.get("failed"), r
    assert r["values"]["focused"] is True and r["values"]["kept"] == [True, True]
    assert not problems(r), problems(r)


def test_video_cancel_keeps_focus_through_polls(browser, live):
    login = "vfocus"
    live.add_user(login)
    running_id = f"video_{len(live.gw.video_ids) + 1:024x}"
    progress = iter(range(5, 100))

    async def running(request):
        if request.method != "GET":
            return await live.gw.builtin(request)
        return httpx.Response(200, json=video_object(running_id, status="in_progress", progress=next(progress), seed=7))

    live.gw.on[f"/slot/videos/{running_id}"] = running
    busy = f"{CLIP}[data-state=in_progress]"
    try:
        r = browser.drive(live.url + "/", [*video_screen(login), fast_polls(3000, r"^/api/videos/[0-9a-f]+$"),
            put_file("#video-file", png_of(48, 64, "teal")),
            ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
            ("click", "#video-go"),
            ("wait", f"document.querySelector('{busy} .card-cancel')"),
            hold(f"{busy} .card-cancel"), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
            ("click", f"{busy} .card-cancel"),
            ("wait", f"document.querySelector('{CLIP}[data-state=cancelled]')")],
            width=1280, height=900, timeout=20000)
    finally:
        live.gw.on.pop(f"/slot/videos/{running_id}", None)
    assert not r.get("failed"), r
    assert r["values"]["focused"] is True and r["values"]["kept"] == [True, True]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("section", LISTS)
def test_running_row_keeps_focus_through_list_polls(browser, live, section):
    """Строка идущей записи и «Показать ещё» — те же узлы после опросов первой страницы; нажатие по
    строке выбирает её."""
    login = f"lfocus{section}"
    ids = seed_list(live, section, live.add_user(login), 3, running_newest=True)
    url = LISTS[section][1]
    row = f".tr-item[data-id='{ids[-1]}']"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"),
        fast_polls(3000, rf"^/api/{url}\?"), *go(section),
        ("wait", f"document.querySelector(\"{row}[data-state=working]\")"),
        hold(row), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
        ("eval", "rows", "[...document.querySelectorAll('.tr-list .tr-item')].map(e => e.dataset.id)"),
        ("click", row), ("wait", f"document.querySelector(\"{row}[aria-current=true]\")"),
        ("eval", "still", "window.held.isConnected")], width=1280, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["focused"] is True and v["kept"] == [True, True]
    assert v["rows"] == ids[::-1] and v["still"] is True
    assert not known_refusals(problems(r), 404), problems(r)
