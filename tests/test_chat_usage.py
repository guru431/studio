"""`usage` шлюза и длительность ответа «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 13 и решение
19): сервер просит `stream_options.include_usage` в каждом потоке, хранит токены (сумма по кругам), время генерации и
всего хода у ответа; шлюз без `usage` или отвергающий параметр — ход работает, поля пустые, один повтор без параметра,
подключение запоминается до перезапуска службы (`chat.NO_USAGE`). Прямое подключение присылает числа само."""
import json

import httpx
import pytest

import chat
import state
from conftest import add_connection, add_gateway
from test_chat import messages, new_chat, sse, turn
from test_chat_web import call, web_chat

USAGE = {"prompt_tokens": 1200, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 1000},
         "completion_tokens_details": {"reasoning_tokens": 7}}


def ratios() -> int:
    return state.DB.execute("SELECT count(*) FROM model_facts WHERE ratio IS NOT NULL").fetchone()[0]


def test_usage_and_timings_are_stored_and_sent(studio, no_pings):
    gw = add_gateway(studio); cid = new_chat(studio); studio.gw.chat_usage = USAGE

    async def slow(request: httpx.Request) -> httpx.Response:
        studio.clock[0] += 1.0                       # до первого куска — 1 с: в total_ms, не в gen_ms
        return await studio.gw.builtin(request)

    def hold() -> bool:
        studio.clock[0] += 2.5                       # между первым и вторым куском ответа
        return False

    studio.gw.on["/v1/chat/completions"] = slow
    studio.gw.chat_hold = hold
    events = sse(turn(studio, cid, "я" * 3000), metrics=True)
    sent = studio.gw.chat_requests[0]
    assert sent["stream_options"] == {"include_usage": True}
    want = {"usage": {"in": 1200, "out": 50, "cached": 1000, "reasoning": 7}, "gen_ms": 2500, "total_ms": 3500}
    assert events[-1] == ("done", {"status": "done"} | want)
    user, answer = messages(studio, cid)
    assert {k: answer[k] for k in want} == want and (user["usage"], user["gen_ms"], user["total_ms"]) == (None,) * 3
    ratio = state.DB.execute("SELECT ratio FROM model_facts WHERE connection_id = ? AND model = ?",
                             (gw, "local-gemma-abl")).fetchone()[0]
    assert ratio == pytest.approx(chat.request_chars(sent) / 1200) and 1.5 <= ratio <= 6.0


def test_gateway_without_usage_leaves_the_fields_empty(studio, no_pings):          # Review Focus 2
    add_gateway(studio); cid = new_chat(studio)
    assert sse(turn(studio, cid), metrics=True)[-1] == (
        "done", {"status": "done", "usage": None, "gen_ms": 0, "total_ms": 0})
    assert messages(studio, cid)[1]["usage"] is None
    assert ratios() == 0
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})        # без metrics — прежний вид


def test_rejected_stream_options_retries_once_and_is_remembered_until_restart(studio):   # Review Focus 2
    gw = add_gateway(studio); cid = new_chat(studio); seen = []

    async def picky(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "Extra inputs are not permitted: stream_options"}})
        return await studio.gw.builtin(request)

    studio.gw.on["/v1/chat/completions"] = picky
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert ["stream_options" in b for b in seen] == [True, False] and gw in chat.NO_USAGE
    assert {k: v for k, v in seen[0].items() if k != "stream_options"} == seen[1]       # тот же запрос, с tools
    assert "tools" in seen[1]
    answer = messages(studio, cid)[1]
    assert (answer["content"], answer["usage"]) == ("Здравствуйте!", None)
    sse(turn(studio, cid, "Ещё"))
    assert len(seen) == 3 and "stream_options" not in seen[2]                           # подключение запомнено
    chat.NO_USAGE.clear()                                                               # перезапуск службы
    sse(turn(studio, cid, "И ещё"))
    assert ["stream_options" in b for b in seen[3:]] == [True, False]


def test_usage_sums_the_rounds_of_one_answer(studio):
    cid = web_chat(studio); studio.gw.chat_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")]}, ["Готово."]]
    assert sse(turn(studio, cid), metrics=True)[-1][1]["usage"] == {"in": 200, "out": 20}
    assert ratios() == 0                                                                # 100 токенов < 500


def test_answer_resumed_after_a_pause_shows_no_numbers_until_done(studio):
    """У продолжения после паузы в строке `messages` — числа части до паузы: пока ответ идёт, страница получает
    `usage`, `gen_ms`, `total_ms` = null (как у любого идущего), после конца — суммы обеих частей."""
    from test_chat import events_of
    from test_chat_pending import confirm_setup, pause_on
    from test_mcp_dialog import name_of
    sid, _ = confirm_setup(studio)
    cid = new_chat(studio); studio.gw.chat_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    shown = pause_on(studio, cid, name_of(sid, "translate"), phrase="hello")
    paused = messages(studio, cid)[-1]
    assert paused["usage"] == {"in": 100, "out": 10} and isinstance(paused["total_ms"], int)
    studio.gw.chat_hold = True                       # продолжение отдаёт первый кусок и ждёт
    body = {"request_id": "pending-usage-0001", "action": "run"}
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/pending/{shown['id']}", json=body) as r:
            assert r.status_code == 200
            events = events_of(r.iter_lines())
            assert next(d for e, d in events if e == "delta" and "Готово" in d["text"])
            going = messages(studio, cid)[-1]
            assert going["status"] == "streaming"
            assert (going["usage"], going["gen_ms"], going["total_ms"]) == (None, None, None)
            studio.gw.chat_hold = False
            assert [e for e, _ in events][-1] == "done"
    finally:
        studio.gw.chat_hold = False
    done = messages(studio, cid)[-1]
    assert done["status"] == "done" and done["usage"] == {"in": 200, "out": 20}
    assert isinstance(done["gen_ms"], int) and done["total_ms"] >= paused["total_ms"]


def test_direct_answer_brings_its_own_numbers(studio):
    add_connection(studio); cid = new_chat(studio)                      # прямое подключение: модель читает браузер
    aid = turn(studio, cid, "Привет").json()["assistant_message_id"]
    url, body = f"/api/chats/{cid}/messages/{aid}", {"content": "Здравствуйте!", "status": "done"}
    for bad in ({"usage": {"in": -1}}, {"usage": {"in": 100_000_001}}, {"usage": {"tokens": 1}}, {"usage": {"out": 1.5}},
                {"usage": {"in": True}}, {"gen_ms": -1}, {"gen_ms": "800"}, {"total_ms": 86_400_001}):
        assert studio.c.put(url, json=body | bad).status_code == 422, bad
    sent = {"usage": {"in": 12, "out": 3}, "gen_ms": 800, "total_ms": 1500}
    assert studio.c.put(url, json=body | sent).status_code == 200
    assert {k: messages(studio, cid)[1][k] for k in sent} == sent
    assert studio.c.put(url, json=body | {"gen_ms": 1}).status_code == 200 and messages(studio, cid)[1]["gen_ms"] == 800
