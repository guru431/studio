"""«Фото» и «Видео» в браузере: чип модели в шапке ленты, «Неудачные» режима выделения, «Ещё варианты» у
картинки (неудачной карточки) и «В поле» просмотра картинки и ролика (Chromium 320/1280)."""
import time

import pytest

from test_ui_browser import (API, CLIP, FEED_READY, NEWEST_DONE, NO_OVERFLOW, READY, SPY, add_gateway, browser,  # noqa: F401
                             clips, done_cards, go, is_open, live, live_sql, make_images, photo_screen, png_of, problems,
                             put_file, ready_clips, set_value, signin, video_screen, viewer_details)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

CARDS = "[...document.querySelectorAll('#photo-feed .photo-card')].map(c => c.dataset.state)"
#: правка последней готовой картинки через API: её исходник — файл истории
EDIT = f"""(async () => {{ const m = {API};
  const [first] = (await m.api('GET', '/api/images?limit=5')).items;
  const job = await m.api('POST', '/api/images', {{ prompt: 'кот правка', size: 'square', model: 'snofs',
    source_media_id: first.media_id, strength: 0.4 }});
  for (let t = 0; t < 200; t++) {{
    if ((await m.api('GET', '/api/images/' + job.id)).state === 'done') return first.media_id;
    await new Promise(r => setTimeout(r, 30));
  }} }})()"""


def chip_pick(section, model):
    return [("click", f"#{section}-model-filter"), ("click", f"#{section}-model-filter-menu [data-model='{model}']")]


@pytest.mark.parametrize("width", [320, 1280])
def test_photo_viewer_uses_the_result_as_source_for_a_new_prompt(browser, live, width):
    login = f"photosource{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("eval", "spy", SPY),
        ("type", "#photo-prompt", "Добавь снег"),
        put_file("#photo-file", png_of(40, 30, "blue")),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth === 40"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("eval", "source", "document.querySelector('#viewer .viewer-stage img').getAttribute('src').split('/').at(-1)"),
        *viewer_details(width), ("click", "#viewer .act-use-source"),
        ("wait", "!document.querySelector('#viewer') && document.querySelector('#photo-source img')?.naturalWidth > 0"),
        ("eval", "composer", "[location.hash, document.activeElement.id, document.querySelector('#photo-prompt').value,"
         " document.querySelector('#photo-source img').getAttribute('src'), window.sentForms.length]"),
        ("click", "#photo-go"), ("wait", done_cards(2)),
        ("eval", "sent", "window.sentForms.map(x => x.form)")], width=width, height=900, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["composer"] == ["#/photo", "photo-prompt", "Добавь снег", f"/api/media/{v['source']}?thumb=1", 0]
    [sent] = v["sent"]
    assert sent["prompt"] == "Добавь снег" and sent["source_media_id"] == v["source"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_photo_model_chip_failed_pick_more_variants_and_to_field(browser, live, width):
    login = f"phtools{width}"
    uid = live.add_user(login, views={"photo": "tiles"})
    old = time.time() - 100
    for n, (model, state_) in enumerate([("flux", "error"), ("snofs", "cancelled")]):
        live_sql(live, "INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                       " VALUES (?, ?, 'generation', ?, 'сломанный кот', '832x1216', 5, ?, 'ошибка шлюза', ?)",
                 (f"{n:032x}{width}", uid, model, state_, old - n))
    sent = "window.sentForms.at(-1)?.form"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), make_images(1),
        ("eval", "source", EDIT), *go("photo"), ("wait", FEED_READY), ("eval", "spy", SPY),
        ("wait", "!document.querySelector('.feed-model').hidden"),
        *chip_pick("photo", "flux"), ("wait", f"{CARDS}.join() === 'error'"),
        ("eval", "chip", "document.querySelector('#photo-model-filter').textContent"),
        *chip_pick("photo", ""), ("wait", f"{CARDS}.length === 4"),
        # «Неудачные»: ошибка и отмена среди подгруженных; повторное нажатие их снимает
        ("click", "#photo-select"), ("click", "#photo-select-failed"),
        ("eval", "picked", "[...document.querySelectorAll('#photo-feed .card[data-selected=true]')].map(c => c.dataset.state)"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#photo-select-failed"),
        ("eval", "unpicked", "document.querySelectorAll('#photo-feed .card[data-selected=true]').length"),
        ("click", "#photo-select-exit"),
        # «В поле» правки: промпт, формат, сила и исходник тем же файлом истории
        ("type", "#photo-prompt", "свой текст"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), *viewer_details(width), ("click", "#viewer .act-to-field"),
        ("wait", is_open("#photo-field-dialog")), ("click", "#photo-field-replace"),
        ("wait", f"!{is_open('#viewer')}"),
        ("eval", "prompt", "document.querySelector('#photo-prompt').value"),
        ("eval", "size", "document.querySelector('#chip-size').textContent"),
        ("eval", "strength", "document.querySelector('#photo-strength').value"),
        ("eval", "shown", "document.querySelector('#photo-source img').getAttribute('src')"),
        ("click", "#photo-go"), ("wait", done_cards(3)),
        ("eval", "toField", sent),
        # «Ещё варианты» неудачной карточки: её промпт, модель и размер, seed — новые
        ("click", "#photo-feed .photo-card[data-state=error] .card-variants"),
        ("wait", is_open("#photo-variants-dialog")), set_value("#photo-variants-count", "2"),
        ("click", "#photo-variants-go"), ("wait", f"{done_cards(5)} && !{is_open('#photo-variants-dialog')}"),
        ("eval", "variants", sent),
        ("eval", "error", "document.querySelector('#photo-error').textContent")],
        width=width, height=900, mobile=width == 320, timeout=30000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["chip"].startswith("FLUX") and v["picked"] == ["error", "cancelled"] and v["unpicked"] == 0 and v["fits"]
    assert v["prompt"] == "кот правка" and v["size"].startswith("Квадрат") and v["strength"] == "0.4"
    assert v["source"] in v["shown"]
    assert v["toField"] == {"prompt": "кот правка", "size": "square", "model": "snofs", "connection_id": v["toField"]["connection_id"],
                            "source_media_id": v["source"], "strength": 0.4}
    assert v["variants"] == {"prompt": "сломанный кот", "model": "flux", "size": "portrait",
                             "series": {"count": 2, "vary": "seed"}}
    assert v["error"] == ""
    assert not problems(r), problems(r)


