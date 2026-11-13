"""Контракты почты: приглашения, миграция, восстановление, коды и ошибки отправки."""
import logging
import re
import sqlite3

import pytest
from fastapi import HTTPException

import account_mail
import auth
import db
import email_accounts as accounts
import manage
import security
import state
from conftest import login, make_user, signin

PASSWORD = "password-123"


@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def send(email, subject, body):
        sent.append((email, subject, body))

    monkeypatch.setattr(account_mail, "send", send)
    return sent


def token(mail):
    return re.search(r"https://testserver/account/\w+#([A-Za-z0-9_-]+)", mail[-1][2])[1]


def code(mail):
    return re.search(r"\b[0-9]{6}\b", mail[-1][2])[0]


def invite(studio, mail, email="friend@example.com", **extra):
    response = studio.c.post("/api/admin/invitations", json={"email": email, **extra})
    assert response.status_code == 201, response.text
    return response.json(), token(mail)


def admin(studio):
    uid = make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    return uid


def test_invitation_registers_verified_email_and_self_selected_credentials(studio, mail, caplog):
    caplog.set_level(logging.INFO)
    admin(studio)
    invitation, secret = invite(studio, mail, email="Friend@Example.com", comment="Private admin note")
    assert invitation["email"] == "friend@example.com" and invitation["status"] == "pending"
    assert state.DB.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    assert secret not in str(tuple(state.DB.execute("SELECT * FROM invitations").fetchone()))
    response = studio.c.post("/account/invite/check", json={"token": secret})
    assert response.json() == {"email": "friend@example.com"}
    assert "Private admin note" not in response.text
    response = studio.c.post("/account/invite/accept", json={"token": secret, "login": "Friend", "password": PASSWORD})
    assert response.status_code == 201
    assert studio.c.post("/account/invite/accept", json={"token": secret, "login": "second", "password": PASSWORD}).status_code == 400
    assert login(studio, "FRIEND@EXAMPLE.COM", PASSWORD).status_code == 303
    me = studio.c.get("/api/me").json()
    assert me["login"] == "friend" and me["email"] == "friend@example.com" and me["email_codes"] is False
    assert studio.c.get("/api/admin/invitations").status_code == 403
    for private in (secret, PASSWORD, "friend@example.com", "Private admin note"):
        assert private not in caplog.text


def test_only_admin_can_invite_or_read_comments_and_csrf_is_required(studio, mail):
    admin(studio)
    assert studio.c.post("/api/admin/invitations", json={"email": "a@example.com"}, headers={auth.CSRF_HEADER: "bad"}).status_code == 403
    make_user(studio, "ordinary"); signin(studio, "ordinary")
    for method, path, body in [("GET", "/api/admin/invitations", None),
                               ("POST", "/api/admin/invitations", {"email": "a@example.com"}),
                               ("PATCH", "/api/admin/invitations/missing", {"comment": "x"}),
                               ("DELETE", "/api/admin/invitations/missing", None)]:
        assert studio.c.request(method, path, json=body).status_code == 403
    assert mail == []


@pytest.mark.parametrize("email", ["", "bad", "x@y", "x\r\nBcc:y@example.com", "a..b@example.com", "a@-example.com"])
def test_invalid_email_cannot_receive_invitation(studio, mail, email):
    admin(studio)
    assert studio.c.post("/api/admin/invitations", json={"email": email}).status_code == 422
    assert not mail


def test_resend_rotates_link_comment_persists_and_revoke_is_final(studio, mail):
    admin(studio)
    invitation, first = invite(studio, mail, comment="First")
    assert studio.c.patch(f"/api/admin/invitations/{invitation['id']}", json={"comment": "Changed"}).status_code == 200
    second_invitation, second = invite(studio, mail, comment="Changed")
    assert first != second and invitation["id"] == second_invitation["id"]
    assert studio.c.post("/account/invite/check", json={"token": first}).status_code == 400
    assert studio.c.post("/account/invite/check", json={"token": second}).status_code == 200
    assert studio.c.delete(f"/api/admin/invitations/{invitation['id']}").status_code == 204
    assert studio.c.post("/account/invite/accept", json={"token": second, "login": "friend", "password": PASSWORD}).status_code == 400
    listed = studio.c.get("/api/admin/invitations").json()["items"]
    assert listed[0]["status"] == "revoked" and listed[0]["comment"] == "Changed"


