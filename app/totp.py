"""Второй фактор приложением-аутентификатором (RFC 6238) и резервные коды: вход не зависит от почты.

- TOTP своим кодом: HMAC-SHA-1, `DIGITS` цифр, шаг `STEP` с, окно ±`WINDOW` шаг; время — только `clock.NOW`.
  Принятый шаг запоминается (`users.totp_step`): тот же код (и более ранний шаг) второй раз не принимается.
- Секрет — шифром `STUDIO_DATA_KEY` (`users.totp_secret`); резервных кодов `BACKUP_COUNT`, в базе — sha256, каждый
  одноразовый (`totp_backup`). Сменённый ключ сервера: секрет нечитаем — приложение считается выключенным
  (`status` → `reenter`), вход идёт как без него, «Защита» просит настроить заново; учётку это не запирает.
- Включение — в два шага из «Защиты»: `POST /api/me/totp/setup` с паролем выдаёт секрет, адрес `otpauth://` для QR и
  билет (секрет шифром, привязан к сессии, `SETUP_SEC`); `POST /api/me/totp` с билетом и кодом приложения включает и
  один раз отдаёт резервные коды. Выключение — паролем. Сброса в панели нет: администратор, снявший приложение и
  задавший пароль, взял бы учётку; потерянный телефон — резервные коды или оператор хоста (`manage.recover_email`).
- Вход (`/login`) у учётки с приложением после пароля ведёт на `/account/totp`: вызов — строка `email_tokens` вида
  `totp` без кода, связанная с cookie `__Host-studio_email` браузера; письма нет. Привязка моста принимает код в поле
  `code`. Паузы: вызовы — как у кода из письма (`code-user`, `code-ip`), попытки — `email_accounts.CODE_ATTEMPTS` на
  вызов и `FAILS` неудач учётки за `FAIL_SEC` по всем входам и мостам.
- Журнал — id и коды; ни секрета, ни кодов.
"""
import base64
import hmac
import json
import logging
import re
import secrets
import struct
from urllib.parse import quote, urlencode, urlsplit

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import auth
import clock
import email_accounts
import security
import security_log
import state

log = logging.getLogger("studio")
router = APIRouter()

STEP = 30
DIGITS = 6
WINDOW = 1
SECRET_BYTES = 20
BACKUP_COUNT = 10
SETUP_SEC = 600
#: неверных кодов приложения на учётку за FAIL_SEC (вход и привязка моста вместе), дальше 429 `locked`
FAILS = 5
FAIL_SEC = 900
ISSUER = "Studio"
#: резервный код — 10 знаков без похожих (0/o, 1/l/i), показывается «xxxxx-xxxxx»
BACKUP_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
BACKUP_RE = re.compile(f"[{BACKUP_ALPHABET}]{{10}}")


class SetupIn(BaseModel):
    password: str = Field(max_length=1024)


class EnableIn(BaseModel):
    ticket: str = Field(max_length=1024)
    code: str = Field(max_length=32)


class OffIn(BaseModel):
    password: str = Field(max_length=1024)


def code_at(secret: bytes, counter: int) -> str:
    """HOTP (RFC 4226) для счётчика: TOTP — счётчик `unix-время // STEP`."""
    mac = hmac.digest(secret, struct.pack(">Q", counter), "sha1")
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 10 ** DIGITS:0{DIGITS}d}"


