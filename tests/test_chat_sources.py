"""Источники «Знаний» под ответом «Чата» (спека 2026-11-25, п. 28): шаг `knowledge_search` и шаг автоматического
блока (`auto`) называют документ и номер куска (`hits`), текст куска — маршрутом `chat_sources` с проверкой доступа."""
import json

import chat_shares
import knowledge
import state
from conftest import add_connection, add_gateway, make_user, signin
from test_chat import attach, messages, new_chat, sse, turn
from test_chat_web import call, steps_of
from test_knowledge import base, big_text, small_model, upload
from test_protected_source import code_of


def chunk_text(doc_id: str, n: int) -> str:
    return state.DB.execute("SELECT text FROM kb_chunks WHERE doc_id = ? AND n = ?", (doc_id, n)).fetchone()[0]


def linked(studio) -> tuple[str, str]:
    """База «Справочник» со справка.txt (секрет посреди большого текста) и диалог, связанный с ней: (kb, cid)."""
    kb = base(studio)
    upload(studio, kb, "справка.txt", big_text("Пароль от сейфа — 4711."))
    cid = new_chat(studio)
    assert studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]}).status_code == 200
    return kb, cid


def test_knowledge_steps_name_document_and_chunk_and_give_the_text(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    kb, cid = linked(studio)
    studio.gw.chat_script = [{"tool_calls": [call("knowledge_search", query="пароль от сейфа")]}, ["4711"]]
    sse(turn(studio, cid, "Какой пароль от сейфа?"))
    answer = messages(studio, cid)[1]
    auto, asked = answer["tools"]
    assert (auto["kind"], auto["auto"], auto["sources"]) == ("knowledge", True, ["справка.txt"])   # автоматический блок
    assert auto["found"] == len(auto["hits"]) >= 1
    assert "<knowledge>" in studio.gw.chat_requests[0]["messages"][-1]["content"]
    assert asked["query"] == "пароль от сейфа" and set(asked["hits"][0]) == {"doc", "n", "id"} and "text" not in asked
    url = f"/api/chats/{cid}/messages/{answer['id']}/sources"
    hit = asked["hits"][0]
    got = studio.c.get(f"{url}/1/0").json()
    assert got["doc"] == "справка.txt" and got["n"] == hit["n"] and got["text"] == chunk_text(hit["id"], hit["n"])
    first = auto["hits"][0]
    assert studio.c.get(f"{url}/0/0").json()["text"] == chunk_text(first["id"], first["n"])
    assert all(isinstance(s.get("text"), str) for s in steps_of(answer["id"]))        # прежний compose читает оба шага
    sse(turn(studio, cid, "Ещё"))
    again = "".join(str(m["content"]) for m in studio.gw.chat_requests[-1]["messages"][:-1])
    assert again.count('<web kind="knowledge">') == 1                                 # авто-шаг в историю не идёт
    snapshot, _ = chat_shares.regular_snapshot(uid, cid)
    shown = json.dumps(snapshot, ensure_ascii=False)
    assert "hits" in shown and "Обычная строка" not in shown
    for bad in ("9/0", "1/99", "0/-1"):
        assert studio.c.get(f"{url}/{bad}").status_code == 404
    make_user(studio, "bob")
    signin(studio, "bob")
    assert studio.c.get(f"{url}/1/0").status_code == 404                              # чужая учётка
    signin(studio, "owner")
    assert studio.c.delete(f"/api/knowledge/{kb}/documents/{hit['id']}").status_code in (200, 204)
    assert code_of(studio.c.get(f"{url}/1/0"), 404) == "gone"


def test_direct_turn_records_the_auto_step_before_the_json_answer(studio):
    add_connection(studio)
    _, cid = linked(studio)
    body = turn(studio, cid, "Какой пароль от сейфа?").json()
    assert body["messages"][-1]["content"].startswith("<knowledge>")
    (step,) = steps_of(body["assistant_message_id"])
    assert (step["kind"], step["auto"], step["text"], step["sources"]) == ("knowledge", True, "", ["справка.txt"])
    assert step["found"] == len(step["hits"]) >= 1


def test_attachment_document_of_another_chat_is_not_a_source(studio):
    """Ревью, п. 6: документ большого вложения — источник только своего диалога; из другого диалога той же учётки
    (подставленный в шаг) — 404 `gone`, и `knowledge.chunk` — None."""
    gw = add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    first = new_chat(studio)
    small_model(studio, first, gw, 20_000)                                            # выдержками — документ знаний
    doc = attach(studio, "отчёт.txt", big_text("Пароль от сейфа — 4711.").encode())
    sse(turn(studio, first, "Какой пароль от сейфа?", attachment_ids=[doc["id"]]))
    doc_id = state.DB.execute("SELECT id FROM kb_documents WHERE attachment_id = ?", (doc["id"],)).fetchone()[0]
    n = state.DB.execute("SELECT min(n) FROM kb_chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0]
    assert knowledge.chunk(uid, first, doc_id, n)["doc"] == "отчёт.txt"               # свой диалог — кусок есть
    other = new_chat(studio)
    studio.gw.chat_script = [["Ответ"]]
    sse(turn(studio, other, "Привет"))
    assert knowledge.chunk(uid, other, doc_id, n) is None
    answer = messages(studio, other)[1]
    forged = [{"kind": "knowledge", "query": "пароль", "found": 1, "sources": ["отчёт.txt"], "text": "",
               "hits": [{"doc": "отчёт.txt", "n": n, "id": doc_id}]}]
    state.DB.execute("UPDATE messages SET tools = ? WHERE id = ?", (json.dumps(forged, ensure_ascii=False), answer["id"]))
    got = studio.c.get(f"/api/chats/{other}/messages/{answer['id']}/sources/0/0")
    assert code_of(got, 404) == "gone"
    own = messages(studio, first)[1]["id"]                                            # тот же шаг в своём диалоге — 200
    state.DB.execute("UPDATE messages SET tools = ? WHERE id = ?", (json.dumps(forged, ensure_ascii=False), own))
    assert studio.c.get(f"/api/chats/{first}/messages/{own}/sources/0/0").json()["doc"] == "отчёт.txt"


def test_head_of_a_document_without_matches_is_not_a_source(studio):
    """Ничего не нашлось — шага нет (начало документа, взятое за неимением совпадений, — не источник)."""
    add_gateway(studio)
    _, cid = linked(studio)
    studio.gw.chat_script = [["Не знаю"]]
    sse(turn(studio, cid, "Вулкан"))
    assert messages(studio, cid)[1]["tools"] == []
