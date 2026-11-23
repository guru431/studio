"""AD login, local ownership, second factor and revocation through Studio entry points."""
import base64
import ssl
from contextlib import contextmanager
from uuid import UUID

import pytest

import auth
import email_accounts
import ldap_auth
import security
import state
import totp
from conftest import login, make_user, signin
from fake_bridge import FakeMachine, connect, enroll, settle

GUID = "00112233-4455-6677-8899-aabbccddeeff"
PASSWORD = "directory-password"
CONNECTION = {"enabled": True, "url": "ldaps://dc.example.com", "base": "DC=example,DC=com",
              "group": "CN=Studio,DC=example,DC=com", "bind_dn": "CN=Reader,DC=example,DC=com",
              "bind_password": "fixture-reader-password", "admin_password": "password-123"}


@pytest.fixture
def directory(studio, monkeypatch):
    for key, field in (("URL", "url"), ("BASE_DN", "base"), ("GROUP_DN", "group"), ("BIND_DN", "bind_dn"),
                       ("BIND_PASSWORD", "bind_password")):
        monkeypatch.setenv(f"STUDIO_LDAP_{key}", CONNECTION[field])
    monkeypatch.setattr(ldap_auth, "find_user", lambda cfg, login: GUID if login == "employee@example.com" else None)
    monkeypatch.setattr(ldap_auth, "verify", lambda cfg, guid, password: (guid, password) == (GUID, PASSWORD))
    return studio


def test_local_admin_can_check_and_save_encrypted_connection(studio, monkeypatch):
    calls = []

    class Connection:
        def search(self, base, selector, **_):
            calls.append((base, selector))
            self.result = {"result": 0}
            self.response = [] if base == "CN=Missing,DC=example,DC=com" else [{"type": "searchResEntry"}]

    @contextmanager
    def service(config):
        calls.append(config.host)
        assert config.bind_password == CONNECTION["bind_password"]
        yield Connection()

    monkeypatch.setattr(ldap_auth, "_service", service)
    make_user(studio, "owner", is_admin=True)
    make_user(studio, "employee")
    endpoint = "/api/admin/ldap"
    signin(studio, "employee")
    assert studio.c.get(endpoint).status_code == 403
    signin(studio, "owner")
    assert not studio.c.get(endpoint).json()["enabled"]
    response = studio.c.put(endpoint, json={**CONNECTION, "admin_password": "wrong"})
    assert response.status_code == 403 and not calls
    assert studio.c.post(endpoint + "/check", json=CONNECTION).json() == {"ok": True}
    assert calls == ["dc.example.com", (CONNECTION["base"], "(objectClass=*)"), (CONNECTION["group"], "(objectClass=group)")]
    assert not studio.c.get(endpoint).json()["enabled"]
    response = studio.c.put(endpoint, json=CONNECTION)
    assert response.status_code == 200 and response.json()["enabled"] and response.json()["password_set"]
    assert CONNECTION["bind_password"] not in response.text and "bind_password" not in response.json()
    assert studio.c.get("/api/admin/users").json()["ldap_configured"]
    cipher = state.DB.execute("SELECT value FROM settings WHERE key='ldap'").fetchone()[0]
    assert CONNECTION["bind_password"].encode() not in cipher
    assert CONNECTION["bind_password"] in security.decrypt(cipher)
    assert "admin_password" not in security.decrypt(cipher)
    reuse = {k: v for k, v in CONNECTION.items() if k != "bind_password"}
    assert studio.c.put(endpoint, json=reuse).status_code == 200
    assert studio.c.put(endpoint, json={**reuse, "url": "ldaps://other.example.com"}).status_code == 422
    assert studio.c.put(endpoint, json={**reuse, "group": "CN=Missing,DC=example,DC=com"}).status_code == 503
    assert studio.c.get(endpoint).json()["url"] == CONNECTION["url"]
    assert studio.c.get(endpoint).json()["group"] == CONNECTION["group"]