def matching_step(secret: bytes, code: str, now: float, after: int = -1) -> int | None:
    """Шаг окна ±WINDOW, чей код совпал и который новее `after` (последнего принятого), иначе None."""
    current = int(now // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > after and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def _secret(uid: int) -> bytes | None:
    row = state.DB.execute("SELECT totp_secret FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return base64.b32decode(security.decrypt(row[0]))
    except (InvalidToken, ValueError):
        return None             # другой STUDIO_DATA_KEY: как выключенное, настроить заново


def status(uid: int) -> str:
    """'on' | 'off' | 'reenter' (секрет есть, но ключ сервера сменился)."""
    row = state.DB.execute("SELECT totp_secret FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None or row[0] is None:
        return "off"
    return "on" if _secret(uid) is not None else "reenter"


def enabled(uid: int) -> bool:
    return _secret(uid) is not None


def backup_left(uid: int) -> int:
    return state.DB.execute("SELECT count(*) FROM totp_backup WHERE user_id = ?", (uid,)).fetchone()[0]


def _backup_digest(uid: int, code: str) -> str:
    return security.digest(f"totp-backup:{uid}:{code}")


def _accept(uid: int, code: str, ip: str | None) -> bool:
    code = re.sub(r"[\s-]", "", code).lower()
    secret = _secret(uid)
    if secret is None:
        return False
    if re.fullmatch(r"\d{%d}" % DIGITS, code):
        last = state.DB.execute("SELECT totp_step FROM users WHERE id = ?", (uid,)).fetchone()[0]
        step = matching_step(secret, code, clock.NOW(), last)
        # условие в UPDATE: параллельный вход тем же кодом второй раз не пройдёт
        return step is not None and state.DB.execute(
            "UPDATE users SET totp_step = ? WHERE id = ? AND totp_step < ?", (step, uid, step)).rowcount == 1
    if BACKUP_RE.fullmatch(code) and state.DB.execute(
            "DELETE FROM totp_backup WHERE user_id = ? AND digest = ?", (uid, _backup_digest(uid, code))).rowcount:
        security_log.record(uid, "totp_backup_used", ip=ip)
        return True
    return False


def verify(uid: int, code: str, source: str, ip: str | None) -> bool:
    """Код приложения или резервный код учётки. Попытка резервируется паузой до проверки, верная — возвращается;
    неверная — `totp_fail` в журнал учётки (подробность — login | bridge). Без await: вызывающий сразу заводит
    сессию или ключ компьютера."""
    email_accounts.throttle("totp-user", str(uid), FAILS, FAIL_SEC)
    if _accept(uid, code, ip):
        email_accounts.refund("totp-user", str(uid))
        return True
    security_log.record(uid, "totp_fail", detail=source, ip=ip)
    return False


def challenge(uid: int, request: Request, next_: str) -> str:
    """Вызов входа после верного пароля: токен для cookie браузера, без письма."""
    email_accounts.throttle("code-user", str(uid), 5, 900)
    email_accounts.throttle("code-ip", auth.client_ip(request), 20, 900)
    row = state.DB.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
    return email_accounts.new_token(uid, "totp", row["email"] or "", email_accounts.CODE_SEC, next_=next_)


def is_challenge(token: str) -> bool:
    return bool(token) and state.DB.execute("SELECT 1 FROM email_tokens WHERE digest = ? AND kind = 'totp'",
                                            (security.digest(token),)).fetchone() is not None


def check_challenge(token: str, code: str, ip: str):
    """Строка вызова при верном коде (вызов гаснет); неверный — 400 `wrong_code` с `left`, исчерпан — `expired`."""
    row = email_accounts.token_row(token, "totp")
    state.DB.execute("UPDATE email_tokens SET attempts = attempts + 1 WHERE digest = ?", (row["digest"],))
    if not verify(row["user_id"], code, "login", ip):
        left = email_accounts.CODE_ATTEMPTS - row["attempts"] - 1
        if left > 0:
            raise HTTPException(400, {"code": "wrong_code", "message": "Неверный код", "left": left})
        raise email_accounts.invalid_token()
    email_accounts.delete_token(token)
    return row


def _uri(secret_b32: str, login: str) -> str:
    host = urlsplit(state.CFG.origin).hostname or ""
    label = quote(f"{ISSUER}:{login}@{host}" if host else f"{ISSUER}:{login}", safe="@:")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret_b32, "issuer": ISSUER, "algorithm": "SHA1",
                                                    "digits": DIGITS, "period": STEP})


def _clear(uid: int) -> None:
    state.DB.execute("UPDATE users SET totp_secret = NULL, totp_step = 0 WHERE id = ?", (uid,))
    state.DB.execute("DELETE FROM totp_backup WHERE user_id = ?", (uid,))


@router.post("/api/me/totp/setup")
async def setup(body: SetupIn, request: Request):
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "приложение-аутентификатор", digest=request.state.digest)
    if enabled(uid):
        raise HTTPException(409, {"code": "totp_on", "message": "Приложение уже подключено. Сначала отключите его."})
    secret_b32 = base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")
    ticket = security.encrypt(json.dumps(["totp-setup", request.state.digest, secret_b32, clock.NOW()])).decode("ascii")
    login = state.DB.execute("SELECT login FROM users WHERE id = ?", (uid,)).fetchone()[0]
    return Response(json.dumps({"secret": secret_b32, "uri": _uri(secret_b32, login), "ticket": ticket}),
                    media_type="application/json", headers={"Cache-Control": "no-store"})


@router.post("/api/me/totp")
async def enable(body: EnableIn, request: Request):
    uid = request.state.user.id
    try:
        purpose, digest, secret_b32, issued = json.loads(security.decrypt(body.ticket.encode("ascii")))
        if purpose != "totp-setup" or digest != request.state.digest or not 0 <= clock.NOW() - issued < SETUP_SEC:
            raise ValueError
        secret = base64.b32decode(secret_b32)
    except (InvalidToken, TypeError, ValueError, UnicodeError):
        raise HTTPException(400, {"code": "expired", "message": "Настройка устарела. Начните заново."}) from None
    step = matching_step(secret, re.sub(r"\s", "", body.code), clock.NOW())
    if step is None:
        raise HTTPException(400, {"code": "wrong_code", "message": "Неверный код"})
    if enabled(uid):
        raise HTTPException(409, {"code": "totp_on", "message": "Приложение уже подключено. Сначала отключите его."})
    codes = ["".join(secrets.choice(BACKUP_ALPHABET) for _ in range(10)) for _ in range(BACKUP_COUNT)]
    state.DB.execute("BEGIN")
    try:
        _clear(uid)
        state.DB.execute("UPDATE users SET totp_secret = ?, totp_step = ? WHERE id = ?",
                         (security.encrypt(secret_b32), step, uid))
        state.DB.executemany("INSERT INTO totp_backup(user_id, digest) VALUES (?, ?)",
                             [(uid, _backup_digest(uid, c)) for c in codes])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    security_log.record(uid, "totp_on", digest=request.state.digest, ip=auth.client_ip(request))
    log.info("пользователь %d: приложение-аутентификатор подключено", uid)
    return Response(json.dumps({"status": "on", "codes": [f"{c[:5]}-{c[5:]}" for c in codes]}),
                    media_type="application/json", headers={"Cache-Control": "no-store"})


@router.post("/api/me/totp/off")
async def disable(body: OffIn, request: Request):
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "приложение-аутентификатор", digest=request.state.digest)
    _clear(uid)
    security_log.record(uid, "totp_off", digest=request.state.digest, ip=auth.client_ip(request))
    log.info("пользователь %d: приложение-аутентификатор отключено", uid)
    return {"status": "off"}

