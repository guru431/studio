"""Отметка хранимого пересказа в ленте «Чата» (chat-summary.js, спека 2026-11-25-studio-skills-status-local-models, п. 30):
после «Сжать сейчас» лента показывает «Ранняя часть свёрнута в пересказ» сразу за последним пересказанным сообщением,
«Показать» открывает окно с текстом пересказа."""
import pytest

from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, add_gateway, browser, go, live,  # noqa: F401
                             open_new_chat, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

MARK = "document.querySelector('#messages .msg-summary')"
ANSWERS = "document.querySelectorAll('#messages .msg.assistant').length"


@pytest.mark.parametrize("width", [320, 1280])
def test_summary_mark_stands_after_the_summarized_part_and_shows_the_text(browser, live, width):
    login = f"summary-{width}"
    live.add_user(login)
    mobile = width == 320
    live.gw.chat_script = [["Ответ-1"], ["Ответ-2"], ["Итог первого хода."]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Вопрос-1"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {ANSWERS} === 1"),
            ("type", "#chat-input", "Вопрос-2"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {ANSWERS} === 2 && location.hash.split('/')[2]"),
            ("eval", "compact", f"""(async () => {{ const m = {API};
              const out = await m.api('POST', '/api/chats/' + location.hash.split('/')[2] + '/compact');
              return out.summary_upto; }})()"""),
            ("eval", "reload", "location.reload()"),
            ("wait", f"{READY} && {MARK} && {ANSWERS} === 2"),
            ("eval", "order", """[...document.querySelector('#messages > .feed').children].map(n =>
              n.classList.contains('msg-summary') ? 'mark' : n.dataset.id ? 'msg' : n.className)"""),
            ("eval", "before", f"{MARK}.previousElementSibling.dataset.id"),
            ("eval", "fits", f"{NO_OVERFLOW} && {MARK}.getBoundingClientRect().right <= innerWidth"),
            ("click", "#messages .msg-summary-show"),
            ("wait", "document.querySelector('#chat-summary-dialog[open] .summary-text')"),
            ("eval", "text", "document.querySelector('#chat-summary-dialog .summary-text').textContent")],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r.get("failed"), r
    values = r["values"]
    assert values["order"] == ["msg", "msg", "mark", "msg", "msg"]
    assert values["before"] == values["compact"]           # отметка — сразу за последним пересказанным
    assert values["fits"] is True              # NO_OVERFLOW и правый край .msg-summary не дальше innerWidth
    assert values["text"] == "Итог первого хода." and not problems(r), r.get("log")
