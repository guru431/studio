"""Инструменты чата «Интернет» (спека 2026-10-21-studio-chat-internet): поиск и страницы Exa, субтитры
YouTube, определения инструментов для модели. Ключ Exa — у учётки (`users.exa_key`, шифром
security.encrypt, как токены подключений); сохраняет и убирает его profile.py, наружу он не уходит и в
журнал не пишется. Неудача инструмента — ToolError со словами для человека.

Exa — постоянный адрес `EXA_URL` через общий клиент state.HTTP, а не upstream: адрес выбирает не
пользователь и не модель (адрес страницы уходит в Exa данными, сервер по нему не ходит). Наружу — только
поисковый запрос или адрес страницы и ключ заголовком `x-api-key`. Шаг инструмента — dict: поиск
`{"kind": "search", "query", "results": [{"title", "url", "published"}], "text"}`, страница
`{"kind": "page", "url", "title", "text"}`; `text` — ровно то, что получит модель."""
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken

import security
import state

EXA_URL = "https://api.exa.ai"
EXA_TIMEOUT_SEC = 30
SEARCH_RESULTS = 10          # базовая цена поиска Exa — до 10 результатов
PAGE_MAX = 40_000            # знаков текста страницы: ~10 тыс. токенов из 131 072 контекста gemma-abl


class ToolError(Exception):
    """Неудача инструмента; args[0] — слова для человека (без ключей и ответов сервиса)."""


def has_exa_key(user_id: int) -> bool:
    row = state.DB.execute("SELECT exa_key IS NOT NULL FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def exa_key(user_id: int) -> "str | None":
    """Ключ Exa учётки; нет — None. Не расшифровался (сменили STUDIO_DATA_KEY) — ToolError."""
    row = state.DB.execute("SELECT exa_key FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return security.decrypt(row[0])
    except InvalidToken:
        raise ToolError("ключ Exa не читается — введите его заново в Настройках") from None


def _exa_error(r: httpx.Response) -> str:
    """Слова для неудачного ответа Exa (коды — exa.ai/docs/admin/error-codes). Не JSON — HTML Cloudflare:
    Exa закрыт для выхода сервера студии."""
    try:
        r.json()
    except ValueError:
        return "Exa недоступен с сервера студии"
    if r.status_code == 401:
        return "ключ Exa не подходит — проверьте в Настройках"
    if r.status_code == 402:
        return "Exa: бесплатный баланс исчерпан, обновится 1-го числа"
    if r.status_code == 429:
        return "Exa: слишком часто — попробуйте позже"
    return f"Exa ответил {r.status_code}"


async def _exa(key: str, path: str, body: dict) -> dict:
    try:
        r = await state.HTTP.post(f"{EXA_URL}{path}", json=body, headers={"x-api-key": key},
                                  timeout=EXA_TIMEOUT_SEC)
    except httpx.TimeoutException:
        raise ToolError(f"Exa не ответил за {EXA_TIMEOUT_SEC} с") from None
    except httpx.HTTPError as e:
        raise ToolError(f"Exa недоступен: {type(e).__name__}") from None
    if r.status_code != 200:
        raise ToolError(_exa_error(r))
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ToolError("Exa недоступен с сервера студии")
    return data


async def search(key: str, query: str) -> dict:
    data = await _exa(key, "/search", {"query": query, "type": "auto", "numResults": SEARCH_RESULTS,
                                       "contents": {"highlights": True}})
    results, blocks = [], []
    for n, res in enumerate((data.get("results") or [])[:SEARCH_RESULTS], 1):
        title, url = res.get("title") or "", res.get("url") or ""
        published = (res.get("publishedDate") or "")[:10]
        results.append({"title": title, "url": url, "published": published})
        lines = [f"{n}. {title or url}", url]
        if published:
            lines.append(published)
        lines += [h.strip() for h in res.get("highlights") or [] if isinstance(h, str) and h.strip()]
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks) if blocks else "Поиск ничего не нашёл."
    return {"kind": "search", "query": query, "results": results, "text": text}


async def page(key: str, url: str) -> dict:
    try:
        parts = urlsplit(url)
    except ValueError:                   # «http://[::1» — адрес составляет модель
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ToolError("адрес страницы — только http или https")
    data = await _exa(key, "/contents", {"urls": [url], "text": {"maxCharacters": PAGE_MAX}})
    status = (data.get("statuses") or [{}])[0]
    if status.get("status") == "error":
        tag = (status.get("error") or {}).get("tag") or "?"
        raise ToolError("страница не найдена" if tag == "CRAWL_NOT_FOUND" else f"страница не прочиталась ({tag})")
    results = data.get("results") or []
    if not results:
        raise ToolError("страница не прочиталась")
    title = results[0].get("title") or ""
    return {"kind": "page", "url": url, "title": title,
            "text": f"{title}\n{url}\n\n{results[0].get('text') or ''}"}
