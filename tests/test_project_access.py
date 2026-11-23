"""Совместная работа: выдача, границы проекта, личные вкладки и отзыв посреди ответа."""
import asyncio

import auth
import bridge
import chat_shares
import claude_code
import security
import state
from fakes import PNG_REAL
from conftest import erase_history, make_user, signin
from test_code import A, ALPHA, BETA, DONE, setup, url


def share(s):
    aid, machine, link = setup(s, pin="2468")
    member = make_user(s, "friend")
    base = url(aid, "projects", ALPHA, "members")
    response = s.c.post(base, json={"login": " FRIEND "})
    assert response.status_code == 201, response.text
    return aid, machine, link, member, base


def test_grant_by_email_normalizes_long_addresses_and_keeps_account_rules(studio, caplog):
    aid, _, _ = setup(studio)
    member = make_user(studio, "friend")
    email = "x" * 64 + "@" + "d" * 60 + ".example.com"
    state.DB.execute("UPDATE users SET email = ? WHERE id = ?", (email, member))
    base = url(aid, "projects", ALPHA, "members")
    response = studio.c.post(base, json={"login": " " + email.upper() + " "})
    assert response.status_code == 201, response.text
    assert response.json() == {"id": member}
    assert studio.c.post(base, json={"login": "friend"}).status_code == 201
    assert studio.c.get(base).json()["items"] == [{"id": member, "login": "friend", "disabled": 0, "role": "editor",
                                                   "expires": None}]
    state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (member,))
    for value in (email, "owner@example.com", "missing@example.com"):
        response = studio.c.post(base, json={"login": value})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "member"
    assert email not in caplog.text and "friend" not in caplog.text


def test_project_shared_flag_tracks_grants_until_last_member_is_removed(studio):
    aid, _, _, member, base = share(studio)
    other = make_user(studio, "another")
    assert studio.c.post(base, json={"login": "another"}).status_code == 201
    assert [{k: v for k, v in i.items() if k != "dir"} for i in studio.c.get(url(aid, "projects")).json()["items"]] == [
        {"pid": ALPHA, "name": "alpha", "shared": True, "member_count": 2},
        {"pid": BETA, "name": "beta", "shared": False}]
    assert studio.c.delete(base + f"/{member}").status_code == 204
    item = studio.c.get(url(aid, "projects")).json()["items"][0]
    assert item["shared"] and item["member_count"] == 1
    assert studio.c.delete(base + f"/{other}").status_code == 204
    assert not any(item["shared"] for item in studio.c.get(url(aid, "projects")).json()["items"])


def test_member_sees_only_shared_project_and_can_work_without_owner_pin(studio):
    aid, machine, link, member, base = share(studio)
    machine.projects["alpha"]["files"]["a.txt"] = b"hello"
    signin(studio, "friend")
    agent = studio.c.get("/api/agents").json()["items"][0]
    assert agent["id"] == aid and agent["shared"] and not agent["projects_root"]
    assert not any(agent[k] for k in ("computer_info", "software_versions", "desktop", "agent_update", "subscription_usage",
                                      "self_check", "agent_log"))
    before = len(machine.requests)
    for method, suffix in [('GET', '/software'), ('POST', '/software/codex/update'), ('POST', '/software/cancel'),
                           ('GET', '/self-check'), ('GET', '/log')]:
        assert studio.c.request(method, f'/api/code/agents/{aid}' + suffix).status_code == 404
    assert len(machine.requests) == before
    assert studio.c.get(url(aid, "projects")).json() == {"items": [{"pid": ALPHA, "name": "alpha", "shared": True,
                                                                     "readonly": False, "expires": None}]}
    compare = url(aid, "projects", ALPHA, "git", "compare")
    before_compare = len(machine.requests)
    assert studio.c.post(compare, json={"path": "a.txt"}).status_code == 404
    assert len(machine.requests) == before_compare
    original = machine.handle

    def leaky(req):  # мост, забывший убрать путь владельца: сервер не передаёт его участнику сам
        res, events = original(req)
        if req.get("op") == "projects" and res.get("ok"):
            res["data"]["items"] = [{**i, "dir": "C:\\owner\\alpha"} for i in res["data"]["items"]]
        return res, events
    machine.handle = leaky
    assert studio.c.get(url(aid, "projects")).json() == {"items": [{"pid": ALPHA, "name": "alpha", "shared": True,
                                                                     "readonly": False, "expires": None}]}
    machine.handle = original
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


