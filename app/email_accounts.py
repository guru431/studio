"""Приглашения, восстановление пароля, подтверждённая почта и дополнительные коды входа.

Публичные секреты передаются телами JSON. В базе — хеши токенов; код браузерного входа
дополнительно требует секретную HttpOnly cookie того же браузера.
"""
import asyncio
import hmac
import json
import logging
import re
import secrets
import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator

import account_mail
import auth
import clock
import db
import account_profile as profile
import security
import security_log
import state

log = logging.getLogger("studio")
router = APIRouter()
CHALLENGE_COOKIE = "__Host-studio_email"
INVITE_SEC = 2 * 86400
RESET_SEC = 1800
CODE_SEC = 600
CODE_ATTEMPTS = 5
SMTP_TEST_SEC = 60
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
EMAIL_PATHS = frozenset({"/api/me", "/api/me/email", "/api/me/email/confirm"})
EMAIL_RE = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}")  # после lower(); re.I сопоставил бы «ſ» и «ı» с s и i


class BadEmail(ValueError):
    """Отказ `email_address`: main.invalid_request отдаёт его слова (свои, без присланного), а не общие."""


BAD_EMAIL = "Укажите корректный адрес электронной почты"


def email_address(value: str) -> str:
    value = value.strip().lower()
    if (len(value) > 254 or not EMAIL_RE.fullmatch(value) or ".." in value
            or value.startswith(".") or ".@" in value
            or len(value.split("@")[0]) > 64
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in value.split("@")[-1].split("."))):
        raise BadEmail(BAD_EMAIL)
    return value


class InviteIn(BaseModel):
    email: str = Field(max_length=254)
    comment: str = Field(default="", max_length=1000)
    is_admin: bool = False

    _email = field_validator("email")(email_address)


class CommentIn(BaseModel):
    comment: str = Field(max_length=1000)


class EmailIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)

    _email = field_validator("email")(email_address)


class CodesIn(BaseModel):
    enabled: bool
    password: str = Field(max_length=1024)


def has_email(uid: int) -> bool:
    row = state.DB.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
    return bool(row and row[0])


def throttle(scope: str, value: str, maximum: int, seconds: int) -> None:
    """Попытка резервируется до await; паузы переживают перезапуск службы."""
    now = clock.NOW()
    state.DB.execute("DELETE FROM email_limits WHERE started < ?", (now - 86400,))
    key = security.digest(scope + ":" + value)
    row = state.DB.execute("SELECT started, count FROM email_limits WHERE key = ?", (key,)).fetchone()
    if row and now - row["started"] < seconds and row["count"] >= maximum:
        raise HTTPException(429, {"code": "locked", "message": "Слишком много запросов. Попробуйте позже."})
    if not row or now - row["started"] >= seconds:
        state.DB.execute("INSERT INTO email_limits VALUES (?, ?, 1) ON CONFLICT(key)"
                         " DO UPDATE SET started=excluded.started, count=1", (key, now))
    else:
        state.DB.execute("UPDATE email_limits SET count=count+1 WHERE key = ?", (key,))


def refund(scope: str, value: str) -> None:
    """Вернуть попытку `throttle`: письмо не ушло (отказ SMTP) — пауза не копится, пока почта лежит."""
    state.DB.execute("UPDATE email_limits SET count=count-1 WHERE key = ? AND count > 0",
                     (security.digest(scope + ":" + value),))


def mail_ready() -> None:
    """Ненастроенная почта — отказ до пауз (как у приглашений): попытка не тратится. Слова — общие, как у `send`."""
    if not account_mail.configured(account_mail.settings()):
        raise HTTPException(503, {"code": "smtp_unset", "message": account_mail.FAILED_MESSAGE})


def new_token(uid: int, kind: str, email: str, seconds: int, *, code: str | None = None, next_: str = "") -> str:
    now = clock.NOW()
    state.DB.execute("DELETE FROM email_tokens WHERE expires <= ?", (now,))
    row = state.DB.execute("SELECT pw_hash FROM users WHERE id = ? AND disabled=0", (uid,)).fetchone()
    if row is None:
        raise invalid_token()
    token = security.new_token()
    secret_hash = (hmac.digest(state.CFG.data_key.encode(), (security.digest(token) + ":" + code).encode(), "sha256").hex()
                   if kind == "bridge" and code else security.digest(token + ":" + code) if code else None)
    state.DB.execute("INSERT INTO email_tokens(digest,kind,user_id,email,password_stamp,secret_hash,next,created,expires)"
                     " VALUES (?,?,?,?,?,?,?,?,?)", (security.digest(token), kind, uid, email,
                     security.digest(row["pw_hash"]), secret_hash,
                     auth.login_target(next_), now, now + seconds))
    return token


