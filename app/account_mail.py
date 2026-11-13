"""Почта учётной записи: получатели, письма и тексты SMTP-ошибок в журнал не попадают."""
import asyncio
import ipaddress
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

from fastapi import HTTPException

log = logging.getLogger("studio")


def _send(recipient: str, subject: str, body: str) -> None:
    host = os.environ.get("STUDIO_SMTP_HOST", "")
    sender = os.environ.get("STUDIO_MAIL_FROM", "")
    mode = os.environ.get("STUDIO_SMTP_TLS", "starttls")
    if not host or not sender or mode not in ("starttls", "ssl"):
        raise ValueError("mail configuration")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, recipient, subject
    message.set_content(body)
    context = ssl.create_default_context()
    port = int(os.environ.get("STUDIO_SMTP_PORT", "465" if mode == "ssl" else "587"))
    transport = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    connect_ip = os.environ.get("STUDIO_SMTP_CONNECT_IP", "").strip()
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
        username = os.environ.get("STUDIO_SMTP_USER", "")
        if username:
            smtp.login(username, os.environ.get("STUDIO_SMTP_PASSWORD", ""))
        smtp.send_message(message)


async def send(recipient: str, subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(_send, recipient, subject, body)
    except (OSError, ValueError):
        log.warning("account email: delivery failed")
        raise HTTPException(503, {"code": "mail", "message": "Не удалось отправить письмо. Попробуйте позже."}) from None
