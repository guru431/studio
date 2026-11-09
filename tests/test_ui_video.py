"""Раздел «Видео» в браузере: опрос идущего ролика и отмена (одна цепочка опросов)."""
import httpx
import pytest

from fakes import video_object
from test_ui_browser import (CLIP, READY, browser, clips, live, png_of, problems, put_file,  # noqa: F401 (фикстуры)
                             video_screen)

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
