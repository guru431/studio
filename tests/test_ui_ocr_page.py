"""«Сканы» — вид документа: опрос не пересобирает страницу (05:F14), нераспознанные страницы видны и
повторяются разом (05:F6), распознанное документа с ошибкой выгружается (05:F5), выбор движка (05:F11)."""
import json

import pytest

from test_ui_browser import (API, NO_OVERFLOW, READY, TL_DONE, add_gateway, add_translator, browser, go, live, problems,
                             put_files, recognized, signin, translator)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

CUT = (200, {"choices": [{"message": {"content": "Обрывок"}, "finish_reason": "length"}]})
PAGE_NO = "document.querySelector('#ocr-page')?.textContent"


def test_translate_reuses_the_scan_and_opens_the_translation(browser, live, translator):
    from scans import photo

    live.add_user("ocrtranslate")
    translator.chat_script = [["Übersetzte Seite"]]
    before = len(live.gw.ocr_requests)
    r = browser.drive(live.url + "/", [*signin("ocrtranslate"), ("wait", READY), add_translator(), *go("ocr"),
        ("wait", "document.querySelector('#ocr-add input')"), put_files("#ocr-add input", [("Лист.png", photo("PNG"))]),
        ("wait", "document.querySelector('#ocr-translate')"), ("click", "#ocr-translate"),
        ("wait", "document.querySelector('#ocr-translate-target option[value=de]')"),
        ("eval", "target", "document.querySelector('#ocr-translate-target').value = 'de'"),
        ("click", "#ocr-translate-confirm"),
        ("wait", f"{TL_DONE} && document.querySelector('#tl-output')?.textContent.includes('Übersetzte Seite')"),
        ("eval", "targetAfter", "document.querySelector('#tl-target').value"),
        ("click", "#tl-scan"), ("wait", "document.querySelector('#ocr-translate')"),
        ("eval", "reload", "window.beforeReload = true; location.reload()"),
        ("wait", "!window.beforeReload && document.querySelector('#ocr-translate')"),
        *go("translate"), ("wait", "document.querySelector('#tl-output')?.textContent.includes('Übersetzte Seite')"),
        ("click", "#tl-new"), *go("ocr"), ("wait", "document.querySelector('#ocr-translate')"),
        *go("translate"), ("wait", f"{TL_DONE} && document.querySelector('#tl-output')?.textContent === ''")],
        width=320, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["targetAfter"] == "de"
    assert len(live.gw.ocr_requests) == before + 1 and not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_failed_pages_of_a_done_document_are_shown_and_recognized_again(browser, live, width):
    from scans import photo
    login = f"ocrmiss{width}"
    live.add_user(login)
    live.gw.ocr_replies = [CUT, (200, live.gw.ocr_reply), CUT]
    try:
        r = browser.drive(live.url + "/", [*recognized(login, [(f"{n}.png", photo("PNG")) for n in (1, 2, 3)]),
            ("wait", "document.querySelector('.ocr-failed')"),
            ("eval", "row", "document.querySelector('.ocr-item .tr-state').textContent"),
            ("eval", "line", "document.querySelector('.ocr-failed span').textContent"),
            ("eval", "acts", "['#ocr-copy', '#ocr-retry'].map(s => !!document.querySelector(s))"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#ocr-next-failed"), ("wait", f"{PAGE_NO} === 'страница 3 из 3'"),
            ("click", "#ocr-next-failed"), ("wait", f"{PAGE_NO} === 'страница 1 из 3'"),
            ("click", "#ocr-retry"),
            ("wait", "document.querySelector('.ocr-item[data-state=done]') && !document.querySelector('.ocr-failed')"
                     " && !document.querySelector('#ocr-retry') && document.querySelector('.ocr-text.md')"),
            ("eval", "after", "document.querySelector('.ocr-item .tr-state').textContent")],
            width=width, mobile=width == 320)
    finally:
        live.gw.ocr_replies = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["row"] == "3 стр. · готово · не распознано: 2" and v["line"] == "не распознано страниц: 2 из 3"
    assert v["acts"] == [True, True] and v["fits"] is True and v["after"] == "3 стр. · готово"
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_engine_is_chosen_for_upload_and_for_recognizing_again(browser, live, width):
    """05:F11: два движка «Сканов» — выбор у «Загрузить скан», в шапке документа — чем он распознан;
    «Распознать заново» другим движком — новый документ, прежний остаётся."""
    from fakes import TOKEN
    from scans import photo
    login = f"ocreng{width}"
    live.add_user(login)
    before = live.gw.ocr_reply
    live.gw.ocr_reply = {**before, "markdown": "Текст s06"}
    first = len(live.gw.ocr_requests)
    meta = "document.querySelector('.ocr-meta')?.textContent"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
            ("eval", "s06", f"""(async () => {{ const m = {API};
              await m.api('POST', '/api/connections', {{ name: 's06', kind: 'openai', s06: true,
                base_url: 'http://10.77.1.15:4000', token: {TOKEN!r}, sections: ['ocr'] }}); return true; }})()"""),
            *go("ocr"), ("wait", "document.querySelector('.split-toolbar .pick:not([hidden]) #ocr-model option:nth-child(2)')"),
            ("eval", "options", "[...document.querySelectorAll('#ocr-model option')].map(o => o.textContent)"),
            ("eval", "fits", NO_OVERFLOW),
            ("eval", "pick", "(() => { const s = document.querySelector('#ocr-model'); s.selectedIndex ="
                             " [...s.options].findIndex(o => o.textContent.includes('ocr-ru')); return s.value; })()"),
            put_files("#ocr-add input", [("Лист.png", photo("PNG"))]),
            ("wait", "document.querySelector('.ocr-item[data-state=done]') && document.querySelector('.ocr-text.md')"),
            ("wait", f"{meta}?.endsWith('s06 · ocr-ru')"),
            ("click", "#ocr-retry"), ("wait", "document.querySelector('#ocr-retry-dialog')?.open"),
            ("eval", "chosen", "document.querySelector('#ocr-retry-model').selectedOptions[0].textContent"),
            ("eval", "other", "(() => { const s = document.querySelector('#ocr-retry-model'); s.selectedIndex ="
                              " [...s.options].findIndex(o => o.textContent.includes('HunyuanOCR')); return true; })()"),
            ("click", "#ocr-retry-confirm"),
            ("wait", f"!document.querySelector('#ocr-retry-dialog') && {meta}?.endsWith('HunyuanOCR')"
                     " && document.querySelectorAll('.ocr-item[data-state=done]').length === 2"),
            ("eval", "text", "document.querySelector('.ocr-text.md').textContent")],
            width=width, mobile=width == 320)
    finally:
        live.gw.ocr_reply = before
    assert not r.get("failed"), r
    v = r["values"]
    assert v["options"] == ["Шлюз · HunyuanOCR", "s06 · ocr-ru"]  # подписи — models.js and v["fits"] is True
    assert v["chosen"] == "s06 · ocr-ru" and "Текст s06" not in v["text"]
    assert [json.loads(q.content)["model"] for q in live.gw.ocr_requests[first:]] == ["ocr-ru", "tencent/HunyuanOCR"]
    assert not problems(r), problems(r)


