"""Шлюз Cleaner для внешних моделей (шаг схемы 64): настройки администратора и самопроверка.

- Настройки — один JSON шифром `STUDIO_DATA_KEY` в `settings[SETTINGS_KEY]`: адрес Cleaner, служебный ключ и флажок
  «Cleaner для всех внешних моделей». Ключ в браузер не отдаётся; после смены `STUDIO_DATA_KEY` настройки не
  расшифровать — панель просит ввести их заново (`reenter`), принудительный режим выключен.
- Сервер Studio ходит к Cleaner только по списку разрешённых адресов (`upstream`, сертификат — `STUDIO_CLEANER_CA`),
  ключ — только заголовком `X-Cleaner-Key`.
- Запросы пользователя (`context`, `stream`, `request`) идут через синтетическое подключение к самому Cleaner
  (`_gateway`): ключ провайдера — `Authorization`, адрес провайдера — `X-Cleaner-Upstream`, контекст — `X-Cleaner-Chat`,
  веб-инструменты — `X-Cleaner-Hold-Tools`. Не настроенный или недоступный Cleaner — отказ словами, обхода нет.
- Контекст — чат Cleaner на запись (`context`): строка `cleaner_contexts` со снимком адреса и служебного ключа,
  отмеченная `cleaner_route_contexts`. Смена адреса или ключа отправляет такие контексты в очередь удаления
  `cleaner_deletions` со снимком прежнего адреса и ключа и признаком `route` (триггер `cleaner_context_delete`):
  очередь удаляет их ключом в `X-Cleaner-Key`.
- Внутреннее и внешнее подключение — `is_external`; подключение через Cleaner — `routes` (переключатель
  `via_cleaner` или принудительный режим): такое подключение идёт через сервер (`Connection.routed`), а
  `upstream._prepare` не пускает его никуда, кроме адреса Cleaner.
- Журнал — только коды и числа: адресов и ключей в нём нет.
"""
import asyncio
import ipaddress
import json
import logging
import re
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, Field

import clock
import connections
import security
import state
import upstream

log = logging.getLogger("studio")

SETTINGS_KEY = "cleaner_gateway"
#: заголовок служебного ключа Cleaner
KEY_HEADER = "X-Cleaner-Key"
#: заголовки запроса к шлюзу: адрес провайдера (база), контекст (чат Cleaner), инструменты без восстановления
UPSTREAM_HEADER = "X-Cleaner-Upstream"
CHAT_HEADER = "X-Cleaner-Chat"
HOLD_HEADER = "X-Cleaner-Hold-Tools"
#: заголовок ответа шлюза: id запроса Cleaner («Что получила модель»)
REQUEST_HEADER = "x-cleaner-request"
#: маршруты шлюза Chat Completions; контексты — `/api/v1/chats`
AGENT_PATH = "/agent/openai/v1"
CHATS_PATH = "/api/v1/chats"
CONTEXT_TIMEOUT = httpx.Timeout(30, connect=10)
#: самопроверка шлюза: ключ, слои, доступность
CHECK_PATH = AGENT_PATH + "/cleaner/check"
CHECK_TIMEOUT = httpx.Timeout(30, connect=10)
URL_MAX = 2048

#: Перед откатом на код схемы 63 (столбца `via_cleaner` он не знает и отправил бы ход такого подключения провайдеру
#: напрямую), служба остановлена: ключ подключения через Cleaner сохраняется в `cleaner_rollback` и портится — прежний
#: код отвечает «токен не расшифрован», прямой отправки нет. Повторный запуск сохранённое не затирает. Принудительный
#: режим прежний код не соблюдает: администратор решает, отключать ли внешние подключения.
ROLLBACK_64 = """BEGIN;
INSERT OR IGNORE INTO cleaner_rollback(connection_id, token_enc)
  SELECT id, token_enc FROM connections WHERE via_cleaner = 1 AND token_enc != X'00';
UPDATE connections SET token_enc = X'00' WHERE id IN (SELECT connection_id FROM cleaner_rollback);
COMMIT;
"""