def test_member_new_chat_is_recorded_in_owner_connection_history_without_member_details(studio):
    aid, _, _, member, _ = share(studio)

    def starts():
        return [e for e in studio.c.get("/api/agents/events").json()["items"] if e["kind"] == "session_start"]

    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 201
    own = starts()
    assert len(own) == 1 and own[0]["device"].startswith("браузер ")   # свой чат — одна строка, как прежде
    signin(studio, "friend")
    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 201
    assert [e["device"][:8] for e in starts()] == ["браузер "]           # след участника — и у него
    signin(studio, "owner")
    rows = starts()
    assert len(rows) == 2 and rows[0]["agent_id"] == aid
    assert rows[0]["device"] == claude_code.MEMBER and rows[0]["ip"] is None
    assert str(member) not in rows[0]["device"] and "friend" not in rows[0]["device"]


def test_push_of_a_member_turn_goes_to_the_member_while_access_lasts(studio, monkeypatch):
    """Карточка и конец хода участника — push ему, не владельцу; ход владельца — владельцу; отозван — никому.
    Досланное завершение (§18) — тому же адресату."""
    from fake_bridge import emit
    from test_code import ASK
    aid, machine, link, member, base = share(studio)
    owner = link.user_id
    pushed = []
    monkeypatch.setattr(bridge.push, "notify_later", lambda uid, *a, **kw: pushed.append(uid))
    monkeypatch.setattr(bridge, "watching", lambda *a: False)       # страница ушла: поток хода — не взгляд
    signin(studio, "friend")
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    machine.script = [DONE]
    assert studio.c.post(url(aid, "projects", ALPHA, "chats", chat, "turn"), json={"text": "x"}).status_code == 200
    assert pushed == [member] and not bridge.INITIATORS
    bridge.initiated(aid, chat, member)                                 # карточка хода участника
    emit(studio, link, ALPHA, chat, ASK)
    assert pushed == [member, member] and bridge.INITIATORS
    bridge.initiated(aid, chat, None)
    signin(studio, "owner")
    studio.c.post(url(aid, "open"), json={"pin": "2468"})
    machine.script = [DONE]
    assert studio.c.post(url(aid, "projects", ALPHA, "chats", chat, "turn"), json={"text": "x"}).status_code == 200
    assert pushed[2:] == [owner]
    bridge.initiated(aid, chat, member)                                 # ход участника идёт, канал оборвался
    link.hello["completions"] = True
    studio.c.portal.call(bridge.deliver, link, {"t": "completions", "items": [
        {"project": ALPHA, "chat": chat, "status": "done", "ago": 0}]})
    assert pushed[3:] == [member] and not bridge.INITIATORS
    bridge.initiated(aid, chat, member)
    assert studio.c.delete(base + f"/{member}").status_code == 204
    emit(studio, link, ALPHA, chat, ASK, DONE)
    assert pushed[4:] == [] and not bridge.INITIATORS


def test_push_skips_only_when_the_addressee_watches_not_another_user(studio, monkeypatch):
    """Чат хода владельца открыт у участника — карточка и конец хода владельцу всё равно приходят push; смотрит
    сам владелец — нет. Очередь наблюдателя знает свою учётку с маршрута."""
    from fake_bridge import emit
    from test_code import ASK
    aid, machine, link, member, _ = share(studio)
    owner = link.user_id
    pushed = []
    monkeypatch.setattr(bridge.push, "notify_later", lambda uid, *a, **kw: pushed.append(uid))
    queues = []
    original = bridge.watch
    monkeypatch.setattr(bridge, "watch", lambda *a: queues.append(original(*a)) or queues[-1])
    signin(studio, "friend")
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    machine.script = [DONE]
    assert studio.c.post(url(aid, "projects", ALPHA, "chats", chat, "turn"), json={"text": "x"}).status_code == 200
    assert queues and queues[-1].user_id == member
    pushed.clear()
    bridge.initiated(aid, chat, None)                                   # идёт ход владельца
    theirs = bridge.watch(aid, chat, "member-tab", member)
    emit(studio, link, ALPHA, chat, ASK)
    assert pushed == [owner]
    mine = bridge.watch(aid, chat, "owner-tab", owner)
    emit(studio, link, ALPHA, chat, ASK, DONE)
    assert pushed == [owner]
    bridge.unwatch(aid, chat, mine)
    bridge.unwatch(aid, chat, theirs)