@pytest.fixture
def large_rotated_photo():
    import io

    from PIL import Image

    noise = Image.effect_noise((4400, 1000), 64).convert("RGB")
    exif = Image.Exif()
    exif[0x0112] = 6
    out = io.BytesIO()
    noise.save(out, "JPEG", quality=95, exif=exif)
    return out.getvalue()


def test_big_photos_are_shrunk_and_turned_before_the_upload(browser, live, large_rotated_photo):
    """05:F15: фото длиннее 2200 точек уходит JPEG с длинной стороной 2200, уже повёрнутым по EXIF (тег 6 —
    поворот на 90°) и без метаданных; фото не длиннее — как есть."""
    import io

    from PIL import Image

    import media
    import state
    from scans import photo
    big = large_rotated_photo
    live.add_user("ocrshrink")
    r = browser.drive(live.url + "/", [*signin("ocrshrink"), ("wait", READY), add_gateway(),
        *go("ocr"), ("wait", "document.querySelector('#ocr-add input')"),
        put_files("#ocr-add input", [("IMG_1.jpg", big)], mime="image/jpeg"),
        ("wait", "document.querySelector('.ocr-item[data-state=done]')"),
        put_files("#ocr-add input", [("Лист.png", photo("PNG"))]),
        ("wait", "document.querySelectorAll('.ocr-item[data-state=done]').length === 2")])
    assert not r.get("failed"), r
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'ocrshrink'").fetchone()[0]
    rows = state.DB.execute("SELECT s.name, p.media_id, m.mime FROM scans s JOIN scan_pages p ON p.scan_id = s.id"
                            " JOIN media m ON m.id = p.media_id WHERE s.user_id = ? ORDER BY s.created", (uid,)).fetchall()
    assert [(row["name"], row["mime"]) for row in rows] == [("IMG_1.jpg", "image/jpeg"), ("Лист.png", "image/png")]
    shrunk = media.path(uid, rows[0]["media_id"]).read_bytes()
    with Image.open(io.BytesIO(shrunk)) as image:
        assert image.size == (500, 2200) and image.getexif().get(0x0112) is None
    assert len(shrunk) < len(big)
    assert media.path(uid, rows[1]["media_id"]).read_bytes() == photo("PNG")
    assert not problems(r), problems(r)


