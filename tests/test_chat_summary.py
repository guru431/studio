"""Хранимый пересказ ранней части диалога и «Сжать сейчас» (спека 2026-11-25-studio-skills-status-local-models,
п. 30, решение 14): пересказ хранится у диалога и переиспользуется, новое сжатие строится из прежнего пересказа,
правка пересказанной части его сбрасывает, текст последнего навыка остаётся дословным. В журнал — без текстов."""
import json
import logging

import httpx

import chat
import chat_shares
import cleaner_route
import sections
import state
from conftest import add_connection, add_gateway, make_user, signin
from test_chat import done_transcript, events_of, from_transcript, messages, new_chat, sse, turn
from test_cleaner_forced import CLEANER_HOSTS, owner_id
from test_cleaner_route_chat import allow, settings
from test_knowledge import small_model
from test_shared_integrations import MODEL, add_models

OLD = ("ABCD " * 2800).rstrip()                  # ≈ 4,7 тыс. токенов
SKILL = "НАВЫК: разбери по частям."


def tiny(studio) -> str:
    """Диалог шлюза: ход OLD на большой модели, затем модель `tiny` с окном 8192 — следующий ход сжимает историю."""
    gw = add_gateway(studio)
    cid = new_chat(studio)
    sse(turn(studio, cid, OLD))
    small_model(studio, cid, gw, 8192)
    chat.LIMITS.clear()
    return cid


def three_turns(studio) -> str:
    cid = new_chat(studio)
    studio.gw.chat_script = [["Ответ-1"], ["Ответ-2"], ["Ответ-3"]]
    for text in ("Вопрос-1 про бирюзу", "Вопрос-2", "Вопрос-3"):
        assert sse(turn(studio, cid, text))[-1] == ("done", {"status": "done"})
    return cid


def stored(cid):
    return state.DB.execute("SELECT summary, summary_upto, summary_at FROM chats WHERE id = ?", (cid,)).fetchone()


def compact(studio, cid):
    return studio.c.post(f"/api/chats/{cid}/compact")


def code(r) -> tuple:
    return r.status_code, r.json()["detail"]["code"]


def with_skill(message_id: str, text: str) -> None:
    """Сообщение с навыком — как его запишет задача 10: текст навыка в начале, отметка `messages.skill`."""
    state.DB.execute("UPDATE messages SET content = ? || char(10, 10) || content, skill = ? WHERE id = ?",
                     (text, json.dumps({"name": "Разбор", "chars": len(text), "parts": False}, ensure_ascii=False),
                      message_id))


def test_auto_summary_is_stored_and_the_next_turn_reuses_it(studio):
    cid = tiny(studio); studio.gw.chat_reply = ["Пересказ: план в out/план.docx."]    # и партии пересказа, и ответ
    assert ("recovery", {"stage": "summary"}) in sse(turn(studio, cid, "Дополни"))
    asked = studio.gw.chat_requests[1]
    assert asked["messages"][0]["content"] == chat.SUMMARY_NOTE and "under 500 words" in chat.SUMMARY_NOTE
    assert asked["max_tokens"] == chat.SUMMARY_TOKENS == 2048 and "tools" not in asked
    first, row = messages(studio, cid), stored(cid)
    assert (row["summary"], row["summary_upto"]) == ("Пересказ: план в out/план.docx.", first[1]["id"])
    assert studio.c.get(f"/api/chats/{cid}").json()["summary"] == {"upto": first[1]["id"], "at": row["summary_at"]}
    assert studio.c.get(f"/api/chats/{cid}/summary").json() == {"text": row["summary"]}
    count = len(studio.gw.chat_requests)
    sse(turn(studio, cid, "Ещё"))
    sent = studio.gw.chat_requests[-1]["messages"]
    assert len(studio.gw.chat_requests) == count + 1                       # пересказ не считается заново
    assert sent[1]["content"].startswith(chat.SUMMARY_BLOCK.format(row["summary"])) and "ABCD" not in json.dumps(sent)
    assert len(messages(studio, cid)) == 6 and messages(studio, cid)[0]["content"] == OLD   # история на месте


