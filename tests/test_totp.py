"""Приложение-аутентификатор (totp.py) и письмо о входе с нового устройства (email_accounts.notice_due)."""
import base64
import logging
from dataclasses import replace

import pytest

import account_mail
import state
import totp
from conftest import LOGIN_IP, login, make_user, signin

PASSWORD = "password-123"
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                         " Chrome/120.0.0.0 Safari/537.36"}


@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def send(email, subject, body):
        sent.append((email, subject, body))

    monkeypatch.setattr(account_mail, "send", send)
    monkeypatch.setattr(account_mail, "configured", lambda config: True)
    return sent


def test_rfc6238_sha1_reference_values():
    """RFC 6238, приложение B (SHA-1, секрет «12345678901234567890»): последние 6 цифр восьмизначных кодов."""
    secret = b"12345678901234567890"
    for moment, expected in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
        assert totp.code_at(secret, moment // totp.STEP) == expected
    assert totp.matching_step(secret, "287082", 59 + totp.STEP) == 1          # прошлый шаг — в окне
    assert totp.matching_step(secret, "287082", 59 + 2 * totp.STEP) is None   # позапрошлый — нет
    assert totp.matching_step(secret, "287082", 59, after=1) is None          # уже принятый шаг


def enable(studio) -> tuple[bytes, list[str]]:
    setup = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD})
    assert setup.status_code == 200 and setup.headers["cache-control"] == "no-store"
    data = setup.json()
    assert data["uri"].startswith("otpauth://totp/Studio:") and f"secret={data['secret']}&issuer=Studio" in data["uri"]
    secret = base64.b32decode(data["secret"])
    response = studio.c.post("/api/me/totp", json={"ticket": data["ticket"], "code": wrong(studio, secret)})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "wrong_code"
    response = studio.c.post("/api/me/totp", json={"ticket": data["ticket"], "code": code_now(studio, secret)})
    assert response.status_code == 200, response.text
    return secret, response.json()["codes"]


def code_now(studio, secret, shift=0):
    return totp.code_at(secret, int(studio.clock[0] // 30) + shift)


def wrong(studio, secret):
    """Код, которого нет в окне ±1 шаг."""
    valid = {code_now(studio, secret, shift) for shift in (-1, 0, 1)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)


def totp_login(studio, ip=LOGIN_IP):
    studio.c.cookies.clear()
    response = login(studio, "owner", PASSWORD, ip=ip)
    assert response.status_code == 303 and response.headers["location"] == "/account/totp"
    return response


def test_setup_needs_password_and_enables_second_factor_without_mail(studio, mail, caplog):
    caplog.set_level(logging.INFO)
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    assert studio.c.post("/api/me/totp/setup", json={"password": "wrong-pass"}).status_code == 403
    assert studio.c.get("/api/me").json()["totp"] == "off"
    secret, codes = enable(studio)
    assert len(codes) == totp.BACKUP_COUNT and all(len(c) == 11 and c[5] == "-" for c in codes)
    me = studio.c.get("/api/me").json()
    assert me["totp"] == "on" and me["totp_backup_left"] == totp.BACKUP_COUNT
    row = state.DB.execute("SELECT totp_secret FROM users WHERE id=?", (uid,)).fetchone()
    assert base64.b32encode(secret) not in row[0]               # секрет — шифром
    assert all(c.replace("-", "") not in str(list(state.DB.execute("SELECT * FROM totp_backup"))) for c in codes)
    # вход: после пароля — страница кода приложения, письма нет
    totp_login(studio)
    assert mail == []
    assert studio.c.get("/api/me").status_code == 401
    studio.clock[0] += 31                                        # код включения уже принят — новый шаг
    response = studio.c.post("/account/code", json={"code": code_now(studio, secret)})
    assert response.status_code == 200 and response.json()["next"] == "/"
    assert studio.c.get("/api/me").status_code == 200
    for private in [base64.b32encode(secret).decode(), *codes]:
        assert private not in caplog.text


def test_same_code_is_not_accepted_twice_and_wrong_codes_count(studio, mail):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    secret, _ = enable(studio)
    studio.clock[0] += 31
    used = code_now(studio, secret)
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": used}).status_code == 200
    totp_login(studio)
    response = studio.c.post("/account/code", json={"code": used})           # повтор того же шага
    assert response.status_code == 400 and response.json()["detail"] == {
        "code": "wrong_code", "message": "Неверный код", "left": 4}
    kinds = [r[0] for r in state.DB.execute("SELECT kind FROM security_events WHERE user_id=?", (uid,))]
    assert "totp_fail" in kinds
    # неудачи учётки — общая пауза по всем вызовам: после FAILS неудач — 429 и с новым вызовом
    for _ in range(3):
        assert studio.c.post("/account/code", json={"code": used}).status_code == 400
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": used}).status_code == 400
    assert studio.c.post("/account/code", json={"code": code_now(studio, secret, 1)}).status_code == 429


def test_enabling_the_app_voids_an_email_sign_in_already_in_progress(studio, mail):
    """R2-2: вход кодом из письма, начатый до включения приложения, не должен впускать без него — ни прежним кодом,
    ни «Отправить код ещё раз»."""
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    data = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD}).json()
    session = list(studio.c.cookies.jar)
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
    studio.c.cookies.clear()
    assert login(studio, "owner", PASSWORD).headers["location"] == "/account/code"
    mailed = mail[-1][2].rsplit("\n", 1)[-1]
    challenge = list(studio.c.cookies.jar)
    studio.c.cookies.clear()
    for cookie in session:
        studio.c.cookies.jar.set_cookie(cookie)
    secret = base64.b32decode(data["secret"])
    assert studio.c.post("/api/me/totp", json={"ticket": data["ticket"], "code": code_now(studio, secret)}).status_code == 200
    studio.c.cookies.clear()
    for cookie in challenge:
        studio.c.cookies.jar.set_cookie(cookie)
    response = studio.c.post("/account/code/resend", json={})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "expired"
    response = studio.c.post("/account/code", json={"code": mailed})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "expired"
    assert studio.c.get("/api/me").status_code == 401