def test_member_visualization_by_absolute_path_needs_a_bridge_that_scopes_it(studio):
    """Мост до 0.20.0 открывает участнику и общий каталог визуализаций Codex (HTML всех проектов владельца):
    без `hello.visualization_scope` участнику — только относительный путь; владельцу — как раньше."""
    aid, machine, link, _, _ = share(studio)
    machine.projects["alpha"]["files"]["v.html"] = b"<p>alpha</p>"
    link.hello["visualizations"] = True
    absolute = "C:\\Users\\owner\\.codex\\visualizations\\2026\\11\\08\\thread\\other.html"
    signin(studio, "friend")
    before = len(machine.requests)
    for source in (absolute, "//server/share/v.html", "/v.html"):
        response = studio.c.post(url(aid, "projects", ALPHA, "visualization"), json={"source": source})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "unsupported"
    assert len(machine.requests) == before
    response = studio.c.post(url(aid, "projects", ALPHA, "visualization"), json={"source": "v.html"})
    assert response.status_code == 200 and machine.requests[-1]["shared_projects"] == [ALPHA]
    link.hello["visualization_scope"] = True
    studio.c.post(url(aid, "projects", ALPHA, "visualization"), json={"source": absolute})
    assert machine.requests[-1]["source"] == absolute                   # проверяет новый мост
    signin(studio, "owner")
    studio.c.post(url(aid, "open"), json={"pin": "2468"})
    del link.hello["visualization_scope"]
    studio.c.post(url(aid, "projects", ALPHA, "visualization"), json={"source": absolute})
    assert machine.requests[-1]["source"] == absolute and "shared_projects" not in machine.requests[-1]


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
        # worktree — только владельцу и в выданном проекте: отказ на сервере, не одним мостом
        ("GET", url(aid, "projects", ALPHA, "worktrees"), None),
        ("POST", url(aid, "projects", ALPHA, "worktrees"), {"name": "feature"}),
    ]:
        response = studio.c.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, path, response.text)
    assert len(machine.requests) == before


