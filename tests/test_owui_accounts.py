"""Учётки Open WebUI в Студии: подключение (список разрешённых, сертификат по отпечатку, версия, вход и его отказы,
предел неудач), переподключение, подпись, удаление с отзывом сессии, `owui.call`, удаление учётки Студии, блокировка."""
import asyncio
import dataclasses
import datetime
import hashlib
import logging
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import owui
import owui_tokens
import state
import upstream
from conftest import make_user, signin
from fake_owui import FakeOwui, attach

HTTPS_ENTRY = "10.77.1.20:8443"
HTTPS_URL = f"https://{HTTPS_ENTRY}"


def body(**kw):
    return {"label": "s06 · work", "url": FakeOwui.URL, "email": FakeOwui.EMAIL, "password": FakeOwui.PASSWORD, **kw}


@pytest.fixture
def setup(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    return studio, attach(studio), uid


def connect(s, **kw):
    return s.c.post("/api/owui/accounts", json=body(**kw))


def rows():
    return state.DB.execute("SELECT * FROM owui_accounts").fetchall()


def test_connect_creates_account_without_storing_password(setup):
    s, fake, uid = setup
    r = connect(s, url=FakeOwui.URL + "/")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out == {"id": out["id"], "label": "s06 · work", "url": FakeOwui.URL,
                   "who": {"name": fake.name, "email": FakeOwui.EMAIL}, "state": "ok", "reenter": False,
                   "version": "0.13.4", "cert": None}
    assert fake.session_names == ["Studio"]
    (row,) = rows()
    for value in row:
        blob = value if isinstance(value, bytes) else str(value).encode()
        assert FakeOwui.PASSWORD.encode() not in blob
    assert s.c.get("/api/owui/accounts").json() == [out]
    assert out["id"] in owui_tokens.TOKENS                     # рабочий токен сессии — сразу в памяти


def test_connect_refuses_unlisted_address(setup):
    s, fake, uid = setup
    r = connect(s, url="http://10.77.1.21:8080")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unlisted"
    assert "10.77.1.21:8080" in r.json()["detail"]["message"]
    assert fake.requests == [] and rows() == []


def test_connect_maps_login_errors(setup):
    s, fake, uid = setup
    assert connect(s, password="wrong").json()["detail"]["code"] == "owui_login"
    fake.role = "pending"                                       # без MFA: обычный ответ входа с ролью pending
    assert connect(s).json()["detail"]["code"] == "pending"
    fake.enable_mfa = True                                      # с MFA: {next_step: "pending"}
    assert connect(s).json()["detail"]["code"] == "pending"
    fake.role, fake.mfa_required = "user", True                 # {next_step: "verify", challenge_token}
    r = connect(s)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "mfa"
    fake.mfa_required = False
    fake.version = "0.12.9"
    assert connect(s).json()["detail"]["code"] == "unsupported"
    fake.version, fake.auth_sessions = "0.13.4", False
    assert connect(s).json()["detail"]["code"] == "unsupported"
    assert rows() == [] and fake.session_names == []


def test_login_failures_are_limited_per_studio_user(setup):
    s, fake, uid = setup
    for _ in range(owui.LOGIN_FAILS):
        assert connect(s, password="wrong").json()["detail"]["code"] == "owui_login"
    asked = len(fake.requests)
    r = connect(s)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "owui_locked"
    assert len(fake.requests) == asked
    s.clock[0] += owui.LOGIN_WINDOW_SEC
    assert connect(s).status_code == 201


def self_signed() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "owui.example.com")])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


def fingerprint(der: bytes) -> str:
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


@pytest.fixture
def tls(setup, monkeypatch):
    """https-адрес с самоподписанным сертификатом: системные корни его не принимают, доверенный клиент — да."""
    s, fake, uid = setup
    der = self_signed()
    pinned = []

    async def untrusted(request):
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")
        except ssl.SSLCertVerificationError as e:
            raise httpx.ConnectError("certificate verify failed", request=request) from e

    async def fetch(conn, url):
        assert url == HTTPS_URL
        return der

    def pinned_http(pem):
        pinned.append(pem)
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    s.gw.hosts[HTTPS_ENTRY] = untrusted
    state.DB.execute("UPDATE settings SET value = json_insert(value, '$[#]', ?) WHERE key = 'allowlist'", (HTTPS_ENTRY,))
    monkeypatch.setattr(upstream, "fetch_certificate", fetch)
    monkeypatch.setattr(state, "pinned_http", pinned_http)
    return s, fake, der, pinned


