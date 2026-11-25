"""«Новый диалог с итогом» (спека 2026-11-25-studio-skills-status-local-models п. 27): модель пересказывает весь
диалог механизмом п. 30, наследник открывается с теми же подключением, моделью, ролью, MCP и знаниями, итог — в поле
ввода (ответом маршрута, не в базе)."""
import logging
from pathlib import Path

import chat
import chat_continue
import state
from conftest import add_connection, add_gateway, make_user, signin
from test_chat import events_of, messages, new_chat, sse, turn
from test_chat_fork import INHERITED, count_chats, row_of
from test_chat_summary import code, compact, stored, three_turns, with_skill
from test_chat_system import role, with_role
from test_chat_web import call
from test_cleaner import EMAIL, preview, protected  # noqa: F401 — фикстура Cleaner (вид предпросмотра)
from test_cleaner_route_chat import cleaner_gw, routed_chat  # noqa: F401 — фикстура шлюза Cleaner
from test_knowledge import base
from test_mcp_dialog import name_of, offered, setup, tool, view
from test_protected_source import code_of, flags, names, protect, web_dialog
from test_shared_integrations import MODEL

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
KEEP = ("connection_id", "model", "role", "mcp_ids", "thinking", "effort", "title")


def go(s, cid):
    return s.c.post(f"/api/chats/{cid}/continue")


def test_continue_summarizes_the_whole_dialog_into_an_heir(studio, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    add_gateway(studio); cid = three_turns(studio); kb = base(studio)
    studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]})
    with_role(studio, cid, role(studio))
    assert studio.c.patch(f"/api/chats/{cid}", json={"thinking": True, "title": "Бирюза"}).status_code == 200
    state.DB.execute("UPDATE chats SET effort = 'high', pinned = 1 WHERE id = ?", (cid,))
    studio.gw.chat_script = [["Итог: бирюза, три вопроса."]]
    before = count_chats()
    r = go(studio, cid)
    assert r.status_code == 201 and r.json()["text"] == "Итог: бирюза, три вопроса." and set(r.json()) == {"chat_id", "text"}
    asked = studio.gw.chat_requests[-1]; prompt = asked["messages"][1]["content"]
    assert asked["messages"][0]["content"] == chat.SUMMARY_NOTE and asked["max_tokens"] == chat.SUMMARY_TOKENS
    assert "tools" not in asked and "Вопрос-1 про бирюзу" in prompt and "Ответ-3" in prompt      # весь диалог
    new = r.json()["chat_id"]; assert new != cid and messages(studio, new) == [] and count_chats() == before + 1
    assert [view(studio, new)[k] for k in KEEP] == [view(studio, cid)[k] for k in KEEP]
    assert view(studio, new)["effort"] == "high" and view(studio, new)["title"] == "Бирюза"
    assert studio.c.get(f"/api/chats/{new}").json()["knowledge"] == [kb]
    source, heir = row_of(cid), row_of(new)
    assert [heir[k] for k in INHERITED] == [source[k] for k in INHERITED]
    # итог — только ответом: ни в пересказ прежнего, ни в базу нового; закрепление не наследуется
    assert tuple(stored(cid)) == (None, None, None) and tuple(stored(new)) == (None, None, None)
    assert heir["pinned"] == 0 and heir["seen"] is not None and len(messages(studio, cid)) == 6   # прежний не тронут
    assert new in [c["id"] for c in studio.c.get("/api/chats").json()["items"]]
    assert "Итог" not in caplog.text and "бирюз" not in caplog.text
    assert f"чат {cid[:8]}: диалог с итогом {new[:8]}, знаков {len(r.json()['text'])}" in caplog.text
    # прежний пересказ — затравка: в расшифровку идёт только то, что после его границы
    studio.gw.chat_script = [["Итог-1"], ["Итог-2"]]; assert compact(studio, cid).status_code == 200
    assert go(studio, cid).json()["text"] == "Итог-2"
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert prompt.startswith("Summary so far:\nИтог-1\n\nTranscript:\nuser: Вопрос-3") and "Вопрос-1" not in prompt
    assert stored(cid)["summary"] == "Итог-1"                                   # хранимый пересказ прежний


def test_continue_retells_the_last_skill_too(studio):
    """Решение ведущего (ревью раунда 2): у наследника шапки нет — текст последнего навыка идёт в итог, а не
    вырезается, как у «Сжать сейчас» (решение 14); и навык из шапки прежнего пересказа тоже."""
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    with_skill(ms[0]["id"], "РАННИЙ НАВЫК"); with_skill(ms[4]["id"], "ПОСЛЕДНИЙ НАВЫК")
    studio.gw.chat_script = [["Итог"]]
    assert go(studio, cid).status_code == 201
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert "РАННИЙ НАВЫК" in prompt and "ПОСЛЕДНИЙ НАВЫК" in prompt and "Вопрос-3" in prompt
    other = three_turns(studio); ms = messages(studio, other)
    with_skill(ms[2]["id"], "НАВЫК-A")
    studio.gw.chat_script = [["Итог-1"], ["Итог-2"]]
    assert compact(studio, other).status_code == 200                 # навык A — в шапке пересказа
    assert "НАВЫК-A" not in studio.gw.chat_requests[-1]["messages"][1]["content"]   # «Сжать сейчас» — как прежде
    assert go(studio, other).status_code == 201
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert prompt.startswith("Summary so far:\nИтог-1\n\nTranscript:\nuser: НАВЫК-A") and "Вопрос-3" in prompt