def test_viewer_reads_the_project_but_every_change_is_refused_before_the_bridge(studio):
    aid, machine, link, member, base = share(studio)
    machine.projects["alpha"]["files"]["a.txt"] = b"hello"
    link.hello.update(file_operations=True, queue_edit=True, file_upload=True, file_search=True)
    other = make_user(studio, "viewer")
    assert studio.c.post(base, json={"login": "viewer", "role": "viewer"}).status_code == 201
    signin(studio, "viewer")
    assert studio.c.get(url(aid, "projects")).json() == {"items": [
        {"pid": ALPHA, "name": "alpha", "shared": True, "readonly": True, "expires": None}]}
    response = studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "a.txt"})
    assert response.status_code == 200 and response.json()["text"] == "hello"
    assert machine.requests[-1]["shared_readonly"] is True and machine.requests[-1]["shared_projects"] == [ALPHA]
    assert studio.c.get(url(aid, "projects", ALPHA, "chats")).status_code == 200
    assert studio.c.get(url(aid, "projects", ALPHA, "chats", A)).status_code == 200
    found = studio.c.post(url(aid, "projects", ALPHA, "files", "search"), json={"path": "", "query": "hello"})
    assert found.status_code == 200 and found.json()["items"] == [{"path": "a.txt", "line": 1, "text": "hello"}]
    assert machine.requests[-1]["op"] == "file_search" and machine.requests[-1]["shared_readonly"] is True
    got = studio.c.post(url(aid, "projects", ALPHA, "files", "download"), json={"path": "a.txt"})
    assert got.status_code == 200 and got.content == b"hello"
    before = len(machine.requests)
    chat = url(aid, "projects", ALPHA, "chats", A)
    for method, path, body in [
        ("PUT", url(aid, "projects", ALPHA, "file"), {"path": "a.txt", "text": "x", "version": "1"}),
        ("POST", url(aid, "projects", ALPHA, "files", "operate"), {"operation": "delete", "path": "a.txt"}),
        ("PUT", url(aid, "projects", ALPHA, "files", "upload"), None),
        ("POST", url(aid, "projects", ALPHA, "chats"), None),
        ("POST", chat + "/turn", {"text": "hello"}), ("POST", chat + "/steer", {"text": "hello"}),
        ("POST", chat + "/answer", {"request_id": "r1", "decision": "allow"}), ("POST", chat + "/stop", None),
        ("POST", chat + "/claim", {}), ("POST", chat + "/queue/pause", {"paused": True}),
        ("POST", chat + "/fork", {"message_id": "m1"}), ("PATCH", chat, {"state": "closed"}),
    ]:
        response = studio.c.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 403 and response.json()["detail"]["code"] == "readonly", (method, path, response.text)
    assert len(machine.requests) == before
    assert not state.DB.execute("SELECT 1 FROM code_chats WHERE project = ?", (ALPHA,)).fetchone()
    signin(studio, "friend")                    # соавтор того же проекта работает, как прежде, без признака
    assert studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "a.txt"}).status_code == 200
    assert "shared_readonly" not in machine.requests[-1]
    signin(studio, "owner")
    assert studio.c.post(url(aid, "open"), json={"pin": "2468"}).status_code == 204
    items = studio.c.get(base).json()["items"]
    assert [(i["login"], i["role"]) for i in items] == [("friend", "editor"), ("viewer", "viewer")]
    assert other != member