def test_self_signed_certificate_needs_confirmed_fingerprint(tls):
    s, fake, der, pinned = tls
    r = connect(s, url=HTTPS_URL)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "certificate" and detail["sha256"] == fingerprint(der)
    assert "owui.example.com" in detail["subject"] and detail["not_after"]
    assert rows() == [] and fake.session_names == []

    wrong = fingerprint(b"other")
    assert connect(s, url=HTTPS_URL, cert_sha256=wrong).json()["detail"]["sha256"] == fingerprint(der)
    assert rows() == []

    r = connect(s, url=HTTPS_URL, cert_sha256=fingerprint(der).lower())
    assert r.status_code == 201, r.text
    assert r.json()["cert"] == fingerprint(der) and r.json()["url"] == HTTPS_URL
    (row,) = rows()
    assert row["cert_pem"] == ssl.DER_cert_to_PEM_cert(der) and pinned and set(pinned) == {row["cert_pem"]}


def test_new_certificate_is_accepted_by_fingerprint(tls, monkeypatch):
    s, fake, der, pinned = tls
    aid = connect(s, url=HTTPS_URL, cert_sha256=fingerprint(der)).json()["id"]
    other = self_signed()

    async def fetch(conn, url):
        return other

    monkeypatch.setattr(upstream, "fetch_certificate", fetch)          # сервер сменил сертификат
    r =s.c.post(f"/api/owui/accounts/{aid}/certificate", json={"sha256": fingerprint(der)})
    assert r.status_code == 409 and r.json()["detail"]["sha256"] == fingerprint(other)
    r = s.c.post(f"/api/owui/accounts/{aid}/certificate", json={"sha256": fingerprint(other)})
    assert r.status_code == 200 and r.json()["cert"] == fingerprint(other)
    assert rows()[0]["cert_pem"] == ssl.DER_cert_to_PEM_cert(other)


def test_pinned_certificate_failing_verification_is_not_offered_again(tls, monkeypatch):
    """Ревью M2: сервер показывает ровно закреплённый сертификат, а проверку тот не проходит (истёк) — понятный
    отказ 502 `owui_certificate`, а не 409 с тем же отпечатком без конца."""
    s, fake, der, pinned = tls
    aid = connect(s, url=HTTPS_URL, cert_sha256=fingerprint(der)).json()["id"]

    def broken(pem):
        async def expired(request):
            try:
                raise ssl.SSLCertVerificationError("certificate verify failed: certificate has expired")
            except ssl.SSLCertVerificationError as e:
                raise httpx.ConnectError("certificate verify failed", request=request) from e
        return httpx.AsyncClient(transport=httpx.MockTransport(expired))

    monkeypatch.setattr(state, "pinned_http", broken)
    owui_tokens.TOKENS.clear()
    r = s.c.get(f"/api/owui/accounts/{aid}/tree")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "owui_certificate"


def test_duplicate_account_is_409_exists(setup):
    s, fake, uid = setup
    assert connect(s).status_code == 201
    r = connect(s, label="again")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "exists"
    assert len(rows()) == 1 and fake.session_names == ["Studio"]


def test_relogin_with_other_user_is_409_other_user(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    first = owui_tokens.load(aid)
    state.DB.execute("UPDATE owui_accounts SET state = 'relogin' WHERE id = ?", (aid,))
    r = s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": FakeOwui.PASSWORD})
    assert r.status_code == 200 and r.json()["state"] == "ok"
    second = owui_tokens.load(aid)
    assert second.session_id != first.session_id and fake.revoked == [first.session_id]   # прежняя — отозвана
    fake.user_id = "owui-user-2"
    r = s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": FakeOwui.PASSWORD})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "other_user"
    assert owui_tokens.load(aid) == second
    r = s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": "wrong"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "owui_login"


def test_changed_data_key_asks_to_sign_in_again_with_email(setup, monkeypatch):
    """Ревью I3: после смены `STUDIO_DATA_KEY` (перезапуск: рабочих токенов в памяти нет) — `relogin`, а не 500;
    вход заново спрашивает email, и его отсутствие не считается неудачей входа."""
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    monkeypatch.setattr(state, "CFG", dataclasses.replace(state.CFG, data_key=Fernet.generate_key().decode()))
    owui_tokens.TOKENS.clear()
    (out,) = s.c.get("/api/owui/accounts").json()
    assert (out["reenter"], out["state"], out["who"]) == (True, "relogin", {"name": None, "email": None})
    r = s.c.get(f"/api/owui/accounts/{aid}/tree")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "relogin"
    assert rows()[0]["state"] == "relogin"
    asked = len(fake.requests)
    for _ in range(owui.LOGIN_FAILS + 1):
        r = s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": FakeOwui.PASSWORD})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "owui_email"
    assert len(fake.requests) == asked and not owui.FAILS.get(uid)
    r = s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": FakeOwui.PASSWORD, "email": FakeOwui.EMAIL})
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["reenter"], r.json()["who"]["email"]) == ("ok", False, FakeOwui.EMAIL)
    assert s.c.get(f"/api/owui/accounts/{aid}/tree").status_code == 200
    assert s.c.delete(f"/api/owui/accounts/{aid}").status_code == 204


