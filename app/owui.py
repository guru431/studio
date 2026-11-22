"""Учётки Open WebUI в учётке Студии (спека 2026-11-22-studio-open-webui-design, «Учётка Open WebUI»).

- Подключение `POST /api/owui/accounts {label, url, email, password, cert_sha256?}`: адрес — по списку разрешённых
  (`upstream`); https с сертификатом, который не проверился системными корнями, — 409 `certificate` с отпечатком,
  повтор с `cert_sha256` запоминает PEM (`cert_pem` — якорь доверия ровно для него); `GET /api/config` — версия не
  ниже 0.13 и `features.auth_sessions`; вход — отказы `login`, `pending`, `mfa` (форма ответа — как у Open WebUI 0.13.4,
  `utils/auth.py::create_signin_response`); постоянная сессия `SESSION_NAME`. Пароль и JWT входа забываются сразу.
- Неудачи входа (неверный пароль) — на учётку Студии: `LOGIN_FAILS` за `LOGIN_WINDOW_SEC`, дальше 429 `owui_locked`
  без запроса к Open WebUI: Студия не служит подбору паролей.
- Переподключение — тот же адрес и та же учётка Open WebUI (другая — 409 `other_user`), прежняя сессия отзывается.
- Удаление — отзыв сессии (срок `REVOKE_SEC`, неудача не мешает), строка удаляется; удаление учётки Студии — то же
  для всех её строк (`drop_user`), отключение — только забыть рабочие токены.
- `call` — запрос к Open WebUI рабочим токеном (`owui_tokens`), 401 — одно обновление и повтор.
- Чужой и несуществующий id — одинаковый 404. Журнал — только id учётки Студии и строки учётки, коды и числа: ни
  адреса, ни email, ни токенов.
"""
import asyncio
import hashlib
import json
import logging
import re
import ssl
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, replace

import httpx
from cryptography import x509
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import clock
import owui_tokens
import screenlock
import security
import state
import upstream

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/owui")

LABEL_MAX = 80
#: неудачи входа в Open WebUI на учётку Студии за окно `LOGIN_WINDOW_SEC`, с; дальше — 429 `owui_locked`
LOGIN_FAILS = 5
LOGIN_WINDOW_SEC = 900
#: срок запроса к Open WebUI, с
TIMEOUT_SEC = 15
#: срок отзыва сессии при удалении, с: недоступный Open WebUI удаление не держит
REVOKE_SEC = 5
#: имя постоянной сессии в списке сессий Open WebUI
SESSION_NAME = "Studio"
#: самая ранняя поддерживаемая версия Open WebUI
MIN_VERSION = (0, 13)

#: uid Студии → моменты неудачных входов (и идущих попыток) по `clock.NOW`
FAILS: dict[int, list[float]] = {}

NOT_FOUND = {"code": "not_found", "message": "такой учётки Open WebUI нет"}
OFFLINE = {"code": "owui_offline", "message": "Open WebUI не отвечает"}
RELOGIN = {"code": "relogin", "message": "войдите в учётку Open WebUI заново"}
LOGIN = {"code": "login", "message": "неверный email или пароль Open WebUI"}
PENDING = {"code": "pending", "message": "учётка Open WebUI ждёт одобрения администратора Open WebUI"}
MFA = {"code": "mfa", "message": "вход со вторым кодом не поддерживается"}
LOCKED = {"code": "owui_locked", "message": "слишком много неудачных входов в Open WebUI — попробуйте позже"}
BUSY = {"code": "owui_locked", "message": "Open WebUI временно не принимает вход — попробуйте позже"}
EXISTS = {"code": "exists", "message": "эта учётка Open WebUI уже подключена"}
OTHER_USER = {"code": "other_user", "message": "это другая учётка Open WebUI — подключите её отдельно"}
BAD_URL = {"code": "url", "message": "адрес Open WebUI — со схемой http или https и хостом"}
NO_LABEL = {"code": "label", "message": "подпись не может быть пустой"}
NO_CERT = {"code": "url", "message": "у адреса http нет сертификата"}
HTTPS_ONLY = {"code": "unlisted", "message": "по имени сервер ходит только по https — укажите https-адрес"}
CERT_WORDS = "сертификат сервера не подтверждён — сверьте отпечаток SHA-256 и подтвердите его"


@dataclass(frozen=True)
class Account:
    id: int
    user_id: int
    label: str
    base_url: str
    cert_pem: str | None
    state: str
    version: str | None = None

    @property
    def name(self) -> str:
        return self.label


