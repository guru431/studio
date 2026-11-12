"""Совместная работа: выдача, границы проекта, личные вкладки и отзыв посреди ответа."""
import asyncio

import auth
import bridge
import chat_shares
import claude_code
import security
import state
from fakes import PNG_REAL
from conftest import make_user, signin
from test_code import A, ALPHA, BETA, DONE, setup, url


def share(s):
    aid, machine, link = setup(s, pin="2468")
    member = make_user(s, "friend")
    base = url(aid, "projects", ALPHA, "members")
    response = s.c.post(base, json={"login": " FRIEND "})
    assert response.status_code == 201, response.text
    return aid, machine, link, member, base


def test_member_sees_only_shared_project_and_can_work_without_owner_pin(studio):
    aid, machine, link, member, base = share(studio)
    machine.projects["alpha"]["files"]["a.txt"] = b"hello"
    signin(studio, "friend")
    agent = studio.c.get("/api/agents").json()["items"][0]
    assert agent["id"] == aid and agent["shared"] and not agent["projects_root"]
    assert not any(agent[k] for k in ("computer_info", "desktop", "agent_update", "subscription_usage"))
    assert studio.c.get(url(aid, "projects")).json() == {"items": [{"pid": ALPHA, "name": "alpha"}]}
    response = studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "a.txt"})
    assert response.status_code == 200 and response.json()["text"] == "hello"
    version = response.json()["version"]
    response = studio.c.put(url(aid, "projects", ALPHA, "file"), json={"path": "a.txt", "text": "changed", "version": version})
    assert response.status_code == 200 and machine.projects["alpha"]["files"]["a.txt"] == b"changed"
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    machine.script = [DONE]
    response = studio.c.post(url(aid, "projects", ALPHA, "chats", chat, "turn"), json={"text": "hello"})
    assert response.status_code == 200 and "event: done" in response.text
    assert machine.requests[-1]["shared_projects"] == [ALPHA]
    assert bridge.tag(auth.security.digest(studio.c.cookies.get(auth.COOKIE))) not in machine.open_sessions
    signin(studio, "owner")
    assert studio.c.post(url(aid, "open"), json={"pin": "2468"}).status_code == 204
    assert chat in [i["chat"] for i in studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]]


def test_member_cannot_reach_other_projects_or_manage_computer_or_members(studio):
    aid, machine, _, _, base = share(studio)
    signin(studio, "friend")
    before = len(machine.requests)
    for method, path, body in [
        ("GET", url(aid, "projects", BETA, "chats"), None),
        ("POST", url(aid, "projects", BETA, "read"), {"path": "a.txt"}),
        ("GET", url(aid, "info"), None), ("GET", url(aid, "desktop"), None),
        ("POST", url(aid, "update"), None), ("GET", url(aid, "usage"), None),
        ("GET", base, None), ("POST", base, {"login": "owner"}), ("DELETE", base + "/1", None),
        ("PATCH", "/api/code/workspace", {"op": "place", "aid": aid, "pid": BETA, "view": "files"}),
    ]:
        response = studio.c.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, path, response.text)
    assert len(machine.requests) == before


def test_workspace_is_personal_and_revocation_clears_member_access(studio):
    aid, machine, _, member, base = share(studio)
    def open_tab(chat):
        r = studio.c.patch("/api/code/workspace", json={"op": "open", "aid": aid, "pid": ALPHA, "chat": chat})
        assert r.status_code == 200, r.text
        return r.json()
    owner = open_tab(A)
    signin(studio, "friend")
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    assert open_tab(chat)["projects"][0]["tabs"] == [chat]
    signin(studio, "owner")
    assert studio.c.get("/api/code/workspace").json() == owner
    assert studio.c.delete(base + f"/{member}").status_code == 204
    signin(studio, "friend")
    assert studio.c.get("/api/agents").json() == {"items": []}
    assert studio.c.get("/api/code/workspace").json()["projects"] == []
    assert studio.c.get(url(aid, "projects", ALPHA, "chats")).status_code == 404


