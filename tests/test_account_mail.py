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
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(account_mail.send("recipient@example.com", "Subject", "secret-link"))
    assert caught.value.status_code == 503
    assert "delivery failed" in caplog.text
    assert all(value not in caplog.text + str(caught.value.detail) for value in ["recipient@example.com", "secret-link", "password"])
