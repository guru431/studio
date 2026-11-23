"""Варианты ответа в «Чате» (chat.js, `messages.unchosen`): «Ответить заново» оставляет прежний ответ вариантом,
переключатель «‹ 1 / 2 ›», «Сравнить» колонками на широком экране; новый ход фиксирует выбор (Playwright, Chromium)."""
import pytest

from test_ui_browser import live  # noqa: F401
from test_ui_send_reliability import LAST, add_cloud, chromium, driver, opened  # noqa: F401
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize("width", [320, 1280])
def test_regenerate_keeps_both_answers_and_switches_between_them(chromium, live, width):
    login = f"variants-{width}"
    live.add_user(login)
    saved = """async () => { const m = await import('/static/js/api.js');
      return (await m.api('GET', '/api/chats/' + location.hash.split('/')[2])).messages.map((x) => x.content); }"""
    try:
        with opened(chromium, live, login, width) as page:
            add_cloud(page, live)
            answer = page.locator(LAST)
            live.cloud.chat_reply = ["Первый ", "вариант."]
            page.locator("#chat-input").fill("Привет")
            page.locator("#send").click()
            pw.expect(answer.locator(".md")).to_have_text("Первый вариант.")
            pw.expect(answer.locator(".msg-variant")).to_have_count(0)

            live.cloud.chat_reply = ["Второй ", "вариант."]
            answer.locator(".msg-retry").click()
            pw.expect(answer.locator(".md")).to_have_text("Второй вариант.")
            pw.expect(answer.locator(".msg-variant-at")).to_have_text("2 / 2")
            pw.expect(answer.locator(".msg-model")).to_have_text("cloud-chat")

            answer.locator(".msg-variant-prev").click()
            pw.expect(answer.locator(".md")).to_have_text("Первый вариант.")
            pw.expect(answer.locator(".msg-variant-at")).to_have_text("1 / 2")
            pw.expect(answer.locator(".msg-variant-prev")).to_be_focused()
            assert page.evaluate(saved) == ["Привет", "Первый вариант."]

            toggle = answer.locator(".msg-compare-toggle")
            if width < 1024:
                pw.expect(toggle).to_be_hidden()
            else:
                toggle.click()
                columns = answer.locator(".msg-compare-item")
                pw.expect(columns).to_have_count(2)
                pw.expect(columns.nth(0)).to_contain_text("Вариант 1 · cloud-chat")
                pw.expect(columns.nth(1)).to_contain_text("Второй вариант.")
                pw.expect(answer.locator(".msg-body > .md")).to_be_hidden()
                columns.nth(1).locator(".msg-compare-choose").click()
                pw.expect(answer.locator(".msg-variant-at")).to_have_text("2 / 2")
                pw.expect(answer.locator(".msg-compare-item.chosen")).to_contain_text("Второй вариант.")
                answer.locator(".msg-compare-toggle").click()
                pw.expect(answer.locator(".msg-compare-item")).to_have_count(0)
                pw.expect(answer.locator(".msg-body > .md")).to_have_text("Второй вариант.")
            chosen = "Второй вариант." if width >= 1024 else "Первый вариант."

            live.cloud.chat_reply = ["Дальше."]
            page.locator("#chat-input").fill("Ещё")
            page.locator("#send").click()
            pw.expect(answer.locator(".md")).to_have_text("Дальше.")
            pw.expect(page.locator("#messages .msg-variant")).to_have_count(0)
            assert page.evaluate(saved) == ["Привет", chosen, "Ещё", "Дальше."]
            assert live.cloud.chat_requests[-1]["messages"][-2] == {"role": "assistant", "content": chosen}
    finally:
        live.cloud.chat_reply = ["Облако ", "на связи."]