def invalid_token() -> HTTPException:
    return HTTPException(400, {"code": "expired", "message": "Ссылка или код недействительны. Запросите новые."})


def token_row(token: str, kind: str):
    row = state.DB.execute("SELECT t.*, u.pw_hash, u.email AS current_email FROM email_tokens t"
                           " JOIN users u ON u.id=t.user_id WHERE t.digest=? AND t.kind=? AND u.disabled=0",
                           (security.digest(token), kind)).fetchone()
    if (row is None or row["expires"] <= clock.NOW() or row["attempts"] >= CODE_ATTEMPTS
            or row["password_stamp"] != security.digest(row["pw_hash"])
            or (kind in ("login", "bridge", "reset") and row["email"] != row["current_email"])):
        raise invalid_token()
    return row


def delete_token(token: str) -> None:
    state.DB.execute("DELETE FROM email_tokens WHERE digest=?", (security.digest(token),))


async def public_data(request: Request) -> dict:
    if auth.foreign_origin(request):
        raise HTTPException(403, {"code": "origin", "message": "Запрос пришёл с чужого сайта"})
    raw = await auth.small_body(request, auth.LOGIN_BODY_MAX)
    if raw is None:
        raise HTTPException(413, {"code": "too_long", "message": "Слишком длинный запрос"})
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
        raise HTTPException(422, {"code": "invalid", "message": "Неверные данные запроса"})
    return data


def link(kind: str, token: str, origin: str | None = None) -> str:
    return (origin or state.CFG.origin.rstrip("/")) + "/account/" + kind + "#" + token


async def login_code(uid: int, request: Request, next_: str, kind: str = "login") -> str:
    row = state.DB.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    mail_ready()
    ip = auth.client_ip(request)
    throttle("code-user", str(uid), 5, 900)
    throttle("code-ip", ip, 20, 900)
    code = f"{secrets.randbelow(1000000):06d}"
    token = new_token(uid, kind, row["email"], CODE_SEC, code=code, next_=next_)
    try:
        try:
            await account_mail.send(row["email"], "Студия: код входа / Studio: sign-in code",
                                    "Код входа действует 10 минут. Никому его не сообщайте.\n"
                                    "The sign-in code is valid for 10 minutes. Do not share it with anyone.\n\n" + code)
        except HTTPException:            # письмо не ушло — попытки вернуть
            refund("code-user", str(uid))
            refund("code-ip", ip)
            raise
        token_row(token, kind)  # за время SMTP могли изменить пароль, почту или отключить учётку
    except BaseException:
        delete_token(token)
        raise
    security_log.record(uid, "login_code_sent", detail=kind, ip=ip)     # пароль верен: знает ли его владелец — видно
    return token


def check_code(token: str, code: str, kind: str = "login", *, ip: str | None = None):
    row = token_row(token, kind)
    state.DB.execute("UPDATE email_tokens SET attempts=attempts+1 WHERE digest=?", (row["digest"],))
    if not hmac.compare_digest(security.digest(token + ":" + code), row["secret_hash"]):
        security_log.record(row["user_id"], "login_code_fail", detail=kind, ip=ip)
        left = CODE_ATTEMPTS - row["attempts"] - 1
        if left > 0:            # код ещё жив: набрать снова, а не начинать вход заново
            raise HTTPException(400, {"code": "wrong_code", "message": "Неверный код", "left": left})
        raise invalid_token()
    delete_token(token)
    return row


def bridge_code(uid: int, code: str, *, ip: str | None = None) -> None:
    row = state.DB.execute("SELECT t.*,u.pw_hash,u.email AS current_email FROM email_tokens t JOIN users u ON u.id=t.user_id"
                           " WHERE t.user_id=? AND t.kind='bridge' AND u.disabled=0 ORDER BY t.created DESC,t.rowid DESC LIMIT 1",
                           (uid,)).fetchone()
    if (row is None or row["expires"] <= clock.NOW() or row["attempts"] >= CODE_ATTEMPTS
            or row["password_stamp"] != security.digest(row["pw_hash"]) or row["email"] != row["current_email"]):
        raise invalid_token()
    state.DB.execute("UPDATE email_tokens SET attempts=attempts+1 WHERE digest=?", (row["digest"],))
    supplied = hmac.digest(state.CFG.data_key.encode(), (row["digest"] + ":" + code).encode(), "sha256").hex()
    if not hmac.compare_digest(supplied, row["secret_hash"]):
        security_log.record(uid, "login_code_fail", detail="bridge", ip=ip)
        raise invalid_token()
    state.DB.execute("DELETE FROM email_tokens WHERE user_id=? AND kind='bridge'", (uid,))