def test_label_can_be_changed(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    r = s.c.patch(f"/api/owui/accounts/{aid}", json={"label": "  home  "})
    assert r.status_code == 200 and r.json()["label"] == "home"
    assert s.c.patch(f"/api/owui/accounts/{aid}", json={"label": "x" * (owui.LABEL_MAX + 1)}).status_code == 422


def test_foreign_account_is_404(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    s.c.cookies.clear()
    make_user(s, "guest")
    signin(s, "guest")
    for r in (s.c.patch(f"/api/owui/accounts/{aid}", json={"label": "mine"}),
              s.c.delete(f"/api/owui/accounts/{aid}"),
              s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": FakeOwui.PASSWORD}),
              s.c.post(f"/api/owui/accounts/{aid + 100}/login", json={"password": FakeOwui.PASSWORD})):
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    assert s.c.get("/api/owui/accounts").json() == [] and len(rows()) == 1


def test_delete_revokes_session_and_removes_row(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    session_id = owui_tokens.load(aid).session_id
    assert s.c.delete(f"/api/owui/accounts/{aid}").status_code == 204
    assert fake.revoked == [session_id] and rows() == [] and aid not in owui_tokens.TOKENS

    aid = connect(s).json()["id"]
    fake.down = True
    assert s.c.delete(f"/api/owui/accounts/{aid}").status_code == 204
    assert rows() == [] and len(fake.revoked) == 1


def test_studio_account_deletion_revokes_owui_sessions(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    session_id = owui_tokens.load(aid).session_id
    r = s.c.request("DELETE", "/api/me/account", json={"password": "password-123", "login": "owner"})
    assert r.status_code in (200, 204), r.text
    assert fake.revoked == [session_id] and rows() == [] and aid not in owui_tokens.TOKENS


def test_disabled_studio_account_forgets_tokens_and_keeps_sessions(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    asyncio.run(owui.drop_user(uid, revoke=False))
    assert aid not in owui_tokens.TOKENS and fake.revoked == [] and len(rows()) == 1


def test_call_renews_once_when_parallel_requests_get_401(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    acc = owui.load(uid, aid)
    fake.expire_tokens()                                        # рабочий токен Open WebUI больше не принимает

    async def three():
        return await asyncio.gather(*[owui.call(acc, "GET", "/api/v1/auths/") for _ in range(3)])

    assert [r.status_code for r in asyncio.run(three())] == [200, 200, 200]
    assert fake.refreshes == 1


def test_call_maps_failures(setup):
    s, fake, uid = setup
    aid = connect(s).json()["id"]
    acc = owui.load(uid, aid)

    def code(account=acc):
        with pytest.raises(owui.HTTPException) as e:
            asyncio.run(owui.call(account, "GET", "/api/v1/auths/"))
        return e.value.status_code, e.value.detail["code"]

    fake.down = True
    assert code() == (503, "owui_offline")
    fake.down = False
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")
    assert code() == (422, "unlisted")
    attach(s)                                                   # снова в списке (новый Fake — старые токены не знает)
    owui_tokens.forget(aid)
    assert code() == (409, "relogin")
    assert owui.shape_error(acc).status_code == 502
    assert "0.13.4" in owui.shape_error(acc).detail["message"]


def test_accounts_log_has_no_address_email_or_secrets(setup, caplog):
    caplog.set_level(logging.DEBUG, logger="studio")
    s, fake, uid = setup
    connect(s, password="wrong")
    aid = connect(s).json()["id"]
    secret = owui_tokens.load(aid)
    s.c.post(f"/api/owui/accounts/{aid}/login", json={"password": FakeOwui.PASSWORD})
    token = owui_tokens.TOKENS[aid][0]
    s.c.delete(f"/api/owui/accounts/{aid}")
    text = caplog.text
    assert "owui" in text
    for plain in ("10.77.1.20", FakeOwui.EMAIL, FakeOwui.PASSWORD, "wrong", secret.refresh_token, secret.session_id,
                  token, fake.user_id):
        assert plain not in text, plain


def test_locked_section_is_423(setup):
    s, fake, uid = setup
    assert s.c.post("/api/me/screen-lock", json={"enabled": True, "sections": ["owui"], "timeout": 300}).status_code == 200
    assert s.c.post("/api/lock/now").status_code == 204
    r = s.c.get("/api/owui/accounts")
    assert r.status_code == 423 and r.json()["detail"]["code"] == "screen_locked"
    assert s.c.get("/api/chats").status_code != 423
