import base64
import hashlib
import html
import json
import re
from contextlib import closing
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import auth
import cleaner_oidc
import config
import db
import security
import state
import totp
from conftest import make_user, signin
from historical_schema import install_schema

CALLBACK = "https://cleaner.example.com/login/studio/callback"
CLIENT_SECRET = "s" * 43
VERIFIER = "v" * 43
STATE = "state-" + "q" * 43
NONCE = "nonce-" + "n" * 43
META = "/.well-known/openid-configuration"


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def oidc(studio, monkeypatch, signing_key):
    pem = signing_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    for name, value in zip(config.OIDC_ENV, (CALLBACK, CLIENT_SECRET, base64.b64encode(pem).decode()), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(state, "CFG", config.load())
    return studio


def authorize(c, **overrides):
    params = {"client_id": "cleaner", "redirect_uri": CALLBACK, "scope": "openid", "response_type": "code",
              "code_challenge_method": "S256", "code_challenge": encode(hashlib.sha256(VERIFIER.encode()).digest()),
              "state": STATE, "nonce": NONCE} | overrides
    return c.get("/oidc/authorize", params=params, follow_redirects=False)


def callback(response):
    if response.status_code == 303:
        location = response.headers["location"]
    else:
        assert response.status_code == 200 and "form-action 'self'" in response.headers["content-security-policy"]
        location = html.unescape(re.search(r'<meta http-equiv="refresh" content="0;url=([^"]+)"', response.text)[1])
        assert f'<a href="{html.escape(location, quote=True)}">' in response.text
    target = urlsplit(location)
    assert target._replace(query="").geturl() == CALLBACK
    assert response.headers["referrer-policy"] == "no-referrer"
    values = {key: value[0] for key, value in parse_qs(target.query).items()}
    assert values.pop("state") == STATE
    return values


def exchange(c, code, *, secret=CLIENT_SECRET, **overrides):
    return c.post("/oidc/token", auth=("cleaner", secret),
                  data={"grant_type": "authorization_code", "redirect_uri": CALLBACK,
                        "code": code, "code_verifier": VERIFIER} | overrides)


def test_provider_is_off_without_a_complete_configuration(studio, monkeypatch):
    for path in (META, "/oidc/jwks", "/oidc/authorize", "/oidc/userinfo"):
        assert studio.c.get(path).status_code == 404
    monkeypatch.setenv(config.OIDC_ENV[1], CLIENT_SECRET)
    with pytest.raises(RuntimeError, match="STUDIO_CLEANER_OIDC") as exc:
        config.load()
    assert CLIENT_SECRET not in str(exc.value)


def test_code_pkce_jwks_replay_and_source_session_revocation(oidc):
    uid = make_user(oidc, "owner", is_admin=True)
    signin(oidc, "owner")
    c = oidc.c
    metadata = c.get(META).json()
    assert metadata["issuer"] == "https://testserver"
    assert metadata["token_endpoint"] == "https://testserver/oidc/token"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    subject = c.get("/api/admin/users").json()["items"][0]["cleaner_oidc_subject"]
    assert re.fullmatch(r"[0-9a-f]{32}", subject)
    code = callback(authorize(c))["code"]
    assert state.DB.execute("SELECT digest FROM cleaner_oidc_codes").fetchone()[0] == security.digest(code)
    assert exchange(c, code, secret="invalid").status_code == 401
    assert exchange(c, code, code_verifier="w" * 43).json() == {"error": "invalid_grant"}
    assert exchange(c, code, redirect_uri=CALLBACK + "/").json() == {"error": "invalid_grant"}
    response = exchange(c, code)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    token_set = response.json()
    access = token_set["access_token"]
    assert token_set["token_type"] == "Bearer" and token_set["expires_in"] == 12 * 3600
    assert state.DB.execute("SELECT digest FROM cleaner_oidc_tokens").fetchone()[0] == security.digest(access)
    token_header, payload, signature = token_set["id_token"].split(".")
    jwk = c.get("/oidc/jwks").json()["keys"][0]
    assert json.loads(decode(token_header)) == {"typ": "JWT", "alg": "RS256", "kid": jwk["kid"]}
    n, e = (int.from_bytes(decode(jwk[key]), "big") for key in ("n", "e"))
    rsa.RSAPublicNumbers(e, n).public_key().verify(
        decode(signature), f"{token_header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256(),
    )
    assert json.loads(decode(payload)) == {
        "iss": "https://testserver", "aud": "cleaner", "sub": subject, "nonce": NONCE,
        "iat": int(oidc.clock[0]), "exp": int(oidc.clock[0]) + cleaner_oidc.ID_TOKEN_AGE,
        "preferred_username": "owner", "at_hash": encode(hashlib.sha256(access.encode()).digest()[:16]),
    }
    assert exchange(c, code).json() == {"error": "invalid_grant"}
    headers = {"Authorization": f"Bearer {access}"}
    assert c.get("/oidc/userinfo").status_code == 401
    assert c.get("/oidc/userinfo", headers=headers).json() == {"sub": subject}
    assert c.post("/oidc/userinfo", headers=headers).json() == {"sub": subject}
    state.DB.execute("UPDATE users SET login='renamed' WHERE id=?", (uid,))
    assert c.get("/oidc/userinfo", headers=headers).json() == {"sub": subject}
    assert c.post("/logout", follow_redirects=False).status_code == 303
    assert c.get("/oidc/userinfo", headers=headers).status_code == 401
    assert not state.DB.execute("SELECT * FROM cleaner_oidc_tokens").fetchall()


def test_not_a_redirector_and_expired_code_fails(oidc, monkeypatch):
    make_user(oidc, "owner")
    signin(oidc, "owner")
    c = oidc.c
    unsafe = authorize(c, redirect_uri=CALLBACK + "?go=https://elsewhere.example")
    assert unsafe.status_code == 400 and "location" not in unsafe.headers
    assert callback(authorize(c, code_challenge_method="plain")) == {"error": "invalid_request"}
    code = callback(authorize(c))["code"]
    cfg = state.CFG
    new_uri = "https://second-cleaner.example.com/login/studio/callback"
    monkeypatch.setattr(state, "CFG", replace(cfg, cleaner_oidc=replace(cfg.cleaner_oidc, redirect_uri=new_uri)))
    assert exchange(c, code, redirect_uri=new_uri).json() == {"error": "invalid_grant"}
    monkeypatch.setattr(state, "CFG", cfg)
    oidc.clock[0] += cleaner_oidc.CODE_AGE
    assert exchange(c, code).json() == {"error": "invalid_grant"}


def test_login_continuation_preserves_second_factor_and_browser_binding(oidc):
    uid = make_user(oidc, "owner")
    secret = b"synthetic-totp-code"
    state.DB.execute("UPDATE users SET totp_secret=? WHERE id=?", (security.encrypt(base64.b32encode(secret).decode()), uid))
    c = oidc.c
    beginning = authorize(c)
    assert beginning.headers["location"] == "/login?continue=cleaner"
    for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
        assert attribute in beginning.headers["set-cookie"].lower()
    login_page = c.get(beginning.headers["location"])
    assert 'name="next" value="oidc/resume"' in login_page.text
    assert login_page.headers["referrer-policy"] == "same-origin"
    assert callback(c.get("/oidc/resume", follow_redirects=False)) == {"error": "login_required"}
    authorize(c)  # the failed resume consumed the continuation
    data = {"next": cleaner_oidc.LOGIN_NEXT, "login": "owner", "password": "password-123"}
    failed = c.post("/login", data=data | {"password": "bad-password"}, follow_redirects=False)
    assert failed.status_code == 401 and 'name="next" value="oidc/resume"' in failed.text
    response = c.post("/login", data=data, follow_redirects=False)
    assert response.headers["location"] == "/account/totp" and auth.COOKIE not in c.cookies
    code = totp.code_at(secret, int(oidc.clock[0] // totp.STEP))
    done = c.post("/account/code", json={"code": code})
    assert done.json() == {"next": "/oidc/resume"}
    assert "code" in callback(c.get(done.json()["next"], follow_redirects=False))
    assert c.get("/oidc/resume").status_code == 400


@pytest.mark.parametrize("invalid", ["locked", "unconfirmed", "disabled"])
def test_source_session_rechecked_when_exchanging_code(oidc, invalid):
    uid = make_user(oidc, "owner")
    signin(oidc, "owner")
    code = callback(authorize(oidc.c))["code"]
    if invalid == "locked":
        state.DB.execute("UPDATE users SET screen_lock=1 WHERE id=?", (uid,))
        state.DB.execute("UPDATE sessions SET seen=NULL WHERE user_id=?", (uid,))
    elif invalid == "unconfirmed":
        state.DB.execute("UPDATE users SET email=NULL WHERE id=?", (uid,))
    else:
        state.DB.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
    assert exchange(oidc.c, code).json() == {"error": "invalid_grant"}


def test_migration_ids_are_stable_and_independent_of_reused_row_ids(env):
    insert = "INSERT INTO users(id, login, name, pw_hash, created) VALUES(1, 'owner', 'Owner', 'fake', 0)"
    get = "SELECT subject FROM cleaner_oidc_subjects WHERE user_id=1"
    with closing(db.connect(env.db_path)) as conn:
        install_schema(conn, 88)
        conn.execute(insert)
        db.migrate(conn)
        old_subject = conn.execute(get).fetchone()[0]
        db.migrate(conn)
        assert conn.execute(get).fetchone()[0] == old_subject
        conn.execute("DELETE FROM users WHERE id=1")
        conn.execute(insert)
        new_subject = conn.execute(get).fetchone()[0]
        assert re.fullmatch(r"[0-9a-f]{32}", old_subject)
        assert new_subject != old_subject and re.fullmatch(r"[0-9a-f]{32}", new_subject)
