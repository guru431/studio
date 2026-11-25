"""Список диалогов «Чата» (спека 2026-11-25, п. 23): метки «идёт ответ» и «не прочитано» (`chat_list.marks`),
закрепление (`PATCH {pinned}`) и отметка взгляда на диалог (`POST /api/chats/{id}/seen`)."""
import chat
import state
from conftest import add_connection, add_gateway, make_user, signin
from test_chat import chat_streams, events_of, new_chat, turn, wait


def test_list_marks_busy_unread_and_pinned(studio, no_pings):
    gateway = add_gateway(studio)
    a, b = new_chat(studio, connection_id=gateway), new_chat(studio, connection_id=gateway)
    cloud = add_connection(studio)
    direct = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    assert turn(studio, direct, "Привет").json()["assistant_message_id"]        # ответ читает вкладка браузера

    def row(cid):
        return next(i for i in studio.c.get("/api/chats").json()["items"] if i["id"] == cid)

    assert (row(a)["busy"], row(a)["unread"], row(a)["pinned"]) == (False, False, False)
    studio.clock[0] += 1                                                       # ответ позже создания диалога
    studio.gw.chat_hold = True
    with studio.c.stream("POST", f"/api/chats/{a}/turns", json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines())
        next(events)
        next(d for e, d in events if e == "delta")
        assert (row(a)["busy"], row(a)["unread"], row(b)["busy"]) == (True, False, False)
        stream = chat_streams()[0]
    studio.gw.chat_hold = False
    wait(studio, stream)
    assert (row(a)["busy"], row(a)["unread"]) == (False, True)                 # ответ кончился, диалог не открывали
    assert studio.c.post(f"/api/chats/{a}/seen").status_code == 204 and row(a)["unread"] is False
    state.DB.execute("UPDATE chats SET seen = NULL, updated = updated + 10 WHERE id = ?", (a,))
    assert row(a)["unread"] is False                                           # диалог до шага 82
    pinned = studio.c.patch(f"/api/chats/{b}", json={"pinned": True})
    assert pinned.json()["pinned"] is True and row(b)["pinned"] is True
    assert studio.c.patch(f"/api/chats/{b}", json={"title": "Имя"}).json()["pinned"] is True   # прочая правка не снимает
    assert studio.c.get(f"/api/chats/{b}").json()["chat"]["pinned"] is True
    assert (row(direct)["busy"], row(direct)["unread"]) == (True, False)       # прямое: streaming с живым alive_at
    studio.clock[0] += chat.ALIVE_SEC + 1
    assert (row(direct)["busy"], row(direct)["unread"]) == (False, False)      # брошен
    assert studio.c.patch(f"/api/chats/{b}", json={"pinned": False}).json()["pinned"] is False
    make_user(studio, "friend")
    studio.c.cookies.clear()
    signin(studio, "friend")                                                   # чужой диалог — 404 обоим маршрутам
    seen, patched = studio.c.post(f"/api/chats/{a}/seen"), studio.c.patch(f"/api/chats/{b}", json={"pinned": True})
    assert [seen.status_code, patched.status_code] == [404, 404] and seen.json()["detail"]["code"] == "gone"


def test_seen_of_a_trashed_chat_is_gone(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204
    r = studio.c.post(f"/api/chats/{cid}/seen")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