class ConnectIn(BaseModel):
    label: str = Field(max_length=LABEL_MAX)
    url: str = Field(max_length=2048)
    email: str = Field(max_length=320)
    password: str = Field(max_length=screenlock.PASSWORD_MAX)
    cert_sha256: str | None = Field(default=None, max_length=128)


class LoginIn(BaseModel):
    password: str = Field(max_length=screenlock.PASSWORD_MAX)


class LabelIn(BaseModel):
    label: str = Field(max_length=LABEL_MAX)


class CertificateIn(BaseModel):
    sha256: str = Field(max_length=128)


# ---- строки --------------------------------------------------------------------------------------------------------

def _row(uid: int, aid: int):
    row = state.DB.execute("SELECT * FROM owui_accounts WHERE id = ? AND user_id = ?", (aid, uid)).fetchone()
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    return row


def _account(row) -> Account:
    return Account(row["id"], row["user_id"], row["label"], row["base_url"], row["cert_pem"], row["state"],
                   row["version"])


def load(uid: int, aid: int) -> Account:
    """Учётка Open WebUI учётки Студии; чужая и несуществующая — одинаковый 404 `not_found`."""
    return _account(_row(uid, aid))


def _who(row) -> dict:
    try:
        who = json.loads(security.decrypt(row["who_enc"]))
    except (InvalidToken, TypeError, ValueError):          # сменили STUDIO_DATA_KEY: подпись без имени
        return {}
    return who if isinstance(who, dict) else {}


def _fingerprint(der: bytes) -> str:
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def _out(row) -> dict:
    who = _who(row)
    return {"id": row["id"], "label": row["label"], "url": row["base_url"],
            "who": {"name": who.get("name"), "email": who.get("email")}, "state": row["state"],
            "version": row["version"],
            "cert": _fingerprint(ssl.PEM_cert_to_DER_cert(row["cert_pem"])) if row["cert_pem"] else None}


def _label(text: str) -> str:
    label = " ".join(text.split())
    if not label:
        raise HTTPException(422, NO_LABEL)
    return label


def _base(text: str) -> str:
    """Адрес Open WebUI: схема, хост, порт и путь-префикс без `/` в конце; userinfo, query и fragment отбрасываются."""
    try:
        url = httpx.URL(text.strip())
        host = url.host
    except (httpx.InvalidURL, UnicodeError, ValueError, TypeError):
        raise HTTPException(422, BAD_URL) from None
    if url.scheme not in upstream.DEFAULT_PORTS or not host:
        raise HTTPException(422, BAD_URL)
    return f"{url.scheme}://{url.netloc.decode('ascii')}{url.path.rstrip('/')}"


def _listed(base: str) -> None:
    """Адрес — в списке разрешённых до всякого запроса: иначе 422 `unlisted` с записью, которую добавить."""
    if upstream.is_allowed(base):
        return
    url = httpx.URL(base)
    host = f"[{url.host}]" if ":" in url.host else url.host
    entry = f"{host}:{url.port or upstream.DEFAULT_PORTS[url.scheme]}"
    if entry in upstream.allowlist():
        raise HTTPException(422, HTTPS_ONLY)
    raise HTTPException(422, {"code": "unlisted",
                              "message": f"адреса нет в списке разрешённых — попросите администратора добавить {entry}"})


def _duplicate(uid: int, base: str, owui_user_id: str, skip: int | None = None) -> bool:
    return any(row["id"] != skip and _who(row).get("owui_user_id") == owui_user_id
               for row in state.DB.execute("SELECT id, who_enc FROM owui_accounts WHERE user_id = ? AND base_url = ?",
                                           (uid, base)))


# ---- отказы и сертификат -------------------------------------------------------------------------------------------

def shape_error(acc) -> HTTPException:
    """Open WebUI ответил не той формой: версия в словах — чтобы было видно, с какой Студия не сошлась."""
    if acc.version:
        return HTTPException(502, {"code": "owui_shape",
                                   "message": f"Open WebUI {acc.version} ответил не так, как ожидает Студия"})
    return HTTPException(502, {"code": "owui_shape", "message": "Open WebUI ответил не так, как ожидает Студия"})


def _untrusted(error: BaseException | None) -> bool:
    """В цепочке причин — несверенный сертификат (httpx → httpcore → ssl)."""
    for _ in range(8):
        if error is None:
            return False
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        error = error.__cause__ or error.__context__
    return False


def _hex(fingerprint: str | None) -> str:
    return re.sub(r"[^0-9a-f]", "", (fingerprint or "").lower())