def test_photo_inputs_shrink_and_turn_the_selected_photo(browser, live, large_rotated_photo):
    live.add_user("shrinkphoto")
    big = [("IMG_1.jpg", large_rotated_photo)]
    r = browser.drive(live.url + "/", [*signin("shrinkphoto"), ("wait", READY), add_gateway(), *go("photo"),
        ("wait", "document.querySelector('#photo-file')"), put_files("#photo-file", big, mime="image/jpeg"),
        ("wait", "document.querySelector('#photo-source img')?.naturalHeight === 2048"),
        ("wait", "!document.querySelector('#describe-file')?.disabled"),
        put_files("#describe-file", big, mime="image/jpeg"),
        ("wait", "document.querySelector('#describe-photo')?.naturalHeight === 2048")])
    assert not r.get("failed"), r
    assert not problems(r), problems(r)


def test_translate_shrinks_and_turns_the_photo_before_upload(browser, live, large_rotated_photo, translator):
    from test_ui_browser import translating

    live.add_user("shrinktranslate")
    r = browser.drive(live.url + "/", [*translating("shrinktranslate"),
        ("eval", "observe", """(() => { const send = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.send = function(data) {
            const file = data?.get?.('file');
            if (file instanceof File) window.sentPhoto = createImageBitmap(file).then(img => {
              const dims = [img.width, img.height, file.size]; img.close(); return dims;
            });
            return send.call(this, data);
          }; return true; })()"""),
        put_files("#tl-photo-input", [("IMG_1.jpg", large_rotated_photo)], mime="image/jpeg"),
        ("wait", "document.querySelector('#tl-chip:not([hidden])') && !document.querySelector('#tl-go').disabled"),
        ("click", "#tl-go"), ("wait", "window.sentPhoto"), ("eval", "sentDims", "window.sentPhoto")])
    assert not r.get("failed"), r
    width, height, size = r["values"]["sentDims"]
    assert (width, height) == (500, 2200) and size < len(large_rotated_photo)
    assert not problems(r), problems(r)


def test_a_failed_document_offers_what_was_recognized(browser, live):
    from scans import photo
    live.add_user("ocrhalf")
    live.gw.ocr_replies = [(200, live.gw.ocr_reply), (500, {"error": {"message": "внутренняя ошибка"}})]
    try:
        r = browser.drive(live.url + "/", [*signin("ocrhalf"), ("wait", READY), add_gateway(),
            *go("ocr"), ("wait", "document.querySelector('#ocr-add input')"),
            put_files("#ocr-add input", [("1.png", photo("PNG")), ("2.png", photo("PNG"))]),
            ("wait", "document.querySelector('.ocr-item[data-state=error]') && document.querySelector('#ocr-copy')"),
            ("eval", "acts", "['#ocr-copy', '#ocr-download', '#ocr-chat', '#ocr-retry'].map(s => !!document.querySelector(s))")])
    finally:
        live.gw.ocr_replies = []
    assert not r.get("failed"), r
    assert r["values"]["acts"] == [True, True, True, True]
    assert not problems(r), problems(r)


def test_polling_keeps_the_shown_page_nodes(browser, live):
    """Первая страница готова, вторая распознаётся: читаемая страница — те же узлы и после итога документа
    (выделение текста и картинка не сбрасываются), а кнопки готового документа появляются."""
    import state
    from scans import photo
    live.add_user("ocrkeep")
    first = len(live.gw.ocr_requests)
    live.gw.ocr_hold = lambda request: (len(live.gw.ocr_requests) > first + 1 and request is live.gw.ocr_requests[first + 1]
                                        and state.DB.execute("SELECT 1 FROM scans WHERE name LIKE 'отпусти%'").fetchone() is None)
    try:
        r = browser.drive(live.url + "/", [*signin("ocrkeep"), ("wait", READY), add_gateway(),
            *go("ocr"), ("wait", "document.querySelector('#ocr-add input')"),
            put_files("#ocr-add input", [("Лист 1.png", photo("PNG")), ("Лист 2.png", photo("PNG"))]),
            ("wait", "document.querySelector('#ocr-cancel') && document.querySelector('.ocr-text.md')"
                     " && document.querySelector('img.ocr-scan')?.complete"),
            ("eval", "kept", "(() => { window.kept = [document.querySelector('.ocr-two'), document.querySelector('img.ocr-scan'),"
                             " document.querySelector('.ocr-text'), document.querySelector('#ocr-rename')]; return true; })()"),
            ("eval", "release", f"""(async () => {{ const m = {API}; const id = document.querySelector('.ocr-item').dataset.id;
              await m.api('PATCH', '/api/ocr/' + id, {{ name: 'отпусти' }}); return true; }})()"""),
            ("wait", "document.querySelector('.ocr-item[data-state=done]') && document.querySelector('#ocr-copy')"),
            ("eval", "same", "[document.querySelector('.ocr-two'), document.querySelector('img.ocr-scan'),"
                             " document.querySelector('.ocr-text'), document.querySelector('#ocr-rename')]"
                             ".map((node, i) => node === window.kept[i])")])
    finally:
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    assert r["values"]["same"] == [True, True, True, False]      # кнопки — заново: появились «Копировать» и др.
    assert not problems(r), problems(r)
