"""«Проверить» подключение (спека 2026-10-21-studio-connection-check-design): паспорт адреса, ключа и
возможностей моделей.

- Пробы — только чтение (`GET`, `CHECK_TIMEOUT_SEC`): srvllm — `{base}/v1/models` и `{base}/slot/models`
  (slot-gate отвечает сам, карту не трогает); openai — `models_url`, ответил 200 — тот же запрос без
  ключа (открытый список — ключ `unverified`), у OpenRouter — ещё `{base}/key`. Ничего не генерирует, не
  освобождает карту и не читает `GET /slot/videos`. Сервер ходит только через `upstream` (список, имя,
  без перенаправлений).
- Каждый запрос — строка `routes` паспорта: адрес (`reach`), ключ (`key`: accepted, refused, limited,
  unverified, unknown), число моделей. Модели и возможности — capabilities.py; пробы прошлых проверок
  сохраняются, пока паспорт не обнулён (смена адреса, вида или токена — connections.patch).
- Подключение, убранное из списка (connections.dropped), — паспорт этими словами без запроса; прямое —
  409 `direct`: его проверяет браузер.
- Журнал — только id и итог одним словом: ни адреса, ни моделей, ни слов сервиса.
"""
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

import capabilities
import clock
import connections
import sections
import upstream

CHECK_TIMEOUT_SEC = 10
MODELS_MAX = 500
WORDS_MAX = 300

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/connections")


def _ms(start: float) -> int:
    return max(0, round((clock.NOW() - start) * 1000))


def _down(e: httpx.HTTPError) -> str:
    if isinstance(e, httpx.TimeoutException):
        why = f"нет ответа за {CHECK_TIMEOUT_SEC} с"
    elif isinstance(e, httpx.ConnectError):
        why = "соединение не установлено"
    else:
        why = f"связь оборвалась ({type(e).__name__})"
    return "не отвечает: " + why


def refused_words(e: upstream.UpstreamRefused, conn) -> str:
    if isinstance(e, upstream.ConnectionChanged):
        return "подключение изменено — откройте заново"
    return upstream.refused_words(e, conn)


def key_of(response: httpx.Response) -> dict:
    code = response.status_code
    if code == 200:
        return {"state": "accepted", "code": code, "words": "принят"}
    if code == 401:
        return {"state": "refused", "code": code, "words": "не принят (401): введите токен заново"}
    if code == 403:
        return {"state": "limited", "code": code, "words": "принят, маршрут закрыт (403)"}
    reason = upstream.message(response)
    return {"state": "unknown", "code": code,
            "words": f"неясно: сервис ответил {code}" + (f" — {reason}" if reason else "")[:WORDS_MAX]}


UNVERIFIED = {"state": "unverified", "code": 200,
              "words": "сервис отдаёт список без ключа — ключ проверит первый запрос или «Пробный ответ»"}


def _row(url: str) -> dict:
    return {"path": httpx.URL(url).path, "ms": None, "status": None, "count": None}


async def _route(conn, url: str) -> "tuple[dict, httpx.Response | None]":
    row = _row(url)
    start = clock.NOW()
    try:
        r = await upstream.request(conn, "GET", url, timeout=CHECK_TIMEOUT_SEC)
    except upstream.UpstreamRefused as e:
        return row | {"reach": {"ok": False, "words": refused_words(e, conn)}, "key": None}, None
    except httpx.HTTPError as e:
        return row | {"reach": {"ok": False, "words": _down(e)}, "key": None}, None
    return row | {"ms": _ms(start), "status": r.status_code, "reach": {"ok": True, "words": "отвечает"},
                  "key": key_of(r)}, r


def _listed(row: dict, r: "httpx.Response | None") -> "list[dict] | None":
    """Модели ответа 200 (записи capabilities.model_entry); иначе None — списка нет."""
    if r is None or r.status_code != 200:
        return None
    data = upstream.json_body(r).get("data")
    entries = [e for e in map(capabilities.model_entry, data if isinstance(data, list) else []) if e][:MODELS_MAX]
    row["count"] = len(entries)
    return entries