def test_turning_ldap_off_in_panel_overrides_environment_and_revokes_access(directory):
    studio = directory
    make_user(studio, "owner", is_admin=True)
    uid = make_user(studio, "employee", is_admin=True)
    signin(studio, "owner")
    assert link(studio, uid).status_code == 204
    assert login(studio, "employee", PASSWORD).status_code == 303
    session = studio.c.cookies[auth.COOKIE]
    aid, _ = enroll(studio, login="employee", password=PASSWORD)
    live = connect(studio, aid, FakeMachine())
    assert studio.c.get("/api/admin/ldap").status_code == 403
    studio.c.cookies.clear()
    signin(studio, "owner")
    response = studio.c.put("/api/admin/ldap", json={**CONNECTION, "enabled": False})
    assert response.status_code == 200 and not response.json()["enabled"]
    assert not studio.c.get("/api/admin/users").json()["ldap_configured"]
    settle(studio)
    assert auth.session_user(session) is None and live.closed_with is not None
    assert state.DB.execute("SELECT disabled FROM users WHERE id=?", (uid,)).fetchone()[0] == 1


def link(studio, uid, password="password-123"):
    return studio.c.post(f"/api/admin/users/{uid}/ldap", json={"login": "employee@example.com", "password": password})


def test_bind_is_explicit_and_preserves_account_and_bridge(directory):
    studio = directory
    admin = make_user(studio, "owner", is_admin=True)
    uid = make_user(studio, "employee")
    other = make_user(studio, "other")
    agent, _ = enroll(studio, login="employee")
    signin(studio, "employee")
    old_session = studio.c.cookies[auth.COOKIE]
    assert link(studio, uid).status_code == 403
    signin(studio, "owner")
    assert link(studio, admin).json()["detail"]["code"] == "last_admin"
    assert link(studio, uid, "wrong").status_code == 403
    token = email_accounts.new_token(uid, "reset", "employee@example.com", 300)
    assert link(studio, uid).status_code == 204
    assert link(studio, other).status_code == 409
    assert state.DB.execute("SELECT user_id FROM agents WHERE id=?", (agent,)).fetchone()[0] == uid
    assert auth.session_user(old_session) is None
    assert studio.c.post("/account/reset/check", json={"token": token}).status_code == 400
    assert studio.c.post(f"/api/admin/users/{uid}/password-link").status_code == 409
    assert studio.c.patch(f"/api/admin/users/{uid}", json={"password": "new-password"}).status_code == 409
    assert login(studio, "employee", "password-123").status_code == 401
    assert login(studio, "employee@example.com", PASSWORD).status_code == 303
    me = studio.c.get("/api/me").json()
    assert me["id"] == uid and me["ldap"] and not me["is_admin"]
    studio.c.headers[auth.CSRF_HEADER] = me["csrf"]
    assert studio.c.post("/api/me/password", json={"current": PASSWORD, "new": "new-password"}).status_code == 409
    setup = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD})
    assert setup.status_code == 200
    assert login(studio, "other", "password-123").status_code == 303