#: После возврата вперёд (выполняет `forward` при старте службы): ключи из `cleaner_rollback`. Ключ, введённый заново
#: прежним кодом, не затирается.
FORWARD_64 = """BEGIN;
UPDATE connections SET token_enc = (SELECT b.token_enc FROM cleaner_rollback b WHERE b.connection_id = connections.id)
 WHERE token_enc = X'00' AND id IN (SELECT connection_id FROM cleaner_rollback);
DELETE FROM cleaner_rollback;
COMMIT;
"""


def forward() -> None:
    """При старте службы: работает новый код — значит, мы снова впереди отката; `FORWARD_64`, если есть что вернуть."""
    count = state.DB.execute("SELECT count(*) FROM cleaner_rollback").fetchone()[0]
    if count:
        state.DB.executescript(FORWARD_64)
        log.info("cleaner: после отката возвращено ключей %d", count)


class SettingsIn(BaseModel):
    base_url: str = Field(max_length=URL_MAX)
    key: str | None = Field(default=None, max_length=512)
    forced: bool = False


def _empty(**extra) -> dict:
    return {"base_url": "", "key": None, "forced": False, "reenter": False} | extra


def settings() -> dict:
    """Настройки с расшифрованным ключом. Не расшифровываются (прежний `STUDIO_DATA_KEY`) — пустые, с `reenter`."""
    row = state.DB.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone() \
        if state.DB is not None else None
    if not row:
        return _empty()
    try:
        saved = json.loads(security.decrypt(row[0]))
    except InvalidToken:
        log.warning("cleaner settings: not decryptable with the current data key")
        return _empty(reenter=True)
    return _empty() | {k: saved[k] for k in ("base_url", "key", "forced") if k in saved}


def public_settings() -> dict:
    config = settings()
    return {"base_url": config["base_url"], "key_set": bool(config["key"]), "forced": config["forced"],
            "reenter": config["reenter"]}


def configured() -> bool:
    config = settings()
    return bool(config["base_url"] and config["key"])


def forced() -> bool:
    """Принудительный режим: только при заданных адресе и ключе (после смены ключа сервера — выключен)."""
    config = settings()
    return bool(config["forced"] and config["base_url"] and config["key"])


def is_external(conn: "connections.Connection") -> bool:
    """Подключение, чьи данные могут уйти в облако. Внутреннее — только s06, srvllm, прежний Cleaner и `openai`,
    чей адрес записан в списке администратора и чей узел — IP-литерал не из публичных. Всё прочее — внешнее:
    любое имя, публичный IP, `localhost` и частный адрес не из списка. Частный адрес не доказывает, что за ним нет
    прокси в облако: внутренним его делает только запись в списке."""
    if conn.s06 or conn.cleaner or conn.kind == "srvllm":
        return False
    if not upstream.is_allowed(conn.base_url):
        return True
    try:
        return ipaddress.ip_address(httpx.URL(conn.base_url).host).is_global
    except ValueError:                    # имя, а не IP-литерал
        return True


def routes(conn: "connections.Connection") -> bool:
    """Ход подключения идёт через Cleaner: внешний `openai`, у которого включён переключатель или принудительный режим."""
    if conn.kind != "openai" or conn.cleaner or conn.s06 or not is_external(conn):
        return False
    return conn.via_cleaner or forced()


#: принудительный режим: внешнее подключение в разделе, где Cleaner не работает (звук, картинки)
FORCED_SECTION = "администратор требует Cleaner для внешних моделей; этот раздел через Cleaner не работает"
#: принудительный режим: облачная роль шлюза srvllm в «Чате» и «Переводе» (Cleaner не ходит на частные адреса)
FORCED_SRVLLM = "администратор требует Cleaner; облачные роли srvllm через Cleaner пока не работают"


def forced_refusal(conn: "connections.Connection", section: str, model: str) -> "str | None":
    """Слова отказа принудительного режима или None. Внешнее подключение вне чата и перевода (`sections.CLEANER`) —
    отказ: звук и картинки Cleaner не очищает. Шлюз srvllm в чате и переводе — отказ облачной роли (не `local-*`,
    `sections.is_local`): Cleaner ходит только на публичные адреса, а srvllm — частный. Внешнее подключение в чате и
    переводе идёт через Cleaner (`routes`), внутреннее — как обычно."""
    if not forced():
        return None
    import sections                           # цикл: sections → connections → cleaner_route
    if section not in sections.CLEANER:
        return FORCED_SECTION if is_external(conn) else None
    if conn.kind == "srvllm" and not sections.is_local(conn, model):
        return FORCED_SRVLLM
    return None


