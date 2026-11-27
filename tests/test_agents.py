"""Привязка моста Claude Code к учётке, компьютеры учётки и история подключений (app/agents.py, app/bridge.py)."""
import logging
import shlex

import pytest

import agents
import auth
import bridge
import security
import state
from conftest import login, make_user, signin
from fake_bridge import FakeMachine, InProcessBridge, connect, enroll, settle

IP = "10.77.48.5"


def unenroll(s, key, headers=None):
    """Отвязка, как её делает мост: DELETE без тела и без Origin, ключ — заголовком."""
    auth_header = {} if key is None else {"Authorization": f"Bearer {key}"}
    return s.c.delete("/api/agent/enroll", headers={"X-Forwarded-For": IP, **auth_header, **(headers or {})})


def post_enroll(s, body=None, content=None, headers=None):
    return s.c.post("/api/agent/enroll", json=body, content=content,
                    headers={"X-Forwarded-For": IP, **(headers or {})})


def events(s, before=None):
    r = s.c.get("/api/agents/events", params={} if before is None else {"before": before})
    assert r.status_code == 200
    return r.json()


def test_enroll_returns_a_key_once_and_keeps_its_hash(studio):
    uid = make_user(studio, "owner")
    agent_id, key = enroll(studio)
    assert len(key) >= 43
    row = state.DB.execute("SELECT * FROM agents").fetchone()
    assert (row["id"], row["user_id"], row["name"], row["key_hash"]) == (agent_id, uid, "Рабочий ПК", security.digest(key))
    for table in ("agents", "agent_events"):
        assert all(key not in str(v) for r in state.DB.execute(f"SELECT * FROM {table}") for v in tuple(r))
    ev = state.DB.execute("SELECT * FROM agent_events").fetchone()
    assert (ev["user_id"], ev["agent_id"], ev["agent_name"], ev["kind"], ev["device"], ev["ip"]) == (
        uid, agent_id, "Рабочий ПК", "enroll", None, IP)


