"""Учётки Open WebUI в учётке Студии (спека 2026-11-22-studio-open-webui-design, «Учётка Open WebUI»).

- Подключение `POST /api/owui/accounts {label, url, email, password, cert_sha256?}`: адрес — по списку разрешённых
  (`upstream`); https с сертификатом, который не проверился системными корнями, — 409 `certificate` с отпечатком,
  повтор с `cert_sha256` запоминает PEM (`cert_pem` — якорь доверия ровно для него); `GET /api/config` — версия не
  ниже 0.13 и `features.auth_sessions`; вход — отказы `login`, `pending`, `mfa` (форма ответа — как у Open WebUI 0.13.4,
  `utils/auth.py::create_signin_response`); постоянная сессия `SESSION_NAME`. Пароль и JWT входа забываются сразу.
- Неудачи входа (неверный пароль) — на учётку Студии: `LOGIN_FAILS` за `LOGIN_WINDOW_SEC`, дальше 429 `owui_locked`
  без запроса к Open WebUI: Студия не служит подбору паролей.
- Переподключение — тот же адрес и та же учётка Open WebUI (другая — 409 `other_user`), прежняя сессия отзывается.
- Удаление — соединение сокета закрывается (`owui_link`), отзыв сессии (срок `REVOKE_SEC`, неудача не мешает),
  строка удаляется; удаление учётки Студии — то же для всех её строк (`drop_user`), отключение — закрыть соединения
  и забыть рабочие токены.
- `call` — запрос к Open WebUI рабочим токеном (`owui_tokens`), 401 — одно обновление и повтор.
- Чтение (`owui_view`): дерево, страницы чатов, чаты папки, поиск (запрос — телом), модели, чат, файл потоком (`no-store`).
  Неожиданная форма ответа — 502 `owui_shape`, недоступный Open WebUI — 503 `owui_offline`, остальная Студия работает.
- Отправка («Отправка» спеки): новый чат и продолжение — серверный ход `POST /api/chat/completions` с `session_id`
  сокета Студии (`owui_link.ensure`), `tool_ids` и возможностями по умолчанию модели, как у страницы Open WebUI 0.13;
  `params.tool_approval_mode` — только `APPROVAL_MODES` из настроек пользователя, `full` — никогда. Сообщение в чат,
  где идёт ответ, Open WebUI ставит в очередь — наблюдателям `queued`. Стоп, решение паузы (`resolve`), вложения
  (сырое тело до `FILE_MAX`, имя — `X-Studio-Name`, ответ — после обработки), SSE учётки (`owui_link.watch`, пинг —
  `PING_SEC`, конец — с сессией Студии).
- Чужой и несуществующий id — одинаковый 404. Журнал — только id учётки Студии и строки учётки, коды и числа: ни
  адреса, ни email, ни токенов, ни id чатов.
"""
import asyncio
import hashlib
import json
import logging
import re
import ssl
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Annotated, Literal
from urllib.parse import unquote

import httpx
from cryptography import x509
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

import auth
import chat as studio_chat
import claude_code
import clock
import documents
import lock_policy
import owui_link
import owui_tokens
import owui_view
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
#: чаты папки Open WebUI отдаёт страницами; больше стольких страниц Студия за раз не читает
FOLDER_PAGES_MAX = 10
#: предел текста поиска, знаков
SEARCH_MAX = 256
#: предел текста сообщения, знаков — как у «Чата»
TEXT_MAX = studio_chat.TEXT_MAX
#: предел вложения, байт
FILE_MAX = 20 * 1024 * 1024
#: вложений в одном сообщении
FILES_MAX = 10
#: срок загрузки вложения в Open WebUI, с
UPLOAD_SEC = 120
#: обработку вложения Студия опрашивает раз в `FILE_POLL_SEC` и ждёт не дольше `FILE_WAIT_SEC`, с
FILE_POLL_SEC = 1
FILE_WAIT_SEC = 300
#: пинг-комментарий SSE учётки без событий, с
PING_SEC = 20
#: SSE сверяет сессию Студии на событиях не чаще, с (и на каждом пинге)
CHECK_SEC = 5
#: режимы подтверждения инструментов (`params.tool_approval_mode`), которые Студия передаёт; `full` — никогда
APPROVAL_MODES = ("ask", "auto")
#: тип вложения в запрос загрузки — только такой, иначе `application/octet-stream`
MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}")

