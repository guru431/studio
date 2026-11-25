"""Неизменное начало запроса (спека 2026-11-25-studio-skills-status-local-models, п. 34): запрос следующего хода
повторяет запрос прошлого до конца предпоследнего сообщения — выжимки результатов инструментов (п. 29) и начало
большого документа истории не зависят от нового вопроса, найденное по нему — в сообщении хода."""
import json

import chat
import state
from test_chat import attach, messages, sse, turn
from test_chat_web import call, web_chat
from test_knowledge import big_text, small_model


def test_next_request_repeats_the_previous_one_up_to_the_penultimate_message(studio):   # приёмка п. 34
    cid = web_chat(studio)
    gw = state.DB.execute("SELECT connection_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    small_model(studio, cid, gw, 20_000)                         # окно 20 тыс.: большой документ — выдержками
    studio.gw.exa_page_text = "слово " * 2000
    doc = attach(studio, "отчёт.txt", big_text("Пароль от сейфа — 4711.").encode())
    url = "https://example.test/long"
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url=url)]}, ["Прочитал."], ["Второй."], ["Третий."]]
    sse(turn(studio, cid, f"Прочитай {url}", attachment_ids=[doc["id"]]))
    sse(turn(studio, cid, "Какой пароль от сейфа?"))
    sse(turn(studio, cid, "А ещё?"))
    second, third = studio.gw.chat_requests[-2:]
    dump = lambda value: json.dumps(value, ensure_ascii=False).encode()
    cut = len(second["messages"]) - 1                            # до конца предпоследнего сообщения
    assert cut == 3 and dump(third["messages"][:cut]) == dump(second["messages"][:cut])
    assert dump(third["tools"]) == dump(second["tools"])
    first = second["messages"][1]["content"]
    assert first.startswith('<web kind="page">\n') and chat.HISTORY_CUT in first
    assert '<document name="отчёт.txt" excerpts="true">\n[кусок 1]' in first and "4711" not in first
    asked = second["messages"][-1]["content"]                    # найденное по новому вопросу — в новом сообщении
    assert asked.startswith("<knowledge>") and "4711" in asked and asked.endswith("Какой пароль от сейфа?")
    assert len(messages(studio, cid)) == 6


def test_history_excerpt_keeps_the_first_chars_and_marks_the_cut():   # выжимка от текста — и для итогов частей
    limit = chat.HISTORY_RESULT_CHARS
    assert chat.history_excerpt("я" * limit) == "я" * limit
    assert chat.history_excerpt("я" * (limit + 1)) == "я" * limit + chat.HISTORY_CUT