def test_enroll_counts_failures_together_with_login(studio):
    """Привязка — тот же вход: неудачи считаются вместе с /login, пауза у них общая."""
    make_user(studio, "owner")
    for _ in range(3):
        r = post_enroll(studio, {"login": "owner", "password": "nope", "name": "ПК"})
        assert r.status_code == 401 and r.json()["detail"] == {"code": "login", "message": "неверный логин или пароль"}
    for _ in range(2):
        assert login(studio, "owner", "nope").status_code == 401
    r = post_enroll(studio, {"login": " Owner ", "password": "password-123", "name": "ПК"})
    assert r.status_code == 429 and r.json()["detail"] == {"code": "locked",
                                                           "message": "слишком много попыток — подождите 15 минут"}
    r = login(studio, "owner", "password-123")
    assert r.status_code == 429 and "Слишком много попыток" in r.text
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_enroll_refuses_foreign_origin_long_body_and_bad_name(studio):
    make_user(studio, "owner")
    good = {"login": "owner", "password": "password-123", "name": "ПК"}
    r = post_enroll(studio, good, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"] == {"code": "origin", "message": "запрос пришёл с чужого сайта"}
    r = post_enroll(studio, content=b" " * (auth.LOGIN_BODY_MAX + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"] == {"code": "too_long", "message": "слишком длинный запрос"}
    for name in ("", "  ", "П" * 65, "ПК\x07", "ПК‮"):
        r = post_enroll(studio, {**good, "name": name})
        assert r.status_code == 422 and r.json()["detail"] == {
            "code": "name", "message": "имя компьютера — от 1 до 64 знаков без управляющих"}, name
    for body in ([], {"login": 1}):
        r = post_enroll(studio, body)
        assert r.status_code == 422 and r.json()["detail"] == {"code": "invalid", "message": "неверные данные запроса"}
    r = post_enroll(studio, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid"
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_agents_belong_to_their_user(studio):
    make_user(studio, "owner"); make_user(studio, "friend")
    agent_id, _ = enroll(studio, name=" Рабочий ПК ")
    signin(studio, "friend")
    assert studio.c.get("/api/agents").json() == {"items": []}
    r = studio.c.delete(f"/api/agents/{agent_id}")
    assert r.status_code == 404 and r.json()["detail"] == {"code": "not_found", "message": "нет такого компьютера"}
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
    assert events(studio) == {"items": [], "next": None}
    signin(studio, "owner")
    items = studio.c.get("/api/agents").json()["items"]
    assert items == [{"id": agent_id, "name": "Рабочий ПК", "online": False, "has_pin": False, "cc_version": None,
                      "agent_version": None, "platform": None, "agent_update": False, "agent_update_cancel": False, "latest_version": None,
                      "projects_root": None, "created": items[0]["created"], "last_seen": None, "terminal": False,
                          "models": [], "efforts": [], "images": False, "chat_state": False, "defaults": {}, "chat_control": False,
                              "attachments": False, "large_attachments": False, "project_activity": False, "subscription_usage": False, "usage_reset": False, "claude_usage_reset": False, "opencode_usage": False, "opencode_features": False, "deepseek_usage": False, "answer_takeover": False, "chat_manage": False, "limits": {"images": 4, "attachments": 8}, "computer_info": False, "software_versions": False, "software_update_all": False, "self_check": False, "agent_log": False, "desktop": False,
                              "project_summary": False, "metadata_cache": False, "visualizations": False, "visualization_images": False, "history_images": False, "file_stats": False, "file_operations": False, "file_suggestions": False, "file_upload": False, "file_search": False, "skills": False,
                              "chat_search": False, "queue_edit": False, "chat_fork": False,
                              "subagents": False, "worktrees": False, "project_sharing": False, "project_create": False, "project_mirror": False, "mcp": False}]


def test_revoke_removes_the_agent_and_keeps_the_history(studio):
    make_user(studio, "owner")
    agent_id, _ = enroll(studio)
    signin(studio, "owner")
    r = studio.c.delete(f"/api/agents/{agent_id}", headers={"X-Forwarded-For": "203.0.113.4"})
    assert r.status_code == 204
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
    assert studio.c.get("/api/agents").json() == {"items": []}
    page = events(studio)
    tag = bridge.tag(security.digest(studio.c.cookies[auth.COOKIE]))
    assert [(e["kind"], e["agent_id"], e["agent_name"], e["device"], e["ip"]) for e in page["items"]] == [
        ("revoke", agent_id, "Рабочий ПК", f"браузер {tag[:6]}", "203.0.113.4"),
        ("enroll", agent_id, "Рабочий ПК", None, IP)]
    assert studio.c.delete(f"/api/agents/{agent_id}").status_code == 404


def test_device_names_the_phone_by_its_session(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    agent_id, _ = enroll(studio)
    state.DB.execute("UPDATE sessions SET mobile = 1")
    assert studio.c.delete(f"/api/agents/{agent_id}").status_code == 204
    assert events(studio)["items"][0]["device"].startswith("телефон ")
    assert len(bridge.tag("x")) == 32 and bridge.tag("x") != bridge.tag("y")


def test_events_come_in_pages(studio):
    uid = make_user(studio, "owner"); other = make_user(studio, "friend")
    agent_id, _ = enroll(studio)
    for _ in range(54):
        bridge.record(uid, agent_id, "Рабочий ПК", "online", ip=IP)
    bridge.record(other, 99, "Чужой", "online")
    signin(studio, "owner")
    first = events(studio)
    ids = [e["id"] for e in first["items"]]
    assert len(ids) == 50 and ids == sorted(ids, reverse=True) and first["next"] == ids[-1]
    second = events(studio, first["next"])
    assert len(second["items"]) == 5 and second["next"] is None
    assert all(e["id"] < ids[-1] and e["agent_name"] == "Рабочий ПК" for e in second["items"])
    assert set(first["items"][0]) == {"id", "created", "kind", "agent_id", "agent_name", "device", "ip"}


def test_events_filter_by_kinds_keeps_pages(studio):
    """«Важное» страницы — `kinds` без online/offline: страница фильтра полная и листается по `next`."""
    uid = make_user(studio, "owner")
    agent_id, _ = enroll(studio)
    for i in range(60):
        bridge.record(uid, agent_id, "Рабочий ПК", "pin_fail" if i % 3 == 0 else "online")
    signin(studio, "owner")
    r = studio.c.get("/api/agents/events", params={"kinds": "enroll,pin_fail"})
    first = r.json()
    assert r.status_code == 200 and len(first["items"]) == 21 and first["next"] is None
    assert {e["kind"] for e in first["items"]} == {"enroll", "pin_fail"}
    assert len(studio.c.get("/api/agents/events", params={"kinds": "online"}).json()["items"]) == 40
    r = studio.c.get("/api/agents/events", params={"kinds": "online", "before": first["items"][0]["id"]})
    assert len(r.json()["items"]) == 38 and r.json()["next"] is None   # два online новее последнего pin_fail
    for bad in ("", "online,", "ONLINE", "online;drop", ",".join(["x"] * (agents.EVENT_KINDS_MAX + 1))):
        r = studio.c.get("/api/agents/events", params={"kinds": bad})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "kinds", bad


def test_enroll_answers_login(studio):
    """Мост показывает, под какой учёткой он на связи: логин, как его проверила студия."""
    make_user(studio, "owner")
    r = post_enroll(studio, {"login": " Owner ", "password": "password-123", "name": "ПК"})
    assert r.status_code == 201
    assert set(r.json()) == {"agent_id", "device_key", "login"} and r.json()["login"] == "owner"


def test_download_gives_the_installer(studio, monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    installer = tmp_path / "studio-agent-setup.exe"
    installer.write_bytes(b"MZ-installer")
    monkeypatch.setattr(agents, "AGENT_SETUP", installer)
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    r = studio.c.get("/api/agent/download")
    assert r.status_code == 200 and r.content == installer.read_bytes()
    assert r.headers["content-type"] == "application/octet-stream"
    assert "studio-agent-setup.exe" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-cache"
    assert f"пользователь {uid}: скачан мост" in caplog.text


def test_download_without_installer_is_404(studio, monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "AGENT_SETUP", tmp_path / "studio-agent-setup.exe")
    make_user(studio, "owner")
    signin(studio, "owner")
    r = studio.c.get("/api/agent/download")
    assert r.status_code == 404 and r.json()["detail"] == {
        "code": "no_agent", "message": "установщика моста нет на сервере — его кладёт выкладка студии"}


@pytest.mark.parametrize(("platform", "setting", "filename"), [
    ("macos", "AGENT_MACOS", "studio-agent-setup.pkg"), ("linux", "AGENT_LINUX", "studio-agent-setup.sh"),
])
def test_download_uses_the_requested_installer(studio, monkeypatch, tmp_path, platform, setting, filename):
    package = tmp_path / filename
    package.write_bytes(platform.encode())
    monkeypatch.setattr(agents, setting, package)
    url = '/api/agent/download?platform=' + platform
    assert studio.c.get(url).status_code == 401
    make_user(studio, 'owner')
    signin(studio, 'owner')
    response = studio.c.get(url)
    assert response.status_code == 200 and response.content == package.read_bytes()
    assert response.headers['content-type'] == 'application/octet-stream'
    assert filename in response.headers['content-disposition']
    package.unlink()
    assert studio.c.get(url).status_code == 404
    assert studio.c.get('/api/agent/download?platform=unknown').status_code == 400


def test_download_needs_a_session(studio, monkeypatch, tmp_path):
    installer = tmp_path / "studio-agent-setup.exe"
    installer.write_bytes(b"MZ")
    monkeypatch.setattr(agents, "AGENT_SETUP", installer)
    r = studio.c.get("/api/agent/download")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "login"
    assert agents.AGENT_ZIP.name == "studio-agent.zip"
    assert agents.AGENT_SETUP.name == "studio-agent-setup.exe"


def test_linux_install_command_only_serves_the_installer_for_a_live_session(studio, monkeypatch, tmp_path):
    install = "/api/agent/linux-installer"
    issue = "/api/agents/linux-install"
    assert studio.c.post(issue).status_code == 401
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    assert studio.c.post(issue, headers={"x-studio-csrf": ""}).status_code == 403
    cmd = studio.c.post(issue).json()["command"]
    args = shlex.split(cmd)
    name, value = args[args.index("-H") + 1].split(": ", 1)
    bearer = {name: value}
    assert "https://testserver" + install in args
    assert studio.c.cookies[auth.COOKIE] not in cmd
    studio.c.cookies.clear()
    package = tmp_path / "setup.sh"
    monkeypatch.setattr(agents, "AGENT_LINUX", package)
    assert studio.c.get(install, headers=bearer).status_code == 404
    package.write_bytes(b"linux installer")
    assert studio.c.get(install, headers={**bearer, "Origin": "https://example.org"}).status_code == 403
    response = studio.c.get(install + "?platform=windows", headers=bearer)
    assert response.content == package.read_bytes() and response.headers["cache-control"] == "no-store"
    for path in ("/api/me", "/api/agent/download", "/api/agent/package"):
        assert studio.c.get(path, headers=bearer).status_code == 401
    assert studio.c.get(install).status_code == 401
    assert studio.c.get(install, headers={"Authorization": "Bearer bogus"}).status_code == 401
    state.DB.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
    assert studio.c.get(install, headers=bearer).status_code == 401
    state.DB.execute("UPDATE users SET disabled=0 WHERE id=?", (uid,))
    studio.clock[0] += agents.INSTALL_TTL
    assert studio.c.get(install, headers=bearer).status_code == 401
    studio.clock[0] -= agents.INSTALL_TTL
    auth.end_sessions(uid)
    assert studio.c.get(install, headers=bearer).status_code == 401


def test_agent_zip_lies_next_to_the_code():
    """Выкладка кладёт архив в <корень кода>/agent/ — рядом с app/ (deploy.sh, install.sh)."""
    import pathlib
    app_dir = pathlib.Path(agents.__file__).resolve().parent
    assert agents.AGENT_ZIP == app_dir.parent / "agent" / "studio-agent.zip"
    assert agents.AGENT_SETUP == app_dir.parent / "agent" / "studio-agent-setup.exe"


def test_unenroll_by_key_drops_the_computer(studio):
    uid = make_user(studio, "owner")
    agent_id, key = enroll(studio)
    link = connect(studio, agent_id, FakeMachine())
    r = unenroll(studio, key)
    assert r.status_code == 204 and not r.content
    settle(studio)
    assert link.closed_with == bridge.CLOSE_AUTH and agent_id not in bridge.LINKS
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
    ev = state.DB.execute("SELECT * FROM agent_events WHERE kind = 'revoke'").fetchone()
    assert (ev["user_id"], ev["agent_id"], ev["agent_name"], ev["device"], ev["ip"]) == (
        uid, agent_id, "Рабочий ПК", "мост", IP)
    assert unenroll(studio, key).status_code == 401           # второй раз ключа уже нет


def test_unenroll_unknown_key_is_401(studio):
    make_user(studio, "owner")
    enroll(studio)
    for key in ("nope", None):
        r = unenroll(studio, key)
        assert r.status_code == 401 and r.json()["detail"] == {"code": "key", "message": "ключ компьютера не принят"}
    r = studio.c.delete("/api/agent/enroll", headers={"Authorization": "nope"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "key"
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1


def test_unenroll_foreign_origin_is_403(studio):
    make_user(studio, "owner")
    _, key = enroll(studio)
    r = unenroll(studio, key, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"] == {"code": "origin", "message": "запрос пришёл с чужого сайта"}
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1


def test_unenroll_works_for_disabled_user(studio):
    """Отключённой учётке канал закрыт, но забыть компьютер мост всё равно может."""
    uid = make_user(studio, "owner")
    _, key = enroll(studio)
    state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    assert unenroll(studio, key).status_code == 204
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_unenroll_log_has_no_name_or_key(studio, caplog):
    caplog.set_level(logging.DEBUG)
    make_user(studio, "owner")
    agent_id, key = enroll(studio)
    unenroll(studio, "foreign-key-123")
    assert unenroll(studio, key).status_code == 204
    text = caplog.text
    assert f"компьютер {agent_id}: отвязан мостом" in text and f"отвязка с {IP}: key" in text
    for secret in (key, security.digest(key), "foreign-key-123", "owner", "Рабочий", "ПК"):
        assert secret not in text, secret


def test_agent_log_holds_no_secrets(studio, caplog):
    caplog.set_level(logging.DEBUG)
    make_user(studio, "owner")
    post_enroll(studio, {"login": "owner", "password": "wrong-secret-9", "name": "Рабочий ПК"})
    post_enroll(studio, {"login": "owner", "password": "password-123", "name": "ПК\x07"})
    agent_id, key = enroll(studio)
    signin(studio, "owner")
    studio.c.get("/api/agents"); events(studio)
    assert studio.c.delete(f"/api/agents/{agent_id}").status_code == 204
    text = caplog.text
    assert f"компьютер {agent_id} привязан" in text and f"компьютер {agent_id} отозван" in text
    assert f"привязка с {IP}: login" in text and f"привязка с {IP}: name" in text
    for secret in (key, security.digest(key), "password-123", "wrong-secret-9", "owner", "Рабочий", "ПК"):
        assert secret not in text, secret


def replace(s, aid, new_id, password="password-123"):
    return s.c.post(f"/api/agents/{aid}/replace", json={"new_id": new_id, "password": password},
                    headers={"X-Forwarded-For": "203.0.113.4"})


def test_replace_moves_the_new_key_to_the_old_computer_and_keeps_what_hangs_on_its_id(studio):
    """IDEAS 09:I8: новая привязка наследует id, имя, выдачи (с ролью и сроком), зеркала и вкладки прежней; прежний
    ключ отозван (4401), строка нового — прочь, его канал — 1012: мост переподключится тем же ключом как прежний."""
    uid = make_user(studio, "owner"); friend = make_user(studio, "friend")
    old, old_key = enroll(studio, name="Старый ПК")
    new, new_key = enroll(studio, name="Новый ПК")
    other, _ = enroll(studio, name="Третий ПК")
    state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created, role, expires)"
                     " VALUES (?, '0123456789abcdef', ?, 0, 'viewer', 9e9)", (old, friend))
    state.DB.execute("INSERT INTO code_tabs(user_id, agent_id, project, tabs, touched) VALUES (?, ?, 'p', '[]', 0)",
                     (uid, old))
    state.DB.execute("INSERT INTO code_tabs(user_id, agent_id, project, tabs, touched) VALUES (?, ?, 'q', '[]', 0)",
                     (uid, new))
    mirror = state.DB.execute("INSERT INTO project_mirrors(source, project, destination, root, name_key, location)"
                              " VALUES (?, '0123456789abcdef', ?, 'r', 'n', x'00')",
                              (old, other)).lastrowid
    state.DB.execute("INSERT INTO code_place(user_id, panel_id, agent_id, project) VALUES (?, ?, ?, 'q')",
                     (uid, new, new))
    old_link = connect(studio, old, FakeMachine())
    new_link = connect(studio, new, FakeMachine(pin="1234"))
    signin(studio, "owner")
    r = replace(studio, old, new, password="wrong")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "password"
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 3
    r = replace(studio, old, new)
    assert r.status_code == 200 and r.json() == {"id": old}
    settle(studio)
    assert old_link.closed_with == bridge.CLOSE_AUTH and new_link.closed_with == bridge.CLOSE_RESTART
    row = state.DB.execute("SELECT name, key_hash, has_pin FROM agents WHERE id = ?", (old,)).fetchone()
    assert (row["name"], row["key_hash"], row["has_pin"]) == ("Старый ПК", security.digest(new_key), 1)
    assert state.DB.execute("SELECT 1 FROM agents WHERE id = ? OR key_hash = ?",
                            (new, security.digest(old_key))).fetchone() is None
    assert [tuple(r) for r in state.DB.execute("SELECT agent_id, user_id, role, expires FROM project_members")] == [
        (old, friend, "viewer", 9e9)]
    assert [tuple(r) for r in state.DB.execute("SELECT agent_id, project FROM code_tabs")] == [(old, "p")]
    assert state.DB.execute("SELECT source FROM project_mirrors WHERE id = ?", (mirror,)).fetchone()[0] == old
    assert tuple(state.DB.execute("SELECT panel_id, agent_id FROM code_place").fetchone()) == (old, old)
    kinds = [(e["kind"], e["agent_id"]) for e in events(studio)["items"]]
    assert kinds[:5] == [("offline", new), ("offline", old), ("enroll", old), ("revoke", old), ("revoke", new)]
    assert state.DB.execute("SELECT COUNT(*) FROM security_events WHERE kind = 'agent_replace'").fetchone()[0] == 1
    link = connect(studio, old, FakeMachine())          # мост нового компьютера с прежним ключом приходит как old
    assert link.sent[0]["agent_id"] == old and link.sent[0]["name"] == "Старый ПК"
    items = studio.c.get("/api/agents").json()["items"]
    assert [(i["id"], i["name"]) for i in items] == [(old, "Старый ПК"), (other, "Третий ПК")]


def test_replace_forgets_what_the_old_computer_s_mirror_copies_knew(studio):
    """Ревью R4b-1: копии `aid` описывали файлы прежней машины — после замены без «после хода», накопленного и
    состояний пар, иначе первый ход нового компьютера молча перезапишет более новые файлы других копий."""
    import mirror_groups
    uid = make_user(studio, "owner")
    old, _ = enroll(studio, name="Старый ПК")
    new, _ = enroll(studio, name="Новый ПК")
    other, _ = enroll(studio, name="Третий ПК")
    grp = mirror_groups.create_group(uid)
    mine, theirs, spare = (mirror_groups.add(grp, aid, "0123456789abcdef", "r", name, None)
                           for aid, name in ((old, "a"), (other, "b"), (other, "c")))
    state.DB.execute("UPDATE mirror_copies SET auto = 1")
    files = {"README.md": ["file", "h", 1, 0]}
    mirror_groups.save(mine, theirs, files, files, 5.0)
    mirror_groups.save(theirs, spare, files, files, 6.0)
    assert mirror_groups.held(mine)
    signin(studio, "owner")
    assert replace(studio, old, new).status_code == 200
    copies = {r["id"]: (r["auto"], r["held"]) for r in state.DB.execute("SELECT id, auto, held FROM mirror_copies")}
    assert copies[mine] == (0, None) and copies[theirs][0] == 1 and copies[spare][1] is not None
    assert mirror_groups.pair(mine, theirs) == (None, None, None, None) and not mirror_groups.held(mine)
    assert mirror_groups.pair(theirs, spare).synced == 6.0


def test_replace_during_a_handshake_checks_the_key_not_only_the_id(studio):
    """Ревью R4b-3: знакомство, начатое до замены, кончается после неё. Прежний ключ к строке `aid` уже не подходит —
    4401, строка не перезаписана данными старой машины; мост нового с id `new` — 1012: ключ теперь у `aid`."""
    uid = make_user(studio, "owner")
    old, _ = enroll(studio, name="Старый ПК")
    new, _ = enroll(studio, name="Новый ПК")
    late_old = InProcessBridge(old, uid, "Старый ПК", FakeMachine(pin="1234"))
    late_new = InProcessBridge(new, uid, "Новый ПК", FakeMachine())
    signin(studio, "owner")
    assert replace(studio, old, new).status_code == 200
    for link in (late_old, late_new):
        studio.c.portal.call(bridge.attach, link)
    assert (late_old.closed_with, late_new.closed_with) == (bridge.CLOSE_AUTH, bridge.CLOSE_RESTART)
    assert not bridge.LINKS and late_old.sent == late_new.sent == []
    assert state.DB.execute("SELECT has_pin FROM agents WHERE id = ?", (old,)).fetchone()[0] == 0
    assert connect(studio, old, FakeMachine()).sent[0]["agent_id"] == old     # с новым ключом — как прежний


def test_replace_window_counts_what_goes_with_the_new_computer(studio):
    """Ревью R4b-4: окно «Заменить» называет числами, что уйдёт каскадом со строкой нового: действующие выдачи
    участникам, его копии зеркал, действующие ссылки «Поделиться» на его чаты; истёкшее не считается."""
    import mirror_groups
    uid = make_user(studio, "owner"); friend = make_user(studio, "friend")
    old, _ = enroll(studio, name="Старый ПК")
    new, _ = enroll(studio, name="Новый ПК")
    for project, expires in (("a", None), ("b", 9e9), ("c", 1.0)):
        state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created, role, expires)"
                         " VALUES (?, ?, ?, 0, 'viewer', ?)", (new, project, friend, expires))
    state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created, role) VALUES (?, 'a', ?, 0,"
                     " 'viewer')", (old, friend))
    mirror_groups.add(mirror_groups.create_group(uid), new, "0123456789abcdef", None, None, None)
    for n, expires in enumerate((None, 1.0)):
        state.DB.execute("INSERT INTO chat_shares(id, user_id, agent_id, project, session_id, engine, token_hash,"
                         " token_enc, pin_hash, snapshot, created, expires) VALUES (?, ?, ?, 'p', ?, 'claude', ?,"
                         " x'00', 'h', '', 0, ?)", (f"s{n}", uid, new, f"chat{n}", f"t{n}", expires))
    signin(studio, "owner")
    url = f"/api/agents/{old}/replace"
    assert studio.c.get(url, params={"new_id": new}).json() == {"members": 2, "mirrors": 1, "shares": 1}
    assert studio.c.get(f"/api/agents/{new}/replace", params={"new_id": old}).json() == {
        "members": 1, "mirrors": 0, "shares": 0}
    assert studio.c.get(url, params={"new_id": old}).status_code == 422
    assert studio.c.get(url, params={"new_id": 999}).status_code == 404


def test_replace_needs_two_own_distinct_computers(studio):
    make_user(studio, "owner"); make_user(studio, "friend")
    mine, _ = enroll(studio)
    theirs, _ = enroll(studio, login="friend")
    signin(studio, "owner")
    r = replace(studio, mine, mine)
    assert r.status_code == 422 and r.json()["detail"] == {"code": "same", "message": "выберите новый компьютер"}
    for aid, new_id in ((mine, theirs), (theirs, mine), (mine, 999)):
        r = replace(studio, aid, new_id)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    assert state.DB.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2
