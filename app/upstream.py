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
Вызывающий без строки подключения (учётка Open WebUI) передаёт свой `Authorization` (`auth`) и доверенный
сертификат (`cert_pem` → `pinned_context`); WebSocket и получение сертификата — по тем же правилам (`ws_target`,
`fetch_certificate`).
"""
import asyncio
import ipaddress
import json
import re
import socket
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

import connections
import state

DEFAULT_PORTS = {"http": 80, "https": 443}
#: DNS-имя записи: метки до 63 знаков, хотя бы одна точка, последняя метка начинается с буквы
NAME_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


class UpstreamRefused(Exception):
    """Запрос не отправлен. `reason`: `unlisted` — адреса нет в списке администратора, `internal` — имя из
    списка разрешается во внутренний адрес, `changed` — подключение сменили после снимка, `cleaner` — подключение
    через Cleaner просит адрес не Cleaner: текст пользователя провайдеру мимо Cleaner не уходит."""

    def __init__(self, message: str, reason: str = "unlisted"):
        super().__init__(message)
        self.reason = reason


class ConnectionChanged(UpstreamRefused):
    """Подключение удалили или сменили его вид или адрес после того, как задание взяло снимок:
    ключ не отдан (connections.token) — новый ключ ушёл бы прежнему адресу."""

    def __init__(self, conn, deleted: bool, route: bool = False):
        super().__init__(f"подключение {conn.id}: {'удалено' if deleted else 'изменено'}", "changed")
        self.deleted = deleted
        #: сменился маршрут через Cleaner (переключатель или флажок администратора) после снимка
        self.route = route


def refused_words(e: UpstreamRefused, conn) -> str:
    """Отказ до отправки — словами для пользователя; каждая причина — своими."""
    if isinstance(e, ConnectionChanged):
        if e.route:
            return f"маршрут подключения «{conn.name}» через Cleaner изменился — данные не отправлены, запустите заново"
        return (f"подключение «{conn.name}» удалено из профиля" if e.deleted
                else f"подключение «{conn.name}» изменено — запустите заново")
    if e.reason == "token":
        return f"токен подключения «{conn.name}» не удалось расшифровать — введите его заново"
    if e.reason == "cleaner":
        return f"подключение «{conn.name}» идёт только через Cleaner — запрос провайдеру не отправлен"
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

NAT64_PREFIXES = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("64:ff9b:1::/48"))


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:                 # адрес с зоной (fe80::1%eth0) и прочее нераспознанное
        return False
    # За стандартными префиксами трансляции могут стоять внутренние IPv4-адреса.
    return ip.is_global and not any(ip in prefix for prefix in NAT64_PREFIXES)


def _listed(conn, url) -> httpx.URL:
    parsed = _parse(url)
    if parsed is None or not is_allowed(parsed):
        raise UpstreamRefused(f"подключение {conn.id}: адрес не из списка администратора")
    return parsed


def _host_header(url: httpx.URL) -> str:
    host = url.raw_host.decode("ascii")
    host = f"[{host}]" if ":" in host else host
    return host if url.port in (None, DEFAULT_PORTS[url.scheme]) else f"{host}:{url.port}"


async def _pinned(conn, url: httpx.URL) -> httpx.URL:
    """Адрес имени, проверенный сейчас: соединение идёт на него, SNI и `Host` — имя."""
    addresses = await RESOLVE(url.raw_host.decode("ascii"), url.port or DEFAULT_PORTS[url.scheme])
    if not addresses or not all(_public(a) for a in addresses):
        raise UpstreamRefused(f"подключение {conn.id}: имя разрешается во внутренний адрес", "internal")
    return url.copy_with(host=addresses[0])


def _cleaner_only(conn, url) -> None:
    """Предохранитель последней линии: подключение через Cleaner (`routed`) — только на адрес Cleaner из настроек
    (схема, хост, порт). Любой путь кода, что по ошибке понесёт ему адрес провайдера, получает отказ до ввода-вывода."""
    if not getattr(conn, "routed", False):
        return
    import cleaner_route                       # цикл: cleaner_route → connections → upstream
    base, parsed = _parse(cleaner_route.settings()["base_url"] or None), _parse(url)
    if base is None or parsed is None or (parsed.scheme, parsed.host, parsed.port or DEFAULT_PORTS.get(parsed.scheme)) != (
            base.scheme, base.host, base.port or DEFAULT_PORTS.get(base.scheme)):
        raise UpstreamRefused(f"подключение {conn.id}: идёт только через Cleaner", "cleaner")


async def _prepare(conn, url, kw: dict, keyless: bool = False, auth: str | None = None) -> tuple[httpx.URL, dict]:
    _cleaner_only(conn, url)
    parsed = _listed(conn, url)
    headers = dict(kw.pop("headers", None) or {})
    if not keyless:
        headers["Authorization"] = auth if auth is not None else f"Bearer {connections.token(conn)}"
    kw.pop("follow_redirects", None)
    if named(parsed):
        headers["Host"] = _host_header(parsed)
        kw["extensions"] = {**(kw.get("extensions") or {}), "sni_hostname": parsed.raw_host.decode("ascii")}
        parsed = await _pinned(conn, parsed)
    return parsed, {**kw, "headers": headers, "follow_redirects": False}


def _client(conn) -> httpx.AsyncClient:
    """Клиент запроса: доверенный сертификат учётки — свой клиент, иначе Cleaner или общий."""
    if getattr(conn, "cert_pem", None):
        return state.pinned_http(conn.cert_pem)
    return state.CLEANER_HTTP if getattr(conn, "cleaner", False) else state.HTTP


async def request(conn, method: str, url: str, *, keyless: bool = False, auth: str | None = None,
                  **kw) -> httpx.Response:
    """keyless — без заголовка `Authorization` (проверка: отдаёт ли сервис список без ключа); auth — готовое
    значение `Authorization` вызывающего (`connections.token` не зовётся); правила списка и имени — те же."""
    parsed, kw = await _prepare(conn, url, kw, keyless, auth)
    return await _client(conn).request(method, parsed, **kw)


@asynccontextmanager
async def stream(conn, method: str, url: str, *, keyless: bool = False, auth: str | None = None, **kw):
    parsed, kw = await _prepare(conn, url, kw, keyless, auth)
    async with _client(conn).stream(method, parsed, **kw) as response:
        yield response


# ---- доверенный сертификат и WebSocket ---------------------------------------

#: срок соединения и рукопожатия при получении сертификата, с
CERT_FETCH_SEC = 10


def pinned_context(pem: str) -> ssl.SSLContext:
    """Якорь доверия — ровно этот сертификат (системные корни не грузятся), без проверки имени: сертификат
    по IP часто без нужного SAN; частичная цепочка — сертификат сервера сам себе якорь. Без `VERIFY_X509_STRICT`:
    когда якорь — ровно этот сертификат, строгость RFC 5280 ничего не добавляет, а иной самоподписанный сертификат
    (`keyCertSign` без `CA:TRUE`) отвергался бы при совпавшем отпечатке."""
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


async def fetch_certificate(conn, url: str) -> bytes:
    """DER сертификата https-сервера рукопожатием без проверки — показать отпечаток владельцу. Адрес — по тем
    же правилам списка и имени (отказ до всякого ввода-вывода); данные по соединению не идут."""
    _cleaner_only(conn, url)
    parsed = _listed(conn, url)
    if parsed.scheme != "https":
        raise ValueError("сертификат есть только у https")
    name = parsed.raw_host.decode("ascii") if named(parsed) else None
    if name:
        parsed = await _pinned(conn, parsed)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(parsed.host, parsed.port or 443, ssl=context, server_hostname=name),
        CERT_FETCH_SEC)
    try:
        return writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()


@dataclass(frozen=True)
class WsTarget:
    """Куда соединяться WebSocket: проверенный IP, SNI-имя и `Host` — имя адреса (для IP-записи SNI нет)."""
    host: str
    port: int
    path: str
    ssl: ssl.SSLContext | None
    server_hostname: str | None
    host_header: str


async def ws_target(conn, url: str) -> WsTarget:
    """Цель `ws://`/`wss://` для `http://`/`https://` адреса — по тем же правилам списка и имени, что у
    запросов; `wss` — с доверенным сертификатом учётки или системными корнями."""
    _cleaner_only(conn, url)
    parsed = _listed(conn, url)
    name = parsed.raw_host.decode("ascii") if named(parsed) else None
    host_header = _host_header(parsed)
    if name:
        parsed = await _pinned(conn, parsed)
    context = None
    if parsed.scheme == "https":
        pem = getattr(conn, "cert_pem", None)
        context = pinned_context(pem) if pem else ssl.create_default_context()
    path = parsed.raw_path.decode("ascii")
    return WsTarget(host=parsed.host, port=parsed.port or DEFAULT_PORTS[parsed.scheme], path=path, ssl=context,
                    server_hostname=name, host_header=host_header)


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


#: где правят ключ подключения (раздела «Профиль» больше нет)
KEY_PLACE = "«Настройки» → раздел → «⋯» у подключения → «Изменить»"


def gateway_error(response: httpx.Response, conn) -> str:
    """Отказ сервиса словами. Шлюз студии (srvllm, s06) — словами шлюза и карты; облако и прочие
    OpenAI-совместимые (и Cleaner) — «сервис «имя» …»: карты у них нет."""
    code = response.status_code
    if conn.shared and conn.user_id is not None:
        return f"общее подключение «{conn.name}» ответило HTTP {code} — обратитесь к администратору"
    if code == 401:
        return f"ключ подключения «{conn.name}» не принят (401) — замените его: {KEY_PLACE}"
    if code == 429:
        return f"«{conn.name}»: слишком много запросов или исчерпан лимит (429) — {_reason(response)}"
    if code == 402:
        return f"«{conn.name}»: закончились средства или лимит оплаты (402) — {_reason(response)}"
    if not (conn.kind == "srvllm" or conn.s06):
        if code == 403:
            return f"сервис «{conn.name}» отказал ключу в доступе (403): {_reason(response)}"
        if code == 400:
            return f"сервис «{conn.name}» отклонил запрос: {_reason(response)}"
        if code == 404:
            return f"сервис «{conn.name}» не нашёл адрес или модель (404): {_reason(response)}"
        if code in (502, 503, 504):
            return f"сервис «{conn.name}» временно недоступен ({code}): {_reason(response)}"
        return f"сервис «{conn.name}» ответил {code}: {_reason(response)}"
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