def test_expired_invitation_and_conflicting_login_do_not_consume_other_invitations(studio, mail):
    admin(studio)
    _, secret = invite(studio, mail)
    assert studio.c.post("/account/invite/accept", json={"token": secret, "login": "owner", "password": PASSWORD}).status_code == 409
    assert studio.c.post("/account/invite/check", json={"token": secret}).status_code == 200
    studio.clock[0] += accounts.INVITE_SEC
    assert studio.c.post("/account/invite/accept", json={"token": secret, "login": "friend", "password": PASSWORD}).status_code == 400


def test_mail_failure_is_visible_and_does_not_cancel_a_previous_link(studio, mail, monkeypatch):
    admin(studio)
    _, secret = invite(studio, mail)

    async def failed(*args):
        raise HTTPException(503, {"code": "mail", "message": "Delivery failed"})

    monkeypatch.setattr(account_mail, "send", failed)
    assert studio.c.post("/api/admin/invitations", json={"email": "friend@example.com"}).status_code == 503
    assert studio.c.post("/account/invite/check", json={"token": secret}).status_code == 200
    assert studio.c.post("/api/admin/invitations", json={"email": "other@example.com"}).status_code == 503
    listed = studio.c.get("/api/admin/invitations").json()["items"]
    assert next(x for x in listed if x["email"] == "other@example.com")["status"] == "failed"


def test_revoke_during_smtp_is_not_undone_by_the_completed_send(studio, monkeypatch):
    admin(studio)

    async def revoke_during_send(*args):
        state.DB.execute("UPDATE invitations SET revoked=?", (studio.clock[0],))

    monkeypatch.setattr(account_mail, "send", revoke_during_send)
    response = studio.c.post("/api/admin/invitations", json={"email": "friend@example.com"})
    assert response.status_code == 400
    row = state.DB.execute("SELECT revoked,token_hash FROM invitations").fetchone()
    assert row["revoked"] and row["token_hash"] is None


def test_disable_during_smtp_does_not_issue_a_login_challenge(studio, monkeypatch):
    uid = make_user(studio, "owner")
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))

    async def disable_during_send(*args):
        state.DB.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))

    monkeypatch.setattr(account_mail, "send", disable_during_send)
    response = login(studio, "owner", PASSWORD)
    assert response.status_code == 400 and accounts.CHALLENGE_COOKIE not in studio.c.cookies
    assert state.DB.execute("SELECT count(*) FROM email_tokens").fetchone()[0] == 0


def test_login_with_email_uses_same_password_pause_as_login(studio):
    make_user(studio, "owner")
    for identity in ["owner", "owner@example.com", "OWNER@EXAMPLE.COM", "owner", "owner@example.com"]:
        assert login(studio, identity, "wrong").status_code == 401
    assert login(studio, "owner@example.com", PASSWORD).status_code == 429
    assert login(studio, "owner", PASSWORD).status_code == 429


def test_existing_account_must_confirm_email_before_using_api(studio, mail):
    uid = make_user(studio, "legacy")
    state.DB.execute("UPDATE users SET email=NULL WHERE id=?", (uid,))
    response = login(studio, "legacy", PASSWORD)
    assert response.headers["location"] == "/account/email"
    assert studio.c.get("/", follow_redirects=False).headers["location"] == "/account/email"
    assert studio.c.get("/api/chats").status_code == 428
    csrf = studio.c.get("/api/me").json()["csrf"]
    studio.c.headers[auth.CSRF_HEADER] = csrf
    response = studio.c.post("/api/me/email", json={"email": "legacy@example.com", "password": PASSWORD})
    assert response.status_code == 200
    secret = token(mail)
    assert state.DB.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()[0] is None
    assert studio.c.post("/api/me/email/confirm", json={"token": secret}).status_code == 200
    assert studio.c.get("/api/chats").status_code == 200
    assert studio.c.post("/api/me/email/confirm", json={"token": secret}).status_code == 400


