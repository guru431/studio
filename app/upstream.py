"""Запросы сервера наружу — только по списку администратора `host:port` (граница SSRF).

Сервер ходит к хосту подключения, только если хост и порт адреса ТОЧНО совпадают с записью
списка (`settings.allowlist`): без userinfo, только http/https, порт по умолчанию 80/443.
Запись — IP (`10.0.0.5:4000`, `[::1]:8080`) или имя (`api.openai.com:443`). Имя — только для
https: при каждом запросе сервер разрешает его сам (`RESOLVE`), отказывает, если хоть один адрес
не публичный (`ipaddress.is_global`: частные, петля, link-local, CGNAT, multicast,
зарезервированные), и соединяется именно с проверенным адресом (SNI и `Host` — имя): подмена DNS
между проверкой и запросом ничего не даёт. Внутренние адреса — только IP-записями.
Переадресации не выполняются — ответ 3xx возвращается как есть, второго запроса нет.
Ключ — только адресу своего снимка подключения: подключение, удалённое или сменившее вид или
адрес после снимка, — ConnectionChanged до отправки (connections.token).
Проверка — до всякого ввода-вывода; адрес разбирает тот же httpx.URL, по которому идёт запрос.
"""
import asyncio
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager

import httpx

import connections
import state

DEFAULT_PORTS = {"http": 80, "https": 443}
#: DNS-имя записи: метки до 63 знаков, хотя бы одна точка, последняя метка начинается с буквы
NAME_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


class UpstreamRefused(Exception):
    """Запрос не отправлен. `reason`: `unlisted` — адреса нет в списке администратора, `internal` — имя из
    списка разрешается во внутренний адрес, `changed` — подключение сменили после снимка."""

    def __init__(self, message: str, reason: str = "unlisted"):
        super().__init__(message)
        self.reason = reason


class ConnectionChanged(UpstreamRefused):
    """Подключение удалили или сменили его вид или адрес после того, как задание взяло снимок:
    ключ не отдан (connections.token) — новый ключ ушёл бы прежнему адресу."""

    def __init__(self, conn, deleted: bool):
        super().__init__(f"подключение {conn.id}: {'удалено' if deleted else 'изменено'}", "changed")
        self.deleted = deleted


def refused_words(e: UpstreamRefused, conn) -> str:
    """Отказ до отправки — словами для пользователя; каждая причина — своими."""
    if isinstance(e, ConnectionChanged):
        return (f"подключение «{conn.name}» удалено из профиля" if e.deleted
                else f"подключение «{conn.name}» изменено — запустите заново")
    if e.reason == "token":
        return f"токен подключения «{conn.name}» не удалось расшифровать — введите его заново"
    if e.reason == "internal":
        return f"имя адреса подключения «{conn.name}» разрешается во внутренний адрес — сервер туда не ходит"
    return f"адрес подключения «{conn.name}» убран из списка администратора"


def _host(host: str) -> str:
    """Хост записи: IPv4, `[IPv6]` (сжатая форма) или DNS-имя (IDNA, нижний регистр); иначе
    ValueError. Имя сверяется с DNS на каждом запросе (`_pinned`): доверять ему можно, только
    проверив адреса, поэтому имя — только для https и только с публичными адресами."""
    if host.startswith("[") and host.endswith("]"):
        return f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
    if re.fullmatch(r"[0-9.]+", host):
        return str(ipaddress.IPv4Address(host))
    try:
        name = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError(host) from None
    if not NAME_RE.fullmatch(name):
        raise ValueError(host)
    return name


def parse_entry(text: str) -> str:
    """`host:port` в нижнем регистре; порт 1–65535 обязателен; иначе ValueError."""
    host, sep, port = text.strip().lower().rpartition(":")
    if not sep or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError(text)
    return f"{_host(host)}:{int(port)}"


def allowlist() -> list[str]:
    return json.loads(state.DB.execute("SELECT value FROM settings WHERE key = 'allowlist'").fetchone()["value"])


def _entry(url: httpx.URL) -> str | None:
    """Запись списка, к которой ведёт адрес, или None — адрес вне правил."""
    if url.scheme not in DEFAULT_PORTS or url.userinfo or not url.host:
        return None
    host = f"[{url.host}]" if ":" in url.host else url.host
    try:
        return parse_entry(f"{host}:{url.port or DEFAULT_PORTS[url.scheme]}")
    except ValueError:
        return None