def _urls(conn) -> list[str]:
    if conn.kind == "srvllm":
        return [connections.models_url(conn), connections.slot_url(conn, "/models")]
    urls = [connections.models_url(conn)]
    if capabilities.profile(conn) == "openrouter":
        urls.append(connections.models_url(conn).removesuffix("/models") + "/key")
    return urls


def passport(conn, source: str, routes: list[dict], models: list[dict], probes: dict) -> dict:
    return {"checked": clock.NOW(), "source": source, "profile": capabilities.profile(conn), "routes": routes,
            "models": capabilities.with_probes(models, probes), "probes": probes}


def outcome(routes: list[dict]) -> str:
    """Итог одним словом — для журнала."""
    if not all(r["reach"]["ok"] for r in routes):
        return "нет-ответа"
    if any((r["key"] or {}).get("state") == "refused" for r in routes):
        return "ключ-не-принят"
    return "ok"


def found_of(conn, chat: "list[str] | None", slot_ids: "list[str] | None", listed: "list[str] | None") -> dict:
    """Найденные модели разделов подключения (как после «Обновить модели»): srvllm — чат из `/v1/models`,
    фото и видео — короткие имена перечня, которые назвал `/slot/models`; openai — весь список."""
    out = {}
    for section in sections.sections_of(conn.user_id, conn.id):
        if conn.kind == "srvllm" and section in sections.CATALOG:
            if slot_ids is not None and section in ("photo", "video"):
                out[section] = [m for m in sections.CATALOG[section] if capabilities.GATEWAY_IDS[m] in slot_ids]
        elif conn.kind == "srvllm":
            if chat is not None:
                out[section] = chat
        elif listed is not None:
            out[section] = listed
    return out


async def check(conn) -> dict:
    """Проверка через сервер: `{"passport", "found"}`; паспорт сохраняется в подключении."""
    probes = (connections.passport_of(conn.id) or {}).get("probes") or {}
    if connections.dropped(conn):
        words = refused_words(upstream.UpstreamRefused("убран"), conn)
        routes = [_row(u) | {"reach": {"ok": False, "words": words}, "key": None} for u in _urls(conn)]
        return _save(conn, passport(conn, "server", routes, [], probes), {})
    if not conn.proxied:
        raise HTTPException(409, {"code": "direct", "message": "прямое подключение проверяет браузер"})
    urls = _urls(conn)
    first, r = await _route(conn, urls[0])
    listed = _listed(first, r)
    routes = [first]
    slot_ids = None
    if conn.kind == "srvllm":
        slot, r2 = await _route(conn, urls[1])
        slot_list = _listed(slot, r2)
        slot_ids = None if slot_list is None else [m["id"] for m in slot_list]
        routes.append(slot)
    else:
        if listed is not None:
            try:
                anon = await upstream.request(conn, "GET", urls[0], keyless=True, timeout=CHECK_TIMEOUT_SEC)
            except (httpx.HTTPError, upstream.UpstreamRefused):
                anon = None
            if anon is not None and anon.status_code == 200:
                first["key"] = dict(UNVERIFIED)
        for url in urls[1:]:
            routes.append((await _route(conn, url))[0])
    ids = None if listed is None else [m["id"] for m in listed]
    models = capabilities.models(conn.kind, listed or [], slot_ids, MODELS_MAX)
    return _save(conn, passport(conn, "server", routes, models, probes), found_of(conn, ids, slot_ids, ids))


def _save(conn, built: dict, found: dict) -> dict:
    connections.save_passport(conn.id, built)
    log.info("пользователь %d: подключение %d — проверка %s", conn.user_id, conn.id, outcome(built["routes"]))
    return {"passport": built, "found": found}


def _uid(request: Request) -> int:
    return request.state.user.id


@router.post("/{conn_id}/check")
async def post_check(conn_id: int, request: Request):
    return await check(connections.get(_uid(request), conn_id))