@router.api_route("/account/{screen}", methods=["GET", "HEAD"])
async def account_page(screen: str):
    if screen not in ("invite", "forgot", "reset", "code", "email", "verify"):
        raise HTTPException(404)
    return HTMLResponse(auth.page("account.html"), headers=PUBLIC_HEADERS)


def invitation(row) -> dict:
    """Статус: принятое без учётки (`user_id` обнулило её удаление) — `deleted`, адрес можно пригласить снова."""
    return {"id": row["id"], "email": row["email"], "comment": row["comment"], "is_admin": bool(row["is_admin"]),
            "created": row["created"], "expires": row["expires"], "user_id": row["user_id"],
            "status": (("accepted" if row["user_id"] is not None else "deleted") if row["accepted"] is not None
                       else "revoked" if row["revoked"] is not None else "failed" if not row["sent"]
                       else "expired" if row["expires"] <= clock.NOW() else "pending")}


@router.get("/api/admin/invitations", dependencies=[Depends(auth.require_admin)])
async def invitations():
    return {"items": [invitation(r) for r in state.DB.execute("SELECT * FROM invitations ORDER BY created DESC, id")]}


@router.post("/api/admin/invitations", status_code=201)
async def invite(body: InviteIn, request: Request, admin: auth.User = Depends(auth.require_admin)):
    if state.DB.execute("SELECT 1 FROM users WHERE email=?", (body.email,)).fetchone():
        raise HTTPException(409, {"code": "email", "message": "Пользователь с этой почтой уже есть"})
    if not account_mail.configured(account_mail.settings()):
        raise account_mail.unset()          # до паузы и строки: не тратить попытку и не оставлять неотправленное
    throttle("invite-admin", str(admin.id), 30, 900)
    throttle("invite-email", body.email, 3, 900)
    old = state.DB.execute("SELECT * FROM invitations WHERE email=?", (body.email,)).fetchone()
    if old and old["accepted"] is not None:
        # учётки с этим адресом нет (проверено выше): её удалили или сменили почту — адрес приглашается снова
        state.DB.execute("UPDATE invitations SET accepted=NULL,user_id=NULL,token_hash=NULL,revoked=NULL,sent=0"
                         " WHERE id=?", (old["id"],))
        old = state.DB.execute("SELECT * FROM invitations WHERE id=?", (old["id"],)).fetchone()
    token, now = security.new_token(), clock.NOW()
    iid = old["id"] if old else secrets.token_hex(12)
    # Неотправленное приглашение остаётся в списке. Ошибка повторной отправки не отзывает прежнюю ссылку.
    if not old:
        state.DB.execute("INSERT INTO invitations(id,email,comment,is_admin,invited_by,created,expires) VALUES (?,?,?,?,?,?,?)",
                         (iid, body.email, body.comment, int(body.is_admin), admin.id, now, now + INVITE_SEC))
    links = link("invite", token)
    if state.CFG.alternate_origin:
        links += "\n\nДругой адрес / Alternate address\n" + link("invite", token, state.CFG.alternate_origin)
    await account_mail.send_admin(body.email, "Приглашение в Студию / Invitation to Studio",
                                  "Вы приглашены в Студию. Выберите логин и пароль по одноразовой ссылке, она действует 48 часов.\n"
                                  "You are invited to Studio. Choose a login and password with this one-time link,"
                                  " valid for 48 hours.\n\n" + links)
    # После SMTP снова проверить права пригласившего и состояние приглашения.
    inviter = state.DB.execute("SELECT is_admin,disabled FROM users WHERE id=?", (admin.id,)).fetchone()
    if inviter is None or not inviter["is_admin"] or inviter["disabled"]:
        raise HTTPException(403)
    changed = state.DB.execute("UPDATE invitations SET comment=?,is_admin=?,invited_by=?,created=?,expires=?,"
                               "token_hash=?,revoked=NULL,sent=1 WHERE id=? AND accepted IS NULL AND token_hash IS ? AND revoked IS ?",
                               (body.comment, int(body.is_admin), admin.id, now, now + INVITE_SEC,
                                security.digest(token), iid, old["token_hash"] if old else None,
                                old["revoked"] if old else None)).rowcount
    if not changed:
        raise invalid_token()
    return invitation(state.DB.execute("SELECT * FROM invitations WHERE id=?", (iid,)).fetchone())


