"""Инструменты чата «Интернет» (спека 2026-10-21-studio-chat-internet): поиск и страницы Exa, субтитры
YouTube, определения инструментов для модели. Ключ Exa — у учётки (`users.exa_key`, шифром
security.encrypt, как токены подключений); сохраняет и убирает его profile.py, наружу он не уходит и в
журнал не пишется. Неудача инструмента — ToolError со словами для человека."""
from cryptography.fernet import InvalidToken

import security
import state


class ToolError(Exception):
    """Неудача инструмента; args[0] — слова для человека (без ключей и ответов сервиса)."""


def has_exa_key(user_id: int) -> bool:
    row = state.DB.execute("SELECT exa_key IS NOT NULL FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def exa_key(user_id: int) -> "str | None":
    """Ключ Exa учётки; нет — None. Не расшифровался (сменили STUDIO_DATA_KEY) — ToolError."""
    row = state.DB.execute("SELECT exa_key FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return security.decrypt(row[0])
    except InvalidToken:
        raise ToolError("ключ Exa не читается — введите его заново в Настройках") from None