def _parse(url) -> httpx.URL | None:
    try:
        parsed = httpx.URL(url)
        parsed.host                  # имя IDNA httpx разбирает лениво: отказ — здесь, а не дальше
        return parsed
    except (httpx.InvalidURL, UnicodeError, ValueError, TypeError):   # IDNAError — это UnicodeError
        return None


def named(url: httpx.URL) -> bool:
    """Хост адреса — имя, а не IP."""
    host = url.host
    return bool(host) and ":" not in host and not re.fullmatch(r"[0-9.]+", host)


def is_allowed(url) -> bool:
    parsed = _parse(url)
    entry = _entry(parsed) if parsed is not None else None
    return entry is not None and entry in allowlist() and (parsed.scheme == "https" or not named(parsed))


async def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise httpx.ConnectError("имя не разрешилось") from None
    return list(dict.fromkeys(info[4][0] for info in infos))


#: разрешение имени (хост, порт) → адреса; тесты подменяют
RESOLVE = _resolve


def _public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:                 # адрес с зоной (fe80::1%eth0) и прочее нераспознанное
        return False


async def _pinned(conn, url: httpx.URL, headers: dict, kw: dict) -> httpx.URL:
    """Адрес имени, проверенный сейчас: запрос идёт на него, SNI и `Host` — имя."""
    name = url.raw_host.decode("ascii")
    addresses = await RESOLVE(name, url.port or DEFAULT_PORTS[url.scheme])
    if not addresses or not all(_public(a) for a in addresses):
        raise UpstreamRefused(f"подключение {conn.id}: имя разрешается во внутренний адрес", "internal")
    headers["Host"] = name if url.port in (None, DEFAULT_PORTS[url.scheme]) else f"{name}:{url.port}"
    kw["extensions"] = {**(kw.get("extensions") or {}), "sni_hostname": name}
    return url.copy_with(host=addresses[0])


async def _prepare(conn, url, kw: dict, keyless: bool = False) -> tuple[httpx.URL, dict]:
    parsed = _parse(url)
    if parsed is None or not is_allowed(parsed):
        raise UpstreamRefused(f"подключение {conn.id}: адрес не из списка администратора")
    headers = dict(kw.pop("headers", None) or {})
    if not keyless:
        headers["Authorization"] = f"Bearer {connections.token(conn)}"
    kw.pop("follow_redirects", None)
    if named(parsed):
        parsed = await _pinned(conn, parsed, headers, kw)
    return parsed, {**kw, "headers": headers, "follow_redirects": False}


async def request(conn, method: str, url: str, *, keyless: bool = False, **kw) -> httpx.Response:
    """keyless — без заголовка `Authorization` (проверка: отдаёт ли сервис список без ключа); правила
    списка и имени — те же."""
    parsed, kw = await _prepare(conn, url, kw, keyless)
    return await state.HTTP.request(method, parsed, **kw)


@asynccontextmanager
async def stream(conn, method: str, url: str, **kw):
    parsed, kw = await _prepare(conn, url, kw)
    async with state.HTTP.stream(method, parsed, **kw) as response:
        yield response


# ---- отказы шлюза ------------------------------------------------------------

def json_body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def message(response: httpx.Response) -> str | None:
    """Сообщение отказа шлюза: OpenAI `error.message`, LiteLLM `detail` или None."""
    body = json_body(response)
    error = body.get("error")
    text = error.get("message") if isinstance(error, dict) else error
    if not text and isinstance(body.get("detail"), dict):
        text = body["detail"].get("message") or body["detail"].get("error")
    text = text or body.get("detail")
    return str(text)[:300] if text else None


def _reason(response: httpx.Response) -> str:
    return message(response) or f"код {response.status_code}"


def gateway_error(response: httpx.Response, conn_name: str) -> str:
    code = response.status_code
    if code == 401:
        return f"шлюз не принял ключ подключения «{conn_name}» — проверьте токен в профиле"
    if code == 403:
        return "ключу закрыт этот маршрут шлюза"
    if code == 400:
        return f"шлюз отклонил запрос: {_reason(response)}"
    if code == 409:
        # slot-gate и переводчик говорят, чем занята карта («идут ролики: 2»)
        return message(response) or "карта занята генерацией"
    if code == 503:
        return f"карта занята или недоступна: {_reason(response)}"
    if code in (502, 504):
        return f"шлюз не дождался службы: {_reason(response)}"
    return f"шлюз ответил {code}: {_reason(response)}"
