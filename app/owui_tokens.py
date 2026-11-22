"""Постоянная сессия Open WebUI: долгий токен — в базе шифром, рабочий — в памяти (спека, «Токены»).

- В `owui_accounts.secret_enc` лежит `Secret`: `session_id`, `refresh_token`, `pending_request_id`. Рабочий токен
  (900 с) живёт только в `TOKENS` этого процесса и после перезапуска получается обновлением.
- Обновление — когда до конца рабочего токена меньше `REFRESH_AHEAD_SEC`, под блокировкой на учётку (одно за раз).
  Перед запросом в `secret_enc` пишется `pending_request_id`; после ответа — новая пара и пустой `pending_request_id`.
  Обрыв между ответом Open WebUI и записью: следующее обновление шлёт тот же `request_id` с прежним токеном, и
  Open WebUI возвращает ту же выдачу.
- 401 на обновление — учётка в `relogin` (нужен пароль заново), рабочий токен забыт.
- Журнал — только id учётки и коды: адрес, токены и тексты ответов туда не пишутся.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass

import httpx

import clock
import security
import state
import upstream

log = logging.getLogger("studio")

#: за сколько секунд до конца рабочего токена его обновляют
REFRESH_AHEAD_SEC = 120
#: срок рабочего токена Open WebUI, с (обновление отвечает новой парой на столько же)
TOKEN_SEC = 900
#: срок запроса обновления, с: молчащий Open WebUI не держит раздел
REQUEST_SEC = 10

#: id учётки Open WebUI → (рабочий токен, конец его срока по `clock.NOW`)
TOKENS: dict[int, tuple[str, float]] = {}
#: id учётки → блокировка обновления
LOCKS: dict[int, asyncio.Lock] = {}


class Relogin(Exception):
    """Open WebUI отверг долгий токен: нужен пароль заново."""


class Unavailable(Exception):
    """Open WebUI не ответил или ответил не так, как ждали; `code` — код HTTP или None (нет соединения)."""

    def __init__(self, code: int | None = None):
        super().__init__(code)
        self.code = code


@dataclass
class Secret:
    session_id: str
    refresh_token: str
    pending_request_id: str | None = None


def save(aid: int, secret: Secret) -> None:
    state.DB.execute("UPDATE owui_accounts SET secret_enc = ? WHERE id = ?",
                     (security.encrypt(json.dumps(asdict(secret))), aid))


def load(aid: int) -> Secret:
    row = state.DB.execute("SELECT secret_enc FROM owui_accounts WHERE id = ?", (aid,)).fetchone()
    if row is None:
        raise KeyError(aid)
    return Secret(**json.loads(security.decrypt(row["secret_enc"])))


def forget(aid: int) -> None:
    TOKENS.pop(aid, None)


def _fresh(aid: int) -> str | None:
    current = TOKENS.get(aid)
    return current[0] if current and current[1] - clock.NOW() >= REFRESH_AHEAD_SEC else None


async def access(acc) -> str:
    """Рабочий токен учётки; близкий к концу — сначала обновляется."""
    if acc.state == "relogin":
        raise Relogin
    return _fresh(acc.id) or await renew(acc)


def _relogin(aid: int) -> Relogin:
    state.DB.execute("UPDATE owui_accounts SET state = 'relogin' WHERE id = ?", (aid,))
    forget(aid)
    log.warning("owui %s: сессия отвергнута, нужен вход заново", aid)
    return Relogin()


async def renew(acc, *, force: bool = False, stale: str | None = None) -> str:
    """Обновить пару токенов под блокировкой учётки. Без `force` токен, обновлённый, пока ждали блокировку, берётся
    как есть: параллельные вызовы дают одно обновление. `stale` — рабочий токен, который Open WebUI только что
    отверг (401): обновлять, только если в памяти всё ещё он, — иначе его уже сменил параллельный вызов, и
    берётся новый (ротация и предел сессий Open WebUI не тратятся на каждый отказ)."""
    async with LOCKS.setdefault(acc.id, asyncio.Lock()):
        if acc.state == "relogin":
            raise Relogin
        if not force and (token := _fresh(acc.id)):
            return token
        if stale is not None and (current := TOKENS.get(acc.id)) and current[0] != stale:
            return current[0]
        secret = load(acc.id)
        if secret.pending_request_id is None:
            secret.pending_request_id = uuid.uuid4().hex
            save(acc.id, secret)                       # до запроса: обрыв после ответа восстановим тем же request_id
        try:
            response = await upstream.request(
                acc, "POST", f"{acc.base_url}/api/v1/auths/sessions/refresh", keyless=True, timeout=REQUEST_SEC,
                json={"refresh_token": secret.refresh_token, "request_id": secret.pending_request_id})
        except httpx.HTTPError:
            raise Unavailable(None) from None
        if response.status_code == 401:
            raise _relogin(acc.id)
        body = upstream.json_body(response)
        token, refresh = body.get("token"), body.get("refresh_token")
        if response.status_code != 200 or not isinstance(token, str) or not isinstance(refresh, str):
            log.warning("owui %s: обновление отвечено кодом %s", acc.id, response.status_code)
            raise Unavailable(response.status_code)
        now = clock.NOW()
        save(acc.id, Secret(secret.session_id, refresh, None))
        state.DB.execute("UPDATE owui_accounts SET used = ? WHERE id = ?", (now, acc.id))
        TOKENS[acc.id] = (token, now + TOKEN_SEC)
        return token