#: uid Студии → моменты неудачных входов (и идущих попыток) по `clock.NOW`
FAILS: dict[int, list[float]] = {}

NOT_FOUND = {"code": "not_found", "message": "такой учётки Open WebUI нет"}
OFFLINE = {"code": "owui_offline", "message": "Open WebUI не отвечает"}
RELOGIN = {"code": "relogin", "message": "войдите в учётку Open WebUI заново"}
LOGIN = {"code": "owui_login", "message": "неверный email или пароль Open WebUI"}
PENDING = {"code": "pending", "message": "учётка Open WebUI ждёт одобрения администратора Open WebUI"}
MFA = {"code": "mfa", "message": "вход со вторым кодом не поддерживается"}
LOCKED = {"code": "owui_locked", "message": "слишком много неудачных входов в Open WebUI — попробуйте позже"}
BUSY = {"code": "owui_locked", "message": "Open WebUI временно не принимает вход — попробуйте позже"}
EXISTS = {"code": "exists", "message": "эта учётка Open WebUI уже подключена"}
OTHER_USER = {"code": "other_user", "message": "это другая учётка Open WebUI — подключите её отдельно"}
BAD_URL = {"code": "url", "message": "адрес Open WebUI — со схемой http или https и хостом"}
NO_LABEL = {"code": "label", "message": "подпись не может быть пустой"}
NO_CHAT = {"code": "not_found", "message": "такого чата нет"}
NO_FOLDER = {"code": "not_found", "message": "такой папки нет"}
NO_FILE = {"code": "not_found", "message": "такого файла нет"}
NO_CERT = {"code": "url", "message": "у адреса http нет сертификата"}
HTTPS_ONLY = {"code": "unlisted", "message": "по имени сервер ходит только по https — укажите https-адрес"}
EMPTY = {"code": "empty", "message": "пустое сообщение"}
EMPTY_FILE = {"code": "empty", "message": "файл пустой"}
TOO_LARGE = {"code": "too_large", "message": "вложение больше 20 МиБ"}
NO_MODEL = {"code": "model", "message": "такой модели в Open WebUI нет"}
REFUSED = {"code": "owui_refused", "message": "Open WebUI отклонил запрос"}
RESOLVED = {"code": "resolved", "message": "это решение уже принято"}
ANSWERS = {"code": "answers", "message": "ответы нужны только к вопросам модели"}
FILE_SLOW = {"code": "file_slow", "message": "Open WebUI слишком долго обрабатывает файл — попробуйте позже"}
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


class SearchIn(BaseModel):
    text: str = Field(max_length=SEARCH_MAX)


class SendIn(BaseModel):
    model: str = Field(min_length=1, max_length=256)
    text: str = Field(max_length=TEXT_MAX)
    files: list[str] = Field(default_factory=list, max_length=FILES_MAX)


class ContinueIn(SendIn):
    parent_id: str = Field(min_length=1, max_length=128)


class OptionAnswer(BaseModel):
    """Выбранный вариант вопроса `ask_user` — как его шлёт `AskUserCard.svelte` Open WebUI 0.13."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["option"]
    option_index: int = Field(ge=0, le=16)
    label: str = Field(max_length=200)
    description: str = Field(max_length=2000)


class OtherAnswer(BaseModel):
    """Свой текст ответа на вопрос `ask_user` (`allow_other`)."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["other"]
    text: str = Field(min_length=1, max_length=4000)


class ResolveIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=256)
    action: Literal["approve", "reject", "answer"]
    answers: dict[str, Annotated[OptionAnswer | OtherAnswer, Field(discriminator="type")]] | None = \
        Field(default=None, max_length=16)


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
        failed = isinstance(e.detail, dict) and e.detail.get("code") == "owui_login"
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
    for acc in accounts:
        await owui_link.drop_account(acc.id)
    if revoke:
        await asyncio.gather(*(_revoke(acc) for acc in accounts))
    for acc in accounts:
        owui_tokens.forget(acc.id)


# ---- чтение --------------------------------------------------------------------------------------------------------

#: id чата, папки и файла Open WebUI — в путь запроса только такие: без `/`, `?`, `%` и сегментов `.`/`..`
OWUI_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _owui_id(value: str, missing: dict) -> str:
    if not OWUI_ID.fullmatch(value) or not value.strip("."):
        raise HTTPException(404, missing)
    return value


def _ok(acc, response: httpx.Response, kind: type):
    """Тело ответа Open WebUI нужного вида (`list` или `dict`); 429 и 5xx — отказы Студии, всё прочее — `owui_shape`."""
    _refused(acc, response)
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not isinstance(body, kind):
        log.warning("owui %d: ответ Open WebUI не той формы (код %d)", acc.id, response.status_code)
        raise shape_error(acc)
    return body


def _view(acc, make, raw):
    """Вид Студии из ответа Open WebUI; `owui_view.Shape` — 502 `owui_shape`."""
    try:
        return make(raw)
    except owui_view.Shape as e:
        log.warning("owui %d: форма ответа не сошлась (%s)", acc.id, e)
        raise shape_error(acc) from None


def _chats(acc, response: httpx.Response) -> list[dict]:
    return [_view(acc, owui_view.chat_item, raw) for raw in _ok(acc, response, list)]


@router.get("/accounts/{aid}/tree")
async def tree(aid: int, request: Request):
    acc = load(request.state.user.id, aid)
    pinned, folders, chats = await asyncio.gather(
        call(acc, "GET", "/api/v1/chats/pinned"), call(acc, "GET", "/api/v1/folders/"),
        call(acc, "GET", "/api/v1/chats/", params={"page": 1}))
    return {"pinned": _chats(acc, pinned),
            # папки выключены на сервере или не разрешены учётке — дерево без них
            "folders": [] if folders.status_code == 403 else
            [_view(acc, owui_view.folder_item, raw) for raw in _ok(acc, folders, list)],
            "chats": _chats(acc, chats)}


@router.get("/accounts/{aid}/chats")
async def chat_page(aid: int, request: Request, page: int = Query(1, ge=1, le=10000)):
    acc = load(request.state.user.id, aid)
    return {"chats": _chats(acc, await call(acc, "GET", "/api/v1/chats/", params={"page": page}))}


@router.get("/accounts/{aid}/folders/{fid}/chats")
async def folder_chats(aid: int, fid: str, request: Request):
    acc = load(request.state.user.id, aid)
    fid = _owui_id(fid, NO_FOLDER)
    found: dict[str, dict] = {}
    for page in range(1, FOLDER_PAGES_MAX + 1):
        response = await call(acc, "GET", f"/api/v1/chats/folder/{fid}/list", params={"page": page})
        fresh = [item for item in _chats(acc, response) if item["id"] not in found]
        if not fresh:
            break
        found.update((item["id"], item) for item in fresh)
    return {"chats": list(found.values())}


@router.post("/accounts/{aid}/search")
async def search(aid: int, body: SearchIn, request: Request):
    acc = load(request.state.user.id, aid)
    text = body.text.strip()
    if not text:
        return {"chats": []}
    return {"chats": _chats(acc, await call(acc, "GET", "/api/v1/chats/search", params={"text": text}))}


async def _last_model(acc, known: set[str]) -> str | None:
    """Модель последнего чата учётки, если она ещё в списке; не вышло по любой причине — `None`."""
    try:
        latest = _chats(acc, await call(acc, "GET", "/api/v1/chats/", params={"page": 1}))
        if not latest:
            return None
        response = await call(acc, "GET", f"/api/v1/chats/{_owui_id(latest[0]['id'], NO_CHAT)}")
        chat_models = _view(acc, owui_view.branch, _ok(acc, response, dict))["models"]
    except HTTPException:
        return None
    return chat_models[0] if chat_models and chat_models[0] in known else None


