"""Общие объекты процесса: ставит lifespan приложения, тесты подменяют."""
import sqlite3

import httpx

from config import Config

CFG: Config | None = None
DB: sqlite3.Connection | None = None
HTTP: httpx.AsyncClient | None = None
WATCHER: bool = True      # быстрые тесты ставят False и зовут проход наблюдателя роликов сами


def make_http() -> httpx.AsyncClient:
    # trust_env=False: HTTP(S)_PROXY / ALL_PROXY окружения не поведут ключи подключений через прокси
    return httpx.AsyncClient(timeout=30, trust_env=False)