@router.post("/api/admin/smtp/test")
async def smtp_test(admin: auth.User = Depends(auth.require_admin)):
    """Тестовое письмо — только на подтверждённый адрес самого администратора, не чаще раза в SMTP_TEST_SEC."""
    row = state.DB.execute("SELECT email FROM users WHERE id=?", (admin.id,)).fetchone()
    if not row["email"]:
        raise HTTPException(409, {"code": "email", "message": "Сначала подтвердите почту"})
    if not account_mail.configured(account_mail.settings()):
        raise account_mail.unset()
    throttle("smtp-test", str(admin.id), 1, SMTP_TEST_SEC)
    await account_mail.send_admin(row["email"], "Студия: тестовое письмо / Studio: test email",
                                  "Это тестовое письмо: почта Студии настроена.\n"
                                  "This is a test email: Studio mail is set up.")
    return {"ok": True}


@router.patch("/api/admin/invitations/{iid}", dependencies=[Depends(auth.require_admin)])
async def comment(iid: str, body: CommentIn):
    if not state.DB.execute("UPDATE invitations SET comment=? WHERE id=?", (body.comment, iid)).rowcount:
        raise HTTPException(404)
    return invitation(state.DB.execute("SELECT * FROM invitations WHERE id=?", (iid,)).fetchone())


@router.delete("/api/admin/invitations/{iid}", dependencies=[Depends(auth.require_admin)], status_code=204)
async def revoke(iid: str):
    """Ожидающее приглашение отзывается (ссылка гаснет, строка — «Отозвано»); отозванное, просроченное,
    неотправленное и принятое удалённой учёткой — удаляется вместе с адресом и комментарием. Живой учётки — 409."""
    now = clock.NOW()
    if state.DB.execute("UPDATE invitations SET revoked=?,token_hash=NULL WHERE id=? AND accepted IS NULL"
                        " AND revoked IS NULL AND sent=1 AND expires>?", (now, iid, now)).rowcount:
        return Response(status_code=204)
    if not state.DB.execute("DELETE FROM invitations WHERE id=? AND (accepted IS NULL OR user_id IS NULL)",
                            (iid,)).rowcount:
        raise HTTPException(409, {"code": "accepted", "message": "Приглашение уже принято или удалено"})
    return Response(status_code=204)


def revoke_invited_by(uid: int) -> None:
    """Ожидающие приглашения держатся на правах пригласившего: разжалование, отключение и удаление их отзывают."""
    state.DB.execute("UPDATE invitations SET revoked=?,token_hash=NULL WHERE invited_by=? AND accepted IS NULL"
                     " AND revoked IS NULL", (clock.NOW(), uid))


def invite_row(token: str):
    row = state.DB.execute("SELECT * FROM invitations WHERE token_hash=? AND accepted IS NULL AND revoked IS NULL"
                           " AND sent=1 AND expires>?", (security.digest(token), clock.NOW())).fetchone()
    if row is None:
        raise invalid_token()
    return row


@router.post("/account/invite/check")
async def check_invite(request: Request):
    data = await public_data(request)
    throttle("link-ip", auth.client_ip(request), 60, 900)
    row = invite_row(data.get("token", ""))
    return {"email": row["email"]}


@router.post("/account/invite/accept", status_code=201)
async def accept_invite(request: Request):
    data = await public_data(request)
    throttle("accept-ip", auth.client_ip(request), 20, 900)
    token = data.get("token", "")
    invite_row(token)
    login = data.get("login", "").strip().lower()
    password = data.get("password", "")
    if not re.fullmatch(auth.LOGIN_RE, login):
        raise HTTPException(422, {"code": "login", "message": "Логин — 2–32 знака: латиница, цифры, . _ -"})
    profile.check_password(password)
    hashed = await asyncio.to_thread(security.hash_password, password)
    state.DB.execute("BEGIN")
    try:
        row = invite_row(token)
        uid = state.DB.execute("INSERT INTO users(id,login,name,pw_hash,is_admin,created,email,theme)"
                               " VALUES (?,?,?,?,?,?,?,?)",
                               (db.next_user_id(state.DB), login, login, hashed, row["is_admin"], clock.NOW(),
                                row["email"], db.DEFAULT_THEME)).lastrowid
        state.DB.execute("UPDATE invitations SET accepted=?,user_id=?,token_hash=NULL WHERE id=?",
                         (clock.NOW(), uid, row["id"]))
        state.DB.execute("COMMIT")
    except sqlite3.IntegrityError:
        state.DB.execute("ROLLBACK")
        raise HTTPException(409, {"code": "taken", "message": "Логин или почта уже заняты"}) from None
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    return {"ok": True}


