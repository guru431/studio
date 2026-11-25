"""Навык в ходе «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 2, решения 2 и 18; app/chat_skills.py):
текст навыка библиотеки — в начале сообщения пользователя, `messages.skill` = {name, chars, parts}; в ссылку и экспорт
идёт имя навыка и то, что дописал человек, в выгрузку своих данных — сообщение целиком. В журнал — ничего из навыка."""
import json
import logging

import chat_shares
import state
from conftest import add_gateway
from test_chat import messages, new_chat, sse, turn
from test_takeout import download, unzip

TEXT = "Ты — редактор. Сократи текст вдвое."
NAME = "Сократить"


def skill(s, name=NAME, text=TEXT, scope="chat", parts=False) -> str:
    r = s.c.post("/api/skills", json={"name": name, "text": text, "scope": scope, "parts": parts})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ready(s) -> tuple[str, str]:
    add_gateway(s)
    return new_chat(s), skill(s)


def code(r) -> tuple:
    return r.status_code, r.json()["detail"]["code"]


def chat_row(cid):
    return state.DB.execute("SELECT * FROM chats WHERE id = ?", (cid,)).fetchone()


def test_turn_with_skill_prepends_text_and_marks_message(studio):
    cid, sid = ready(studio)
    sse(turn(studio, cid, "вот текст", skill_id=sid))
    user, answer = messages(studio, cid)
    assert user["content"] == TEXT + "\n\nвот текст" and answer["skill"] is None
    assert user["skill"] == {"name": NAME, "chars": len(TEXT), "parts": False}
    assert studio.gw.chat_requests[-1]["messages"][-1] == {"role": "user", "content": user["content"]}
    assert chat_row(cid)["title"] == "вот текст"
    # один навык без текста — не 422 `empty`: сообщение — текст навыка, название диалога — имя навыка
    other = new_chat(studio)
    sse(turn(studio, other, "", skill_id=sid))
    assert messages(studio, other)[0]["content"] == TEXT and chat_row(other)["title"] == NAME


def test_unknown_foreign_and_agents_only_skill_is_refused_before_writing(studio):
    cid, _ = ready(studio)
    assert code(turn(studio, cid, "x", skill_id="0" * 32)) == (404, "gone")
    state.DB.execute("INSERT INTO users(login, name, pw_hash, is_admin, created, email)"
                     " VALUES ('other', 'other', '', 0, 0, 'other@example.com')")
    other = state.DB.execute("SELECT id FROM users WHERE login = 'other'").fetchone()[0]
    state.DB.execute("INSERT INTO skills(id, user_id, name, description, text, scope, quick, parts, position, updated)"
                     " VALUES ('foreign', ?, 'Чужой', '', 'чужой текст', 'chat', 0, 0, 0, 0)", (other,))
    assert code(turn(studio, cid, "x", skill_id="foreign")) == (404, "gone")
    agents = skill(studio, name="Только агенты", scope="agents")
    assert code(turn(studio, cid, "x", skill_id=agents)) == (422, "skill")
    assert messages(studio, cid) == []
    assert studio.gw.chat_requests == []


