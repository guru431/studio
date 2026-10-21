"""Общие объекты процесса: ставит lifespan приложения, тесты подменяют."""
import sqlite3
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from config import Config

CFG: Config | None = None
DB: sqlite3.Connection | None = None
HTTP: httpx.AsyncClient | None = None
WATCHER: bool = True      # быстрые тесты ставят False и зовут проход наблюдателя роликов сами


def make_http() -> httpx.AsyncClient:
    # trust_env=False: HTTP(S)_PROXY / ALL_PROXY окружения не поведут ключи подключений через прокси.
    # Клиент один на всех пользователей: cookie ответов он не хранит (пустой список доменов политики),
    # иначе cookie сервиса из запроса одного пользователя уходила бы с запросами других к тому же хосту.
    # Банка — самим CookieJar: httpx.Cookies(...) скопировал бы cookie в новую банку без этой политики.
    jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(timeout=30, trust_env=False, cookies=jar)