@router.get("/accounts/{aid}/models")
async def models(aid: int, request: Request):
    acc = load(request.state.user.id, aid)
    body = _ok(acc, await call(acc, "GET", "/api/models"), dict)
    if not isinstance(body.get("data"), list):
        log.warning("owui %d: список моделей не той формы", acc.id)
        raise shape_error(acc)
    found = [item for item in (_view(acc, owui_view.model_item, raw) for raw in body["data"]) if item]
    default = await _last_model(acc, {item["id"] for item in found}) if found else None
    return {"models": found, "default": default or (found[0]["id"] if found else None)}


@router.get("/accounts/{aid}/chats/{chat}")
async def chat_view(aid: int, chat: str, request: Request):
    acc = load(request.state.user.id, aid)
    chat = _owui_id(chat, NO_CHAT)
    response = await call(acc, "GET", f"/api/v1/chats/{chat}")
    if response.status_code in (401, 403, 404):        # Open WebUI на чужой и несуществующий чат отвечает 401
        raise HTTPException(404, NO_CHAT)
    view = _view(acc, owui_view.branch, _ok(acc, response, dict))
    tasks = await call(acc, "GET", f"/api/tasks/chat/{chat}")
    body = upstream.json_body(tasks) if tasks.status_code == 200 else {}
    ids = body.get("task_ids")
    return {**view, "active": bool(ids) if isinstance(ids, list) else False}


class _Relay(StreamingResponse):
    """Поток файла, закрывающий соединение с Open WebUI при любом конце ответа."""

    def __init__(self, stack: AsyncExitStack, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stack = stack

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.stack.aclose()


async def _open(acc, path: str, stack: AsyncExitStack) -> httpx.Response:
    """Потоковый `GET` рабочим токеном, как `call` (401 — одно обновление и повтор); соединение — в `stack`."""
    url = acc.base_url + path
    async with _errors(acc):
        token = await owui_tokens.access(acc)
        for attempt in (1, 2):
            opened = AsyncExitStack()
            response = await opened.enter_async_context(
                upstream.stream(acc, "GET", url, auth=f"Bearer {token}", timeout=TIMEOUT_SEC))
            if response.status_code != 401 or attempt == 2:
                stack.push_async_callback(opened.aclose)
                return response
            await opened.aclose()
            token = await owui_tokens.renew(acc, force=True, stale=token)


@router.get("/accounts/{aid}/files/{fid}")
async def file_content(aid: int, fid: str, request: Request):
    acc = load(request.state.user.id, aid)
    fid = _owui_id(fid, NO_FILE)
    async with AsyncExitStack() as stack:
        response = await _open(acc, f"/api/v1/files/{fid}/content", stack)
        if response.status_code in (401, 403, 404):
            raise HTTPException(404, NO_FILE)
        _refused(acc, response)
        if response.status_code != 200:
            log.warning("owui %d: файл — код %d", acc.id, response.status_code)
            raise shape_error(acc)
        mime = response.headers.get("content-type", "application/octet-stream")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "sandbox; default-src 'none'"}
        if not mime.lower().startswith(("image/", "audio/", "video/")):       # страницу Студии файл не исполняет
            headers["Content-Disposition"] = "attachment"
        return _Relay(stack.pop_all(), response.aiter_bytes(), media_type=mime, headers=headers)


# ---- отправка ------------------------------------------------------------------------------------------------------

def _raster(mime) -> bool:
    """Картинка, которую модель видит как картинку (как `isRasterImageContentType` страницы Open WebUI)."""
    return isinstance(mime, str) and mime.startswith("image/") and "svg" not in mime