def test_legacy_account_can_unlock_while_email_is_missing(studio):
    uid = admin(studio)
    state.DB.execute("UPDATE users SET email=NULL,screen_lock=1 WHERE id=?", (uid,))
    state.DB.execute("UPDATE sessions SET seen=NULL WHERE user_id=?", (uid,))
    assert studio.c.get("/api/me").status_code == 423
    assert studio.c.get("/api/lock").status_code == 200
    assert studio.c.post("/api/lock/password", json={"password": PASSWORD}).status_code == 204
    assert studio.c.get("/api/me").status_code == 200
    assert studio.c.get("/api/chats").status_code == 428


def test_email_change_requires_password_and_link_for_same_account(studio, mail):
    admin(studio)
    assert studio.c.post("/api/me/email", json={"email": "new@example.com", "password": "wrong"}).status_code == 403
    assert not mail
    assert studio.c.post("/api/me/email", json={"email": "new@example.com", "password": PASSWORD}).status_code == 200
    secret = token(mail)
    make_user(studio, "friend"); signin(studio, "friend")
    assert studio.c.post("/api/me/email/confirm", json={"token": secret}).status_code == 400
    signin(studio, "owner")
    assert studio.c.post("/api/me/email/confirm", json={"token": secret}).status_code == 200
    assert studio.c.get("/api/me").json()["email"] == "new@example.com"


def test_codes_are_off_by_default_and_changing_option_requires_password(studio, mail):
    admin(studio)
    assert not studio.c.get("/api/me").json()["email_codes"]
    assert studio.c.put("/api/me/email-codes", json={"enabled": True, "password": "wrong"}).status_code == 403
    assert studio.c.put("/api/me/email-codes", json={"enabled": True, "password": PASSWORD}).json() == {"enabled": True}
    assert studio.c.get("/api/me").json()["email_codes"] is True
    assert not mail