def _certificate_info(der: bytes) -> dict:
    info = {"sha256": _fingerprint(der), "subject": None, "not_after": None}
    try:
        cert = x509.load_der_x509_certificate(der)
        info["subject"], info["not_after"] = cert.subject.rfc4514_string(), int(cert.not_valid_after_utc.timestamp())
    except ValueError:
        pass
    return info


async def _confirm(acc, sha256: str | None) -> str:
    """PEM сертификата сервера, если его отпечаток — `sha256`; иначе 409 `certificate` с отпечатком сервера."""
    try:
        der = await upstream.fetch_certificate(acc, acc.base_url)
    except upstream.UpstreamRefused as e:
        raise HTTPException(422, {"code": "unlisted", "message": upstream.refused_words(e, acc)}) from None
    except (OSError, TimeoutError):
        raise HTTPException(503, OFFLINE) from None
    info = _certificate_info(der)
    if not sha256 or _hex(sha256) != _hex(info["sha256"]):
        raise HTTPException(409, {"code": "certificate", "message": CERT_WORDS, **info})
    return ssl.DER_cert_to_PEM_cert(der)


@asynccontextmanager
async def _errors(acc):
    """Отказы связи — словами Студии: `relogin`, `unlisted`, `certificate`, `owui_offline`."""
    try:
        yield
    except owui_tokens.Relogin:
        raise HTTPException(409, RELOGIN) from None
    except upstream.UpstreamRefused as e:
        raise HTTPException(422, {"code": "unlisted", "message": upstream.refused_words(e, acc)}) from None
    except (httpx.HTTPError, owui_tokens.Unavailable, OSError, TimeoutError) as e:
        if _untrusted(e):
            await _confirm(acc, None)          # без отпечатка — всегда 409 с отпечатком нынешнего сертификата
        log.info("owui %s: Open WebUI недоступен (%s)", acc.id, type(e).__name__)
        raise HTTPException(503, OFFLINE) from None


def _refused(acc, response: httpx.Response) -> None:
    if response.status_code == 429:
        raise HTTPException(429, BUSY)
    if response.status_code >= 500:
        log.info("owui %s: Open WebUI ответил кодом %d", acc.id, response.status_code)
        raise HTTPException(503, OFFLINE)


# ---- запросы к Open WebUI ------------------------------------------------------------------------------------------

async def call(acc, method: str, path: str, *, timeout: float = TIMEOUT_SEC, **kw) -> httpx.Response:
    """Запрос к Open WebUI учётки рабочим токеном. 401 — обновить токен (если его ещё не обновил параллельный
    вызов) и повторить один раз. Отказы: 409 `relogin`, 422 `unlisted`, 409 `certificate`, 503 `owui_offline`."""
    url = acc.base_url + path
    async with _errors(acc):
        token = await owui_tokens.access(acc)
        response = await upstream.request(acc, method, url, auth=f"Bearer {token}", timeout=timeout, **kw)
        if response.status_code == 401:
            token = await owui_tokens.renew(acc, force=True, stale=token)
            response = await upstream.request(acc, method, url, auth=f"Bearer {token}", timeout=timeout, **kw)
        return response


def _at_least(version: str) -> bool:
    found = re.match(r"(\d+)\.(\d+)", version)
    return found is not None and (int(found[1]), int(found[2])) >= MIN_VERSION


async def _config(acc) -> str:
    """Версия Open WebUI; ниже 0.13 или без постоянных сессий — 422 `unsupported`."""
    response = await upstream.request(acc, "GET", f"{acc.base_url}/api/config", keyless=True, timeout=TIMEOUT_SEC)
    _refused(acc, response)
    body = upstream.json_body(response)
    version, features = body.get("version"), body.get("features")
    if (response.status_code == 200 and isinstance(version, str) and _at_least(version)
            and isinstance(features, dict) and features.get("auth_sessions") is True):
        return version
    shown = version if isinstance(version, str) and len(version) <= 32 else "?"
    raise HTTPException(422, {"code": "unsupported", "message":
                              f"Open WebUI {shown} не поддерживается: нужна версия 0.13 или новее с постоянными сессиями"})


