"""Окно контекста «Чата» (спека п. 31): знаков на токен — по `model_facts.ratio`, окно — ручное значение профиля,
затем список моделей шлюза, затем запомненное (`model_facts.window`), затем таблица `chat.CONTEXT`; описания
инструментов входят в проверку хода; отказ шлюза по длине — один повтор со сжатием, потом слова."""
import json
import math

import httpx
import pytest

import chat
import state
from conftest import add_gateway
from test_chat import messages, new_chat, sse, turn, window
from test_mcp_dialog import HOST, server, tool


def limit_of(studio, cid):
    r = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": ""})
    assert r.status_code == 200, r.text
    return r.json()["limit"]


def test_window_is_manual_then_listed_then_remembered_then_the_table(studio):       # п. 31(б)
    gw = add_gateway(studio); cid = new_chat(studio); url = f"/api/connections/{gw}/profile"
    window(studio, 65536)
    assert limit_of(studio, cid) == 65536                                  # список шлюза сильнее таблицы CONTEXT
    assert state.DB.execute("SELECT window FROM model_facts WHERE connection_id = ? AND model = ?",
                            (gw, "local-gemma-abl")).fetchone()[0] == 65536
    assert studio.c.put(url, json={"params": {"context_window": 32768}}).status_code == 200
    assert limit_of(studio, cid) == 32768                                  # ручное у подключения
    studio.c.put(url, json={"params": {"context_window": 32768},
                            "model_params": {"local-gemma-abl": {"context_window": 16384}}})
    assert limit_of(studio, cid) == 16384                                  # ручное у модели — сильнее
    studio.c.put(url, json={})

    async def down(request):
        return httpx.Response(503)
    studio.gw.on["/v1/models"] = down
    chat.LIMITS.clear()
    assert limit_of(studio, cid) == 65536                                  # список не прочитан — запомненное
    state.DB.execute("DELETE FROM model_facts")
    assert limit_of(studio, cid) == chat.CONTEXT["local-gemma-abl"] == 131072      # прежняя таблица


def test_ratio_from_model_facts_scales_the_count(studio):                          # п. 31(а)
    text = [{"role": "user", "content": "я" * 6000}]
    assert (chat.estimate_tokens(text), chat.estimate_tokens(text, 2.0)) == (2000, 3000) and chat.DEFAULT_RATIO == 3.0
    specs = [{"type": "function", "function": {"name": "t", "description": "д" * 600, "parameters": {}}}]
    assert chat.spec_tokens(specs, 2.0) == math.ceil(len(json.dumps(specs, ensure_ascii=False)) / 2)
    gw = add_gateway(studio); cid = new_chat(studio)
    before = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "я" * 6000}).json()["tokens"]
    # окно из списка уже записано в эту пару (`model_facts.window`): отношение — только своим столбцом
    state.DB.execute("INSERT INTO model_facts(connection_id, model, ratio, updated) VALUES (?, ?, 2.0, ?)"
                     " ON CONFLICT(connection_id, model) DO UPDATE SET ratio = excluded.ratio,"
                     " updated = excluded.updated", (gw, "local-gemma-abl", studio.clock[0]))
    after = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "я" * 6000}).json()["tokens"]
    assert after == pytest.approx(before * 1.5, rel=0.01)


def test_turn_check_counts_tool_descriptions(studio):                              # п. 31(в)
    add_gateway(studio); cid = new_chat(studio)
    server(studio, "/mine", tools=[tool(f"t{n}", description="д" * 1000) for n in range(12)])
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]})
    window(studio, chat.RESERVE_TOKENS + 3000)                 # сообщение помещается, с описаниями 12 инструментов — нет
    r = turn(studio, cid, "Привет")
    assert (r.status_code, r.json()["detail"]["code"]) == (413, "context") and messages(studio, cid) == []
    assert r.json()["detail"]["message"].startswith(chat.CONTEXT_WORDS)
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": []})
    assert sse(turn(studio, cid, "Привет"))[-1] == ("done", {"status": "done"})


def test_context_refusal_retries_once_with_compaction_then_refuses_in_words(studio):   # п. 31(г)
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_script = [["Ответ-1"], ["Ответ-2"]]
    sse(turn(studio, cid, "Вопрос-1")); sse(turn(studio, cid, "Вопрос-2"))
    refuse, seen = [1], []

    async def completions(request):
        body = json.loads(request.content)
        seen.append(body)
        summary = body["messages"][0]["role"] == "system" and body["messages"][0]["content"] == chat.SUMMARY_NOTE
        if not summary and refuse[0] > 0:
            refuse[0] -= 1
            return httpx.Response(400, json={"error": {"message": "This model's maximum context length is 8192 tokens."}})
        return await studio.gw.builtin(request)
    studio.gw.on["/v1/chat/completions"] = completions
    studio.gw.chat_script = [["Пересказ двух ходов."], ["Ответ-3"]]
    events = sse(turn(studio, cid, "Вопрос-3"))
    assert events[-1] == ("done", {"status": "done"}) and ("recovery", {"stage": "summary"}) in events
    assert len(seen) == 3                                                  # отказ, пересказ, повтор
    assert seen[2]["messages"][1]["content"] == chat.SUMMARY_BLOCK.format("Пересказ двух ходов.") + "Вопрос-3"
    assert messages(studio, cid)[-1]["content"] == "Ответ-3"
    refuse[0] = 9                                                          # отказ и после сжатия
    events = sse(turn(studio, cid, "Вопрос-4"))
    assert events[-1][0] == "error" and events[-1][1]["message"].startswith(chat.CONTEXT_WORDS)
    assert len(seen) == 6                                                  # отказ, пересказ, отказ — третьего нет
