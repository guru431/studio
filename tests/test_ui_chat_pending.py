"""Пауза ответа чата на странице (chat-pending.js, спека 2026-11-21-studio-source-capabilities-design, п. 17): вопрос
модели `ask_user` — карточка под ответом с кнопками вариантов и полем «Свой ответ»; ответ кнопкой продолжает тот же
пузырь, модель получает выбранный вариант. Инструмент MCP «с подтверждением» — карточка «Запустить / Отмена»."""
import json

import pytest

import chat_pending
import mcp_client
from test_mcp import TOKEN as MCP_KEY
from test_mcp_dialog import HOST, server, tool
from test_ui_browser import (ANSWERED, LAST_ANSWER, NO_OVERFLOW, READY, add_gateway, browser, go, live,  # noqa: F401
                             open_new_chat, problems, set_value, signin)
from test_ui_chat_mcp import admin_client

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

CARD = f"{LAST_ANSWER}?.querySelector('.msg-pending-card')"
ASK = json.dumps({"question": "Какой цвет?", "options": ["Красный", "Синий"]}, ensure_ascii=False)


@pytest.mark.parametrize("width", [320, 1280])
def test_question_card_answer_continues_the_same_bubble(browser, live, width):
    login = f"pending-{width}"
    live.add_user(login)
    mobile = width == 320
    live.gw.chat_script = [{"text": ["Уточню."], "tool_calls": [("ask_user", ASK)]}, ["Выбран красный."]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Нарисуй"), ("press", "Enter"),
            ("wait", f"{CARD} && !document.querySelector('#send').hidden && {LAST_ANSWER}.dataset.id"),
            ("eval", "card", f"""[{CARD}.dataset.kind, {CARD}.querySelector('.msg-pending-text').textContent,
              [...{CARD}.querySelectorAll('.pending-option')].map(b => b.textContent),
              !!{CARD}.querySelector('.pending-text'), !!{CARD}.querySelector('.pending-refuse'),
              {LAST_ANSWER}.dataset.id, {LAST_ANSWER}.querySelector('.md').textContent.trim()]"""),
            ("eval", "fits", f"{NO_OVERFLOW} && {CARD}.getBoundingClientRect().right <= innerWidth"),
            ("eval", "reload", "location.reload()"),
            ("wait", f"{READY} && {CARD}"),
            ("eval", "duplicated", f"{LAST_ANSWER}.querySelector('.web-ask')?.textContent.includes('Какой цвет?') || false"),
            ("click", ".msg-pending-card .pending-option"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.md').textContent.includes('Выбран красный')"),
            ("eval", "after", f"""[document.querySelectorAll('#messages .msg.assistant').length, {LAST_ANSWER}.dataset.id,
              {LAST_ANSWER}.querySelector('.md').textContent.trim(), !!{LAST_ANSWER}.querySelector('.msg-pending-card')]""")],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    kind, question, options, own, skip, mid, before = v["card"]
    assert (kind, question, options, own, skip, before) == ("ask", "Какой цвет?", ["Красный", "Синий"], True, True,
                                                            "Уточню.")
    assert v["fits"] is True and v["duplicated"] is False
    count, after_id, text, card_left = v["after"]
    assert count == 1 and after_id == mid and not card_left
    assert text.startswith("Уточню.") and text.endswith("Выбран красный.")
    assert json.loads(live.gw.chat_requests[-1]["messages"][-1]["content"]) == {"question": "Какой цвет?",
                                                                                "answer": "Красный"}
    assert not problems(r), r.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_confirm_card_refuse_continues_and_run_calls_once(browser, live, width):
    """Карточка «Запустить инструмент?» показывает сервер, инструмент и аргументы; «Отмена» продолжает тот же пузырь
    отказом без вызова MCP; «Запустить» вызывает инструмент один раз."""
    login, name, path = f"confirm-{width}", f"Подтверждение {width}", f"/mcp-confirm-{width}"
    calls = server(live, path, tools=[tool("lookup"), tool("translate", _meta={"studio/confirm": True})])
    client = admin_client(live)
    response = client.post("/api/admin/shared/mcp", json={"name": name, "url": HOST + path, "token": MCP_KEY})
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    translate = mcp_client.tool_name(name, "translate")
    live.add_user(login)
    mobile = width == 320
    args = json.dumps({"phrase": "hello"})
    live.gw.chat_script = [{"text": ["Сейчас переведу."], "tool_calls": [(translate, args)]}, ["Не запускаю."],
                           {"text": ["Ещё раз."], "tool_calls": [(translate, args)]}, ["Готово."]]
    md = f"{LAST_ANSWER}.querySelector('.md').textContent"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Переведи"), ("press", "Enter"),
            ("wait", f"{CARD} && !document.querySelector('#send').hidden && {LAST_ANSWER}.dataset.id"),
            ("eval", "card", f"""[{CARD}.dataset.kind, {CARD}.querySelector('.msg-pending-title').textContent,
              {CARD}.querySelector('.msg-pending-tool').textContent,
              [...{CARD}.querySelectorAll('.msg-pending-args dt, .msg-pending-args dd')].map(n => n.textContent),
              !!{CARD}.querySelector('.pending-run'), !!{CARD}.querySelector('.pending-refuse'),
              {LAST_ANSWER}.dataset.id]"""),
            ("eval", "fits", f"{NO_OVERFLOW} && {CARD}.getBoundingClientRect().right <= innerWidth"),
            ("click", ".msg-pending-card .pending-refuse"),
            ("wait", f"{ANSWERED} && {md}.includes('Не запускаю')"),
            ("eval", "refused", f"""[document.querySelectorAll('#messages .msg.assistant').length,
              {LAST_ANSWER}.dataset.id, {md}.trim(), !!{LAST_ANSWER}.querySelector('.msg-pending-card')]"""),
            ("type", "#chat-input", "Всё-таки переведи"), ("press", "Enter"),
            ("wait", f"{CARD} && !document.querySelector('#send').hidden && {md}.includes('Ещё раз')"),
            ("click", ".msg-pending-card .pending-run"),
            ("wait", f"{ANSWERED} && {md}.includes('Готово')"),
            ("eval", "ran", f"""[document.querySelectorAll('#messages .msg.assistant').length, {md}.trim(),
              !!{LAST_ANSWER}.querySelector('.msg-pending-card')]""")],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
        client.delete(f"/api/admin/shared/mcp/{sid}")
        client.close()
    assert not r.get("failed"), r
    v = r["values"]
    kind, title, where, shown, run, refuse, mid = v["card"]
    assert (kind, title, where, shown, run, refuse) == ("confirm", "Запустить инструмент?", f"{name} · translate",
                                                        ["phrase", "hello"], True, True)
    assert v["fits"] is True
    count, after_id, text, card_left = v["refused"]
    assert count == 1 and after_id == mid and not card_left
    assert text.startswith("Сейчас переведу.") and text.endswith("Не запускаю.")
    assert any(m["role"] == "tool" and m["content"] == chat_pending.REFUSED
               for request in live.gw.chat_requests for m in request["messages"])
    count, text, card_left = v["ran"]
    assert count == 2 and not card_left and text.startswith("Ещё раз.") and text.endswith("Готово.")
    assert [p for m, p in calls if m == "tools/call"] == [{"name": "translate", "arguments": {"phrase": "hello"}}]
    assert not problems(r), r.get("log")