def test_role_changes_without_recreating_the_grant_and_is_journaled(studio):
    aid, machine, _, member, base = share(studio)
    created = state.DB.execute("SELECT created FROM project_members WHERE user_id = ?", (member,)).fetchone()[0]
    studio.clock[0] += 100
    assert studio.c.patch(base + f"/{member}", json={"role": "viewer"}).json() == {"role": "viewer"}
    assert studio.c.patch(base + "/999", json={"role": "viewer"}).status_code == 404
    assert studio.c.patch(base + f"/{member}", json={"role": "owner"}).status_code == 422
    assert state.DB.execute("SELECT created, role FROM project_members WHERE user_id = ?",
                            (member,)).fetchone()[:] == (created, "viewer")
    kinds = [e["kind"] for e in studio.c.get("/api/agents/events").json()["items"]]
    assert kinds.count("member_add") == 2
    signin(studio, "friend")
    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 403
    assert studio.c.patch(base + f"/{member}", json={"role": "editor"}).status_code == 404   # не владелец
    signin(studio, "owner")                     # новая сессия: компьютер с PIN для неё закрыт
    response = studio.c.patch(base + f"/{member}", json={"role": "editor"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "closed"   # повышение — как выдача
    assert studio.c.patch(base + f"/{member}", json={"role": "viewer"}).status_code == 200  # понижение — без PIN
    assert state.DB.execute("SELECT role FROM project_members WHERE user_id = ?", (member,)).fetchone()[0] == "viewer"
    assert studio.c.post(url(aid, "open"), json={"pin": "2468"}).status_code == 204
    assert studio.c.patch(base + f"/{member}", json={"role": "editor"}).status_code == 200
    signin(studio, "friend")
    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 201


def test_term_grant_expires_and_is_removed_on_first_access(studio):
    aid, machine, _, _, base = share(studio)
    member = make_user(studio, "temp")
    assert studio.c.post(base, json={"login": "temp", "term": "week"}).status_code == 201
    assert studio.c.post(base, json={"login": "temp", "term": "year"}).status_code == 422
    expires = studio.clock[0] + 7 * 86400
    assert {i["login"]: i["expires"] for i in studio.c.get(base).json()["items"]} == {"friend": None, "temp": expires}
    access = studio.c.get("/api/me/access").json()["members"]
    assert {m["login"]: (m["role"], m["expires"]) for m in access} == {"friend": ("editor", None),
                                                                     "temp": ("editor", expires)}
    signin(studio, "temp")
    assert studio.c.patch("/api/code/workspace", json={"op": "open", "aid": aid, "pid": ALPHA, "chat": A}).status_code == 200
    assert studio.c.get(url(aid, "projects")).json()["items"][0]["expires"] == expires
    studio.clock[0] = expires
    assert studio.c.get(url(aid, "projects", ALPHA, "chats")).status_code == 404
    assert studio.c.get("/api/agents").json() == {"items": []}
    assert not state.DB.execute("SELECT 1 FROM project_members WHERE user_id = ?", (member,)).fetchone()
    assert not state.DB.execute("SELECT 1 FROM code_member_tabs WHERE user_id = ?", (member,)).fetchone()
    signin(studio, "owner")
    assert studio.c.post(url(aid, "open"), json={"pin": "2468"}).status_code == 204
    assert [i["login"] for i in studio.c.get(base).json()["items"]] == ["friend"]


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


def test_deepseek_profile_overlay_is_only_sent_to_the_computer_owner(studio):
    aid, machine, _, _, _ = share(studio)
    overlay = {"applied": ["owner-profile-row"]}
    settings = {"access_fixed": True, "overlay": overlay}
    machine._op_state = lambda req, events: {"settings": settings}
    path = url(aid, "projects", ALPHA, "chats", A)
    for login, owner in (("friend", False), ("owner", True)):
        signin(studio, login)
        if owner:
            assert studio.c.post(url(aid, "open"), json={"pin": "2468"}).status_code == 204
        state_response = studio.c.post(f"{path}/state", json={})
        assert state_response.status_code == 200 and state_response.json()["settings"]["access_fixed"]
        assert ("owner-profile-row" in state_response.text) == owner
        machine.script = [{"k": "settings", **settings}, {"k": "snapshot", "settings": settings}, DONE]
        stream = studio.c.post(f"{path}/turn", json={"text": "Привет"})
        assert stream.status_code == 200 and "event: settings" in stream.text and "event: snapshot" in stream.text
        assert stream.text.count("owner-profile-row") == (2 if owner else 0)
    assert settings["overlay"] == overlay


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
    assert erase_history(studio).status_code == 200
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
    chat_url = studio.c.post("/api/chats/c1/share", json={"pin": "0123", "days": None}).json()["url"]
    code_url = studio.c.post(url(aid, "projects", ALPHA, "chats", A, "share"),
                             json={"pin": "0123", "title": "Чат агента"}).json()["url"]
    # название чата «Агентов» — только шифром (шаг 53)
    label = state.DB.execute("SELECT label FROM chat_shares WHERE agent_id IS NOT NULL").fetchone()[0]
    assert label and "Чат агента".encode() not in label
    del machine.projects["beta"]                                     # папку удалили или переименовали
    data = studio.c.get("/api/me/access").json()
    assert [(m["project"], m["name"], m["missing"], m["login"]) for m in data["members"]] == sorted(
        [(ALPHA, "alpha", False, "friend"), (BETA, None, True, "friend")])
    shares = {s["kind"]: s for s in data["shares"]}
    assert (shares["chat"]["title"], shares["chat"]["expires"], "name" in shares["chat"]) == ("Мой чат", None, False)
    assert (shares["code"]["title"], shares["code"]["name"], shares["code"]["project"]) == ("Чат агента", "alpha", ALPHA)
    # FINDINGS 06:F15: куда перейти и «Копировать ссылку» — адрес только владельцу, no-store, без журнала
    assert (shares["chat"]["chat_id"], shares["chat"]["computer_id"], shares["chat"]["session_id"]) == ("c1", None, None)
    assert (shares["code"]["chat_id"], shares["code"]["computer_id"], shares["code"]["session_id"]) == (None, aid, A)
    events = state.DB.execute("SELECT count(*) FROM security_events").fetchone()[0]
    for kind, expected in (("chat", chat_url), ("code", code_url)):
        answer = studio.c.get(f"/api/me/access/shares/{shares[kind]['id']}/link")
        assert answer.json() == {"url": expected} and answer.headers["cache-control"] == "no-store"
    assert state.DB.execute("SELECT count(*) FROM security_events").fetchone()[0] == events
    assert studio.c.get("/api/me/access/shares/nope/link").status_code == 404
    machine.open_sessions.clear()                                    # PIN закрыт — имён нет, id остаются
    closed = studio.c.get("/api/me/access").json()["members"]
    assert {(m["project"], m["name"], m["missing"]) for m in closed} == {(ALPHA, None, None), (BETA, None, None)}
    make_user(studio, "stranger")
    signin(studio, "stranger")
    assert studio.c.get("/api/me/access").json() == {"members": [], "shares": [], "file_links": 0}
    assert studio.c.get(f"/api/me/access/shares/{shares['chat']['id']}/link").status_code == 404   # чужая — не видна
    assert studio.c.delete(f"/api/me/access/members/{aid}/{BETA}/{member}").status_code == 404
    assert studio.c.delete("/api/me/access/shares/" + data["shares"][0]["id"]).status_code == 204
    assert studio.c.delete("/api/me/access").json() == {"members": 0, "shares": 0, "file_links": 0}
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
    assert studio.c.delete("/api/me/access").json() == {"members": 1, "shares": 1, "file_links": 0}
    assert links() == [] and state.DB.execute("SELECT count(*) FROM project_members").fetchone()[0] == 0
    kinds = [r[0] for r in state.DB.execute("SELECT kind FROM security_events WHERE user_id = ? ORDER BY id", (owner_id,))]
    assert kinds[-4:] == ["project_member_remove", "share_revoke", "project_member_remove", "share_revoke"]
    signin(studio, "friend")
    assert studio.c.get("/api/agents").json() == {"items": []}


def test_member_leaves_shared_projects_of_a_foreign_computer_without_the_bridge(studio):
    """FINDINGS 07:F3: участник не может отозвать чужой компьютер, но сам покидает выданные проекты — все выдачи
    этого компьютера, вкладки и место; след — в его журнале и в истории подключений владельца без его данных."""
    aid, machine, _, member, base = share(studio)
    owner_id = state.DB.execute("SELECT user_id FROM agents WHERE id = ?", (aid,)).fetchone()[0]
    assert studio.c.post(url(aid, "projects", BETA, "members"), json={"login": "friend"}).status_code == 201
    signin(studio, "friend")
    assert studio.c.delete(f"/api/agents/{aid}").status_code == 404           # отозвать чужой — нельзя, как прежде
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    assert studio.c.patch("/api/code/workspace", json={"op": "open", "aid": aid, "pid": ALPHA, "chat": chat}).status_code == 200
    before = len(machine.requests)
    assert studio.c.delete(f"/api/me/access/shared/{aid}").status_code == 204
    assert len(machine.requests) == before
    for table in ("project_members", "code_member_tabs"):
        assert state.DB.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert studio.c.get("/api/agents").json() == {"items": []}
    assert studio.c.delete(f"/api/me/access/shared/{aid}").status_code == 404   # уже не участник
    assert [r[0] for r in state.DB.execute("SELECT kind FROM security_events WHERE user_id = ?"
                                           " AND kind LIKE 'project%'", (member,))] == ["project_leave"]
    row = state.DB.execute("SELECT device, ip FROM agent_events WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                           (owner_id,)).fetchone()
    assert tuple(row) == (claude_code.MEMBER, None)
    assert state.DB.execute("SELECT kind FROM agent_events WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                            (owner_id,)).fetchone()[0] == "member_remove"
    signin(studio, "owner")
    assert studio.c.delete(f"/api/me/access/shared/{aid}").status_code == 404   # свой компьютер не «покинуть»
    assert state.DB.execute("SELECT count(*) FROM agents").fetchone()[0] == 1


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