def test_video_to_field_keeps_the_frame_and_settings(browser, live, ready_clips):
    login = "vtofield"
    live.add_user(login)
    v0 = len(live.gw.video_forms)
    r = browser.drive(live.url + "/", [*video_screen(login),
        put_file("#video-file", png_of(48, 64, "navy")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
        ("type", "#video-prompt", "камера отъезжает"), set_value("#video-seconds", "7"),
        ("click", "#chip-smooth"), ("click", "#video-go"), ("wait", clips("done", 1)),
        ("click", "#video-source-clear"), set_value("#video-seconds", "3"), ("click", "#chip-smooth"),
        ("click", f"{CLIP} .card-viewer"), ("wait", is_open("#video-viewer")),
        ("click", "#video-viewer .act-to-field"), ("wait", f"!{is_open('#video-viewer')}"),
        ("eval", "prompt", "document.querySelector('#video-prompt').value"),
        ("eval", "seconds", "document.querySelector('#video-seconds').value"),
        ("eval", "smooth", "document.querySelector('#chip-smooth').getAttribute('aria-pressed')"),
        ("eval", "source", "document.querySelector('.video-source-name').textContent"),
        ("click", "#video-go"), ("wait", clips("done", 2))],
        width=1280, height=900, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["prompt"] == "камера отъезжает" and v["seconds"] == "7" and v["smooth"] == "false"
    assert v["source"] == "Фото из ленты"
    first, again = live.gw.video_forms[v0:]
    assert first[1] == again[1]                                     # тот же кадр
    assert {k: x for k, x in again[0].items() if k != "seed"} == {k: x for k, x in first[0].items() if k != "seed"}
    assert not problems(r), problems(r)