async def _signin(acc, email: str, password: str) -> dict:
    """Вход паролем: ответ с `token`, `id`, `name`, `email`. Open WebUI 0.13.4 отвечает 200 и без токена: учётка
    `pending` при включённой MFA — `{next_step: "pending"}`, нужен второй код — `{next_step: "verify" | "enroll" |
    "recover", challenge_token}`; без MFA учётка `pending` входит с `role: "pending"`."""
    response = await upstream.request(acc, "POST", f"{acc.base_url}/api/v1/auths/signin", keyless=True,
                                      timeout=TIMEOUT_SEC, json={"email": email, "password": password})
    if response.status_code in (400, 401, 403):
        raise HTTPException(422, LOGIN)
    _refused(acc, response)
    body = upstream.json_body(response)
    if response.status_code == 200 and "pending" in (body.get("next_step"), body.get("role")):
        raise HTTPException(422, PENDING)
    if response.status_code == 200 and body.get("next_step") in ("enroll", "verify", "recover"):
        raise HTTPException(422, MFA)
    if response.status_code != 200 or not all(isinstance(body.get(k), str) for k in ("token", "id")):
        raise shape_error(acc)
    return body


async def _session(acc, login_token: str) -> dict:
    """Постоянная сессия `SESSION_NAME` по JWT входа: `session_id`, `token`, `refresh_token`."""
    response = await upstream.request(acc, "POST", f"{acc.base_url}/api/v1/auths/sessions",
                                      auth=f"Bearer {login_token}", timeout=TIMEOUT_SEC, json={"name": SESSION_NAME})
    _refused(acc, response)
    body = upstream.json_body(response)
    if response.status_code != 200 or not all(isinstance(body.get(k), str)
                                              for k in ("session_id", "token", "refresh_token")):
        raise shape_error(acc)
    return body


async def _config_trusting(acc, sha256: str | None) -> Account:
    """`_config` с версией в снимке; сертификат https, не принятый системными корнями, — по отпечатку `sha256`."""
    try:
        version = await _config(acc)
    except httpx.HTTPError as e:
        if not _untrusted(e) or acc.cert_pem:
            raise
        acc = replace(acc, cert_pem=await _confirm(acc, sha256))
        version = await _config(acc)
    return replace(acc, version=version)


@contextmanager
def _attempt(uid: int):
    """Попытка входа на счету учётки Студии сразу (и параллельные считаются); неверный пароль её оставляет."""
    now = clock.NOW()
    fails = [t for t in FAILS.get(uid, ()) if now - t < LOGIN_WINDOW_SEC]
    FAILS[uid] = fails
    if len(fails) >= LOGIN_FAILS:
        log.warning("пользователь %d: вход в Open WebUI на паузе, неудач %d", uid, len(fails))
        raise HTTPException(429, LOCKED)
    fails.append(now)
    failed = False
    try:
        yield
    except HTTPException as e:
        failed = isinstance(e.detail, dict) and e.detail.get("code") == "login"
        if failed:
            log.info("пользователь %d: вход в Open WebUI отклонён", uid)
        raise
    finally:
        if not failed and now in FAILS.get(uid, ()):
            FAILS[uid].remove(now)


async def _revoke(acc) -> None:
    """Отозвать сессию учётки в Open WebUI — по возможности: недоступный сервер или `relogin` удалению не мешают."""
    try:
        session_id = owui_tokens.load(acc.id).session_id
        response = await asyncio.wait_for(
            call(acc, "DELETE", f"/api/v1/auths/sessions/{session_id}", timeout=REVOKE_SEC), REVOKE_SEC)
        log.info("owui %d: отзыв сессии — код %d", acc.id, response.status_code)
    except Exception as e:                     # любой отказ: строку всё равно удаляют
        code = e.detail.get("code") if isinstance(e, HTTPException) and isinstance(e.detail, dict) else None
        log.info("owui %d: сессия не отозвана (%s)", acc.id, code or type(e).__name__)


async def drop_user(uid: int, *, revoke: bool = True) -> None:
    """Учётку Студии удаляют (`revoke`) или отключают: сессии Open WebUI отозвать (каждую — не дольше `REVOKE_SEC`,
    параллельно), рабочие токены забыть. Строки при удалении уносит каскад `users`."""
    accounts = [_account(row) for row in state.DB.execute("SELECT * FROM owui_accounts WHERE user_id = ?", (uid,))]
    if revoke:
        await asyncio.gather(*(_revoke(acc) for acc in accounts))
    for acc in accounts:
        owui_tokens.forget(acc.id)


# ---- маршруты ------------------------------------------------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(request: Request):
    return [_out(row) for row in state.DB.execute(
        "SELECT * FROM owui_accounts WHERE user_id = ? ORDER BY created, id", (request.state.user.id,))]