def test_summary_cut_by_length_is_accepted(studio):                        # находка FINDINGS 2026-11-25
    cid = tiny(studio); studio.gw.chat_reply = ["Пересказ оборван на полусло"]

    async def completions(request: httpx.Request) -> httpx.Response:
        first = json.loads(request.content)["messages"][0]["content"]
        studio.gw.chat_ending = "length" if first == chat.SUMMARY_NOTE else "stop"
        return studio.gw._chat(request)

    studio.gw.on["/v1/chat/completions"] = completions
    assert sse(turn(studio, cid, "Дополни"))[-1] == ("done", {"status": "done"})
    assert stored(cid)["summary"] == "Пересказ оборван на полусло"


def test_compact_now_keeps_the_last_turn_and_builds_on_the_previous_summary(studio, caplog):
    caplog.set_level(logging.INFO)
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    studio.gw.chat_script = [["Итог: говорили про бирюзу."], ["Ответ-4"], ["Итог-2"]]
    r = compact(studio, cid)
    assert r.status_code == 200 and set(r.json()) == {"summary_upto", "tokens", "limit"}
    assert r.json()["summary_upto"] == ms[3]["id"] and r.json()["limit"] == 131072
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert "Вопрос-1 про бирюзу" in prompt and "Ответ-2" in prompt and "Вопрос-3" not in prompt
    sse(turn(studio, cid, "Вопрос-4"))
    sent = studio.gw.chat_requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == chat.SUMMARY_BLOCK.format("Итог: говорили про бирюзу.") + "Вопрос-3"
    assert compact(studio, cid).status_code == 200                         # новое сжатие — из прежнего и нового
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert prompt.startswith("Summary so far:\nИтог: говорили про бирюзу.\n\nTranscript:\nuser: Вопрос-3")
    assert "Вопрос-1" not in prompt and stored(cid)["summary"] == "Итог-2" and len(messages(studio, cid)) == 8
    assert "Итог" not in caplog.text and "бирюз" not in caplog.text        # журнал — без текстов
    uid = state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    assert "Итог-2" not in json.dumps(chat_shares.regular_snapshot(uid, cid)[0], ensure_ascii=False)


def test_last_skill_stays_verbatim_and_earlier_ones_are_summarized(studio):    # решение 14 спеки
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    with_skill(ms[0]["id"], "РАННИЙ НАВЫК"); with_skill(ms[2]["id"], SKILL)
    studio.gw.chat_script = [["Итог"], ["Ответ-4"]]
    assert compact(studio, cid).status_code == 200
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert "РАННИЙ НАВЫК" in prompt and SKILL not in prompt and "Вопрос-2" in prompt
    sse(turn(studio, cid, "Вопрос-4"))
    assert studio.gw.chat_requests[-1]["messages"][1]["content"] == (
        SKILL + "\n\n" + chat.SUMMARY_BLOCK.format("Итог") + "Вопрос-3")


def test_changing_a_summarized_message_resets_the_summary(studio):
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    assert compact(studio, cid).status_code == 200                         # пересказаны ms[0]…ms[3]
    assert studio.c.delete(f"/api/chats/{cid}/turns/{ms[4]['id']}").status_code == 204   # ход после пересказа
    assert stored(cid)["summary"] is not None
    assert studio.c.delete(f"/api/chats/{cid}/turns/{ms[0]['id']}").status_code == 204   # ход внутри пересказа
    assert tuple(stored(cid)) == (None, None, None) and studio.c.get(f"/api/chats/{cid}").json()["summary"] is None
    # правка первого сообщения второго диалога — тоже сброс, и ход идёт без пересказа
    cid = three_turns(studio); ms = messages(studio, cid)
    assert compact(studio, cid).status_code == 200 and stored(cid)["summary"] is not None
    studio.gw.chat_script = [["Ответ-1б"]]
    assert sse(turn(studio, cid, "Вопрос-1 заново", edit={"id": ms[0]["id"], "last_id": ms[-1]["id"]}))[-1] == (
        "done", {"status": "done"})
    assert tuple(stored(cid)) == (None, None, None)
    assert "<conversation-summary>" not in json.dumps(studio.gw.chat_requests[-1], ensure_ascii=False)


