"""«Сканы» — «Скачать ▾ → PDF со слоем» (05:I9): первый раз документ ищет строки как идущий (состояние, «Отменить»),
по окончании PDF скачивается сам; второй раз — сразу, без карты."""
import pytest

from test_ui_browser import API, CLICKS, NO_OVERFLOW, browser, live, problems, recognized  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize("width", [320, 1280])
def test_pdf_with_text_layer_finds_lines_once_then_downloads(browser, live, width):
    import state
    import translate_spot
    from scans import photo
    login = f"ocrlayer{width}"
    live.add_user(login)
    go = f"отпусти{width}.png"                         # переименование отпускает поиск строк
    live.gw.ocr_hold = lambda request: (translate_spot.PROMPT in request.content.decode() and state.DB.execute(
        "SELECT 1 FROM scans WHERE name = ?", (go,)).fetchone() is None)
    busy = "document.querySelector('.ocr-view').textContent.includes('ищу строки для PDF · стр. 1 из 1')"
    try:
        r = browser.drive(live.url + "/", [*recognized(login, [("Лист.png", photo("PNG"))]),
            ("eval", "sid", "document.querySelector('.ocr-item').dataset.id"),
            ("eval", "spy", CLICKS), ("click", "#ocr-download"), ("click", ".ocr-formats [data-format=pdf]"),
            ("wait", f"document.querySelector('#ocr-cancel') && {busy}"),
            ("eval", "fits", NO_OVERFLOW),
            ("eval", "release", f"""(async () => {{ const m = {API}; const id = document.querySelector('.ocr-item').dataset.id;
              await m.api('PATCH', '/api/ocr/' + id, {{ name: '{go}' }}); return true; }})()"""),
            ("wait", "window.clicked.length === 1 && document.querySelector('#ocr-copy')"),
            ("eval", "first", "window.clicked[0]"),
            ("eval", "type", "(async () => (await fetch(window.clicked[0][0])).headers.get('content-type'))()"),
            ("click", "#ocr-download"), ("click", ".ocr-formats [data-format=pdf]"),
            ("wait", "window.clicked.length === 2"), ("eval", "again", "window.clicked[1]")],
            width=width, mobile=width == 320)
    finally:
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    href = [f"/api/ocr/{v['sid']}/export?format=pdf&lang=ru", True]
    assert v["first"] == href and v["again"] == href and v["type"] == "application/pdf" and v["fits"] is True
    assert sum(translate_spot.PROMPT in q.content.decode() for q in live.gw.ocr_requests) >= 1
    assert not problems(r), problems(r)