def refuse_forced(conn: "connections.Connection", section: str, model: str) -> None:
    """409 `cleaner_forced` словами `forced_refusal`, если принудительный режим этот запрос не пускает."""
    message = forced_refusal(conn, section, model)
    if message is not None:
        raise HTTPException(409, {"code": "cleaner_forced", "message": message})


def _bad(code: str, message: str) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message})


def _address(text: str) -> str:
    """Адрес Cleaner: http(s)://хост[:порт] без `/`, пути и логина; пустой — Cleaner не задан. Только из списка."""
    raw = text.strip().rstrip("/")
    if not raw:
        return ""
    try:
        url = httpx.URL(raw)
        url.host                     # имя IDNA httpx разбирает лениво: IDNAError — это UnicodeError
    except (httpx.InvalidURL, UnicodeError, ValueError):
        url = None
    if (url is None or url.scheme not in upstream.DEFAULT_PORTS or not url.host or url.userinfo or url.path not in ("", "/")
            or "?" in raw or "#" in raw):
        raise _bad("url", "адрес Cleaner — http(s)://хост[:порт], без пути, логина, ? и #")
    if not upstream.is_allowed(raw):
        raise _bad("server", "адрес Cleaner должен быть в списке разрешённых адресов")
    return raw


def _key(text: str) -> str:
    secret = text.strip()
    if not re.fullmatch(connections.TOKEN_RE, secret):
        raise _bad("key", "служебный ключ — 1–512 печатных знаков ASCII")
    return secret


def save_settings(body: SettingsIn) -> dict:
    previous = settings()
    base_url = _address(body.base_url)
    key = previous["key"] if body.key is None or not body.key.strip() else _key(body.key)
    if body.forced and not (base_url and key):
        raise _bad("cleaner", "принудительный режим требует адрес и служебный ключ Cleaner")
    config = {"base_url": base_url, "key": key, "forced": body.forced}
    state.DB.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (SETTINGS_KEY, security.encrypt(json.dumps(config))))
    if (base_url, key) != (previous["base_url"], previous["key"]):
        # контексты прежнего адреса и ключа: триггер кладёт их в `cleaner_deletions` со снимком прежнего ключа;
        # контексты прежнего вида Cleaner (личный ключ, без отметки `cleaner_route_contexts`) не трогаются
        state.DB.execute("DELETE FROM cleaner_contexts WHERE id IN (SELECT id FROM cleaner_route_contexts)")
    log.info("cleaner: настройки сохранены: адрес %s, ключ %s, принудительный режим %s",
             "изменён" if base_url != previous["base_url"] else "прежний",
             "изменён" if key != previous["key"] else "прежний", "включён" if config["forced"] else "выключен")
    return public_settings()


def _unset() -> HTTPException:
    return HTTPException(503, {"code": "cleaner_unset", "message": "Cleaner не настроен: нужны адрес и служебный ключ"})


async def check() -> dict:
    """Самопроверка шлюза Cleaner служебным ключом: `{ok, checks[]}`; недоступный Cleaner — 502 словами."""
    config = settings()
    if config["reenter"]:
        raise HTTPException(503, {"code": "cleaner_reenter",
                                  "message": "Ключ сервера сменился: введите настройки Cleaner заново"})
    if not (config["base_url"] and config["key"]):
        raise _unset()
    try:
        response = await upstream.request(_gateway(config["base_url"]), "GET", config["base_url"] + CHECK_PATH, keyless=True,
                                          headers={KEY_HEADER: config["key"]}, timeout=CHECK_TIMEOUT)
    except upstream.UpstreamRefused:
        raise HTTPException(502, {"code": "cleaner", "message": "адрес Cleaner убран из списка разрешённых"}) from None
    except httpx.HTTPError:
        log.warning("cleaner check: unreachable")
        raise HTTPException(502, {"code": "cleaner", "message": "Cleaner недоступен"}) from None
    if response.status_code in (401, 403):
        raise HTTPException(502, {"code": "cleaner", "message": "Cleaner не принял служебный ключ"})
    if not response.is_success:
        log.warning("cleaner check: code %d", response.status_code)
        raise HTTPException(502, {"code": "cleaner", "message": upstream.message(response)
                                  or f"Cleaner ответил кодом {response.status_code}"})
    body = upstream.json_body(response)
    checks = body.get("checks")
    return {"ok": body.get("ok") is True, "checks": checks if isinstance(checks, list) else []}