def test_edit_keeps_or_drops_the_skill(studio):
    cid, sid = ready(studio)
    sse(turn(studio, cid, "вот текст", skill_id=sid))
    user, answer = messages(studio, cid)
    # навык в библиотеке изменили: правка с skill_keep оставляет прежний текст навыка
    assert studio.c.put(f"/api/skills/{sid}", json={"name": "Иначе", "text": "Новый текст навыка",
                                                    "scope": "chat"}).status_code == 200
    sse(turn(studio, cid, "иначе", edit={"id": user["id"], "last_id": answer["id"]}, skill_keep=True))
    edited, answer = messages(studio, cid)
    assert edited["content"] == TEXT + "\n\nиначе" and edited["skill"]["chars"] == len(TEXT)
    assert edited["skill"]["name"] == NAME
    # и удалили — skill_keep всё ещё берёт текст из сообщения
    assert studio.c.delete(f"/api/skills/{sid}").status_code == 204
    sse(turn(studio, cid, "ещё", edit={"id": user["id"], "last_id": answer["id"]}, skill_keep=True))
    edited, answer = messages(studio, cid)
    assert edited["content"] == TEXT + "\n\nещё" and edited["skill"]["name"] == NAME
    # правка без skill_keep — навыка больше нет
    sse(turn(studio, cid, "без навыка", edit={"id": user["id"], "last_id": answer["id"]}))
    edited, answer = messages(studio, cid)
    assert edited["content"] == "без навыка" and edited["skill"] is None
    # skill_keep у сообщения без навыка, skill_id вместе с skill_keep, regenerate вместе с skill_id — 422 `invalid`
    edit = {"id": user["id"], "last_id": answer["id"]}
    assert code(turn(studio, cid, "x", edit=edit, skill_keep=True)) == (422, "invalid")
    fresh = skill(studio, name="Ещё")
    assert code(turn(studio, cid, "x", edit=edit, skill_keep=True, skill_id=fresh)) == (422, "invalid")
    assert code(turn(studio, cid, "", regenerate=True, skill_id=fresh)) == (422, "invalid")
    assert code(turn(studio, cid, "x", skill_keep=True)) == (422, "invalid")              # без правки
    assert messages(studio, cid)[0]["content"] == "без навыка"


def test_same_request_id_with_another_skill_is_a_conflict(studio):
    cid, sid = ready(studio)
    other = skill(studio, name="Другой", text="Другой текст")
    request_id = "r" * 20
    sse(turn(studio, cid, "вот текст", skill_id=sid, request_id=request_id))
    assert turn(studio, cid, "вот текст", skill_id=sid, request_id=request_id).json()["replayed"] is True
    assert code(turn(studio, cid, "вот текст", skill_id=other, request_id=request_id)) == (409, "request_conflict")
    assert code(turn(studio, cid, "вот текст", request_id=request_id)) == (409, "request_conflict")


def test_link_and_export_carry_the_name_not_the_text_and_takeout_carries_all(studio):
    cid, sid = ready(studio)
    sse(turn(studio, cid, "вот текст", skill_id=sid))
    uid = chat_row(cid)["user_id"]
    snapshot, _ = chat_shares.regular_snapshot(uid, cid)
    assert snapshot["messages"][0]["text"] == "вот текст" and snapshot["messages"][0]["skill"] == NAME
    assert snapshot["messages"][1]["skill"] is None
    assert TEXT not in json.dumps(snapshot, ensure_ascii=False)
    md = studio.c.post(f"/api/chats/{cid}/export", json={"format": "md"}).text
    assert "Навык: Сократить" in md and TEXT not in md
    en = studio.c.post(f"/api/chats/{cid}/export", json={"format": "md", "lang": "en"}).text
    assert "Skill: Сократить" in en and TEXT not in en
    _, z = download(studio, ["chat"])
    took = json.loads(unzip(z).read(f"chats/{cid}.json"))
    assert took["messages"][0]["content"].startswith(TEXT) and took["messages"][0]["skill"]["name"] == NAME
    assert took["messages"][1]["skill"] is None


def test_estimate_counts_the_chosen_skill(studio):
    cid, sid = ready(studio)
    big = skill(studio, name="Большой", text="навык " * 500)
    bare = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "вот"}).json()["tokens"]
    more = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "вот", "skill_id": big}).json()["tokens"]
    assert more - bare >= len("навык " * 500) // 3
    # незнакомый навык — оценка как без него
    assert studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "вот", "skill_id": "nope"}).json()["tokens"] == bare


def test_skill_never_reaches_the_log(studio, caplog):
    cid, sid = ready(studio)
    with caplog.at_level(logging.INFO, logger="studio"):
        sse(turn(studio, cid, "вот текст", skill_id=sid))
        user, answer = messages(studio, cid)
        sse(turn(studio, cid, "иначе", edit={"id": user["id"], "last_id": answer["id"]}, skill_keep=True))
        studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "вот", "skill_id": sid})
    assert NAME not in caplog.text and TEXT not in caplog.text and "Сократи" not in caplog.text
