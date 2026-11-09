"""«Сканы»: «Повторить страницу» у нераспознанной страницы и «Распознать заново» у вложения чата (320/1280)."""
import pytest

from test_ui_browser import (LAST_ANSWER, NO_OVERFLOW, READY, add_gateway, attach, browser, go, live, open_new_chat,
                             problems, recognized, scan_file, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

CUT = (200, {"choices": [{"message": {"content": "Обрывок"}, "finish_reason": "length"}]})


@pytest.mark.parametrize("width", [320, 1280])
def test_failed_page_is_recognized_again(browser, live, width):
    from scans import photo
    login = f"retrypage{width}"
    live.add_user(login)
    live.gw.ocr_replies = [CUT]
    try:
        r = browser.drive(live.url + "/", [*recognized(login, [("Лист.png", photo("PNG"))]),
            ("wait", "document.querySelector('#ocr-retry-page')"),
            ("eval", "failed", "document.querySelector('.ocr-text .error').textContent"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#ocr-retry-page"),
            ("wait", "document.querySelector('.ocr-item[data-state=done]') && document.querySelector('.ocr-text.md')"
                     " && !document.querySelector('#ocr-retry-page')"),
            ("eval", "text", "document.querySelector('.ocr-text.md').textContent")],
            width=width, mobile=width == 320)
    finally:
        live.gw.ocr_replies = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["failed"] == "не распознана: ответ модели обрезан — страница слишком плотная"
    assert v["fits"] is True and "Текст страницы." in v["text"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_failed_chat_scan_is_recognized_again_from_the_attachment(browser, live, width):
    import chat
    login = f"rescan{width}"
    live.add_user(login)
    live.gw.ocr_replies = [(500, {"error": {"message": "внутренняя ошибка"}})]
    first = len(live.gw.ocr_requests)
    # отказ первой страницы — когда ход уже ждёт скан; повторное распознавание не держится
    live.gw.ocr_hold = lambda request: request is live.gw.ocr_requests[first] and not chat.STREAMS
    meta = f"{LAST_ANSWER}?.querySelector('.msg-meta')?.textContent"
    rescan = "document.querySelector('#messages .msg.user .att-rescan')"
    phone = width == 320
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile=phone),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            attach(scan_file()), ("wait", "document.querySelector('#composer .att-chip')"),
            ("type", "#chat-input", "О чём документ?"), ("press", "Enter"),
            ("wait", f"{meta}?.startsWith('скан не распознан') && {rescan}"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#messages .msg.user .att-rescan"),
            ("wait", f"!{rescan}"),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", "document.querySelector('#messages .msg.user .att')?.textContent.includes('зн.')"),
            ("eval", "after", f"[!!{rescan}, document.querySelector('#messages .msg.user .att').textContent]")],
            width=width, mobile=phone)
    finally:
        live.gw.ocr_replies = []
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["fits"] is True and v["after"][0] is False and "скан.pdf" in v["after"][1]
