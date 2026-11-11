"""Совместная работа: выдача, границы проекта, личные вкладки и отзыв посреди ответа."""
import asyncio

import auth
import bridge
import claude_code
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