# ---- запросы к шлюзу: контекст, ход, отказы словами ----------------------------------

UNSET = "Cleaner не настроен: данные не отправлены — обратитесь к администратору"
DOWN = "Cleaner недоступен: данные не отправлены"
UNLISTED = "адрес Cleaner убран из списка разрешённых: данные не отправлены"
CHANGED = "Диалог или настройки Cleaner изменились: отправьте запрос заново"
BAD_ID = "Cleaner вернул неверный идентификатор контекста"
#: таблицы записей, у которых бывает контекст: ресурс → таблица (владелец — её `user_id`)
PARENTS = {"chat": "chats", "translate": "translations", "probe": "connections"}
#: ожидание контекста одной записи одним подключением: два хода не создают два чата Cleaner
LOCKS: dict[tuple, asyncio.Lock] = {}


def _gateway(base_url: str) -> "connections.Connection":
    """Синтетическое подключение к самому Cleaner: список адресов, сертификат `STUDIO_CLEANER_CA` и проверка имени —
    как у прежнего вида; оно не `routed`, поэтому предохранитель `upstream._cleaner_only` его не держит, а настоящее
    подключение провайдера в `upstream` не попадает вовсе."""
    return connections.Connection(0, None, "Cleaner", "openai", base_url, frozenset(), True,
                                  server_only=True, cleaner=True)


def _config() -> dict:
    """Адрес и служебный ключ для запроса пользователя; не заданы (или не расшифровались) — отказ, данные не уходят."""
    config = settings()
    if not (config["base_url"] and config["key"]):
        raise HTTPException(503, {"code": "cleaner_unset", "message": UNSET})
    return config


def _headers(config: dict, conn: "connections.Connection", remote: "str | None", hold: Iterable[str]) -> dict[str, str]:
    out = {KEY_HEADER: config["key"], "Authorization": f"Bearer {connections.token(conn)}",
           UPSTREAM_HEADER: conn.base_url}
    if remote is not None:
        out[CHAT_HEADER] = remote
    names = list(dict.fromkeys(hold))
    if names:
        out[HOLD_HEADER] = ",".join(names)
    return out


def headers(conn: "connections.Connection", remote: str, hold: Iterable[str] = ()) -> dict[str, str]:
    """Заголовки запроса к шлюзу по контракту Cleaner: служебный ключ — `X-Cleaner-Key`, ключ провайдера снимка
    задания — `Authorization` (`connections.token`: подключение сменили — ConnectionChanged до отправки), адрес
    провайдера, контекст и веб-инструменты, чьи аргументы Cleaner оставляет с заменами."""
    return _headers(_config(), conn, remote, hold)


def _target(conn, path: str, remote: "str | None", hold: Iterable[str]) -> tuple:
    config = _config()
    return _gateway(config["base_url"]), config["base_url"] + AGENT_PATH + path, _headers(config, conn, remote, hold)


@asynccontextmanager
async def stream(conn: "connections.Connection", path: str, *, remote: str, hold: Iterable[str] = (), **kw):
    """`POST` потоком в шлюз агентов Cleaner (`path` — `/chat/completions`); провайдер — только за Cleaner."""
    gateway, url, sent = _target(conn, path, remote, hold)
    async with upstream.stream(gateway, "POST", url, keyless=True, headers=sent, **kw) as response:
        yield response


async def request(conn: "connections.Connection", method: str, path: str, *, remote: "str | None" = None,
                  hold: Iterable[str] = (), **kw) -> httpx.Response:
    """Запрос в шлюз агентов Cleaner (`/models`, `/chat/completions` без потока); без `remote` — без контекста."""
    gateway, url, sent = _target(conn, path, remote, hold)
    return await upstream.request(gateway, method, url, keyless=True, headers=sent, **kw)


