"""Формулы на странице «Сканов» (идея 05:I8): `$…$` и `$$…$$` распознанного текста рисует KaTeX (vendor/katex),
деньги «$5 и $10» остаются текстом, ошибочная формула — текстом с разделителями; «Копировать» кладёт Markdown
текстом и HTML с формулами MathML."""
import pytest

from test_ui_browser import NO_OVERFLOW, browser, live, problems, recognized  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PAGE = "Энергия $E=mc^2$, цена $5 и $10.\n\n$$\\frac{a}{b} = \\sum_{i=1}^{n} x_i$$\n\nОшибка $\\frac{a$ тут."
TEXT = "document.querySelector('.ocr-text.md')"
CLIP = """(() => { navigator.clipboard.write = async (items) => {
  window.clipHtml = await (await items[0].getType('text/html')).text();
  window.clipText = await (await items[0].getType('text/plain')).text(); }; return true; })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_formulas_are_drawn_and_copied_as_mathml(browser, live, width):
    from scans import photo
    login = f"ocrmath{width}"
    live.add_user(login)
    before = live.gw.ocr_reply
    live.gw.ocr_reply = {"choices": [{"message": {"content": PAGE}, "finish_reason": "stop"}]}
    try:
        r = browser.drive(live.url + "/", [*recognized(login, [("Формулы.png", photo("PNG"))]),
            ("wait", f"{TEXT}.querySelector('.katex-display') && !{TEXT}.querySelector('code[class^=language-math]')"),
            ("eval", "drawn", f"""[{TEXT}.querySelectorAll('.katex').length,
              {TEXT}.querySelectorAll('.katex-display').length, {TEXT}.textContent]"""),
            ("eval", "fonts", "document.fonts.ready.then(() => [...document.fonts].some(f => f.family.includes('KaTeX')"
                              " && f.status === 'loaded'))"),
            ("eval", "fits", NO_OVERFLOW),
            ("eval", "spy", CLIP), ("click", "#ocr-copy"), ("wait", "window.clipHtml !== undefined"),
            ("eval", "clip", "[window.clipText, window.clipHtml]")], width=width, mobile=width < 1000)
    finally:
        live.gw.ocr_reply = before
    assert not r.get("failed"), r
    count, display, text = r["values"]["drawn"]
    assert count == 2 and display == 1
    assert "$5 и $10" in text and "$\\frac{a$" in text
    assert r["values"]["fonts"] is True and r["values"]["fits"] is True
    plain, html = r["values"]["clip"]
    assert "$E=mc^2$" in plain and "<math" in html and "katex-html" not in html and "<button" not in html
    assert not problems(r), r.get("log")
