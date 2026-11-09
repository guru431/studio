"""Ход чата через сервер: шлюз ответил 503 (карта занята) — ожидание и повтор раз в RETRY_SEC до WAIT_MAX_SEC
(срок не идёт, пока карту держит своё распознавание пользователя), «Не ждать»; причина отказа — в
`messages.error` (видна после перечитывания); поток сверяет сессию и блокировку «Чата» — `closed`."""
import asyncio

import httpx
import pytest

import chat
import ocr
import ocr_render
import state
from conftest import add_gateway
from scans import scan_pdf
from test_chat import events_of, messages, new_chat, sse, turn


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 300)


def on_loop(s, fn, *args):
    return s.c.portal.call(fn, *args)


def wait_until(s, cond, limit=3000):
    for _ in range(limit):
        if on_loop(s, cond):
            return
        s.c.portal.call(asyncio.sleep, 0.001)
    raise AssertionError("не дождались")


def busy_gateway(s, times):
    """Первые `times` запросов модели — 503 «слот занят», дальше — обычный ответ (times=None — всегда 503)."""
    left = [times]

    async def reply(request):
        if left[0] is None or left[0] > 0:
            if left[0] is not None:
                left[0] -= 1
            s.gw.chat_busy = getattr(s.gw, "chat_busy", 0) + 1
            return httpx.Response(503, json={"error": {"message": "slot busy"}})
        return await s.gw.builtin(request)
    s.gw.on["/v1/chat/completions"] = s.gw.on["/chat/completions"] = reply


def test_busy_card_waits_and_retries_the_model(studio):
    add_gateway(studio); cid = new_chat(studio)
    busy_gateway(studio, 2)
    events = sse(turn(studio, cid, "Привет"))
    waits = [d for e, d in events if e == "wait"]
    # часы двигают и пинги потока: секунды второго ожидания — не меньше паузы
    assert waits[0] == {"seconds": 0} and waits[1]["seconds"] >= chat.RETRY_SEC and waits[2:] == [{}]
    assert events[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_busy == 2 and len(studio.gw.chat_requests) == 1
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"], answer["error"], answer["wait"]) == ("done", "Здравствуйте!", None, None)


def test_busy_card_too_long_is_an_error_kept_after_reload(studio, monkeypatch):
    monkeypatch.setattr(chat, "WAIT_MAX_SEC", 30)
    add_gateway(studio); cid = new_chat(studio)
    busy_gateway(studio, None)
    events = sse(turn(studio, cid, "Привет"))
    words = "карта занята дольше 0 минут — попробуйте позже"
    assert events[-1] == ("error", {"message": words})
    assert 2 <= studio.gw.chat_busy <= 3 and studio.gw.chat_requests == []
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"], answer["error"]) == ("error", "", words)


def test_own_recognition_holding_the_card_does_not_eat_the_wait(studio, monkeypatch):
    """Пока идёт своё распознавание пользователя (его скан в «Сканах»), срок ожидания карты не идёт: карту
    освободит его конец, а не «попробуйте позже» (ревью: чат со сканом не ждал другие распознавания)."""
    from test_ocr import pdf_scan
    monkeypatch.setattr(chat, "WAIT_MAX_SEC", 30)
    gw = add_gateway(studio); cid = new_chat(studio)
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (gw,)).fetchone()[0]
    import connections
    studio.gw.ocr_hold = True
    sid = pdf_scan(studio, uid, connections.get(uid, gw), "ink")
    try:
        wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
        busy_gateway(studio, 5)                                   # 75 с > WAIT_MAX_SEC
        events = sse(turn(studio, cid, "Привет"))
        assert events[-1] == ("done", {"status": "done"})
        assert studio.gw.chat_busy == 5
    finally:
        studio.gw.ocr_hold = False
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans WHERE id = ?", (sid,)).fetchone()[0] == "done")