@router.post("/accounts", status_code=201)
async def connect(body: ConnectIn, request: Request):
    uid = request.state.user.id
    label, base = _label(body.label), _base(body.url)
    _listed(base)
    target = Account(0, uid, label, base, None, "ok")
    with _attempt(uid):
        async with _errors(target):
            target = await _config_trusting(target, body.cert_sha256)
            found = await _signin(target, body.email, body.password)
            if _duplicate(uid, base, found["id"]):
                raise HTTPException(409, EXISTS)
            issue = await _session(target, found["token"])
    who = {"owui_user_id": found["id"], "name": found.get("name"), "email": found.get("email")}
    secret = owui_tokens.Secret(issue["session_id"], issue["refresh_token"], None)
    now = clock.NOW()
    aid = state.DB.execute(
        "INSERT INTO owui_accounts(user_id, label, base_url, who_enc, secret_enc, cert_pem, state, version, created, used)"
        " VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?)",
        (uid, label, base, security.encrypt(json.dumps(who)), security.encrypt(json.dumps(asdict(secret))),
         target.cert_pem, target.version, now, now)).lastrowid
    owui_tokens.TOKENS[aid] = (issue["token"], now + owui_tokens.TOKEN_SEC)
    log.info("owui %d: подключена учётке Студии %d", aid, uid)
    return _out(_row(uid, aid))


@router.patch("/accounts/{aid}")
async def rename(aid: int, body: LabelIn, request: Request):
    uid = request.state.user.id
    _row(uid, aid)
    state.DB.execute("UPDATE owui_accounts SET label = ? WHERE id = ? AND user_id = ?", (_label(body.label), aid, uid))
    return _out(_row(uid, aid))


@router.post("/accounts/{aid}/login")
async def relogin(aid: int, body: LoginIn, request: Request):
    uid = request.state.user.id
    row = _row(uid, aid)
    acc, who = _account(row), _who(row)
    with _attempt(uid):
        async with _errors(acc):
            acc = await _config_trusting(acc, None)
            found = await _signin(acc, who.get("email") or "", body.password)
            if found["id"] != who.get("owui_user_id"):
                raise HTTPException(409, OTHER_USER)
            issue = await _session(acc, found["token"])
    try:
        old = owui_tokens.load(aid).session_id
    except (KeyError, InvalidToken, TypeError, ValueError):
        old = None
    async with owui_tokens.LOCKS.setdefault(aid, asyncio.Lock()):      # идущее обновление прежней пары — до записи
        now = clock.NOW()
        owui_tokens.save(aid, owui_tokens.Secret(issue["session_id"], issue["refresh_token"], None))
        owui_tokens.TOKENS[aid] = (issue["token"], now + owui_tokens.TOKEN_SEC)
        state.DB.execute("UPDATE owui_accounts SET state = 'ok', version = ?, used = ? WHERE id = ?",
                         (acc.version, now, aid))
    log.info("owui %d: вход заново", aid)
    if old and old != issue["session_id"]:
        try:                                   # прежняя сессия, если жива, — отозвать JWT входа; неудача не мешает
            response = await upstream.request(acc, "DELETE", f"{acc.base_url}/api/v1/auths/sessions/{old}",
                                              auth=f"Bearer {found['token']}", timeout=REVOKE_SEC)
            log.info("owui %d: отзыв прежней сессии — код %d", aid, response.status_code)
        except (httpx.HTTPError, upstream.UpstreamRefused) as e:
            log.info("owui %d: прежняя сессия не отозвана (%s)", aid, type(e).__name__)
    return _out(_row(uid, aid))


@router.post("/accounts/{aid}/certificate")
async def accept_certificate(aid: int, body: CertificateIn, request: Request):
    uid = request.state.user.id
    acc = load(uid, aid)
    if not acc.base_url.startswith("https://"):
        raise HTTPException(422, NO_CERT)
    pem = await _confirm(acc, body.sha256)
    state.DB.execute("UPDATE owui_accounts SET cert_pem = ? WHERE id = ? AND user_id = ?", (pem, aid, uid))
    log.info("owui %d: принят новый сертификат", aid)
    return _out(_row(uid, aid))


@router.delete("/accounts/{aid}", status_code=204)
async def delete_account(aid: int, request: Request):
    uid = request.state.user.id
    acc = load(uid, aid)
    await _revoke(acc)
    owui_tokens.forget(aid)
    state.DB.execute("DELETE FROM owui_accounts WHERE id = ? AND user_id = ?", (aid, uid))
    log.info("owui %d: удалена", aid)
    return Response(status_code=204)