def _cut_status(acc, response: httpx.Response, missing: dict) -> None:
    """Общие отказы отправки: чата (файла) нет — 404; Open WebUI отверг запрос — 422 `owui_refused`."""
    if response.status_code in (401, 403, 404):
        raise HTTPException(404, missing)
    _refused(acc, response)
    if response.status_code in (400, 413, 422):
        log.info("owui %d: Open WebUI отклонил запрос (код %d)", acc.id, response.status_code)
        raise HTTPException(422, REFUSED)


async def _attachment(acc, file_id: str) -> dict:
    """Вложение сообщения в форме страницы Open WebUI 0.13 (`MessageInput.svelte`): `type: file`, `id`, `url` — id,
    имя, тип, размер, `collection_name`. Файла нет или он чужой — 404."""
    response = await call(acc, "GET", f"/api/v1/files/{_owui_id(file_id, NO_FILE)}")
    _cut_status(acc, response, NO_FILE)
    body = _ok(acc, response, dict)
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    name = meta.get("name") or body.get("filename") or file_id
    return {"type": "file", "id": file_id, "url": file_id, "name": name if isinstance(name, str) else file_id,
            "collection_name": meta.get("collection_name") or body.get("collection_name") or "",
            "status": "uploaded", "size": meta.get("size"), "error": "", "content_type": meta.get("content_type")}


def _features(config: dict, meta: dict, ui: dict) -> dict:
    """Возможности хода — как их ставит страница Open WebUI 0.13 по умолчанию модели (`Chat.svelte`: `setDefaults`,
    `getFeatures`): возможность включена на сервере, модель её умеет и она в `defaultFeatureIds` модели. Права
    пользователя Open WebUI перепроверяет сам (`utils/middleware.py`). Чего Студия не включает: интерпретатор кода,
    исполняемый в браузере собеседника (движок `pyodide` или пустой), и веб-поиск с подтверждением на странице."""
    enabled = config.get("features") if isinstance(config.get("features"), dict) else {}
    code = config.get("code") if isinstance(config.get("code"), dict) else {}
    caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    defaults = meta.get("defaultFeatureIds") if isinstance(meta.get("defaultFeatureIds"), list) else []

    def on(name: str) -> bool:
        return bool(enabled.get(f"enable_{name}")) and caps.get(name, True) is not False and name in defaults

    features: dict = {"voice": False}
    for name, model_key in (("image_generation", "imageModelId"), ("video_generation", "videoModelId")):
        chosen = meta.get(model_key)
        features[name] = ({"model": chosen} if isinstance(chosen, str) and chosen else True) if on(name) else False
    if code.get("interpreter_engine") not in (None, "", "pyodide"):
        features["code_interpreter"] = on("code_interpreter")
    always = ui.get("webSearch") == "always" and enabled.get("enable_web_search") and caps.get("web_search", True) is not False
    features["web_search"] = bool((on("web_search") or always) and not enabled.get("enable_web_search_confirmation"))
    if ui.get("memory") if ui.get("memory") is not None else enabled.get("enable_memories"):
        features["memory"] = True
    return features


def _tool_ids(config: dict, meta: dict, ui: dict) -> list[str]:
    """Инструменты модели (`info.meta.toolIds`, иначе выбранные пользователем `settings.tools`), как у страницы:
    `server:*` — только при включённых серверах инструментов, прочие — при включённых инструментах."""
    enabled = config.get("features") if isinstance(config.get("features"), dict) else {}
    ids = meta.get("toolIds") if isinstance(meta.get("toolIds"), list) else ui.get("tools")
    ids = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
    return [i for i in ids if enabled.get("enable_tool_servers" if i.startswith("server:") else "enable_tools")]