def test_continue_of_a_protected_dialog_stays_protected(studio):                     # Review Focus 3
    sid, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid); cid = web_dialog(studio)
    studio.gw.models.append({"id": MODEL, "context_length": 131072}); chat.LIMITS.clear()   # окно общей модели известно
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Секрет источника: 4711"], ["Итог с 4711"], ["Ответ"]]
    sse(turn(studio, cid, "Use tool")); assert flags(cid) == (1, 0)
    r = go(studio, cid); assert r.status_code == 201, r.text
    out = r.json(); new = out["chat_id"]
    assert "tools" not in studio.gw.chat_requests[-1]                                # запрос итога — без инструментов
    assert flags(new) == (1, 0) and view(studio, new)["protected"] is True and view(studio, new)["web"] is False
    assert code_of(studio.c.patch(f"/api/chats/{new}", json={"web": True})) == "protected"
    sse(turn(studio, new, out["text"] + "\n\nЧто дальше?"))
    assert names(studio.gw.chat_requests[-1]) == [] and offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    assert code_of(studio.c.post(f"/api/chats/{new}/share", json={"pin": "0123"})) == "protected"
    assert code_of(studio.c.post(f"/api/chats/{new}/preview", json={"kind": "html", "text": "<p>4711</p>"})) == "protected"


def test_protection_set_while_the_summary_was_written_reaches_the_heir(studio, monkeypatch):
    """Строка диалога читается заново после итога: отметка, вставшая за время запроса, наследуется."""
    add_gateway(studio); cid = three_turns(studio)
    real = chat.StreamTask._summarize

    async def summarize(self, turns, seed):
        state.DB.execute("UPDATE chats SET protected_used = 1, web = 0 WHERE id = ?", (cid,))
        return await real(self, turns, seed)

    monkeypatch.setattr(chat.StreamTask, "_summarize", summarize)
    studio.gw.chat_script = [["Итог"]]
    new = go(studio, cid).json()["chat_id"]
    assert flags(new) == (1, 0)


def refused(s, cid) -> tuple:
    """Отказ `/continue` — (статус, код); диалогов после него столько же, сколько до."""
    before = count_chats()
    r = go(s, cid)
    assert count_chats() == before, r.text
    return code(r)


def test_continue_refusals_create_nothing(studio):
    add_gateway(studio); empty = new_chat(studio)
    assert refused(studio, empty) == (409, "nothing")
    assert go(studio, empty).json()["detail"]["message"] == chat_continue.NOTHING
    cid = new_chat(studio); sse(turn(studio, cid, "Один ход"))
    # идёт ответ — busy
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Второй"}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn"
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            assert refused(studio, cid) == (409, "busy")
            studio.gw.chat_hold = False
            assert [e for e, _ in events if e != "ping"][-1] == "done"
    finally:
        studio.gw.chat_hold = False
    # шлюз ответил 503 — карта занята
    studio.gw.chat_error = (503, {"error": {"message": "busy"}})
    assert refused(studio, cid) == (503, "card")
    studio.gw.chat_error = None
    # прямое подключение — no_compact
    direct = add_connection(studio)
    other = new_chat(studio, connection_id=direct)
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES ('u1', ?, 'user', 'a', 'done', 'browser', 'm', 1), ('a1', ?, 'assistant', 'b', 'done',"
                     " 'browser', 'm', 2)", (other, other))
    assert refused(studio, other) == (409, "no_compact")
    # диалог в корзине и чужая учётка — 404
    assert studio.c.delete(f"/api/chats/{other}").status_code == 204
    assert refused(studio, other) == (404, "gone")
    make_user(studio, "bob"); signin(studio, "bob")
    assert refused(studio, cid) == (404, "gone")
    # слова отказа по длине — копия chat.CONTEXT_WORDS в модуле страницы
    assert f"'{chat.CONTEXT_WORDS}'" in (STATIC / "js" / "chat-continue.js").read_text("utf-8")


def test_continue_refuses_cleaner_preview_dialogs_in_words(protected):
    """Предпросмотр Cleaner (`conn.cleaner`), как и шлюз: 409 `cleaner` словами, нового диалога нет."""
    studio, _ = protected
    cid = new_chat(studio)
    shown = preview(studio, cid)
    assert shown.status_code == 200, shown.text
    sse(turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown.json()["ticket"]))
    asked = len(studio.gw.chat_requests)
    assert refused(studio, cid) == (409, "cleaner")
    assert go(studio, cid).json()["detail"]["message"] == chat_continue.CLEANER
    assert len(studio.gw.chat_requests) == asked                       # итог модели не заказан


def test_continue_refuses_cleaner_dialogs_in_words(cleaner_gw):
    s, gw = cleaner_gw
    gw.models[0]["context_length"] = 131072; chat.LIMITS.clear()
    routed = routed_chat(s, gw)
    assert sse(turn(s, routed, "Привет"))[-1] == ("done", {"status": "done"})
    before, sent = count_chats(), len(gw.turns)
    refused = go(s, routed)
    assert refused.status_code == 409 and refused.json()["detail"] == {"code": "cleaner", "message": chat_continue.CLEANER}
    assert count_chats() == before and len(gw.turns) == sent
