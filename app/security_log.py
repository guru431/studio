"""Журнал безопасности учётки (db.SECURITY_34): входы и неудачи, отпирания, исчерпание PIN, завершение сессий,
аварийный выход, ссылки «Поделиться». По образцу `agent_events`: только добавляется (`record`), владелец видит
свои события (account.py), уходит с учёткой; «Удалить всю историю» его не трогает — это след входов в учётку, а не
история работы (стирание истории не требует пароля, и чужая сессия не заметёт им свои следы).

Строка — вид события (`KINDS`), короткий код подробности (`detail`, не текст), грубый вид устройства
('phone' | 'computer'), короткий признак сессии (`handle`, не дайджест и не токен) и адрес — как у истории
подключений компьютеров (`agent_events.ip`): владелец видит, откуда входили. Ни логинов, ни паролей, ни PIN, ни
User-Agent, ни текстов. Хранится не дольше `KEEP_SEC` и не больше `KEEP_MAX` строк на учётку — старшие уходят
при записи новой.

Модуль внизу цепочки импорта (только state и clock): его зовут auth, screenlock, chat_shares и account."""
import hashlib

import clock
import state

#: виды событий; новый вид — сюда (столбец kind без CHECK: шаг схемы не нужен)
KINDS = frozenset({
    "login", "login_fail", "logout",                  # вход паролем, неверный пароль известного логина, выход
    "unlock", "unlock_fail", "pin_exhausted",         # отпирание (detail — password | pin | passkey)
    "password",                                       # смена пароля из профиля
    "session_end", "emergency",                       # завершение одной сессии, аварийный выход (detail — agents)
    "share_create", "share_revoke",                   # ссылка «Поделиться» (detail — chat | code)
    "share_pin_fail", "share_locked",                 # неверный PIN ссылки и пауза ссылки (зовёт chat_shares)
})
DETAIL_MAX = 32
KEEP_SEC = 180 * 86400
KEEP_MAX = 1000


def handle(digest: str) -> str:
    """Признак сессии для списка сессий и журнала: из него не получить ни дайджест, ни токен."""
    return hashlib.sha256(("studio-session\0" + digest).encode()).hexdigest()[:16]


def session_device(digest: str) -> "str | None":
    """Вид устройства сессии: записанный при входе, у прежних сессий — по отметке касания (`mobile`)."""
    row = state.DB.execute("SELECT device, mobile FROM sessions WHERE digest = ?", (digest,)).fetchone()
    if row is None:
        return None
    return row["device"] or ("phone" if row["mobile"] else None)


def record(user_id: int, kind: str, *, detail: "str | None" = None, digest: "str | None" = None,
           device: "str | None" = None, ip: "str | None" = None) -> None:
    """Строка журнала учётки `user_id`. `digest` — сессия, от которой событие (её признак и вид устройства);
    `device` — вид устройства, когда сессии ещё нет (вход); `ip` — адрес клиента (auth.client_ip). Вид — из
    `KINDS`, иначе ValueError: опечатка в вызывающем коде не должна тихо копить мусор."""
    if kind not in KINDS:
        raise ValueError(kind)
    if digest is not None and device is None:
        device = session_device(digest)
    now = clock.NOW()
    # учётку могли удалить, пока шла проверка (scrypt в потоке): тогда записывать некому
    state.DB.execute("INSERT INTO security_events(user_id, created, kind, detail, device, session, ip)"
                     " SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM users WHERE id = ?)",
                     (user_id, now, kind, detail[:DETAIL_MAX] if detail else None, device,
                      handle(digest) if digest else None, ip, user_id))
    state.DB.execute("DELETE FROM security_events WHERE user_id = ? AND (created < ? OR id <= (SELECT id FROM"
                     " security_events WHERE user_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?))",
                     (user_id, now - KEEP_SEC, user_id, KEEP_MAX))
