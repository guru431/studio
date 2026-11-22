"""Шлюз Cleaner для внешних моделей (шаг схемы 64): настройки администратора и самопроверка.

- Настройки — один JSON шифром `STUDIO_DATA_KEY` в `settings[SETTINGS_KEY]`: адрес Cleaner, служебный ключ и флажок
  «Cleaner для всех внешних моделей». Ключ в браузер не отдаётся; после смены `STUDIO_DATA_KEY` настройки не
  расшифровать — панель просит ввести их заново (`reenter`), принудительный режим выключен.
- Сервер Studio ходит к Cleaner только по списку разрешённых адресов (`upstream`, сертификат — `STUDIO_CLEANER_CA`),
  ключ — только заголовком `X-Cleaner-Key`.
- Смена адреса или ключа отправляет контексты нового вида (чаты Cleaner обычных подключений) в очередь удаления
  `cleaner_deletions` со снимком прежнего адреса и ключа: триггер `cleaner_context_delete` делает это сам.
- Внутреннее и внешнее подключение — `is_external`; подключение через Cleaner — `routes` (переключатель
  `via_cleaner` или принудительный режим): такое подключение идёт через сервер (`Connection.routed`), а
  `upstream._prepare` не пускает его никуда, кроме адреса Cleaner.
- Журнал — только коды и числа: адресов и ключей в нём нет.
"""
import ipaddress
import json
import logging
import re

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, Field

import connections
import security
import state
import upstream

log = logging.getLogger("studio")

SETTINGS_KEY = "cleaner_gateway"
#: заголовок служебного ключа Cleaner
KEY_HEADER = "X-Cleaner-Key"
#: самопроверка шлюза: ключ, слои, доступность
CHECK_PATH = "/agent/openai/v1/cleaner/check"
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
        # контексты прежнего вида Cleaner (подключение `cleaner = 1`, личный ключ) не трогаются
        state.DB.execute("DELETE FROM cleaner_contexts WHERE connection_id IN (SELECT id FROM connections WHERE cleaner = 0)")
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
    conn = connections.Connection(0, None, "Cleaner", "openai", config["base_url"], frozenset(), True,
                                  server_only=True, cleaner=True)
    try:
        response = await upstream.request(conn, "GET", config["base_url"] + CHECK_PATH, keyless=True,
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