def test_revoked_late_reply_does_not_disclose_contents(studio):
    aid, machine, _, member, base = share(studio)
    original = machine._op_history
    def reply(req, events):
        result = original(req, events)
        state.DB.execute("DELETE FROM project_members WHERE user_id = ?", (member,))
        return result
    machine._op_history = reply
    signin(studio, "friend")
    response = studio.c.get(url(aid, "projects", ALPHA, "chats", A))
    assert response.status_code == 404 and "items" not in response.text


def test_old_bridge_refuses_grants_and_disabled_owner_hides_project(studio):
    aid, machine, link, member, base = share(studio)
    link.hello["project_sharing"] = False
    assert studio.c.post(base, json={"login": "friend"}).status_code == 409
    signin(studio, "friend")
    assert studio.c.get(url(aid, "projects")).status_code == 409
    state.DB.execute("UPDATE users SET disabled = 1 WHERE login = 'owner'")
    assert studio.c.get("/api/agents").json() == {"items": []}
    assert studio.c.get(url(aid, "projects", ALPHA, "chats")).status_code == 404


def test_stream_rechecks_membership_before_first_and_queued_events(studio):
    aid, _, _, member, _ = share(studio)
    signin(studio, "friend")
    digest = state.DB.execute("SELECT digest FROM sessions WHERE user_id = ?", (member,)).fetchone()[0]
    async def run():
        q = bridge.Watch(None)
        stream = claude_code._events(q, ("snapshot", {"text": "allowed"}), False, digest, (aid, ALPHA))
        assert "allowed" in await anext(stream)
        state.DB.execute("DELETE FROM project_members WHERE user_id = ?", (member,))
        q.put_nowait(("ev", {"k": "text", "text": "private"}))
        assert "event: closed" in await anext(stream)
        await stream.aclose()
        stream = claude_code._events(q, ("snapshot", {"text": "private"}), False, digest, (aid, ALPHA))
        assert "event: closed" in await anext(stream)
        await stream.aclose()
    asyncio.run(run())


def test_access_check_is_scoped_even_when_another_project_is_shared(studio):
    aid, _, _, member, base = share(studio)
    other = url(aid, "projects", BETA, "members")
    assert studio.c.post(other, json={"login": "friend"}).status_code == 201
    assert studio.c.delete(base + f"/{member}").status_code == 204
    signin(studio, "friend")
    assert studio.c.get(url(aid, "access") + f"?project={ALPHA}").status_code == 404
    assert studio.c.get(url(aid, "access") + f"?project={BETA}").status_code == 204


def member_link(aid, uid, session=A) -> str:
    """Ссылка «Поделиться» участника на агентский чат владельца — так её записывал прежний код."""
    share_id = security.new_token()[:32]
    state.DB.execute(
        "INSERT INTO chat_shares(id,user_id,agent_id,project,session_id,engine,token_hash,token_enc,pin_hash,"
        "snapshot,created) VALUES(?,?,?,?,?,'claude',?,?,'x','{}',0)",
        (share_id, uid, aid, ALPHA, session, security.digest(share_id), security.encrypt(share_id)))
    return share_id


def links(**where) -> list:
    sql = " AND ".join(f"{k} = ?" for k in where) or "1"
    return [r[0] for r in state.DB.execute(f"SELECT id FROM chat_shares WHERE {sql}", tuple(where.values()))]


