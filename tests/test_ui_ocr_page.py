"""«Сканы» — вид документа: опрос не пересобирает страницу (05:F14), нераспознанные страницы видны и
повторяются разом (05:F6), распознанное документа с ошибкой выгружается (05:F5)."""
import pytest

from test_ui_browser import (API, NO_OVERFLOW, READY, add_gateway, browser, go, live, problems, put_files, recognized,
                             signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

CUT = (200, {"choices": [{"message": {"content": "Обрывок"}, "finish_reason": "length"}]})
PAGE_NO = "document.querySelector('#ocr-page')?.textContent"


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