def test_non_ascii_digits_are_an_ordinary_wrong_code(studio, mail):
    """R2-5: `\\d` пропускал цифры Unicode, а `hmac.compare_digest` на не-ASCII строке падал — 500 без `totp_fail`."""
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    data = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD}).json()
    response = studio.c.post("/api/me/totp", json={"ticket": data["ticket"], "code": "١٢٣٤٥٦"})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "wrong_code"
    enable(studio)
    totp_login(studio)
    response = studio.c.post("/account/code", json={"code": "١٢٣٤٥٦"})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "wrong_code"
    response = studio.c.post("/account/code", json={"code": "２３４５６７８９ab"})    # полноширинные — не алфавит кодов
    assert response.status_code == 400
    assert [r[0] for r in state.DB.execute(
        "SELECT count FROM security_events WHERE user_id=? AND kind='totp_fail'", (uid,))] == [2]
    response = studio.c.post("/api/agent/enroll", json={"login": "owner", "password": PASSWORD, "name": "pc",
                                                        "code": "١٢٣٤٥٦"})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "wrong_code"


def test_wrong_app_code_after_right_password_tells_the_owner_once_per_pause(studio, mail, monkeypatch):
    """R2-7: верный пароль и подбор кода не должны идти молча. Неудача — письмо (почта и SMTP есть) и push `service`
    владельцу, не чаще раза за окно паузы `FAIL_SEC`; без кода в тексте."""
    import push
    pushed = []
    monkeypatch.setattr(push, "notify_later", lambda *args, **kw: pushed.append((args, kw)))
    make_user(studio, "owner")
    signin(studio, "owner")
    secret, _ = enable(studio)
    bad = wrong(studio, secret)
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": bad}).status_code == 400
    assert studio.c.post("/account/code", json={"code": bad}).status_code == 400
    body = {"login": "owner", "password": PASSWORD, "name": "pc", "code": bad}
    assert studio.c.post("/api/agent/enroll", json=body).status_code == 400
    assert studio.c.get("/health").status_code == 200                    # фоновая отправка письма успела
    assert len(pushed) == 1 and len(mail) == 1
    (uid, words, href, tag), options = pushed[0]
    assert href == "settings/security" and options.get("kind", "service") == "service"
    _, subject, text = mail[0]
    assert "Studio" in subject and bad not in text and bad not in str(words) and "http" not in text
    studio.clock[0] += totp.FAIL_SEC                                       # новое окно — снова сказать
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": wrong(studio, secret)}).status_code == 400
    studio.c.get("/health")
    assert len(pushed) == 2 and len(mail) == 2


