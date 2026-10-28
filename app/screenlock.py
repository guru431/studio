"""Блокировка на телефоне (спека 2026-10-21-studio-screen-lock): отметки «на экране», «заперто»,
отпирание паролем учётки, passkey и PIN, ключи учётки, PIN учётки, включение и выключение.

Признак «заперто» считает auth.guard (auth.screen_locked); запертая сессия делает только запросы
auth.SCREEN_LOCK_PATHS. Сессия запроса — request.state.digest. Passkey: RP ID — хост STUDIO_ORIGIN,
проверка ответов — библиотекой webauthn (origin, RP ID, вызов, UV, подпись, счётчик). PIN (спека
2026-10-28-studio-lock-pin): 4–8 цифр ASCII, хеш users.pin_hash (scrypt, как пароль); задать и сменить —
с паролем учётки, убрать — без. Журнал — только события с id: credential id, открытый ключ, вызов, ответы
устройства, пароль, PIN и его хеш туда не пишутся."""
import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from urllib.parse import urlparse

import webauthn
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AttestationConveyancePreference, AuthenticatorAttachment,
                                      AuthenticatorSelectionCriteria, AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

import auth
import clock
import security
import state

PASSWORD_MAX = 1024
PIN_FAILS = 10                   # неверные PIN подряд на учётку — дальше только пароль
PIN_RE = re.compile(r"[0-9]{4,8}")   # только ASCII: \d и isdigit() берут «１２３４» и «١٢٣٤»
PIN_MAX = 64                     # предел строки PIN в теле
CHALLENGE_SEC = 120
#: (digest сессии, "register" | "open") → (вызов, когда выдан); в памяти службы — перезапуск его забудет
CHALLENGES: dict[tuple[str, str], tuple[bytes, float]] = {}
NAMES = ("iPhone", "iPad", "Android", "Устройство")      # lock.js deviceName()
TRANSPORTS = frozenset(t.value for t in AuthenticatorTransport)
REFUSED_KEY = {"code": "passkey", "message": "устройство не подтвердило ключ"}
REFUSED_OPEN = {"code": "passkey", "message": "ключ не подошёл — нажмите «Открыть» ещё раз"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class Alive(BaseModel):
    touch: bool = False          # основной указатель — палец (matchMedia('(pointer: coarse)'))


class Password(BaseModel):
    password: str = Field(max_length=PASSWORD_MAX)


class ScreenLock(BaseModel):
    enabled: bool
    password: str | None = Field(default=None, max_length=PASSWORD_MAX)


class KeyPassword(BaseModel):
    password: str | None = Field(default=None, max_length=PASSWORD_MAX)


class Pin(BaseModel):
    pin: str = Field(max_length=PIN_MAX)


class NewPin(BaseModel):
    password: str | None = Field(None, max_length=PASSWORD_MAX)
    pin: str = Field(max_length=PIN_MAX)


class NewKey(BaseModel):
    credential: dict             # ответ navigator.credentials.create() в JSON-форме
    name: str


class Answer(BaseModel):
    credential: dict             # ответ navigator.credentials.get()


def _has_keys(uid: int) -> bool:
    return state.DB.execute("SELECT 1 FROM passkeys WHERE user_id = ? LIMIT 1", (uid,)).fetchone() is not None


def _seen(request: Request, value: float | None) -> None:
    state.DB.execute("UPDATE sessions SET seen = ? WHERE digest = ?", (value, request.state.digest))


def _rp() -> tuple[str, str]:
    """(RP ID, origin): хост и адрес STUDIO_ORIGIN — ключ работает только на нём."""
    origin = state.CFG.origin.rstrip("/")
    return urlparse(origin).hostname, origin


def _issue(digest: str, kind: str) -> bytes:
    """Новый вызов сессии (прежний того же вида заменяется). Заодно забыть просроченные: иначе вызовы
    закрытых сессий лежали бы до перезапуска."""
    now = clock.NOW()
    for k in [k for k, (_, issued) in CHALLENGES.items() if now - issued > CHALLENGE_SEC]:
        del CHALLENGES[k]
    challenge = secrets.token_bytes(32)
    CHALLENGES[(digest, kind)] = (challenge, now)
    return challenge


def _take(digest: str, kind: str) -> bytes | None:
    """Вызов одноразовый: взятие удаляет; старше CHALLENGE_SEC — вызова нет."""
    issued = CHALLENGES.pop((digest, kind), None)
    if issued is None or clock.NOW() - issued[1] > CHALLENGE_SEC:
        return None
    return issued[0]


def _handle(key_id: str) -> str:
    """id ключа в API — хеш credential id: DELETE несёт его строкой запроса, а её пишет журнал Apache."""
    return hashlib.sha256(key_id.encode()).hexdigest()[:16]


def _descriptors(uid: int) -> list[PublicKeyCredentialDescriptor]:
    rows = state.DB.execute("SELECT id, transports FROM passkeys WHERE user_id = ? ORDER BY created", (uid,))
    return [PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(r["id"]),
                                          transports=[AuthenticatorTransport(t) for t in json.loads(r["transports"])])
            for r in rows]


