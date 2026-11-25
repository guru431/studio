"""«Поделиться» перепиской Open WebUI: снимок шифром со своим `item_kind` (`chat_shares.OWUI`), экспорт Markdown/PDF."""
import logging

import chat_shares
import security
import state
from conftest import make_user, signin
from test_chat_shares import create, unlock
from test_owui_read import BASE, setup, talk  # noqa: F401 — `setup` — фикстура


def test_owui_link_is_a_sealed_snapshot_of_its_own_kind(setup, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    s, fake, aid = setup; chat = talk(fake); endpoint = f"{BASE}/{aid}/chats/{chat}/share"
    assert s.c.get(endpoint).json() == {"enabled": False}
    out, token = create(s, endpoint)
    assert s.c.get(endpoint).json() == out
    opened = unlock(s, token).json()
    assert [(m["role"], m["text"]) for m in opened["messages"]] == [("user", "hello"), ("assistant", "hi there")]
    assert opened["messages"][0]["files"] == ["doc.txt"] and opened["messages"][1]["reasoning"] is None
    assert opened["title"] == "Talk" and "item" not in opened and opened["messages"][0]["attachments"] == []
    assert [m["status"] for m in opened["messages"]] == ["done", "done"] and opened["messages"][1]["error"] is None
    row = state.DB.execute("SELECT * FROM chat_shares").fetchone()
    assert (row["item_kind"], row["item_id"], row["chat_id"], row["agent_id"]) == ("owui", f"{aid}:{chat}", None, None)
    assert row["snapshot"] == chat_shares.PLACEHOLDER_RAW and security.decrypt(row["label"]) == "Talk"
    assert b"hi there" not in bytes(row["sealed"])                                    # снимок — только шифром
    listed = s.c.get("/api/me/access").json()["shares"]
    assert [(i["kind"], i["item_id"], i["title"]) for i in listed] == [("owui", f"{aid}:{chat}", "Talk")]
    kinds = [(e["kind"], e["detail"]) for e in s.c.get("/api/me/security-events").json()["items"]]
    assert ("share_create", "owui") in kinds
    # fake меняет ответ; PUT — тот же адрес и PIN, unlock показывает новое
    fake.chats[chat]["messages"]["a1"]["output"] = [
        {"type": "message", "id": "msg2", "content": [{"type": "output_text", "text": "new answer"}]},
        {"type": "function_call", "call_id": "c1", "name": "search", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "found it"}]}]
    refreshed = s.c.put(endpoint, json={})
    assert refreshed.status_code == 200 and refreshed.json()["url"] == out["url"]
    answer = unlock(s, token).json()["messages"][1]
    assert answer["text"] == "new answer" and answer["steps"] == [{"name": "search", "text": "found it"}]
    # Open WebUI молчит — POST 503 `owui_offline`, строка ссылки прежняя
    fake.down = True
    offline = s.c.post(endpoint, json={"pin": "4321"})
    assert offline.status_code == 503 and offline.json()["detail"]["code"] == "owui_offline"
    assert state.DB.execute("SELECT id FROM chat_shares").fetchone()[0] == row["id"]
    assert unlock(s, token).status_code == 200
    fake.down = False
    # чужая учётка Студии — 404 на все четыре метода
    make_user(s, "other")
    signin(s, "other")
    for method in ("GET", "POST", "PUT", "DELETE"):
        assert s.c.request(method, endpoint, json={"pin": "0123"}).status_code == 404
    signin(s, "owner")
    assert s.c.delete(f"{BASE}/{aid}").status_code == 204                             # учётка Open WebUI удалена
    assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0 and unlock(s, token).status_code == 404
    assert "hello" not in caplog.text and "hi there" not in caplog.text and "Talk" not in caplog.text
    assert chat not in caplog.text


def test_owui_revoke_removes_only_its_link(setup):
    s, fake, aid = setup
    first, second = talk(fake), talk(fake, "Other")
    _, token = create(s, f"{BASE}/{aid}/chats/{first}/share")
    _, kept = create(s, f"{BASE}/{aid}/chats/{second}/share")
    assert s.c.delete(f"{BASE}/{aid}/chats/{first}/share").status_code == 204
    assert unlock(s, token).status_code == 404 and unlock(s, kept).status_code == 200
    assert s.c.get(f"{BASE}/{aid}/chats/{first}/share").json() == {"enabled": False}
    assert s.c.get(f"{BASE}/{aid}/chats/a%3Fb/share").status_code == 404


def test_owui_export_gives_markdown_and_follows_the_section_lock(setup):
    s, fake, aid = setup; chat = talk(fake)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    r = s.c.post(f"{BASE}/{aid}/chats/{chat}/export", json={"format": "md", "title": "Talk", "lang": "ru"})
    assert r.status_code == 200 and "hi there" in r.text and r.headers["cache-control"] == "no-store"
    assert "hmm" not in r.text                                              # рассуждение в файл не идёт
    assert r.headers["content-type"].startswith("text/markdown") and "Talk.md" in r.headers["content-disposition"]
    # раздел Open WebUI заперт (lock_policy) — share и export отвечают 423 `screen_locked`
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"owui\"]' WHERE id=?", (uid,))
    state.DB.execute("UPDATE sessions SET seen=0")
    for method, path, body in (("POST", "export", {"format": "md"}), ("POST", "share", {"pin": "0123"}),
                               ("GET", "share", None)):
        r = s.c.request(method, f"{BASE}/{aid}/chats/{chat}/{path}", json=body)
        assert r.status_code == 423 and r.json()["detail"]["code"] == "screen_locked", path
    assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0