def test_chat_edit_and_delete_turn_keep_only_intended_history(browser, live):
    chat = "#messages .msg.user"
    count = f"document.querySelectorAll('{chat}').length"
    contents = f"[...document.querySelectorAll('{chat} .bubble')].map(m => m.textContent)"
    result = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        ("type", "#chat-input", "Первый"), ("click", "#send"), ("wait", ANSWERED),
        ("type", "#chat-input", "Второй"), ("click", "#send"), ("wait", f"{ANSWERED} && {count} === 2"),
        ("type", "#chat-input", "Черновик"),
        ("click", f"{chat}:first-child .msg-edit"),
        ("wait", "document.querySelector('#chat-input').value === 'Первый'"),
        ("click", ".chat-edit-cancel"),
        ("wait", "document.querySelector('#chat-input').value === 'Черновик'"),
        ("click", f"{chat}:first-child .msg-edit"), set_value("#chat-input", "Правка"),
        ("click", "#send"), ("wait", f"{ANSWERED} && {count} === 1"),
        ("eval", "edited", contents),
        ("eval", "draft", "document.querySelector('#chat-input').value"),
        set_value("#chat-input", "Третий"), ("click", "#send"), ("wait", f"{ANSWERED} && {count} === 2"),
        ("click", f"{chat}:first-child .msg-delete-turn"), ("click", ".msg-delete-confirm"),
        ("wait", f"{count} === 1"), ("eval", "remaining", contents)])
    assert not result.get("failed"), result
    assert result["values"]["edited"] == ["Правка"]
    assert result["values"]["draft"] == "Черновик"
    assert result["values"]["remaining"] == ["Третий"]
    assert not problems(result), problems(result)