def test_backup_code_signs_in_once(studio, mail):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    _, codes = enable(studio)
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": codes[0].upper().replace("-", " ")}).status_code == 200
    assert studio.c.get("/api/me").json()["totp_backup_left"] == totp.BACKUP_COUNT - 1
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": codes[0]}).status_code == 400
    assert ("totp_backup_used",) in [tuple(r) for r in state.DB.execute(
        "SELECT kind FROM security_events WHERE user_id=?", (uid,))]


def test_backup_codes_are_keyed_and_bare_sha256_does_not_sign_in(studio, mail):
    """R2-8: голый sha256 резервного кода подбирался из копии базы; теперь — только HMAC ключом данных (кодов
    прежнего формата не выдавали: приложение вышло сразу с HMAC)."""
    import hashlib
    import hmac
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    _, codes = enable(studio)
    plain = codes[0].replace("-", "")
    stored = {r[0] for r in state.DB.execute("SELECT digest FROM totp_backup WHERE user_id=?", (uid,))}
    assert hashlib.sha256(f"totp-backup:{uid}:{plain}".encode()).hexdigest() not in stored
    assert hmac.digest(state.CFG.data_key.encode(), f"totp-backup:{uid}:{plain}".encode(), "sha256").hex() in stored
    legacy = "abcdefghjk"
    state.DB.execute("INSERT INTO totp_backup(user_id, digest) VALUES (?, ?)",
                     (uid, hashlib.sha256(f"totp-backup:{uid}:{legacy}".encode()).hexdigest()))
    totp_login(studio)
    assert studio.c.post("/account/code", json={"code": legacy}).status_code == 400
    assert studio.c.post("/account/code", json={"code": codes[0]}).status_code == 200


def test_bridge_enrollment_accepts_authenticator_code_in_code_field(studio, mail):
    make_user(studio, "owner")
    signin(studio, "owner")
    secret, codes = enable(studio)
    body = {"login": "owner", "password": PASSWORD, "name": "pc"}
    response = studio.c.post("/api/agent/enroll", json=body)
    assert response.status_code == 428 and response.json()["detail"]["code"] == "totp_code" and mail == []
    response = studio.c.post("/api/agent/enroll", json={**body, "code": wrong(studio, secret)})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "wrong_code"
    response = studio.c.post("/api/agent/enroll", json={**body, "code": code_now(studio, secret, 1)})
    assert response.status_code == 201
    assert studio.c.post("/api/agent/enroll", json={**body, "code": codes[1]}).status_code == 201


def test_disable_needs_password_and_admin_cannot_reset_it(studio, mail):
    """Сбросить чужое приложение в панели нельзя: иначе администратор (или его украденная сессия) снял бы второй
    фактор и задал пароль. Потерянный телефон — резервные коды или оператор хоста (`recover-email`)."""
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    enable(studio)
    assert studio.c.post("/api/me/totp/off", json={"password": "wrong-pass"}).status_code == 403
    assert studio.c.post("/api/me/totp/off", json={"password": PASSWORD}).json() == {"status": "off"}
    assert state.DB.execute("SELECT count(*) FROM totp_backup").fetchone()[0] == 0
    friend = make_user(studio, "friend")
    signin(studio, "friend")
    enable(studio)
    signin(studio, "owner")
    assert studio.c.delete(f"/api/admin/users/{friend}/totp").status_code in (404, 405)
    assert state.DB.execute("SELECT totp_secret FROM users WHERE id=?", (friend,)).fetchone()[0] is not None
    assert "totp" not in studio.c.get("/api/admin/users").json()["items"][0]
    studio.c.cookies.clear()
    assert login(studio, "friend", PASSWORD).headers["location"] == "/account/totp"