# ---- отметки, «заперто», пароль ----------------------------------------------

@router.get("/lock")
async def lock_state(request: Request):
    """Только «заперто ли», «есть ли ключи», CSRF (без него не отправить отпирание), задан ли PIN и
    сколько попыток PIN осталось — профиля нет."""
    uid = request.state.user.id
    row = state.DB.execute("SELECT pin_hash IS NOT NULL AS pin, pin_fails FROM users WHERE id = ?", (uid,)).fetchone()
    return {"locked": request.state.locked, "keys": _has_keys(uid), "csrf": request.state.csrf,
            "pin": bool(row["pin"]), "pin_left": max(0, PIN_FAILS - row["pin_fails"])}


@router.post("/lock/alive")
async def alive(body: Alive, request: Request):
    """Отметка «на экране». Сначала защёлка mobile (сбросить её может только новый вход), затем
    «заперто» заново по базе: иначе старая сессия телефона с пустым seen открылась бы первой же
    отметкой. Запертой seen не трогается."""
    if body.touch:
        state.DB.execute("UPDATE sessions SET mobile = 1 WHERE digest = ?", (request.state.digest,))
    row = state.DB.execute("SELECT u.screen_lock, s.mobile, s.seen FROM sessions s JOIN users u ON u.id = s.user_id"
                           " WHERE s.digest = ?", (request.state.digest,)).fetchone()
    if row is None:                  # сессию закрыли между guard и этим запросом
        raise HTTPException(401, {"code": "login", "message": "нужен вход"})
    now = clock.NOW()
    locked = auth.screen_locked(row["screen_lock"], row["mobile"], row["seen"], now)
    if not locked:
        _seen(request, now)
    return {"locked": locked}


@router.post("/lock/now", status_code=204)
async def lock_now(request: Request):
    """Студия на телефоне заперлась сама (ушла с экрана дольше минуты): сервер с ней согласен."""
    _seen(request, None)
    log.info("пользователь %d: заперто", request.state.user.id)
    return Response(status_code=204)


@router.post("/lock/password", status_code=204)
async def open_with_password(body: Password, request: Request):
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "отпирание")
    state.DB.execute("UPDATE users SET pin_fails = 0 WHERE id = ?", (uid,))    # PIN снова работает везде
    _seen(request, clock.NOW())
    log.info("пользователь %d: открыто (пароль)", uid)
    return Response(status_code=204)


@router.post("/lock/pin", status_code=204)
async def open_with_pin(body: Pin, request: Request):
    """Счёт неверных PIN — один на учётку (все её сессии). Попытка сначала занимает место в счёте одним
    UPDATE и лишь потом считает scrypt: пачка параллельных запросов не проверит больше PIN_FAILS PIN.
    Верный засчитывается, только если хеш после scrypt тот же (PIN не сменили и не убрали). Не 4–8 цифр —
    неверный PIN с тратой попытки: формат ничего не подсказывает. Пауза пароля PIN не касается."""
    uid = request.state.user.id
    rows = state.DB.execute("UPDATE users SET pin_fails = pin_fails + 1"
                            " WHERE id = ? AND pin_hash IS NOT NULL AND pin_fails < ?"
                            " RETURNING pin_fails, pin_hash", (uid, PIN_FAILS)).fetchall()
    if not rows:
        row = state.DB.execute("SELECT pin_hash FROM users WHERE id = ?", (uid,)).fetchone()
        if row is None or row["pin_hash"] is None:
            raise HTTPException(409, {"code": "no_pin", "message": "PIN не задан — откройте паролем"})
        log.warning("пользователь %d: PIN исчерпан", uid)
        raise HTTPException(403, {"code": "pin_exhausted",
                                  "message": "PIN не принимается после 10 ошибок — откройте паролем учётки"})
    fails, stored = rows[0]["pin_fails"], rows[0]["pin_hash"]
    right = bool(PIN_RE.fullmatch(body.pin)) and await asyncio.to_thread(security.verify_password, body.pin, stored)
    if right and state.DB.execute("UPDATE users SET pin_fails = 0 WHERE id = ? AND pin_hash = ? AND disabled = 0",
                                  (uid, stored)).rowcount == 1:
        _seen(request, clock.NOW())
        log.info("пользователь %d: открыто (PIN)", uid)
        return Response(status_code=204)
    log.warning("пользователь %d: PIN неверен", uid)
    if fails >= PIN_FAILS:
        log.warning("пользователь %d: PIN исчерпан", uid)
    raise HTTPException(403, {"code": "pin", "message": "неверный PIN", "left": PIN_FAILS - fails})