async def _send(acc, body: "SendIn", chat: str | None, parent: str | None) -> tuple[str | None, str, bool]:
    """Серверный ход Open WebUI 0.13 (`main.py::chat_completion`): сообщение и ответ с id Студии, `session_id` сокета
    Студии (без него Open WebUI не включает встроенные инструменты), историю Open WebUI берёт из своей базы.
    → (id чата, id ответа, в очереди ли)."""
    model = body.model.strip()
    text = body.text
    if not text.strip() and not body.files:
        raise HTTPException(422, EMPTY)
    sid = await owui_link.ensure(acc)
    config_r, settings_r, models_r, *items = await asyncio.gather(
        call(acc, "GET", "/api/config"), call(acc, "GET", "/api/v1/users/user/settings"),
        call(acc, "GET", "/api/models"), *(_attachment(acc, file_id) for file_id in body.files))
    config = _ok(acc, config_r, dict)
    _refused(acc, settings_r)
    settings = upstream.json_body(settings_r) if settings_r.status_code == 200 else {}
    ui = settings.get("ui") if isinstance(settings.get("ui"), dict) else {}
    listed = _ok(acc, models_r, dict).get("data")
    raw = next((m for m in listed if isinstance(m, dict) and m.get("id") == model), None) \
        if isinstance(listed, list) else None
    if raw is None or model.startswith("agent:"):
        raise HTTPException(422, NO_MODEL)
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    message_id, answer_id = str(uuid.uuid4()), str(uuid.uuid4())
    user_message = {"id": message_id, "parentId": parent, "childrenIds": [], "role": "user", "content": text,
                    "timestamp": int(clock.NOW()), "models": [model]}
    if items:
        user_message["files"] = items
    messages = [{"role": "user", "content": text}]
    if isinstance(ui.get("system"), str) and ui["system"].strip():     # системный текст пользователя — как у страницы
        messages.insert(0, {"role": "system", "content": ui["system"]})
    payload = {"stream": True, "model": model, "messages": messages, "tool_ids": _tool_ids(config, meta, ui),
               "features": _features(config, meta, ui), "session_id": sid, "id": answer_id, "parent_id": parent,
               "user_message": user_message}
    documents_only = [item for item in items if not _raster(item["content_type"])]
    if documents_only:
        payload["files"] = documents_only
    if chat is None:
        payload["background_tasks"] = {"title_generation": True}
    else:
        payload["chat_id"] = chat
    params = ui.get("params") if isinstance(ui.get("params"), dict) else {}
    if params.get("tool_approval_mode") in APPROVAL_MODES:            # `full` Студия не шлёт никогда
        payload["params"] = {"tool_approval_mode": params["tool_approval_mode"]}
    owui_link.track(acc, chat or "", answer_id)                        # события ответа идут сразу за ответом POST
    try:
        response = await call(acc, "POST", "/api/chat/completions", json=payload)
        _cut_status(acc, response, NO_CHAT)
        result = _ok(acc, response, dict)
        if result.get("queued") is True:                               # в чате идёт ответ: Open WebUI взял в очередь
            owui_link.untrack(acc.id, answer_id)
            link = owui_link.LINKS.get(acc.id)
            if link is not None:
                link.publish({"type": "queued", "chat_id": chat})
            log.info("owui %d: сообщение в очереди", acc.id)
            return chat, answer_id, True
        found = result.get("chat_id")
        if chat is None and not (isinstance(found, str) and OWUI_ID.fullmatch(found)):
            log.warning("owui %d: ответ отправки без чата", acc.id)
            raise shape_error(acc)
    except BaseException:
        owui_link.untrack(acc.id, answer_id)
        raise
    chat = chat or found
    owui_link.track(acc, chat, answer_id)
    log.info("owui %d: сообщение отправлено, вложений %d", acc.id, len(items))
    return chat, answer_id, False


@router.post("/accounts/{aid}/chats", status_code=201)
async def new_chat(aid: int, body: SendIn, request: Request):
    acc = load(request.state.user.id, aid)
    chat, message_id, _ = await _send(acc, body, None, None)
    return {"chat_id": chat, "message_id": message_id}


@router.post("/accounts/{aid}/chats/{chat}/messages")
async def send_message(aid: int, chat: str, body: ContinueIn, request: Request):
    acc = load(request.state.user.id, aid)
    chat = _owui_id(chat, NO_CHAT)
    _, message_id, queued = await _send(acc, body, chat, _owui_id(body.parent_id, NO_CHAT))
    return {"message_id": message_id, "queued": queued}