def test_operator_resets_authenticator_of_the_only_admin(studio, mail, capsys):
    """Нет телефона и резервных кодов: сброса в панели нет — только оператор хоста, `recover-email`."""
    import manage
    uid = make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    enable(studio)
    assert manage.recover_email({"STUDIO_ACCOUNT_LOGIN": "owner", "STUDIO_TOTP": "off"}) == 0
    assert "приложение-аутентификатор сброшено" in capsys.readouterr().out
    assert state.DB.execute("SELECT totp_secret FROM users WHERE id=?", (uid,)).fetchone()[0] is None
    assert state.DB.execute("SELECT count(*) FROM totp_backup").fetchone()[0] == 0
    assert ("totp_reset", "operator") in [tuple(r) for r in state.DB.execute(
        "SELECT kind, detail FROM security_events WHERE user_id=?", (uid,))]
    studio.c.cookies.clear()
    assert login(studio, "owner", PASSWORD).headers["location"] == "/"


def test_changed_data_key_turns_authenticator_off_without_locking_the_account(studio, mail, monkeypatch):
    from cryptography.fernet import Fernet
    make_user(studio, "owner")
    signin(studio, "owner")
    enable(studio)
    monkeypatch.setattr(state, "CFG", replace(state.CFG, data_key=Fernet.generate_key().decode()))
    studio.c.cookies.clear()
    assert login(studio, "owner", PASSWORD).headers["location"] == "/"           # без второго шага
    assert studio.c.get("/api/me").json()["totp"] == "reenter"


def test_setup_ticket_is_bound_to_its_session_and_expires(studio, mail):
    make_user(studio, "owner")
    signin(studio, "owner")
    data = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD}).json()
    code = totp.code_at(base64.b32decode(data["secret"]), int(studio.clock[0] // 30))
    studio.clock[0] += totp.SETUP_SEC
    response = studio.c.post("/api/me/totp", json={"ticket": data["ticket"], "code": code})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "expired"
    data = studio.c.post("/api/me/totp/setup", json={"password": PASSWORD}).json()
    signin(studio, "owner")                                                        # другая сессия
    code = totp.code_at(base64.b32decode(data["secret"]), int(studio.clock[0] // 30))
    assert studio.c.post("/api/me/totp", json={"ticket": data["ticket"], "code": code}).json()["detail"]["code"] == "expired"


# ---- письмо о входе с нового устройства ------------------------------------------------------------------------

def browser_login(studio, ip=LOGIN_IP, headers=BROWSER):
    studio.c.cookies.clear()
    return studio.c.post("/login", data={"login": "owner", "password": PASSWORD},
                         headers={"X-Forwarded-For": ip, **headers}, follow_redirects=False)


def test_login_from_new_device_browser_or_address_sends_a_notice(studio, mail):
    uid = make_user(studio, "owner")
    assert browser_login(studio).status_code == 303 and mail == []        # первый вход — сам владелец
    studio.clock[0] += 3600
    browser_login(studio)                                                 # тот же браузер и адрес
    browser_login(studio, headers={"User-Agent": BROWSER["User-Agent"].replace("120.0.0.0", "121.0.0.0")})
    assert mail == []                                                     # новая версия браузера — не новое
    browser_login(studio, ip="203.0.113.9")
    assert len(mail) == 1
    _, subject, body = mail[0]
    assert "Studio" in subject and "203.0.113.9" in body and "Chrome 120" in body and "Windows" in body
    assert "http" not in body and "owner" not in body
    browser_login(studio, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
                                                 " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"})
    assert len(mail) == 2 and "iOS" in mail[1][2]
    studio.clock[0] += 181 * 86400                                        # входы старше 180 дней не в счёт
    browser_login(studio)
    assert len(mail) == 2                                                 # и истории нет — снова «первый»
    state.DB.execute("UPDATE users SET login_notice=0 WHERE id=?", (uid,))
    browser_login(studio, ip="198.51.100.7")
    assert len(mail) == 2


def test_notice_switch_and_missing_smtp_stay_silent(studio, monkeypatch):
    sent = []

    async def send(email, subject, body):
        sent.append(body)

    monkeypatch.setattr(account_mail, "send", send)
    make_user(studio, "owner")
    signin(studio, "owner")
    assert studio.c.get("/api/me").json()["login_notice"] is True
    assert studio.c.put("/api/me/login-notice", json={"enabled": False}).json() == {"enabled": False}
    assert studio.c.get("/api/me").json()["login_notice"] is False
    studio.c.put("/api/me/login-notice", json={"enabled": True})
    monkeypatch.setattr(account_mail, "configured", lambda config: False)       # SMTP не настроен
    browser_login(studio, ip="203.0.113.9")
    assert sent == []
