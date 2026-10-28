"""Привязка моста Claude Code к учётке, компьютеры учётки и история подключений (app/agents.py, app/bridge.py)."""
import logging

import auth
import bridge
import security
import state
from conftest import login, make_user, signin
from fake_bridge import enroll

IP = "10.77.48.5"


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
                      "projects_root": None, "created": items[0]["created"], "last_seen": None,
                      "models": [], "efforts": []}]


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