def test_only_the_computer_owner_shares_agent_chats(studio):
    """FINDINGS «Ссылка участника на агентский чат владельца невидима владельцу и переживает отзыв»: участник
    ссылку не создаёт и не отзывает; старая строка участника не мешает владельцу и уходит при старте."""
    aid, machine, _, member, _ = share(studio)
    endpoint = url(aid, "projects", ALPHA, "chats", A, "share")
    signin(studio, "friend")
    assert studio.c.get(endpoint).json() == {"enabled": False}
    created = studio.c.post(endpoint, json={"pin": "0123"})
    assert created.status_code == 403 and created.json()["detail"]["code"] == "owner_only"
    assert studio.c.delete(endpoint).status_code == 403
    assert links() == []
    old = member_link(aid, member)                                   # записал прежний код (откат)
    signin(studio, "owner")
    assert studio.c.post(url(aid, "open"), json={"pin": "2468"}).status_code == 204
    assert chat_shares.disown() == 1 and links() == [] and old not in links()
    assert studio.c.post(endpoint, json={"pin": "0123"}).status_code == 201
    assert studio.c.get(endpoint).json()["enabled"] is True
    assert chat_shares.disown() == 0                                 # своя ссылка владельца — на месте
    signin(studio, "friend")
    assert studio.c.get(endpoint).json() == {"enabled": False}       # ссылку владельца участник не видит
    assert studio.c.delete(endpoint).status_code == 403
    assert len(links()) == 1


def test_member_links_go_with_revocation_history_wipe_and_emergency_exit(studio):
    aid, _, _, member, base = share(studio)
    member_link(aid, member)
    assert studio.c.delete(base + f"/{member}").status_code == 204
    assert links(user_id=member) == []
    assert studio.c.post(base, json={"login": "friend"}).status_code == 201
    member_link(aid, member)
    assert studio.c.delete("/api/history").status_code == 200
    assert links(user_id=member) == []
    member_link(aid, member)
    assert studio.c.post("/api/me/emergency", json={"password": "password-123"}).status_code == 204
    assert links(user_id=member) == []


def test_grants_are_journaled_and_emergency_exit_revokes_them_without_the_agents_box(studio):
    """FINDINGS «Выдача доступа к проекту не журналируется и переживает аварийный выход»."""
    aid, _, _, member, base = share(studio)
    owner_id = state.DB.execute("SELECT user_id FROM agents WHERE id = ?", (aid,)).fetchone()[0]
    assert studio.c.post(base, json={"login": "friend"}).status_code == 201          # повтор — не новое событие
    def journal(table, uid):
        return [r[0] for r in state.DB.execute(f"SELECT kind FROM {table} WHERE user_id = ? AND kind LIKE '%member%'"
                                               " ORDER BY id", (uid,))]
    assert journal("security_events", owner_id) == ["project_member_add"]
    assert journal("agent_events", owner_id) == ["member_add"]
    assert journal("security_events", member) == []
    assert studio.c.delete(base + f"/{member}").status_code == 204
    assert studio.c.delete(base + f"/{member}").status_code == 204                    # уже снят — без события
    assert journal("security_events", owner_id) == ["project_member_add", "project_member_remove"]
    assert journal("agent_events", owner_id) == ["member_add", "member_remove"]
    events = state.DB.execute("SELECT detail, ip FROM security_events WHERE kind LIKE 'project_member%'").fetchall()
    assert all(r["detail"] is None and r["ip"] for r in events)                      # ни логина, ни проекта
    assert studio.c.post(base, json={"login": "friend"}).status_code == 201
    signin(studio, "friend")
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    assert studio.c.patch("/api/code/workspace", json={"op": "open", "aid": aid, "pid": ALPHA, "chat": chat}).status_code == 200
    signin(studio, "owner")
    assert studio.c.post("/api/me/emergency", json={"password": "password-123", "agents": False}).status_code == 204
    assert state.DB.execute("SELECT count(*) FROM agents WHERE id = ?", (aid,)).fetchone()[0] == 1   # компьютер — на месте
    for table in ("project_members", "code_member_tabs"):
        assert state.DB.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    signin(studio, "friend")
    assert studio.c.get("/api/agents").json() == {"items": []}
    assert studio.c.get(url(aid, "projects", ALPHA, "chats")).status_code == 404


