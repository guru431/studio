"""«Продолжить иначе» (`chat_fork.py`, спека 2026-11-25-studio-skills-status-local-models п. 18): новый диалог с
историей до своего сообщения (выбранные варианты ответов, вложения — ссылками на те же файлы), текст сообщения —
странице для поля. Ветка наследует подключение, модель, роль, MCP, знания и отметку защищённого источника; диалог
через Cleaner не ветвится — отказ словами."""
import json
import logging

import chat
import chat_fork
import media
import ocr
import state
from conftest import add_gateway, make_user, signin
from test_chat import attach, messages, new_chat, png, sse, turn
from test_chat_system import role, with_role
from test_chat_web import call
from test_cleaner import EMAIL, preview, protected  # noqa: F401 — фикстура Cleaner (вид предпросмотра)
from test_cleaner_route_chat import cleaner_gw, routed_chat  # noqa: F401 — фикстура шлюза Cleaner
from test_knowledge import base
from test_mcp_dialog import name_of, offered, setup, tool, view
from test_protected_source import code_of, flags, names, protect, web_dialog

KEEP = ("connection_id", "model", "role", "mcp_ids", "thinking", "title")
#: столбцы диалога, которые ветка берёт у исходного как есть
INHERITED = ("connection_id", "model", "mcp_model_sid", "role_id", "thinking", "effort", "mcp_ids", "mcp_off",
             "mcp_tools_off", "protected_used", "title", "web")


def post(s, cid, message_id):
    return s.c.post(f"/api/chats/{cid}/fork", json={"message_id": message_id})


def fork(s, cid, message_id) -> dict:
    r = post(s, cid, message_id)
    assert r.status_code == 201, r.text
    return r.json()


def count_chats() -> int:
    return state.DB.execute("SELECT count(*) FROM chats").fetchone()[0]


def row_of(cid):
    return state.DB.execute("SELECT * FROM chats WHERE id = ?", (cid,)).fetchone()


def test_fork_copies_history_before_the_message_and_shares_files(studio, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    add_gateway(studio)
    uid = studio.c.get("/api/me").json()["id"]
    kb = base(studio)
    cid = new_chat(studio)
    assert studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]}).status_code == 200
    with_role(studio, cid, role(studio))
    assert studio.c.patch(f"/api/chats/{cid}", json={"thinking": True, "title": "Договор"}).status_code == 200
    doc = attach(studio, "notes.txt", "Срок оплаты — десять дней.".encode(), "text/plain")
    pic = attach(studio, "p.png", png(), "image/png")
    sse(turn(studio, cid, "Первый вопрос", attachment_ids=[doc["id"], pic["id"]]))
    studio.gw.chat_reply = ["Другой ", "ответ"]
    sse(turn(studio, cid, "", regenerate=True))                              # «Ответить заново»
    late = attach(studio, "late.txt", b"later", "text/plain")
    sse(turn(studio, cid, "Второй вопрос", attachment_ids=[late["id"]]))
    # хранимый пересказ, закрепление и согласия ветка не наследует
    state.DB.execute("UPDATE chats SET effort = 'high', mcp_off = '[7]', mcp_tools_off = '{\"7\": [\"x\"]}',"
                     " summary = 'Пересказ', summary_upto = ?, summary_at = 1, pinned = 1, web_consent = 5,"
                     " mcp_file_consent = '{\"7\": \"allowed\"}' WHERE id = ?", (messages(studio, cid)[1]["id"], cid))
    first, answer, second, _ = messages(studio, cid)
    before = count_chats()

    out = fork(studio, cid, second["id"])
    assert out["text"] == "Второй вопрос" and count_chats() == before + 1
    new = out["chat_id"]
    copy = messages(studio, new)
    assert [(m["role"], m["content"]) for m in copy] == [("user", "Первый вопрос"), ("assistant", answer["content"])]
    assert answer["content"] == "Другой ответ"                                # выбранный вариант
    assert {m["id"] for m in copy}.isdisjoint({first["id"], answer["id"]}) and "variants" not in copy[1]
    assert [(a["name"], a["media_id"]) for a in copy[0]["attachments"]] == [
        ("notes.txt", doc["media_id"]), ("p.png", pic["media_id"])]
    assert {a["id"] for a in copy[0]["attachments"]}.isdisjoint({doc["id"], pic["id"]})
    assert [view(studio, new)[k] for k in KEEP] == [view(studio, cid)[k] for k in KEEP]
    assert view(studio, new)["role"] is not None and view(studio, new)["thinking"] is True
    assert studio.c.get(f"/api/chats/{new}").json()["knowledge"] == [kb] and len(messages(studio, cid)) == 4
    source, branch = row_of(cid), row_of(new)
    assert [branch[k] for k in INHERITED] == [source[k] for k in INHERITED]
    assert (branch["summary"], branch["summary_upto"], branch["summary_at"], branch["pinned"],
            branch["web_consent"], branch["mcp_file_consent"]) == (None, None, None, 0, None, "{}")
    assert branch["created"] == branch["updated"] == branch["seen"]
    assert studio.c.get(f"/api/chats/{new}").json()["summary"] is None
    assert new in [c["id"] for c in studio.c.get("/api/chats").json()["items"]]

    # ход в ветке видит скопированную историю
    sse(turn(studio, new, "Третий вопрос"))
    sent = [m for m in studio.gw.chat_requests[-1]["messages"] if m["role"] != "system"]
    assert sent[1] == {"role": "assistant", "content": "Другой ответ"}

    assert studio.c.delete(f"/api/chats/{new}").status_code == 204
    studio.c.portal.call(chat.purge, uid, new)
    assert media.alive(uid, doc["media_id"]) and media.alive(uid, pic["media_id"])    # файлы держит исходный диалог
    assert len(messages(studio, cid)) == 4
    for secret in ("Первый вопрос", "Второй вопрос", "notes.txt", "p.png"):
        assert secret not in caplog.text
    assert f"чат {cid[:8]}: ветка {new[:8]}, сообщений 2" in caplog.text