def test_compact_refusals(studio):
    add_gateway(studio); cid = new_chat(studio); sse(turn(studio, cid, "Один ход"))
    assert code(compact(studio, cid)) == (409, "nothing")
    assert studio.c.get(f"/api/chats/{cid}/summary").status_code == 404
    assert studio.c.get(f"/api/chats/{cid}/summary").json()["detail"]["code"] == "gone"
    # идёт ответ — busy
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Второй"}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn"
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            assert code(compact(studio, cid)) == (409, "busy")
            studio.gw.chat_hold = False
            assert [e for e, _ in events if e != "ping"][-1] == "done"
    finally:
        studio.gw.chat_hold = False
    # шлюз ответил 503 — карта занята
    studio.gw.chat_error = (503, {"error": {"message": "busy"}})
    assert code(compact(studio, cid)) == (503, "card")
    studio.gw.chat_error = None
    assert stored(cid)["summary"] is None
    # прямое подключение — no_compact
    direct = add_connection(studio)
    other = new_chat(studio, connection_id=direct)
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES ('u1', ?, 'user', 'a', 'done', 'browser', 'm', 1), ('a1', ?, 'assistant', 'b', 'done',"
                     " 'browser', 'm', 2), ('u2', ?, 'user', 'c', 'done', 'browser', 'm', 3)", (other, other, other))
    assert code(compact(studio, other)) == (409, "no_compact")
    # чужой диалог — 404
    make_user(studio, "bob"); signin(studio, "bob")
    assert code(compact(studio, cid)) == (404, "gone")


def test_compact_is_refused_for_a_cloud_role_when_cleaner_is_forced(studio):
    """Ход облачной роли srvllm при принудительном Cleaner — 409 `cleaner_forced`; «Сжать сейчас» тоже не уносит
    историю мимо Cleaner."""
    gw = add_gateway(studio)
    sections.set_models(owner_id(), "chat", gw, ["local-gemma-abl", "gpt-x"])
    cid = new_chat(studio, connection_id=gw, model="gpt-x")
    studio.gw.chat_script = [["Ответ-1"], ["Ответ-2"]]
    for text in ("Вопрос-1", "Вопрос-2"):
        assert sse(turn(studio, cid, text))[-1] == ("done", {"status": "done"})
    allow(*CLEANER_HOSTS)
    settings(studio, forced=True)
    sent = len(studio.gw.chat_requests)
    r = compact(studio, cid)
    assert code(r) == (409, "cleaner_forced") and r.json()["detail"]["message"] == cleaner_route.FORCED_SRVLLM
    assert code(turn(studio, cid, "Вопрос-3")) == (409, "cleaner_forced")                  # тот же допуск у хода
    assert len(studio.gw.chat_requests) == sent and stored(cid)["summary"] is None