def test_code_login_creates_session_only_after_browser_bound_one_time_code(studio, mail):
    uid = make_user(studio, "owner")
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
    response = studio.c.post("/login", data={"login": "owner@example.com", "password": PASSWORD, "next": "#/settings/security"},
                             follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/account/code"
    assert auth.COOKIE not in studio.c.cookies
    challenge = studio.c.cookies.get(accounts.CHALLENGE_COOKIE)
    one_time = code(mail)
    assert challenge and one_time not in str(tuple(state.DB.execute("SELECT * FROM email_tokens").fetchone()))
    studio.c.cookies.clear()
    assert studio.c.post("/account/code", json={"code": one_time}).status_code == 400
    studio.c.cookies.set(accounts.CHALLENGE_COOKIE, challenge)
    response = studio.c.post("/account/code", json={"code": one_time}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/#/settings/security"
    assert studio.c.get("/api/me").json()["login"] == "owner"
    assert studio.c.post("/account/code", json={"code": one_time}).status_code == 400


@pytest.mark.parametrize("invalidate", ["attempts", "expiry", "password", "disabled", "email"])
def test_login_code_cannot_survive_invalidating_account_changes(studio, mail, invalidate):
    uid = make_user(studio, "owner")
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
    assert login(studio, "owner", PASSWORD).headers["location"] == "/account/code"
    one_time = code(mail)
    if invalidate == "attempts":
        wrong = "000000" if one_time != "000000" else "111111"
        for _ in range(accounts.CODE_ATTEMPTS):
            assert studio.c.post("/account/code", json={"code": wrong}).status_code == 400
    elif invalidate == "expiry":
        studio.clock[0] += accounts.CODE_SEC
    elif invalidate == "password":
        state.DB.execute("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password("other-password"), uid))
    elif invalidate == "disabled":
        state.DB.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
    else:
        state.DB.execute("UPDATE users SET email='other@example.com' WHERE id=?", (uid,))
    assert studio.c.post("/account/code", json={"code": one_time}, follow_redirects=False).status_code == 400
    assert state.DB.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_recovery_has_neutral_response_single_use_and_ends_sessions(studio, mail):
    uid = admin(studio)
    session = studio.c.cookies[auth.COOKIE]
    response = studio.c.post("/account/forgot", json={"login": "owner@example.com"})
    secret = token(mail)
    assert studio.c.post("/account/forgot", json={"login": "missing@example.com"}).json() == response.json()
    assert len(mail) == 1
    response = studio.c.post("/account/reset", json={"token": secret, "password": "new-password-123"})
    assert response.status_code == 200
    assert auth.session_user(session) is None
    assert studio.c.post("/account/reset", json={"token": secret, "password": "different-password"}).status_code == 400
    assert login(studio, "owner", PASSWORD).status_code == 401
    assert login(studio, "owner@example.com", "new-password-123").status_code == 303
    assert not state.DB.execute("SELECT 1 FROM email_tokens WHERE user_id=?", (uid,)).fetchone()


def test_reset_link_expires_and_existing_password_still_works(studio, mail):
    make_user(studio, "owner")
    assert studio.c.post("/account/forgot", json={"login": "owner"}).status_code == 200
    secret = token(mail)
    studio.clock[0] += accounts.RESET_SEC
    assert studio.c.post("/account/reset", json={"token": secret, "password": "new-password-123"}).status_code == 400
    assert login(studio, "owner", PASSWORD).status_code == 303


def test_bridge_enrollment_also_requires_email_code(studio, mail):
    uid = make_user(studio, "owner")
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
    body = {"login": "owner@example.com", "password": PASSWORD, "name": "Computer"}
    response = studio.c.post("/api/agent/enroll", json=body)
    assert response.status_code == 428 and response.json()["detail"]["code"] == "email_code"
    one_time = code(mail)
    assert not state.DB.execute("SELECT 1 FROM agents").fetchone()
    response = studio.c.post("/api/agent/enroll", json={**body, "code": one_time})
    assert response.status_code == 201
    assert studio.c.post("/api/agent/enroll", json={**body, "code": one_time}).status_code == 400


@pytest.mark.parametrize("path,body", [("/account/forgot", {"login": "owner"}), ("/account/code", {"code": "123456"}),
                                       ("/account/invite/accept", {"token": "secret"}), ("/account/reset", {"token": "secret"})])
def test_public_account_posts_reject_foreign_origin_and_large_body(studio, path, body):
    assert studio.c.post(path, json=body, headers={"Origin": "https://foreign.example"}).status_code == 403
    assert studio.c.post(path, content=b"x" * (auth.LOGIN_BODY_MAX + 1)).status_code == 413
    assert studio.c.get("/account/reset").headers["referrer-policy"] == "no-referrer"


def test_operator_adds_missing_email_without_resetting_password_or_sessions(studio):
    uid = admin(studio)
    state.DB.execute("UPDATE users SET email=NULL WHERE id=?", (uid,))
    before = state.DB.execute("SELECT pw_hash FROM users WHERE id=?", (uid,)).fetchone()[0]
    assert manage.set_email({"STUDIO_ACCOUNT_LOGIN": "owner", "STUDIO_ACCOUNT_EMAIL": "Owner@Example.com"}) == 0
    assert state.DB.execute("SELECT email,pw_hash FROM users WHERE id=?", (uid,)).fetchone()[:] == ("owner@example.com", before)
    assert studio.c.get("/api/me").status_code == 200
    assert manage.set_email({"STUDIO_ACCOUNT_LOGIN": "owner", "STUDIO_ACCOUNT_EMAIL": "other@example.com"}) == 2


def test_migration_39_to_40_preserves_accounts_sessions_and_unique_email(env):
    connection = db.connect(env.db_path)
    connection.executescript(db.SCHEMA.removesuffix(db.EMAIL_40) + "\nPRAGMA user_version=39;")
    connection.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'old','Old','hash',0)")
    connection.execute("INSERT INTO sessions(digest,user_id,csrf,expires,renewed) VALUES('d',1,'c',100,0)")
    db.migrate(connection); db.migrate(connection)
    assert connection.execute("SELECT email,email_codes,pw_hash FROM users").fetchone()[:] == (None, 0, "hash")
    assert connection.execute("SELECT digest FROM sessions").fetchone()[0] == "d"
    connection.execute("UPDATE users SET email='old@example.com'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO users(login,name,pw_hash,email,created) VALUES('new','New','hash','OLD@example.com',0)")
    connection.close()