def test_fork_point_rules(studio, monkeypatch):
    add_gateway(studio)
    cid = new_chat(studio)
    sse(turn(studio, cid, "Первое"))
    sse(turn(studio, cid, "Второе"))
    first, answer, with_skill, _ = messages(studio, cid)
    state.DB.execute("UPDATE messages SET content = ?, skill = ? WHERE id = ?",
                     ("НАВЫК.\n\nуточнение", json.dumps({"name": "Разбор", "chars": 6}), with_skill["id"]))
    assert fork(studio, cid, with_skill["id"])["text"] == "уточнение"      # чип навыка в поле ветки не идёт
    empty = fork(studio, cid, first["id"])                                     # ветка от первого сообщения
    assert empty["text"] == "Первое" and messages(studio, empty["chat_id"]) == []

    studio.gw.chat_reply = ["Другой ответ"]
    sse(turn(studio, cid, "", regenerate=True))
    unchosen = state.DB.execute("SELECT id FROM messages WHERE chat_id = ? AND unchosen = 1", (cid,)).fetchone()[0]
    before = count_chats()
    for bad in (answer["id"], unchosen, "0" * 32):
        assert code_of(post(studio, cid, bad), 404) == "gone"
    assert code_of(post(studio, "0" * 32, first["id"]), 404) == "gone"     # нет диалога
    assert count_chats() == before

    make_user(studio, "other")                                                 # чужой диалог — как несуществующий
    signin(studio, "other")
    assert code_of(post(studio, cid, with_skill["id"]), 404) == "gone"
    signin(studio, "owner")

    # документ истории до точки ветки ещё распознаётся — 409 `not_ready`, диалога нет
    scanned = attach(studio, "скан.txt", b"text", "text/plain")
    later = new_chat(studio)
    sse(turn(studio, later, "Со сканом", attachment_ids=[scanned["id"]]))
    sse(turn(studio, later, "Дальше"))
    state.DB.execute("UPDATE attachments SET text = NULL WHERE id = ?", (scanned["id"],))
    monkeypatch.setattr(ocr, "pending", lambda uid, ids: ["scan"] if scanned["id"] in ids else [])
    point = messages(studio, later)[2]["id"]
    before = count_chats()
    refused = post(studio, later, point)
    assert code_of(refused) == "not_ready" and refused.json()["detail"]["message"] == chat.NOT_READY
    assert count_chats() == before
    # скан, вложенный в само сообщение-точку, не мешает: вложения точки не переносятся
    assert messages(studio, fork(studio, later, messages(studio, later)[0]["id"])["chat_id"]) == []
    # корзина: из корзины ветки нет
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204
    assert code_of(post(studio, cid, first["id"]), 404) == "gone"


def test_fork_of_a_protected_dialog_stays_protected(studio):                   # Review Focus 3
    sid, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid)
    cid = web_dialog(studio)
    sse(turn(studio, cid, "До источника"))                                      # диалог ещё не помечен
    assert flags(cid) == (0, 1)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Use tool"))
    assert flags(cid) == (1, 0)
    new = fork(studio, cid, messages(studio, cid)[2]["id"])["chat_id"]         # история в ветке — до срабатывания
    assert [m["content"] for m in messages(studio, new)][0] == "До источника"
    assert flags(new) == (1, 0) and view(studio, new)["protected"] is True and view(studio, new)["web"] is False
    assert code_of(studio.c.patch(f"/api/chats/{new}", json={"web": True})) == "protected"
    studio.gw.chat_script = [["Ответ"]]
    sse(turn(studio, new, "Вопрос в ветке"))
    assert names(studio.gw.chat_requests[-1]) == [] and offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    assert code_of(studio.c.post(f"/api/chats/{new}/share", json={"pin": "0123"})) == "protected"
    assert code_of(studio.c.post(f"/api/chats/{new}/export", json={"format": "md"})) == "protected"


def test_fork_refuses_cleaner_dialogs_in_words(cleaner_gw):
    s, gw = cleaner_gw
    routed = routed_chat(s, gw)
    assert sse(turn(s, routed, "Привет"))[-1] == ("done", {"status": "done"})
    mid = messages(s, routed)[0]["id"]
    before = count_chats()
    refused = post(s, routed, mid)
    assert refused.status_code == 409 and refused.json()["detail"] == {"code": "cleaner", "message": chat_fork.CLEANER}
    assert count_chats() == before


def test_fork_refuses_cleaner_preview_dialogs_in_words(protected):
    studio, remote = protected
    cid = new_chat(studio)
    shown = preview(studio, cid)
    assert shown.status_code == 200, shown.text
    sse(turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown.json()["ticket"]))
    before = count_chats()
    refused = post(studio, cid, messages(studio, cid)[0]["id"])
    assert refused.status_code == 409 and refused.json()["detail"] == {"code": "cleaner", "message": chat_fork.CLEANER}
    assert count_chats() == before