@router.post("/accounts/{aid}/chats/{chat}/stop", status_code=204)
async def stop(aid: int, chat: str, request: Request):
    acc = load(request.state.user.id, aid)
    response = await call(acc, "POST", f"/api/tasks/chat/{_owui_id(chat, NO_CHAT)}/stop")
    _cut_status(acc, response, NO_CHAT)
    if response.status_code != 200:
        log.warning("owui %d: остановка — код %d", acc.id, response.status_code)
        raise shape_error(acc)
    log.info("owui %d: ответ остановлен", acc.id)
    return Response(status_code=204)


@router.post("/accounts/{aid}/chats/{chat}/resolve", status_code=204)
async def resolve(aid: int, chat: str, body: ResolveIn, request: Request):
    """Решение паузы — `resolve` Open WebUI 0.13 (`utils/tool_approval.py`): `approve` и `reject` — подтверждение
    инструмента, `answer` — ответы на вопросы `ask_user` (`{id вопроса: option | other}`, как `AskUserCard.svelte`).
    Open WebUI сам продолжает ответ — Студия снова держит соединение до его итога. Уже решено — 409 `resolved`."""
    acc = load(request.state.user.id, aid)
    chat, message_id = _owui_id(chat, NO_CHAT), _owui_id(body.message_id, NO_CHAT)
    if (body.action == "answer") != (body.answers is not None):
        raise HTTPException(422, ANSWERS)
    payload: dict = {"call_id": body.call_id, "action": body.action}
    if body.answers is not None:
        payload["answers"] = {key: answer.model_dump() for key, answer in body.answers.items()}
    await owui_link.ensure(acc)                                        # события продолжения — через сокет Студии
    owui_link.track(acc, chat, message_id)
    try:
        response = await call(acc, "POST", f"/api/v1/chats/{chat}/messages/{message_id}/resolve", json=payload)
        if response.status_code == 409:
            raise HTTPException(409, RESOLVED)
        _cut_status(acc, response, NO_CHAT)
        result = _ok(acc, response, dict)
        if not result.get("task_ids"):                                 # ждут решения другие вызовы — ответ не идёт
            owui_link.untrack(acc.id, message_id)
    except BaseException:
        owui_link.untrack(acc.id, message_id)
        raise
    log.info("owui %d: пауза решена (%s)", acc.id, body.action)
    return Response(status_code=204)


async def _upload_body(request: Request) -> bytes:
    too_large = HTTPException(413, TOO_LARGE)
    length = request.headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > FILE_MAX:
        raise too_large
    raw = bytearray()
    async for piece in request.stream():
        raw.extend(piece)
        if len(raw) > FILE_MAX:
            raise too_large
    if not raw:
        raise HTTPException(422, EMPTY_FILE)
    auth.recheck(request)
    return bytes(raw)


@router.post("/accounts/{aid}/files")
async def upload(aid: int, request: Request):
    """Вложение: тело — сам файл до `FILE_MAX`, имя — заголовком `X-Studio-Name` (как `documents`), тип —
    `Content-Type`. Ответ — после обработки Open WebUI (`process/status`: `completed` или `failed`); `failed`
    страница Open WebUI тоже прикладывает, с предупреждением — Студия отдаёт его признаком `failed`."""
    acc = load(request.state.user.id, aid)
    data = await _upload_body(request)
    name = documents.clean_name(unquote(request.headers.get("x-studio-name", "")))
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not MIME.fullmatch(mime):
        mime = "application/octet-stream"
    response = await call(acc, "POST", "/api/v1/files/", files={"file": (name, data, mime)}, timeout=UPLOAD_SEC)
    if response.status_code == 413:
        raise HTTPException(413, TOO_LARGE)
    _cut_status(acc, response, NO_FILE)
    file_id = _ok(acc, response, dict).get("id")
    if not isinstance(file_id, str) or not OWUI_ID.fullmatch(file_id):
        log.warning("owui %d: ответ загрузки без id", acc.id)
        raise shape_error(acc)
    deadline = clock.NOW() + FILE_WAIT_SEC
    while True:
        status = await call(acc, "GET", f"/api/v1/files/{file_id}/process/status")
        _cut_status(acc, status, NO_FILE)
        stage = _ok(acc, status, dict).get("status")
        if stage in ("completed", "failed"):
            break
        if clock.NOW() >= deadline:
            log.info("owui %d: файл не обработан за %d с", acc.id, FILE_WAIT_SEC)
            raise HTTPException(504, FILE_SLOW)
        await clock.SLEEP(FILE_POLL_SEC)
    log.info("owui %d: файл загружен, байт %d%s", acc.id, len(data), ", обработка не удалась" if stage == "failed" else "")
    found = {"id": file_id, "name": name, "kind": "image" if _raster(mime) else "file"}
    return {**found, "failed": True} if stage == "failed" else found