def test_do_not_wait_ends_the_answer_with_words(studio):
    add_gateway(studio); cid = new_chat(studio)
    assert studio.c.post(f"/api/chats/{cid}/nowait").status_code == 409
    busy_gateway(studio, None)
    with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines())
        assert next(d for e, d in events if e == "wait") == {"seconds": 0}
        assert messages(studio, cid)[1]["wait"] >= 0
        assert studio.c.post(f"/api/chats/{cid}/nowait").status_code == 204
        rest = [(e, d) for e, d in events if e not in ("ping", "wait")]
    assert rest == [("error", {"message": chat.GAVE_UP})]
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["error"], answer["wait"]) == ("error", chat.GAVE_UP, None)
    assert not chat.STREAMS and studio.gw.chat_requests == []


def test_other_gateway_503_is_an_error_at_once(studio):
    """Ждёт карту только шлюз студии (srvllm, s06): 503 иного подключения через сервер — прежняя ошибка шлюза."""
    gw = add_gateway(studio); cid = new_chat(studio)
    on_loop(studio, lambda: state.DB.execute("UPDATE connections SET kind = 'openai' WHERE id = ?", (gw,)))
    busy_gateway(studio, None)
    events = sse(turn(studio, cid, "Привет"))
    assert events[-1][0] == "error" and not [e for e, _ in events if e == "wait"]
    assert studio.gw.chat_busy == 1


def test_scan_turn_reason_survives_reload(studio, monkeypatch):
    """Ход не поместился в контекст после распознавания — слова в `error` и после перечитывания (ревью)."""
    from test_chat_ocr import ask, held_until_the_turn_waits, upload
    monkeypatch.setitem(chat.CONTEXT, "local-gemma-abl", chat.RESERVE_TOKENS + 10)
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    att = upload(studio, scan_pdf("ink"))
    events = sse(ask(studio, cid, ids=[att["id"]]))
    words = events[-1][1]["message"]
    assert events[-1][0] == "error" and words.startswith("Не помещается в контекст модели")
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"], answer["error"]) == ("error", "", words)


@pytest.mark.parametrize("change", ["logout", "disable", "lock_chat", "lock_all"])
def test_stream_closes_when_the_session_ends_or_the_chat_locks(studio, change):
    add_gateway(studio); cid = new_chat(studio)
    uid = on_loop(studio, lambda: state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0])
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(e for e, _ in events if e == "delta")

            def apply():
                if change == "logout":
                    state.DB.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                elif change == "disable":
                    state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
                else:
                    sections = '["chat"]' if change == "lock_chat" else '["*"]'
                    state.DB.execute("UPDATE users SET screen_lock = 1, screen_lock_sections = ? WHERE id = ?",
                                     (sections, uid))
                    state.DB.execute("UPDATE sessions SET seen = 0")
            on_loop(studio, apply)
            rest = [e for e, _ in events if e not in ("ping", "delta")]
    finally:
        studio.gw.chat_hold = False
    assert rest == ["closed"]
    # ответ дочитывается и сохраняется: поток лишь перестал показывать его закрытой сессии
    wait_until(studio, lambda: not chat.STREAMS)
    assert on_loop(studio, lambda: state.DB.execute(
        "SELECT status, content FROM messages WHERE role = 'assistant'").fetchone()[:]) == ("done", "Здравствуйте!")


def test_stream_keeps_going_when_another_section_locks(studio):
    add_gateway(studio); cid = new_chat(studio)
    uid = on_loop(studio, lambda: state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0])
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(e for e, _ in events if e == "delta")
            on_loop(studio, lambda: (state.DB.execute(
                "UPDATE users SET screen_lock = 1, screen_lock_sections = '[\"photo\"]' WHERE id = ?", (uid,)),
                state.DB.execute("UPDATE sessions SET seen = 0")))
            for _ in range(3):                                     # несколько тиков пинга — поток жив
                assert next(e for e, _ in events) in ("ping", "delta")
            studio.gw.chat_hold = False
            rest = [e for e, _ in events if e not in ("ping", "delta")]
    finally:
        studio.gw.chat_hold = False
    assert rest == ["done"]
