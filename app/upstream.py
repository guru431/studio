"""Запросы сервера наружу — только по списку администратора `host:port` (граница SSRF).

Сервер ходит к хосту подключения, только если хост и порт адреса ТОЧНО совпадают с записью
списка (`settings.allowlist`): без userinfo, только http/https, порт по умолчанию 80/443.
Переадресации не выполняются — ответ 3xx возвращается как есть, второго запроса нет.
Проверка — до всякого ввода-вывода; адрес разбирает тот же httpx.URL, по которому идёт запрос.
"""
import ipaddress
import json
import re
from contextlib import asynccontextmanager

import httpx

import connections
import state

DEFAULT_PORTS = {"http": 80, "https": 443}


class UpstreamRefused(Exception):
    """Адрес не из списка администратора: запрос не отправлен."""


def _host(host: str) -> str:
    """Хост записи: IPv4 или `[IPv6]` (сжатая форма); иначе ValueError. Имён нет: имя сверялось
    бы с DNS на каждом запросе, и граница SSRF была бы у того, кто правит DNS."""
    if host.startswith("[") and host.endswith("]"):
        return f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
    if re.fullmatch(r"[0-9.]+", host):
        return str(ipaddress.IPv4Address(host))
    raise ValueError(host)


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


def is_allowed(url: str) -> bool:
    parsed = _parse(url)
    entry = _entry(parsed) if parsed is not None else None
    return entry is not None and entry in allowlist()


def _prepare(conn, url, kw: dict) -> tuple[httpx.URL, dict]:
    parsed = _parse(url)
    if parsed is None or not is_allowed(parsed):
        raise UpstreamRefused(f"подключение {conn.id}: адрес не из списка администратора")
    headers = dict(kw.pop("headers", None) or {})
    headers["Authorization"] = f"Bearer {connections.token(conn)}"
    kw.pop("follow_redirects", None)
    return parsed, {**kw, "headers": headers, "follow_redirects": False}


async def request(conn, method: str, url: str, **kw) -> httpx.Response:
    parsed, kw = _prepare(conn, url, kw)
    return await state.HTTP.request(method, parsed, **kw)


@asynccontextmanager
async def stream(conn, method: str, url: str, **kw):
    parsed, kw = _prepare(conn, url, kw)
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
