"""Почтовый транспорт: TLS до пароля и отсутствие секретов в ошибках."""
import asyncio
import logging

import pytest
from fastapi import HTTPException

import account_mail


def test_smtp_uses_verified_tls_before_login_and_sends_only_to_recipient(monkeypatch):
    events = []

    class SMTP:
        def __init__(self, host, port, **options):
            events.append((host, port, options["timeout"]))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, *, context):
            assert context.check_hostname
            events.append("tls")

        def login(self, user, password):
            events.append((user, password))

        def send_message(self, message):
            events.append((message["To"], message["From"], message.get_content()))

    monkeypatch.setattr(account_mail.smtplib, "SMTP", SMTP)
    for key, value in {"STUDIO_SMTP_HOST": "smtp.example.com", "STUDIO_MAIL_FROM": "studio@example.com",
                       "STUDIO_SMTP_USER": "user", "STUDIO_SMTP_PASSWORD": "private-password",
                       "STUDIO_SMTP_TLS": "starttls", "STUDIO_SMTP_PORT": "587"}.items():
        monkeypatch.setenv(key, value)
    account_mail._send("recipient@example.com", "Subject", "Secret link")
    assert events == [("smtp.example.com", 587, 10), "tls", ("user", "private-password"),
                      ("recipient@example.com", "studio@example.com", "Secret link\n")]


def test_delivery_failure_never_discloses_smtp_exception_or_message(monkeypatch, caplog):
    def failed(*args):
        raise OSError("recipient@example.com secret-link password")

    monkeypatch.setattr(account_mail, "_send", failed)
    monkeypatch.setattr(account_mail, "settings", lambda: {"host": "smtp.example.com", "sender": "s@example.com",
                                                           "tls": "starttls"})
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(account_mail.send("recipient@example.com", "Subject", "secret-link"))
    assert caught.value.status_code == 503
    assert "delivery failed" in caplog.text
    assert all(value not in caplog.text + str(caught.value.detail) for value in ["recipient@example.com", "secret-link", "password"])


