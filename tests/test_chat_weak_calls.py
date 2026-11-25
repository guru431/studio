"""Терпимость к слабому вызову инструментов (спека 2026-11-25-studio-skills-status-local-models, п. 33): вызов
структурой исполняется при любом признаке конца, кроме обрыва (`length`); вызов текстом в `<tool_call>` разбирается
только у локальной модели (`sections.is_local`) и никогда — через Cleaner (решение 13, сторож —
tests/test_cleaner_route_chat.py); неразобранные аргументы — отказ инструмента словами модели, не ошибка хода."""
from test_chat import messages, sse, turn
from test_chat_web import call, chat_of, patch, web_chat

TEXT_CALL = 'Ищу.\n<tool_call>\n{"name": "web_search", "arguments": {"query": "версия Python"}}\n</tool_call>'


def test_structured_call_runs_with_any_finish_reason(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")], "ending": "stop"}, ["Нашёл."]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert len(studio.gw.exa_requests) == 1 and messages(studio, cid)[1]["content"] == "Нашёл."


def test_local_model_text_call_is_executed_and_leaves_the_answer(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [[TEXT_CALL], ["Python 3.14."]]                 # local-gemma-abl
    events = sse(turn(studio, cid))
    assert studio.gw.exa_requests[0][1]["query"] == "версия Python" and ("replace", {"text": "Ищу."}) in events
    assert messages(studio, cid)[1]["content"] == "Ищу.\n\nPython 3.14."
    assistant, result = studio.gw.chat_requests[1]["messages"][-2:]
    assert assistant["content"] == "Ищу." and result["role"] == "tool"
    assert assistant["tool_calls"][0]["function"] == {"name": "web_search", "arguments": '{"query": "версия Python"}'}


def test_text_call_with_broken_arguments_is_a_tool_refusal_not_a_turn_error(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [['<tool_call>{"name": "web_search", "arguments": {"query": </tool_call>'], ["Не вышло."]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("done", "Не вышло.") and answer["tools"][0]["error"]
    assert studio.gw.exa_requests == []
    assert studio.gw.chat_requests[1]["messages"][-1]["content"].startswith("Ошибка инструмента: ")


def test_text_call_stays_text_for_a_cloud_role(studio):
    cid = web_chat(studio)
    gw = chat_of(studio, cid)["connection_id"]
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    patch(studio, cid, model="gemma4-abl")                                  # облачная роль того же шлюза
    studio.gw.chat_script = [[TEXT_CALL]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert messages(studio, cid)[1]["content"] == TEXT_CALL and studio.gw.exa_requests == []
    assert len(studio.gw.chat_requests) == 1 and studio.gw.chat_requests[0]["model"] == "gemma4-abl"
