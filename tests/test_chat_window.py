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


def test_turn_without_a_known_window_skips_tool_descriptions(studio, monkeypatch):
    """Окно модели не известно (`limit` None) — проверки окна нет, и ход не ждёт каталоги MCP (`_turn_specs`)."""
    gw = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    cid = new_chat(studio, connection_id=gw, model="gemma4-abl")
    asked, real = [], chat._turn_specs

    async def spy(uid, row, conn, model):
        asked.append(model)
        return await real(uid, row, conn, model)
    monkeypatch.setattr(chat, "_turn_specs", spy)
    assert limit_of(studio, cid) is None
    asked.clear()                                                          # оценка (`/estimate`) зовёт всегда
    assert sse(turn(studio, cid, "Привет"))[-1] == ("done", {"status": "done"})
    assert asked == []
    window(studio, 65536, model="gemma4-abl")
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})
    assert asked == ["gemma4-abl"]


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


def test_context_refusal_round_returns_the_round_text_not_the_gateway_body(studio, monkeypatch):
    """Договор `StreamTask._round`: четвёртый элемент — текст круга; у отказа по длине его нет, тело ошибки шлюза
    туда не попадает."""
    add_gateway(studio); cid = new_chat(studio)
    rounds, real = [], chat.StreamTask._round

    async def spy(self, *args, **kwargs):
        out = await real(self, *args, **kwargs)
        rounds.append(out)
        return out
    monkeypatch.setattr(chat.StreamTask, "_round", spy)
    studio.gw.chat_error = (400, {"error": {"message": "This model's maximum context length is 8192 tokens"}})
    assert sse(turn(studio, cid))[-1] == ("error", {"message": chat.TOO_LONG_WORDS})
    assert rounds == [(None, chat.TOO_LONG, [], "")]


def test_context_refusal_from_a_profile_max_tokens_names_the_parameter(studio):
    """Предел ответа профиля (на модель) не меньше окна без запаса: отказ шлюза по длине — слова о параметре
    профиля, без сжатия и без записи пересказа (`chats.summary`): сжатие не помогло бы."""
    gw = add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_script = [["Ответ-1"], ["Ответ-2"]]
    sse(turn(studio, cid, "Вопрос-1")); sse(turn(studio, cid, "Вопрос-2"))
    window(studio, 65536)
    big = 65536 - chat.RESERVE_TOKENS
    r = studio.c.put(f"/api/connections/{gw}/profile", json={"model_params": {"local-gemma-abl": {"max_tokens": big}}})
    assert r.status_code == 200, r.text
    seen = []

    async def completions(request):                                         # отказ vLLM — только с большим пределом
        body = json.loads(request.content)
        seen.append(body)
        if body.get("max_tokens") != big:
            return await studio.gw.builtin(request)
        return httpx.Response(400, json={"error": {"message": (
            "This model's maximum context length is 65536 tokens. However, you requested 66000 tokens "
            f"({66000 - big} in the messages, {big} in the completion).")}})
    studio.gw.on["/v1/chat/completions"] = completions
    studio.gw.chat_script = [["Пересказ двух ходов."]]
    events = sse(turn(studio, cid, "Вопрос-3"))
    assert state.DB.execute("SELECT summary FROM chats WHERE id = ?", (cid,)).fetchone()[0] is None
    assert len(seen) == 1 and seen[0]["max_tokens"] == big                 # без пересказа и без повтора
    assert ("recovery", {"stage": "summary"}) not in events
    assert events[-1] == ("error", {"message": chat.PARAM_TOO_BIG})
    assert not chat.PARAM_TOO_BIG.startswith(chat.CONTEXT_WORDS)          # «Новый диалог с итогом» не поможет
