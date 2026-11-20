"""Почта учётной записи: получатели, письма и тексты SMTP-ошибок в журнал не попадают."""
import asyncio
import ipaddress
import json
import logging
import os
import smtplib
import ssl
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import parseaddr
from enum import StrEnum

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, Field

import security
import state

log = logging.getLogger("studio")


class SMTPMode(StrEnum):
    STARTTLS = "starttls"
    SSL = "ssl"
    PLAIN = "plain"


DEFAULT_PORTS = {SMTPMode.STARTTLS: 587, SMTPMode.SSL: 465, SMTPMode.PLAIN: 25}


class Settings(BaseModel):
    host: str = Field(max_length=253)
    port: int = Field(ge=1, le=65535)
    tls: SMTPMode = SMTPMode.STARTTLS
    username: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=8192)
    sender: str = Field(max_length=320)
    connect_ip: str = Field(default="", max_length=64)


def _environment():
    mode = os.environ.get("STUDIO_SMTP_TLS", SMTPMode.STARTTLS)
    default_port = DEFAULT_PORTS.get(mode, DEFAULT_PORTS[SMTPMode.STARTTLS])
    return {"host": os.environ.get("STUDIO_SMTP_HOST", ""), "sender": os.environ.get("STUDIO_MAIL_FROM", ""),
            "tls": mode, "port": int(os.environ.get("STUDIO_SMTP_PORT", str(default_port))),
            "username": os.environ.get("STUDIO_SMTP_USER", ""), "password": os.environ.get("STUDIO_SMTP_PASSWORD", ""),
            "connect_ip": os.environ.get("STUDIO_SMTP_CONNECT_IP", "").strip()}


def settings():
    """База читается до to_thread; прежняя конфигурация окружения действует до первого сохранения.
    Сохранённые настройки, зашифрованные прежним STUDIO_DATA_KEY, тоже уступают окружению — с признаком
    `reenter`: панель просит ввести их заново, письма и вход с кодом отвечают 503, а не 500."""
    row = state.DB.execute("SELECT value FROM settings WHERE key='smtp'").fetchone() if state.DB is not None else None
    if not row:
        return _environment()
    try:
        return json.loads(security.decrypt(row[0]))
    except InvalidToken:
        log.warning("smtp settings: not decryptable with the current data key")
        return _environment() | {"reenter": True}


def public_settings():
    config = settings()
    return {**{k: v for k, v in config.items() if k not in ("password", "reenter")},
            "password_set": bool(config.get("password")), "reenter": bool(config.get("reenter"))}


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
    if not host or mode not in SMTPMode:
        raise ValueError("mail configuration")
    port = config["port"]
    transport = smtplib.SMTP_SSL if mode == SMTPMode.SSL else smtplib.SMTP
    connect_ip = config.get("connect_ip", "")
    if connect_ip:
        connect_ip = str(ipaddress.ip_address(connect_ip))
        # Меняется только адрес TCP: SMTP._host остаётся исходным именем для проверки TLS и SNI.
        class RoutedSMTP(transport):
            def _get_socket(self, host, port, timeout):
                return super()._get_socket(connect_ip, port, timeout)

        transport = RoutedSMTP
    options = {"context": ssl.create_default_context()} if mode == SMTPMode.SSL else {}
    with transport(host, port, timeout=10, **options) as smtp:
        if mode == SMTPMode.STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if config.get("username"):
            smtp.login(config["username"], config.get("password", ""))
        yield smtp


def _send(recipient: str, subject: str, body: str, config=None) -> None:
    config = config if config is not None else _environment()
    if not configured(config):
        raise ValueError("mail configuration")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = config["sender"], recipient, subject
    message.set_content(body)
    with _connection(config) as smtp:
        smtp.send_message(message)


FAILED_MESSAGE = "Не удалось отправить письмо. Попробуйте позже."
UNSET_MESSAGE = "Почта не настроена — Настройки → SMTP"


def configured(config) -> bool:
    return bool(config.get("host") and config.get("sender")) and config.get("tls") in SMTPMode


def unset() -> HTTPException:
    """Отказ для администратора; публичные формы и вход отвечают общими словами (код тот же)."""
    return HTTPException(503, {"code": "smtp_unset", "message": UNSET_MESSAGE})


async def send(recipient: str, subject: str, body: str) -> None:
    config = settings()
    if not configured(config):
        log.warning("account email: not configured")
        raise HTTPException(503, {"code": "smtp_unset", "message": FAILED_MESSAGE})
    try:
        await asyncio.to_thread(_send, recipient, subject, body, config)
    except (OSError, ValueError):
        log.warning("account email: delivery failed")
        raise HTTPException(503, {"code": "mail", "message": FAILED_MESSAGE}) from None


async def send_admin(recipient: str, subject: str, body: str) -> None:
    """Письмо по действию администратора: ненастроенная почта — отказ со ссылкой на «Настройки → SMTP»."""
    try:
        await send(recipient, subject, body)
    except HTTPException as err:
        if err.detail["code"] == "smtp_unset":
            raise unset() from None
        raise


async def check():
    config = settings()
    if not configured(config):
        raise unset()

    def connect():
        with _connection(config):
            pass

    try:
        await asyncio.to_thread(connect)
    except (OSError, ValueError):
        raise HTTPException(503, {"code": "smtp", "message": "Не удалось подключиться к SMTP. Проверьте настройки."}) from None
    return {"ok": True}
