"""Почта учётной записи: получатели, письма и тексты SMTP-ошибок в журнал не попадают."""
import asyncio
import ipaddress
import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from contextlib import contextmanager

from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

import security
import state

log = logging.getLogger("studio")


class Settings(BaseModel):
    host: str = Field(max_length=253)
    port: int = Field(ge=1, le=65535)
    tls: Literal["starttls", "ssl"] = "starttls"
    username: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=8192)
    sender: str = Field(max_length=320)
    connect_ip: str = Field(default="", max_length=64)


def _environment():
    mode = os.environ.get("STUDIO_SMTP_TLS", "starttls")
    return {"host": os.environ.get("STUDIO_SMTP_HOST", ""), "sender": os.environ.get("STUDIO_MAIL_FROM", ""),
            "tls": mode, "port": int(os.environ.get("STUDIO_SMTP_PORT", "465" if mode == "ssl" else "587")),
            "username": os.environ.get("STUDIO_SMTP_USER", ""), "password": os.environ.get("STUDIO_SMTP_PASSWORD", ""),
            "connect_ip": os.environ.get("STUDIO_SMTP_CONNECT_IP", "").strip()}


def settings():
    """База читается до to_thread; прежняя конфигурация окружения действует до первого сохранения."""
    row = state.DB.execute("SELECT value FROM settings WHERE key='smtp'").fetchone() if state.DB is not None else None
    return json.loads(security.decrypt(row[0])) if row else _environment()


def public_settings():
    config = settings()
    return {**{k: v for k, v in config.items() if k != "password"}, "password_set": bool(config.get("password"))}


def save_settings(body):
    config = body.model_dump()
    for key in ("host", "sender", "connect_ip"):
        config[key] = config[key].strip()
    if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in config.values() if isinstance(value, str)):
        raise HTTPException(422, {"code": "smtp", "message": "Поля SMTP не должны содержать управляющие символы"})
    host = config["host"]
    try:
        if not host or any(c in host for c in "/\\@?# "):
            raise ValueError()
        if config["connect_ip"]:
            config["connect_ip"] = str(ipaddress.ip_address(config["connect_ip"]))
        _, address = parseaddr(config["sender"], strict=True)
        if not address or "@" not in address or any(c.isspace() for c in address):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, {"code": "smtp", "message": "Проверьте сервер SMTP, IP и адрес отправителя"}) from None
    if body.password is None:
        config["password"] = settings().get("password", "")
    state.DB.execute("INSERT INTO settings(key,value) VALUES('smtp',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (security.encrypt(json.dumps(config)),))
    return public_settings()


@contextmanager
def _connection(config):
    host, mode = config["host"], config["tls"]
    if not host or mode not in ("starttls", "ssl"):
        raise ValueError("mail configuration")
    context = ssl.create_default_context()
    port = config["port"]
    transport = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    connect_ip = config.get("connect_ip", "")
    if connect_ip:
        connect_ip = str(ipaddress.ip_address(connect_ip))
        # Меняется только адрес TCP: SMTP._host остаётся исходным именем для проверки TLS и SNI.
        class RoutedSMTP(transport):
            def _get_socket(self, host, port, timeout):
                return super()._get_socket(connect_ip, port, timeout)

        transport = RoutedSMTP
    options = {"context": context} if mode == "ssl" else {}
    with transport(host, port, timeout=10, **options) as smtp:
        if mode == "starttls":
            smtp.starttls(context=context)
        if config.get("username"):
            smtp.login(config["username"], config.get("password", ""))
        yield smtp


def _send(recipient: str, subject: str, body: str, config=None) -> None:
    config = config if config is not None else _environment()
    sender = config["sender"]
    host, mode = config["host"], config["tls"]
    if not host or not sender or mode not in ("starttls", "ssl"):
        raise ValueError("mail configuration")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, recipient, subject
    message.set_content(body)
    with _connection(config) as smtp:
        smtp.send_message(message)


async def send(recipient: str, subject: str, body: str) -> None:
    try:
        config = settings()
        await asyncio.to_thread(_send, recipient, subject, body, config)
    except (OSError, ValueError):
        log.warning("account email: delivery failed")
        raise HTTPException(503, {"code": "mail", "message": "Не удалось отправить письмо. Попробуйте позже."}) from None


async def check():
    config = settings()

    def connect():
        with _connection(config):
            pass

    try:
        await asyncio.to_thread(connect)
    except (OSError, ValueError):
        raise HTTPException(503, {"code": "smtp", "message": "Не удалось подключиться к SMTP. Проверьте настройки."}) from None
    return {"ok": True}