def refusal(response: httpx.Response) -> "tuple[str, str] | None":
    """Отказ самого Cleaner (`{"error": {"message": "Cleaner: …", "code"}}`) — (код, сообщение без «Cleaner: »);
    ответ провайдера, который шлюз передал как есть, — None."""
    error = upstream.json_body(response).get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.startswith("Cleaner"):
        return None
    code = error.get("code")
    return (code if isinstance(code, str) else ""), message.removeprefix("Cleaner:").strip()[:300]


def words(response: httpx.Response) -> str:
    """Слова отказа Cleaner по коду ответа и `error.code` (контракт шлюза); ключей и адресов в них нет."""
    code, message = refusal(response) or ("", upstream.message(response) or "")
    status = response.status_code
    if status == 400 and code == "upstream":
        return ("Cleaner ходит только на публичные https-адреса; "
                "локальную модель администратор может добавить в список разрешённых")
    if status == 400 and code == "hold_tools":
        return "Cleaner не принял список веб-инструментов хода — обратитесь к администратору"
    if status == 401 and code == "client_login":
        return "Cleaner не принял ключи запроса (служебный ключ или ключ подключения) — обратитесь к администратору"
    if status in (401, 403):
        return "Cleaner не принял служебный ключ — обратитесь к администратору"
    if status == 404 and code == "chat":
        return "Cleaner не нашёл контекст диалога — отправьте запрос ещё раз"
    if status == 429 or (status == 503 and code == "busy"):
        return "Cleaner занят — повторите запрос через минуту"
    if status == 503:
        return "Cleaner: слой очистки недоступен — данные не отправлены, повторите позже"
    if status in (502, 504):
        return f"Cleaner не дождался провайдера ({status}): {message}" if message else \
            f"Cleaner не дождался провайдера ({status})"
    return f"Cleaner отклонил запрос ({status}): {message}" if message else f"Cleaner отклонил запрос ({status})"


def failure(response: httpx.Response, conn: "connections.Connection") -> str:
    """Отказ ответа шлюза словами: сам Cleaner — `words`, ошибка провайдера, которую шлюз передал как есть, — как у
    обычного подключения (`upstream.gateway_error`)."""
    return words(response) if refusal(response) is not None else upstream.gateway_error(response, conn)


def down(e: httpx.HTTPError, seconds: "int | float") -> str:
    """Нет связи с Cleaner словами: данные не отправлены."""
    if isinstance(e, httpx.TimeoutException):
        return f"Cleaner не ответил за {seconds:g} с"
    if isinstance(e, httpx.ConnectError):
        return DOWN
    return f"связь с Cleaner оборвалась: {type(e).__name__}"


def unreachable(e: upstream.UpstreamRefused, conn: "connections.Connection") -> str:
    """Отказ до отправки словами: подключение сменили после снимка или ключ не расшифровался — как обычно; прочее —
    адрес самого Cleaner убран из списка."""
    return upstream.refused_words(e, conn) if e.reason in ("changed", "token") else UNLISTED


async def models(conn: "connections.Connection") -> list[dict]:
    """Список моделей подключения через Cleaner: `GET /agent/openai/v1/models` с ключом провайдера и его адресом;
    результат — как `connections.fetch_models`. Отказы — 409 `refused` и 502 словами, провайдеру мимо Cleaner не идёт."""
    try:
        response = await request(conn, "GET", "/models", timeout=CONTEXT_TIMEOUT)
    except upstream.UpstreamRefused as e:
        raise HTTPException(409, {"code": "refused", "message": unreachable(e, conn)}) from None
    except httpx.HTTPError as e:
        log.warning("cleaner models: connection %d unreachable", conn.id)
        raise HTTPException(502, {"code": "upstream", "message": down(e, CONTEXT_TIMEOUT.read)}) from None
    if not response.is_success:
        log.info("cleaner models: connection %d code %d", conn.id, response.status_code)
        raise HTTPException(502, {"code": "upstream", "message": failure(response, conn)})
    return connections.model_items(upstream.json_body(response).get("data"))


def _owner(resource: str, item_id: str) -> "int | None":
    row = state.DB.execute(f"SELECT user_id FROM {PARENTS[resource]} WHERE id = ?", (item_id,)).fetchone()
    return row[0] if row is not None else None


def _same(row, config: dict) -> bool:
    try:
        return row["base_url"] == config["base_url"] and security.decrypt(row["token_enc"]) == config["key"]
    except InvalidToken:
        return False