def test_ldap_login_and_new_bridge_keep_the_totp_step(directory):
    studio = directory
    make_user(studio, "owner", is_admin=True)
    uid = make_user(studio, "employee")
    signin(studio, "owner")
    assert link(studio, uid).status_code == 204
    assert login(studio, "employee", PASSWORD).status_code == 303
    studio.c.headers[auth.CSRF_HEADER] = studio.c.get("/api/me").json()["csrf"]
    setup = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD}).json()
    secret = base64.b32decode(setup["secret"])
    code = totp.code_at(secret, int(studio.clock[0] // totp.STEP))
    assert studio.c.post("/api/me/totp", json={"ticket": setup["ticket"], "code": code}).status_code == 200
    studio.c.cookies.clear()
    assert login(studio, "employee", PASSWORD).headers["location"] == "/account/totp"
    assert studio.c.get("/api/me").status_code == 401
    body = {"login": "employee", "password": PASSWORD, "name": "PC"}
    response = studio.c.post("/api/agent/enroll", json=body)
    assert response.status_code == 428 and response.json()["detail"]["code"] == "totp_code"
    studio.clock[0] += totp.STEP
    code = totp.code_at(secret, int(studio.clock[0] // totp.STEP))
    assert studio.c.post("/api/agent/enroll", json={**body, "code": code}).status_code == 201


def test_confirmed_directory_revocation_ends_live_access(directory, monkeypatch):
    studio = directory
    make_user(studio, "owner", is_admin=True)
    uid = make_user(studio, "employee")
    signin(studio, "owner")
    assert link(studio, uid).status_code == 204
    assert login(studio, "employee", PASSWORD).status_code == 303
    aid, _ = enroll(studio, login="employee", password=PASSWORD)
    live = connect(studio, aid, FakeMachine())

    def outage(cfg, guids):
        raise ldap_auth.Unavailable()

    monkeypatch.setattr(ldap_auth, "eligible", outage)
    with pytest.raises(ldap_auth.Unavailable):
        studio.c.portal.call(ldap_auth.sync_access)
    assert studio.c.get("/api/me").status_code == 200
    assert live.closed_with is None
    monkeypatch.setattr(ldap_auth, "eligible", lambda cfg, guids: set())
    studio.c.portal.call(ldap_auth.sync_access)
    settle(studio)
    assert studio.c.get("/api/me").status_code == 401
    assert live.closed_with is not None
    assert login(studio, "employee", PASSWORD).status_code == 401
    signin(studio, "owner")
    route = f"/api/admin/users/{uid}"
    assert studio.c.patch(route, json={"disabled": False}).status_code == 409
    monkeypatch.setattr(ldap_auth, "eligible", lambda cfg, guids: {GUID})
    assert studio.c.patch(route, json={"disabled": False}).status_code == 200
    assert login(studio, "employee", PASSWORD).status_code == 303


def test_transport_uses_starttls_before_credentials_and_matches_the_guid(monkeypatch):
    cfg = ldap_auth.Settings("dc.example.com", 389, False, "DC=example,DC=com", "CN=Studio (AD),DC=example,DC=com",
                             "CN=reader,DC=example,DC=com", "reader-password")
    calls = []

    class Connection:
        def __init__(self, server, *, user, password, **kwargs):
            assert server.tls.validate == ssl.CERT_REQUIRED and server.tls.sni == cfg.host
            assert kwargs["auto_referrals"] is False
            self.user, self.password = user, password

        def open(self, **kwargs):
            calls.append("open")

        def start_tls(self, **kwargs):
            calls.append("tls")
            return True

        def bind(self, **kwargs):
            calls.append((self.user, self.password))
            return True

        def search(self, base, filters, **kwargs):
            assert base == cfg.base and r"Studio \28AD\29" in filters
            calls.append(filters)
            self.result = {"result": 0}
            self.response = [{"type": "searchResEntry", "raw_attributes": {"objectGUID": [UUID(GUID).bytes_le]}}]

        def unbind(self):
            calls.append("close")

    monkeypatch.setattr(ldap_auth, "Connection", Connection)
    assert not ldap_auth.verify(cfg, GUID, "") and not calls
    assert ldap_auth.find_user(cfg, "a*)(b@example.com") == GUID
    assert r"(userPrincipalName=a\2a\29\28b@example.com)" in calls[3]
    calls.clear()
    assert ldap_auth.verify(cfg, GUID, PASSWORD)
    assert calls[:3] == ["open", "tls", (cfg.bind_dn, cfg.bind_password)]
    assert r"(objectGUID=\33\22\11\00\55\44\77\66" in calls[3]
    assert calls[4:] == ["close", "open", "tls", ("{" + GUID + "}", PASSWORD), "close"]