@router.post("/me/screen-lock")
async def set_screen_lock(body: ScreenLock, request: Request):
    """Включить — без пароля; выключить — только с паролем учётки: иначе получивший телефон открытым
    на минуту выключил бы её. Запертой сессии путь не открыт (guard)."""
    uid = request.state.user.id
    if not body.enabled:
        await auth.confirm_password(uid, body.password or "", "выключение блокировки")
    state.DB.execute("UPDATE users SET screen_lock = ? WHERE id = ?", (int(body.enabled), uid))
    log.info("пользователь %d: блокировка %s", uid, "включена" if body.enabled else "выключена")
    return {"screen_lock": body.enabled}


# ---- PIN учётки --------------------------------------------------------------

@router.put("/me/pin")
async def set_pin(body: NewPin, request: Request):
    """Задать или сменить — только с паролем учётки (сначала формат: он паузу пароля не тратит). Счёт
    неверных PIN не трогается: новый PIN не возвращает истраченные попытки. Запертой сессии путь не
    открыт (guard)."""
    uid = request.state.user.id
    if not PIN_RE.fullmatch(body.pin):
        raise HTTPException(422, {"code": "pin_format", "message": "PIN — от 4 до 8 цифр"})
    await auth.confirm_password(uid, body.password or "", "PIN")
    pin_hash = await asyncio.to_thread(security.hash_password, body.pin)
    state.DB.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (pin_hash, uid))
    log.info("пользователь %d: PIN задан", uid)
    return {"pin": True}


@router.delete("/me/pin", status_code=204)
async def delete_pin(request: Request):
    """Без пароля: доступа удаление не даёт, отпирать тогда — паролем или passkey."""
    uid = request.state.user.id
    state.DB.execute("UPDATE users SET pin_hash = NULL WHERE id = ?", (uid,))
    log.info("пользователь %d: PIN убран", uid)
    return Response(status_code=204)


# ---- ключи passkey -----------------------------------------------------------

@router.post("/passkeys/options")
async def passkey_options(body: KeyPassword, request: Request):
    """Варианты создания ключа — только с паролем учётки: иначе получивший телефон открытым на минуту
    добавил бы свой. user.name — логин (его покажут «Пароли» iPhone), дескриптор — не логин и не имя."""
    user = request.state.user
    await auth.confirm_password(user.id, body.password or "", "ключ")
    options = webauthn.generate_registration_options(
        rp_id=_rp()[0], rp_name="Студия", user_name=user.login, user_display_name=user.login,
        user_id=f"studio-{user.id}".encode(), challenge=_issue(request.state.digest, "register"),
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM, resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=_descriptors(user.id))
    return json.loads(webauthn.options_to_json(options))