@pytest.mark.parametrize("mode", ["starttls", "ssl"])
def test_explicit_connect_ip_keeps_the_tls_hostname_and_certificate_checks(monkeypatch, mode):
    connections = []

    class SMTP:
        def __init__(self, host, port, timeout, **options):
            self._host = host
            self._get_socket(host, port, timeout)
            if "context" in options:
                assert options["context"].check_hostname

        def _get_socket(self, host, port, timeout):
            connections.append((host, port, self._host))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, *, context):
            assert context.check_hostname and self._host == "smtp.example.com"

        def send_message(self, message):
            assert message["To"] == "recipient@example.com"

    monkeypatch.setattr(account_mail.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(account_mail.smtplib, "SMTP_SSL", SMTP)
    for key,value in {"STUDIO_SMTP_HOST":"smtp.example.com", "STUDIO_SMTP_CONNECT_IP":"10.77.1.20",
                      "STUDIO_SMTP_PORT":"587", "STUDIO_SMTP_TLS":mode, "STUDIO_SMTP_USER":"",
                      "STUDIO_MAIL_FROM":"studio@example.com"}.items():
        monkeypatch.setenv(key,value)
    account_mail._send("recipient@example.com", "Subject", "Body")
    assert connections == [("10.77.1.20", 587, "smtp.example.com")]


def test_smtp_settings_are_admin_only_encrypted_and_used_without_restart(studio, monkeypatch):
    from conftest import make_user, signin
    import security
    import state
    uid = make_user(studio, 'owner', is_admin=True); signin(studio, 'owner')
    body = {'host': 'smtp.example.com', 'port': 465, 'tls': 'ssl', 'username': 'studio',
            'password': 'private-smtp-password', 'sender': 'Studio <studio@example.com>', 'connect_ip': '10.77.1.20'}
    response = studio.c.put('/api/admin/smtp', json=body)
    assert response.status_code == 200 and response.json()['password_set']
    assert 'private-smtp-password' not in response.text + studio.c.get('/api/admin/smtp').text
    raw = state.DB.execute("SELECT value FROM settings WHERE key='smtp'").fetchone()[0]
    assert b'private-smtp-password' not in raw
    assert 'private-smtp-password' in security.decrypt(raw)
    del body['password']; body['port'] = 587; body['tls'] = 'starttls'
    assert studio.c.put('/api/admin/smtp', json=body).status_code == 200
    calls = []
    monkeypatch.setattr(account_mail, '_send', lambda *args: calls.append(args))
    studio.c.portal.call(account_mail.send, 'recipient@example.com', 'Subject', 'Body')
    assert calls[0][3]['port'] == 587 and calls[0][3]['password'] == 'private-smtp-password'
    make_user(studio, 'other'); signin(studio, 'other')
    for method, endpoint, value in [('GET', '/api/admin/smtp', None), ('PUT', '/api/admin/smtp', body), ('POST', '/api/admin/smtp/check', None)]:
        assert studio.c.request(method, endpoint, json=value).status_code == 403


def test_smtp_settings_under_a_previous_data_key_fall_back_to_environment(studio, monkeypatch, caplog):
    """Смена STUDIO_DATA_KEY: сохранённые настройки не расшифровать — действуют переменные окружения, панель
    просит ввести настройки заново, почта и вход с кодом — 503, а не 500."""
    from cryptography.fernet import Fernet
    from conftest import login, make_user, signin
    import state
    uid = make_user(studio, 'owner', is_admin=True); signin(studio, 'owner')
    state.DB.execute("INSERT INTO settings(key,value) VALUES('smtp',?)",
                     (Fernet(Fernet.generate_key()).encrypt(b'{"host": "old.example.com"}'),))
    for key in ('STUDIO_SMTP_HOST', 'STUDIO_MAIL_FROM'):
        monkeypatch.delenv(key, raising=False)
    caplog.set_level(logging.WARNING)
    response = studio.c.get('/api/admin/smtp')
    assert response.status_code == 200 and response.json()['reenter'] is True and response.json()['host'] == ''
    assert studio.c.post('/api/admin/smtp/check').status_code == 503
    assert studio.c.post('/api/admin/invitations', json={'email': 'friend@example.com'}).status_code == 503
    body = {'host': 'smtp.example.com', 'port': 587, 'tls': 'starttls', 'username': '', 'sender': 'studio@example.com'}
    response = studio.c.put('/api/admin/smtp', json=body)
    assert response.status_code == 200 and response.json()['reenter'] is False
    assert 'old.example.com' not in caplog.text
    state.DB.execute("UPDATE settings SET value=? WHERE key='smtp'", (Fernet(Fernet.generate_key()).encrypt(b'{}'),))
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
    studio.c.cookies.clear()
    assert login(studio, 'owner', 'password-123').status_code == 503


def test_unconfigured_mail_points_the_admin_to_smtp_settings_and_keeps_public_answers(studio, monkeypatch, caplog):
    from conftest import login, make_user, signin
    import state
    uid = make_user(studio, 'owner', is_admin=True); signin(studio, 'owner')
    for key in ('STUDIO_SMTP_HOST', 'STUDIO_MAIL_FROM'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(account_mail, '_send', lambda *args: pytest.fail('ненастроенная почта не отправляет'))
    for path, body in [('/api/admin/invitations', {'email': 'friend@example.com'}), ('/api/admin/smtp/check', None),
                       ('/api/admin/smtp/test', None)]:
        response = studio.c.post(path, json=body)
        assert response.status_code == 503 and response.json()['detail'] == {
            'code': 'smtp_unset', 'message': account_mail.UNSET_MESSAGE}, path
    assert studio.c.post('/account/forgot', json={'login': 'owner'}).status_code == 200
    state.DB.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
    studio.c.cookies.clear()
    response = login(studio, 'owner', 'password-123')
    assert response.status_code == 503 and account_mail.UNSET_MESSAGE not in response.text


def test_smtp_test_letter_goes_only_to_the_admins_own_address_with_a_pause(studio, monkeypatch, caplog):
    from conftest import make_user, signin
    import email_accounts
    make_user(studio, 'owner', is_admin=True); signin(studio, 'owner')
    monkeypatch.setenv('STUDIO_SMTP_HOST', 'smtp.example.com'); monkeypatch.setenv('STUDIO_MAIL_FROM', 'studio@example.com')
    sent = []
    monkeypatch.setattr(account_mail, '_send', lambda *args: sent.append(args[:3]))
    caplog.set_level(logging.INFO)
    response = studio.c.post('/api/admin/smtp/test', json={'email': 'other@example.com'})
    assert response.status_code == 200, response.text
    assert [s[0] for s in sent] == ['owner@example.com'] and 'Studio' in sent[0][1]
    assert studio.c.post('/api/admin/smtp/test').status_code == 429
    studio.clock[0] += email_accounts.SMTP_TEST_SEC
    assert studio.c.post('/api/admin/smtp/test').status_code == 200 and len(sent) == 2
    assert 'owner@example.com' not in caplog.text
    make_user(studio, 'other'); signin(studio, 'other')
    assert studio.c.post('/api/admin/smtp/test').status_code == 403