async def recovery_mail(token: str, email: str, origin: str | None, *, admin: bool = False) -> None:
    send = account_mail.send_admin if admin else account_mail.send
    try:
        await send(email, "Студия: восстановление пароля / Studio: password recovery",
                   "Чтобы задать новый пароль, откройте одноразовую ссылку, она действует 30 минут."
                   " Если вы не запрашивали её, проигнорируйте письмо.\n"
                   "To set a new password, open this one-time link, valid for 30 minutes."
                   " If you did not request it, ignore this email.\n\n" + link("reset", token, origin))
    except HTTPException:
        delete_token(token)
        if admin:
            raise


@router.post("/account/forgot")
async def forgot(request: Request, tasks: BackgroundTasks):
    data = await public_data(request)
    throttle("forgot-ip", auth.client_ip(request), 20, 900)
    identity = data.get("login", "").strip().lower()
    # Известные и неизвестные учётки получают одинаковый ответ; отправка начинается после ответа.
    throttle("forgot-identity", identity, 3, 900)
    row = state.DB.execute("SELECT id,email FROM users WHERE (login=? OR email=?) AND disabled=0 AND email IS NOT NULL",
                           (identity, identity)).fetchone()
    if row:
        try:
            throttle("forgot-user", str(row["id"]), 5, 900)
        except HTTPException:
            pass
        else:
            token = new_token(row["id"], "reset", row["email"], RESET_SEC)
            tasks.add_task(recovery_mail, token, row["email"], auth.request_origin(request))
    return {"message": "Если учётная запись найдена, на её почту отправлена ссылка восстановления."}


@router.post("/account/reset/check")
async def check_reset(request: Request):
    """Ссылка восстановления жива? — до того, как человек дважды наберёт новый пароль (как `check_invite`)."""
    data = await public_data(request)
    throttle("reset-ip", auth.client_ip(request), 20, 900)
    token_row(data.get("token", ""), "reset")
    return {"ok": True}


@router.post("/account/reset")
async def reset(request: Request):
    data = await public_data(request)
    throttle("reset-ip", auth.client_ip(request), 20, 900)
    token, password = data.get("token", ""), data.get("password", "")
    token_row(token, "reset")
    profile.check_password(password)
    hashed = await asyncio.to_thread(security.hash_password, password)
    state.DB.execute("BEGIN")
    try:
        row = token_row(token, "reset")
        state.DB.execute("UPDATE users SET pw_hash=? WHERE id=?", (hashed, row["user_id"]))
        auth.end_sessions(row["user_id"])
        state.DB.execute("DELETE FROM email_tokens WHERE user_id=?", (row["user_id"],))
        security_log.record(row["user_id"], "password_recovery", ip=auth.client_ip(request))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    return {"ok": True}