@router.post("/passkeys", status_code=201)
async def add_passkey(body: NewKey, request: Request):
    uid = request.state.user.id
    if body.name not in NAMES:
        raise HTTPException(422, {"code": "name", "message": "название ключа — по устройству"})
    challenge = _take(request.state.digest, "register")
    rp_id, origin = _rp()
    try:
        if challenge is None:
            raise ValueError("нет вызова")
        verified = webauthn.verify_registration_response(
            credential=body.credential, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
            require_user_verification=True)
    except Exception as e:       # ответ устройства — данные клиента: любой сбой разбора — отказ, а не 500
        # тип, а не сообщение библиотеки (в нём бывает origin): настройку STUDIO_ORIGIN видно по нему
        log.warning("пользователь %d: ключ не добавлен (%s)", uid, type(e).__name__)
        raise HTTPException(400, REFUSED_KEY) from None
    key_id = bytes_to_base64url(verified.credential_id)
    sent = body.credential["response"].get("transports")
    transports = [t for t in sent if isinstance(t, str) and t in TRANSPORTS] if isinstance(sent, list) else []
    now = clock.NOW()
    try:
        state.DB.execute("INSERT INTO passkeys(id, user_id, public_key, sign_count, transports, name, created)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)", (key_id, uid, verified.credential_public_key,
                                                          verified.sign_count, json.dumps(transports), body.name, now))
    except sqlite3.IntegrityError:
        raise HTTPException(409, {"code": "exists", "message": "этот ключ уже добавлен"}) from None
    log.info("пользователь %d: ключ добавлен", uid)
    return {"id": _handle(key_id), "name": body.name, "created": now, "used": None}


@router.get("/passkeys")
async def list_passkeys(request: Request):
    rows = state.DB.execute("SELECT id, name, created, used FROM passkeys WHERE user_id = ? ORDER BY created DESC",
                            (request.state.user.id,))
    return {"items": [{"id": _handle(r["id"]), "name": r["name"], "created": r["created"], "used": r["used"]}
                      for r in rows]}


@router.delete("/passkeys/{key_id}", status_code=204)
async def delete_passkey(key_id: str, request: Request):
    """Без пароля (и последний тоже): доступа удаление не даёт, отпирать тогда — паролем."""
    uid = request.state.user.id
    rows = state.DB.execute("SELECT id FROM passkeys WHERE user_id = ?", (uid,)).fetchall()
    found = next((r["id"] for r in rows if _handle(r["id"]) == key_id), None)
    if found is None:
        raise HTTPException(404, {"code": "not_found", "message": "такого ключа нет"})
    state.DB.execute("DELETE FROM passkeys WHERE id = ? AND user_id = ?", (found, uid))
    log.info("пользователь %d: ключ удалён", uid)
    return Response(status_code=204)


# ---- отпирание passkey -------------------------------------------------------

@router.post("/lock/challenge")
async def lock_challenge(request: Request):
    """Варианты входа: ключи учётки, проверка пользователя (Face ID, отпечаток, код устройства) обязательна."""
    uid = request.state.user.id
    allowed = _descriptors(uid)
    if not allowed:
        raise HTTPException(409, {"code": "no_keys", "message": "у учётки нет ключей — откройте паролем"})
    options = webauthn.generate_authentication_options(
        rp_id=_rp()[0], challenge=_issue(request.state.digest, "open"), allow_credentials=allowed,
        user_verification=UserVerificationRequirement.REQUIRED)
    return json.loads(webauthn.options_to_json(options))


@router.post("/lock/open", status_code=204)
async def open_with_passkey(body: Answer, request: Request):
    """Пауза пароля passkey не касается. Любой отказ — один ответ, seen не меняется."""
    uid = request.state.user.id
    challenge = _take(request.state.digest, "open")
    sent_id = body.credential.get("id")
    row = None
    if isinstance(sent_id, str):
        row = state.DB.execute("SELECT id, public_key, sign_count FROM passkeys WHERE id = ? AND user_id = ?",
                               (sent_id, uid)).fetchone()
    rp_id, origin = _rp()
    try:
        if challenge is None or row is None:
            raise ValueError("нет вызова или ключа")
        verified = webauthn.verify_authentication_response(
            credential=body.credential, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"],
            require_user_verification=True)
    except Exception as e:       # ответ устройства — данные клиента: любой сбой разбора — отказ, а не 500
        log.warning("пользователь %d: ключ не подошёл (%s)", uid, type(e).__name__)
        raise HTTPException(403, REFUSED_OPEN) from None
    now = clock.NOW()
    state.DB.execute("UPDATE passkeys SET sign_count = ?, used = ? WHERE id = ?",
                     (verified.new_sign_count, now, row["id"]))
    _seen(request, now)
    log.info("пользователь %d: открыто (passkey)", uid)
    return Response(status_code=204)
