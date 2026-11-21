"""План работы `todo_write` и личная память (спека 2026-11-21-studio-source-capabilities-design, п. 18): план —
в `messages.todo` ответа и событием SSE; память — шифром `STUDIO_DATA_KEY`, только при `users.memory_on`, кусок
системного сообщения после роли; стирание истории уносит память, выгрузка — с ней."""
import json
import logging

import chat_system
import chat_tools
import memory
import security
import state
import takeout
from conftest import add_gateway, erase_history, make_user, signin
from test_chat import messages, new_chat, sse, turn
from test_chat_web import call

PLAN = [{"text": "Найти источники", "status": "done"}, {"text": "Сравнить цены", "status": "in_progress"},
        {"text": "Написать вывод", "status": "pending"}]


def tool_names(request) -> list[str]:
    return [t["function"]["name"] for t in request.get("tools", [])]


def system_of(request) -> str:
    return request["messages"][0]["content"]


def uid_of(studio) -> int:
    return studio.c.get("/api/me").json()["id"]


def test_todo_write_persists_and_streams(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("todo_write", items=PLAN)]},
                             {"tool_calls": [call("todo_write", items=[{"text": "x" * 201, "status": "done"}])]},
                             ["Готово"]]
    events = sse(turn(studio, cid, "Сделай по плану"))
    assert events[-1] == ("done", {"status": "done"})
    assert "todo_write" in tool_names(studio.gw.chat_requests[0])
    assert ("todo", {"items": PLAN}) in events
    answer = messages(studio, cid)[-1]
    assert answer["todo"] == PLAN                      # неверный второй план прежний не затёр
    assert [s["kind"] for s in answer["tools"]] == ["todo", "todo"] and "error" in answer["tools"][1]
    stored = state.DB.execute("SELECT todo FROM messages WHERE id = ?", (answer["id"],)).fetchone()[0]
    assert json.loads(stored) == PLAN
    bad = [{"text": "a", "status": "later"}], [{"text": "", "status": "done"}], [{"text": "a", "status": "done"}] * 21
    for items in bad:
        assert chat_tools.todo_items(json.dumps({"items": items})) is None
    assert messages(studio, cid)[0].get("todo") is None


def test_memory_off_offers_no_memory_tools_and_no_block(studio):
    add_gateway(studio)
    me = studio.c.get("/api/me").json()
    assert me["memory_on"] is False
    saved = studio.c.post("/api/memory", json={"text": "Любит краткие ответы"})
    assert saved.status_code == 201
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("memory_save", text="Живёт в Казани")]}, ["Ладно"]]
    assert sse(turn(studio, cid, "Запомни"))[-1] == ("done", {"status": "done"})
    request = studio.gw.chat_requests[0]
    assert not {"memory_save", "memory_forget"} & set(tool_names(request))
    assert "Любит краткие ответы" not in system_of(request) and chat_system.MEMORY_HEAD not in system_of(request)
    assert chat_system.memory_block(uid_of(studio)) == ""
    assert [i["text"] for i in studio.c.get("/api/memory").json()["items"]] == ["Любит краткие ответы"]
    assert "error" in messages(studio, cid)[-1]["tools"][0]


def test_memory_saved_encrypted_and_in_system_block_capped(studio, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    add_gateway(studio)
    assert studio.c.patch("/api/me", json={"memory_on": True}).json()["memory_on"] is True
    cid = new_chat(studio)
    secret = "Любит краткие ответы без смайликов"
    studio.gw.chat_script = [{"tool_calls": [call("memory_save", text=secret)]}, ["Запомнил"]]
    assert sse(turn(studio, cid, "Запомни"))[-1] == ("done", {"status": "done"})
    assert {"memory_save", "memory_forget"} <= set(tool_names(studio.gw.chat_requests[0]))
    rows = state.DB.execute("SELECT id, text_enc FROM user_memory").fetchall()
    stored = rows[0]["text_enc"]
    assert len(rows) == 1 and secret.encode() not in stored and security.decrypt(stored) == secret
    item_id = rows[0]["id"]
    assert secret not in caplog.text and item_id not in caplog.text
    assert messages(studio, cid)[-1]["tools"] == [{"kind": "memory", "action": "save"}]
    # следующий ход видит запись в системном сообщении после роли
    studio.gw.chat_script = [["Ок"]]
    sse(turn(studio, cid, "Как отвечать?"))
    system = system_of(studio.gw.chat_requests[-1])
    assert chat_system.MEMORY_HEAD in system and secret in system and item_id[:8] in system
    # модель забывает по короткому номеру из блока
    studio.gw.chat_script = [{"tool_calls": [call("memory_forget", id=item_id[:8])]}, ["Забыл"]]
    sse(turn(studio, cid, "Забудь"))
    assert studio.c.get("/api/memory").json()["items"] == []
    # пределы: длина записи, число записей, кусок — не длиннее BLOCK_MAX, новые первыми
    assert studio.c.post("/api/memory", json={"text": "x" * (memory.ITEM_MAX + 1)}).status_code == 422
    assert studio.c.post("/api/memory", json={"text": "  "}).status_code == 422
    uid = uid_of(studio)
    for n in range(memory.MAX_ITEMS):
        studio.clock[0] += 1
        memory.save(uid, f"{n:03d} " + "ж" * 400)
    full = studio.c.post("/api/memory", json={"text": "лишняя"})
    assert full.status_code == 409 and full.json()["detail"]["code"] == "memory_full"
    block = chat_system.memory_block(uid)
    assert len(block) <= memory.BLOCK_MAX and block.startswith(chat_system.MEMORY_HEAD)
    assert block.index(f"{memory.MAX_ITEMS - 1:03d} ") < block.index(f"{memory.MAX_ITEMS - 2:03d} ")
    assert "000 " not in block
    listed = studio.c.get("/api/memory").json()
    assert listed["on"] is True and len(listed["items"]) == memory.MAX_ITEMS
    one = listed["items"][0]["id"]
    assert studio.c.delete(f"/api/memory/{one}").status_code == 204
    assert studio.c.delete(f"/api/memory/{one}").status_code == 404
    assert studio.c.delete("/api/memory").status_code == 204
    assert studio.c.get("/api/memory").json()["items"] == []


def test_memory_is_private_to_its_owner(studio):
    add_gateway(studio)
    studio.c.post("/api/memory", json={"text": "моё"})
    mine = studio.c.get("/api/memory").json()["items"][0]["id"]
    make_user(studio, "bob", is_admin=True)
    signin(studio, "bob")
    assert studio.c.get("/api/memory").json()["items"] == []
    assert studio.c.delete(f"/api/memory/{mine}").status_code == 404
    signin(studio, "owner")
    assert len(studio.c.get("/api/memory").json()["items"]) == 1


def test_history_erase_clears_memory(studio):
    add_gateway(studio)
    studio.c.post("/api/memory", json={"text": "моё"})
    uid = uid_of(studio)
    assert takeout._settings(uid)["memory"][0]["text"] == "моё"       # выгрузка истории — с памятью
    bob = make_user(studio, "bob")
    memory.save(bob, "его")
    assert erase_history(studio).status_code == 200
    assert memory.list_for(uid) == [] and [i["text"] for i in memory.list_for(bob)] == ["его"]
