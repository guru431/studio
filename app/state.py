"""Общие объекты процесса: ставит lifespan приложения, тесты подменяют."""
import asyncio
import hashlib
import sqlite3
import ssl
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

import clock
from config import Config

CFG: Config | None = None
DB: sqlite3.Connection | None = None
HTTP: httpx.AsyncClient | None = None
CLEANER_HTTP: httpx.AsyncClient | None = None
WATCHER: bool = True      # быстрые тесты ставят False и зовут проход наблюдателя роликов сами


def make_http() -> httpx.AsyncClient:
    return _http()


def make_cleaner_http() -> httpx.AsyncClient:
    if CFG.cleaner_ca is None:
        return HTTP
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=CFG.cleaner_ca)
    return _http(context)


#: клиенты доверенных сертификатов учёток: sha256 PEM → клиент; не больше PINNED_MAX, давний закрывается
PINNED: dict[str, httpx.AsyncClient] = {}
PINNED_MAX = 32
#: вытесненный клиент закрывается не сразу: его может держать идущий запрос другой учётки (поток файла, загрузка) —
#: через столько секунд, сколько ждёт Apache самый долгий маршрут `/api/owui/`
PINNED_GRACE_SEC = 600
#: задачи отложенного закрытия вытесненных клиентов
RETIRED: set[asyncio.Task] = set()


async def _retire(client: httpx.AsyncClient) -> None:
    try:
        await clock.SLEEP(PINNED_GRACE_SEC)
    finally:
        await client.aclose()


def pinned_http(pem: str) -> httpx.AsyncClient:
    """Клиент, доверяющий ровно этому сертификату (`upstream.pinned_context`); тот же PEM — тот же клиент."""
    import upstream                      # upstream импортирует state
    key = hashlib.sha256(pem.encode()).hexdigest()
    client = PINNED.pop(key, None) or _http(upstream.pinned_context(pem))
    PINNED[key] = client                 # в конец: давний — первым
    while len(PINNED) > PINNED_MAX:
        task = asyncio.get_running_loop().create_task(_retire(PINNED.pop(next(iter(PINNED)))))
        RETIRED.add(task)
        task.add_done_callback(RETIRED.discard)
    return client


async def close_pinned() -> None:
    while PINNED:
        await PINNED.popitem()[1].aclose()
    for task in list(RETIRED):                 # отложенные закрытия — сейчас
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _http(verify=True) -> httpx.AsyncClient:
    # trust_env=False: HTTP(S)_PROXY / ALL_PROXY окружения не поведут ключи подключений через прокси.
    # Клиент один на всех пользователей: cookie ответов он не хранит (пустой список доменов политики),
    # иначе cookie сервиса из запроса одного пользователя уходила бы с запросами других к тому же хосту.
    # Банка — самим CookieJar: httpx.Cookies(...) скопировал бы cookie в новую банку без этой политики.
    jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(timeout=30, trust_env=False, cookies=jar, verify=verify)
