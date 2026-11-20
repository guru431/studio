"""«Сканы» — вид документа: опрос не пересобирает страницу (05:F14)."""
import pytest

from test_ui_browser import API, READY, add_gateway, browser, go, live, problems, put_files, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


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