# ---- поток на страницу ---------------------------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True, allow_nan=False)}\n\n"


def _watching(digest: str, uid: int, aid: int) -> bool:
    """Сессия Студии жива, не заперта для раздела, учётка не отключена и учётка Open WebUI ещё есть."""
    row = state.DB.execute("SELECT u.disabled, s.expires FROM sessions s JOIN users u ON u.id = s.user_id"
                           " WHERE s.digest = ? AND s.user_id = ?", (digest, uid)).fetchone()
    return (row is not None and not row["disabled"] and row["expires"] > clock.NOW()
            and not lock_policy.locked(digest, "owui")
            and state.DB.execute("SELECT 1 FROM owui_accounts WHERE id = ? AND user_id = ?", (aid, uid)).fetchone()
            is not None)


async def _events(q: asyncio.Queue, digest: str, uid: int, aid: int):
    """События учётки строками SSE; без событий `PING_SEC` — `: ping`. Сессию поток сверяет на каждом пинге и на
    событиях не чаще `CHECK_SEC`; сессия кончилась, заперта или учётку удалили — `closed` и конец. После
    `link relogin` поток кончается: соединения больше нет, после входа заново страница откроет новый."""
    checked = clock.NOW()
    while True:
        try:
            event = await asyncio.wait_for(q.get(), PING_SEC)
        except TimeoutError:
            event = None
        now = clock.NOW()
        if event is None or now - checked >= CHECK_SEC:
            checked = now
            if not _watching(digest, uid, aid):
                yield _sse("closed", {})
                return
        if event is None:
            yield ": ping\n\n"
            continue
        try:
            yield _sse(event["type"], {key: value for key, value in event.items() if key != "type"})
        except (ValueError, RecursionError):                            # NaN в событии — не JSON
            continue
        if event["type"] == "link" and event.get("state") == "relogin":
            return


class _Watch(StreamingResponse):
    """Поток SSE учётки для одного наблюдателя. Наблюдателя снимает сам ответ, а не finally генератора: отмена может
    застать ответ до первого куска — как `claude_code._Watch`."""

    def __init__(self, aid: int, q: asyncio.Queue, digest: str, uid: int):
        super().__init__(_events(q, digest, uid, aid), media_type="text/event-stream",
                         headers=claude_code.SSE_HEADERS)
        self.aid, self.q = aid, q

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            owui_link.unwatch(self.aid, self.q)
            await self.body_iterator.aclose()


@router.get("/accounts/{aid}/events")
async def events(aid: int, request: Request):
    uid = request.state.user.id
    acc = load(uid, aid)
    return _Watch(aid, await owui_link.watch(acc), request.state.digest, uid)


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
    await owui_link.drop_account(aid)
    await _revoke(acc)
    owui_tokens.forget(aid)
    state.DB.execute("DELETE FROM owui_accounts WHERE id = ? AND user_id = ?", (aid, uid))
    log.info("owui %d: удалена", aid)
    return Response(status_code=204)