@router.post("/account/code")
async def complete_login(request: Request):
    data = await public_data(request)
    throttle("verify-code-ip", auth.client_ip(request), 30, 900)
    token = request.cookies.get(CHALLENGE_COOKIE, "")
    row = check_code(token, data.get("code", ""), ip=auth.client_ip(request))
    response = auth.finish_login(row["user_id"], request, row["next"][1:], as_json=True)
    response.delete_cookie(CHALLENGE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def challenge_cookie(response: Response, token: str) -> None:
    response.set_cookie(CHALLENGE_COOKIE, token, max_age=CODE_SEC, path="/", secure=True, httponly=True, samesite="lax")


@router.post("/account/code/resend")
async def resend_code(request: Request):
    """«Отправить код ещё раз»: письмо не пришло. Cookie вызова — доказательство верного пароля этого браузера; новый
    код заменяет прежний (тот гаснет), паузы — те же, что у входа (`login_code`). Вызов истёк или исчерпан — 400
    `expired`: начать вход заново."""
    await public_data(request)
    throttle("verify-code-ip", auth.client_ip(request), 30, 900)
    old = request.cookies.get(CHALLENGE_COOKIE, "")
    row = token_row(old, "login")
    token = await login_code(row["user_id"], request, row["next"][1:])
    delete_token(old)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    challenge_cookie(response, token)
    return response


@router.post("/api/me/email")
async def request_email(body: EmailIn, request: Request):
    uid = request.state.user.id
    if state.DB.execute("SELECT 1 FROM users WHERE id=? AND email=?", (uid, body.email)).fetchone():
        # своя же почта (поле заполнено ею) — не «уже используется» и без траты попытки из трёх
        raise HTTPException(409, {"code": "same", "message": "Это и есть почта вашей учётки"})
    await auth.confirm_password(uid, body.password, "подтверждение почты", digest=request.state.digest)
    mail_ready()
    throttle("email-user", str(uid), 3, 900)        # до проверки занятости: 409 не даёт перебирать чужие адреса
    if state.DB.execute("SELECT 1 FROM users WHERE email=?", (body.email,)).fetchone():
        raise HTTPException(409, {"code": "email", "message": "Эта почта уже используется"})
    token = new_token(uid, "email", body.email, RESET_SEC)
    try:
        await account_mail.send(body.email, "Студия: подтвердите почту / Studio: confirm your email",
                                "Подтвердите адрес почты для своей учётной записи. Ссылка действует 30 минут;"
                                " откройте её в браузере, где вошли в Студию.\n"
                                "Confirm the email address of your account. The link is valid for 30 minutes;"
                                " open it in the browser where you are signed in to Studio.\n\n"
                                + link("verify", token, auth.request_origin(request)))
    except HTTPException:                # письмо не ушло — попытку вернуть (адрес свободен: 409 не возвращается)
        delete_token(token)
        refund("email-user", str(uid))
        raise
    except BaseException:
        delete_token(token)
        raise
    return {"message": "Письмо отправлено. Откройте ссылку в этом браузере."}


async def change_notice(uid: int, email: str) -> None:
    """Прежнему адресу — без нового адреса и ссылок: смена почты переносит восстановление пароля."""
    try:
        await account_mail.send(email, "Студия: почта изменена / Studio: email changed",
                                "Почта вашей учётной записи Студии изменена. Если это сделали не вы,"
                                " срочно сообщите администратору Студии.\n"
                                "The email address of your Studio account was changed. If it was not you,"
                                " contact your Studio administrator right away.")
    except HTTPException as err:     # смена уже состоялась; в журнал — только id учётки и код отказа
        log.warning("account email: change notice for user %d failed (%s)", uid, err.detail.get("code"))


@router.post("/api/me/email/confirm")
async def confirm_email(request: Request, tasks: BackgroundTasks):
    data = await public_data(request)
    state.DB.execute("BEGIN")
    try:
        row = token_row(data.get("token", ""), "email")
        if row["user_id"] != request.state.user.id:
            raise invalid_token()
        state.DB.execute("UPDATE users SET email=? WHERE id=?", (row["email"], row["user_id"]))
        state.DB.execute("DELETE FROM email_tokens WHERE user_id=?", (row["user_id"],))
        auth.end_sessions(row["user_id"], request.state.digest)
        security_log.record(row["user_id"], "email_change", digest=request.state.digest, ip=auth.client_ip(request))
        state.DB.execute("COMMIT")
    except sqlite3.IntegrityError:
        state.DB.execute("ROLLBACK")
        raise HTTPException(409, {"code": "email", "message": "Эта почта уже используется"}) from None
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    if row["current_email"] and row["current_email"] != row["email"]:
        tasks.add_task(change_notice, row["user_id"], row["current_email"])
    return {"ok": True}


@router.put("/api/me/email-codes")
async def email_codes(body: CodesIn, request: Request):
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "коды входа", digest=request.state.digest)
    if not has_email(uid):
        raise HTTPException(409, {"code": "email", "message": "Сначала подтвердите почту"})
    state.DB.execute("UPDATE users SET email_codes=? WHERE id=?", (int(body.enabled), uid))
    state.DB.execute("DELETE FROM email_tokens WHERE user_id=? AND kind IN ('login','bridge')", (uid,))
    security_log.record(uid, "email_codes_on" if body.enabled else "email_codes_off", digest=request.state.digest,
                        ip=auth.client_ip(request))
    return {"enabled": body.enabled}