def test_access_list_shows_and_revokes_grants_of_vanished_folders_and_links(studio):
    """IDEAS «Единый список выданных доступов и публичных ссылок» и FINDINGS «Совместный доступ переживает удаление
    папки»: выдача к исчезнувшей папке видна и отзывается без моста; чужие строки не видны и не отзываются."""
    aid, machine, _, member, base = share(studio)
    assert studio.c.post(url(aid, "projects", BETA, "members"), json={"login": "friend"}).status_code == 201
    owner_id = state.DB.execute("SELECT user_id FROM agents WHERE id = ?", (aid,)).fetchone()[0]
    state.DB.execute("INSERT INTO chats(id,user_id,title,created,updated) VALUES('c1',?,'Мой чат',0,0)", (owner_id,))
    assert studio.c.post("/api/chats/c1/share", json={"pin": "0123", "days": None}).status_code == 201
    assert studio.c.post(url(aid, "projects", ALPHA, "chats", A, "share"), json={"pin": "0123"}).status_code == 201
    del machine.projects["beta"]                                     # папку удалили или переименовали
    data = studio.c.get("/api/me/access").json()
    assert [(m["project"], m["name"], m["missing"], m["login"]) for m in data["members"]] == sorted(
        [(ALPHA, "alpha", False, "friend"), (BETA, None, True, "friend")])
    shares = {s["kind"]: s for s in data["shares"]}
    assert (shares["chat"]["title"], shares["chat"]["expires"], "name" in shares["chat"]) == ("Мой чат", None, False)
    assert (shares["code"]["title"], shares["code"]["name"], shares["code"]["project"]) == (None, "alpha", ALPHA)
    machine.open_sessions.clear()                                    # PIN закрыт — имён нет, id остаются
    closed = studio.c.get("/api/me/access").json()["members"]
    assert {(m["project"], m["name"], m["missing"]) for m in closed} == {(ALPHA, None, None), (BETA, None, None)}
    make_user(studio, "stranger")
    signin(studio, "stranger")
    assert studio.c.get("/api/me/access").json() == {"members": [], "shares": []}
    assert studio.c.delete(f"/api/me/access/members/{aid}/{BETA}/{member}").status_code == 404
    assert studio.c.delete("/api/me/access/shares/" + data["shares"][0]["id"]).status_code == 204
    assert studio.c.delete("/api/me/access").json() == {"members": 0, "shares": 0}
    assert len(links()) == 2 and state.DB.execute("SELECT count(*) FROM project_members").fetchone()[0] == 2
    signin(studio, "owner")
    before = len(machine.requests)
    assert studio.c.delete(f"/api/me/access/members/{aid}/{BETA}/{member}").status_code == 204
    assert studio.c.delete(f"/api/me/access/members/{aid}/xyz/{member}").status_code == 422
    assert len(machine.requests) == before                           # отзыв — без моста
    assert [r[0] for r in state.DB.execute("SELECT project FROM project_members")] == [ALPHA]
    chat_link = next(s["id"] for s in data["shares"] if s["kind"] == "chat")
    assert studio.c.delete("/api/me/access/shares/" + chat_link).status_code == 204
    assert studio.c.get("/api/chats/c1/share").json() == {"enabled": False}
    assert studio.c.delete("/api/me/access").json() == {"members": 1, "shares": 1}
    assert links() == [] and state.DB.execute("SELECT count(*) FROM project_members").fetchone()[0] == 0
    kinds = [r[0] for r in state.DB.execute("SELECT kind FROM security_events WHERE user_id = ? ORDER BY id", (owner_id,))]
    assert kinds[-4:] == ["project_member_remove", "share_revoke", "project_member_remove", "share_revoke"]
    signin(studio, "friend")
    assert studio.c.get("/api/agents").json() == {"items": []}


def test_image_decode_rechecks_membership_before_sending_bytes(studio, monkeypatch):
    aid, machine, _, member, _ = share(studio)
    machine.projects["alpha"]["files"]["a.png"] = PNG_REAL
    original = claude_code._image_bytes
    def decode(data):
        result = original(data)
        state.DB.execute("DELETE FROM project_members WHERE user_id = ?", (member,))
        return result
    monkeypatch.setattr(claude_code, "_image_bytes", decode)
    signin(studio, "friend")
    response = studio.c.post(url(aid, "projects", ALPHA, "image"), json={"path": "a.png"})
    assert response.status_code == 404 and response.content != PNG_REAL