def test_compact_asks_for_consent_when_a_transcript_goes_to_a_cloud_model(studio, fake_audio):
    """Расшифровка в истории и нелокальная модель без её ответа после вложения — ход 409 `external`, сжатие тоже;
    ответ этой модели (подтверждённый ход) снимает отказ."""
    gw = add_gateway(studio); tid = done_transcript(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    studio.gw.models[1]["context_length"] = 131072                                     # окно модели известно
    cid = new_chat(studio, connection_id=gw, model="local-gemma-abl")
    a = from_transcript(studio, tid).json()
    studio.gw.chat_script = [["Ответ-1"], ["Ответ-2"], ["Ответ-3"], ["Итог"]]
    sse(turn(studio, cid, "Итоги?", attachment_ids=[a["id"]]))                         # локальная — без вопроса
    sse(turn(studio, cid, "Ещё?"))
    studio.c.patch(f"/api/chats/{cid}", json={"model": "gemma4-abl"})
    sent = len(studio.gw.chat_requests)
    r = compact(studio, cid)
    assert code(r) == (409, "external") and r.json()["detail"]["message"] == (
        "Расшифровка уйдёт во внешний сервис «srvllm» — подтвердите")
    assert len(studio.gw.chat_requests) == sent and stored(cid)["summary"] is None
    sse(turn(studio, cid, "Третий", external_ok=True))                                  # подтвердили ходом
    assert compact(studio, cid).status_code == 200 and stored(cid)["summary"] == "Итог"


def test_compact_is_refused_once_the_shared_model_is_withdrawn(studio):
    """Общее подключение: модель снята из раздела для учёток — ход 422, сжатие тоже, без запроса к модели."""
    make_user(studio, "admin", is_admin=True); make_user(studio, "existing")
    signin(studio, "admin"); shared = add_models(studio)
    signin(studio, "existing")
    assert studio.c.put("/api/sections/chat/default", json={"connection_id": shared, "model": MODEL}).status_code == 200
    cid = new_chat(studio)
    for text in ("Вопрос-1", "Вопрос-2"):
        assert sse(turn(studio, cid, text))[-1] == ("done", {"status": "done"})
    signin(studio, "admin")
    assert studio.c.put(f"/api/admin/shared/sections/chat/links/{shared}", json={"models": []}).status_code == 200
    signin(studio, "existing")
    sent = len(studio.gw.chat_requests)
    assert compact(studio, cid).status_code == 422 and turn(studio, cid, "Вопрос-3").status_code == 422
    assert len(studio.gw.chat_requests) == sent and stored(cid)["summary"] is None


def test_skill_in_the_summary_head_stays_after_a_newer_skill(studio):
    """Решение 14 и п. 34: навык A пересказанной части стоит в шапке; новый навык B после границы шапку не меняет
    (начало запроса прежнее, текст A не теряется). Следующее сжатие с B внутри границы пересказывает A, B — в шапке."""
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    with_skill(ms[2]["id"], "НАВЫК-A")
    studio.gw.chat_script = [["Итог-1"], ["Ответ-4"], ["Ответ-5"], ["Итог-2"]]
    assert compact(studio, cid).status_code == 200                         # пересказаны ms[0]…ms[3], A — в шапке
    sse(turn(studio, cid, "Вопрос-4"))
    first = studio.gw.chat_requests[-1]["messages"]
    assert first[1]["content"] == "НАВЫК-A\n\n" + chat.SUMMARY_BLOCK.format("Итог-1") + "Вопрос-3"
    ms = messages(studio, cid)
    with_skill(ms[6]["id"], "НАВЫК-B")                                     # «Вопрос-4» — вызван навык B
    sse(turn(studio, cid, "Вопрос-5"))
    second = studio.gw.chat_requests[-1]["messages"]
    dump = lambda value: json.dumps(value, ensure_ascii=False)             # noqa: E731
    assert dump(second[:3]) == dump(first[:3])                             # начало запроса не изменилось
    assert second[3]["content"].startswith("НАВЫК-B\n\n")
    assert compact(studio, cid).status_code == 200                         # граница — ответ на «Вопрос-4»
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert "user: НАВЫК-A" in prompt and "НАВЫК-B" not in prompt and "Вопрос-4" in prompt
    head = chat.compose(cid, messages(studio, cid)[-2]["id"])[0]["content"]
    assert head.startswith("НАВЫК-B\n\n" + chat.SUMMARY_BLOCK.format("Итог-2")) and "НАВЫК-A" not in head