async def _create(config: dict) -> str:
    """Чат Cleaner вида `client` служебным ключом: `POST /api/v1/chats` → id."""
    try:
        response = await upstream.request(_gateway(config["base_url"]), "POST", config["base_url"] + CHATS_PATH,
                                          keyless=True, headers={KEY_HEADER: config["key"]}, json={"title": "Studio"},
                                          timeout=CONTEXT_TIMEOUT)
    except upstream.UpstreamRefused:
        raise HTTPException(502, {"code": "cleaner", "message": UNLISTED}) from None
    except httpx.HTTPError:
        log.warning("cleaner: контекст не создан — Cleaner недоступен")
        raise HTTPException(502, {"code": "cleaner", "message": DOWN}) from None
    if not response.is_success:
        log.info("cleaner: контекст не создан — код %d", response.status_code)
        raise HTTPException(502, {"code": "cleaner", "message": words(response)})
    remote = upstream.json_body(response).get("id")
    if not isinstance(remote, str) or not remote.isascii() or not remote.isalnum() or len(remote) > 128:
        raise HTTPException(502, {"code": "cleaner", "message": BAD_ID})
    return remote


async def context(conn: "connections.Connection", resource: str, item_id: str) -> str:
    """id чата Cleaner записи (`resource` — `chat`, `translate` или `probe` — «Пробный ответ» подключения, запись — его id) для подключения через Cleaner. Строка
    `cleaner_contexts` хранит снимок адреса Cleaner и служебного ключа (шифром) и отмечена `cleaner_route_contexts`;
    повторно — та же, пока снимок совпадает с настройками. Иначе прежняя уходит в очередь удаления (триггер, со своим
    снимком), создаётся новая. Владелец — владелец записи: у общего подключения своей учётки нет."""
    key = (resource, item_id, conn.id)
    lock = LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            config = _config()
            owner = _owner(resource, item_id)
            if owner is None:
                raise HTTPException(404, {"code": "gone", "message": CHANGED})
            row = state.DB.execute(
                "SELECT c.*, r.id IS NOT NULL AS route FROM cleaner_contexts c"
                " LEFT JOIN cleaner_route_contexts r ON r.id = c.id"
                " WHERE c.resource = ? AND c.item_id = ? AND c.connection_id = ?", key).fetchone()
            if row is not None:
                if row["route"] and _same(row, config):
                    return row["remote_id"]
                state.DB.execute("DELETE FROM cleaner_contexts WHERE id = ?", (row["id"],))   # триггер — в очередь
            remote = await _create(config)
            identifier, now = uuid.uuid4().hex, clock.NOW()
            fresh = settings()
            if (fresh["base_url"], fresh["key"]) != (config["base_url"], config["key"]) or _owner(resource, item_id) is None:
                state.DB.execute("INSERT INTO cleaner_deletions(id, user_id, remote_id, base_url, token_enc, queued, route)"
                                 " VALUES (?, ?, ?, ?, ?, ?, 1)",
                                 (identifier, owner, remote, config["base_url"], security.encrypt(config["key"]), now))
                raise HTTPException(409, {"code": "changed", "message": CHANGED})
            state.DB.execute("INSERT INTO cleaner_contexts(id, user_id, resource, item_id, connection_id, remote_id,"
                             " base_url, token_enc, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (identifier, owner, resource, item_id, conn.id, remote, config["base_url"],
                              security.encrypt(config["key"]), now))
            state.DB.execute("INSERT INTO cleaner_route_contexts(id) VALUES (?)", (identifier,))
            log.info("cleaner: контекст для %s %s создан", resource, item_id[:8])
            return remote
    finally:
        if not lock.locked() and not getattr(lock, "_waiters", None):     # ждущего не оставить без замка
            LOCKS.pop(key, None)


def forget(conn: "connections.Connection", resource: str, item_id: str) -> None:
    """Cleaner не знает контекста (404 `chat`: его удалили в Cleaner) — строка уходит в очередь удаления, следующий
    запрос создаст новый."""
    state.DB.execute("DELETE FROM cleaner_contexts WHERE resource = ? AND item_id = ? AND connection_id = ?"
                     " AND id IN (SELECT id FROM cleaner_route_contexts)", (resource, item_id, conn.id))
