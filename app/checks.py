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
- Прямое подключение проверяет браузер и присылает итог `PUT …/passport`: сервер проверяет форму
  (пределы), ставит своё время, `source: browser` и сам выводит возможности.
- «Пробный ответ» (`POST …/probe`, браузером — полем `probes` того же PUT) — единственная тратящая проба:
  одно короткое сообщение отмеченной модели чата не-srvllm подключения.
- Журнал — только id и итог одним словом: ни адреса, ни моделей, ни слов сервиса.
"""
import logging
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import capabilities
import clock
import connections
import sections
import upstream

CHECK_TIMEOUT_SEC = 10
PROBE_TIMEOUT_SEC = 60
PROBE_TEXT = "Ответь одним словом: ok"
PROBE_TOKENS = 16
MODELS_MAX = 500
ROUTES_MAX = 4
WORDS_MAX = 300

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/connections")


def _ms(start: float) -> int:
    return max(0, round((clock.NOW() - start) * 1000))


def _down(e: httpx.HTTPError, seconds: int = CHECK_TIMEOUT_SEC) -> str:
    if isinstance(e, httpx.TimeoutException):
        why = f"нет ответа за {seconds} с"
    elif isinstance(e, httpx.ConnectError):
        why = "соединение не установлено"
    else:
        why = f"связь оборвалась ({type(e).__name__})"
    return "не отвечает: " + why


def refused_words(e: upstream.UpstreamRefused, conn) -> str:
    if isinstance(e, upstream.ConnectionChanged):
        return "подключение удалено — откройте заново" if e.deleted else "подключение изменено — откройте заново"
    return upstream.refused_words(e, conn)


#: 401 и 403 — своими словами, без текста сервиса: бывает, что он повторяет часть ключа
KEY_WORDS = {401: "не принят (401): введите токен заново", 403: "принят, маршрут закрыт (403)"}


def key_of(response: httpx.Response) -> dict:
    code = response.status_code
    if code == 200:
        return {"state": "accepted", "code": code, "words": "принят"}
    if code == 401:
        return {"state": "refused", "code": code, "words": KEY_WORDS[401]}
    if code == 403:
        return {"state": "limited", "code": code, "words": KEY_WORDS[403]}
    reason = upstream.message(response)
    return {"state": "unknown", "code": code,
            "words": (f"неясно: сервис ответил {code}" + (f" — {reason}" if reason else ""))[:WORDS_MAX]}


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
                out[section] = [m for m in sections.CATALOG[section] if capabilities.GATEWAY_IDS.get(m) in slot_ids]
        elif conn.kind == "srvllm":
            if chat is not None:
                out[section] = chat
        elif listed is not None:
            out[section] = listed
    return out


def _probes(snap: tuple) -> dict:
    """Пробы паспорта — читаются перед самой записью (после ожиданий): проба, кончившаяся во время
    проверки, не теряется."""
    return (connections.passport_at(snap) or {}).get("probes") or {}


async def check(conn) -> dict:
    """Проверка через сервер: `{"passport", "found"}`; паспорт сохраняется в подключении, если оно за время
    проверки не изменилось (снимок connections.snapshot), иначе 409 `changed`."""
    snap = connections.snapshot(conn)
    if connections.dropped(conn):
        words = refused_words(upstream.UpstreamRefused("убран"), conn)
        routes = [_row(u) | {"reach": {"ok": False, "words": words}, "key": None} for u in _urls(conn)]
        return _save(conn, snap, passport(conn, "server", routes, [], _probes(snap)), {})
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
    return _save(conn, snap, passport(conn, "server", routes, models, _probes(snap)),
                 found_of(conn, ids, slot_ids, ids))


def _changed() -> HTTPException:
    return HTTPException(409, {"code": "changed", "message": "подключение изменено, пока шла проверка — проверьте заново"})


def _save(conn, snap: tuple, built: dict, found: dict) -> dict:
    if not connections.save_passport(snap, built):
        raise _changed()
    log.info("пользователь %d: подключение %d — проверка %s", conn.user_id, conn.id, outcome(built["routes"]))
    return {"passport": built, "found": found}


# ---- пробный ответ -----------------------------------------------------------------

def _chat_models(conn) -> list[str]:
    return next((list(l.models) for l in sections.links(conn.user_id, "chat") if l.connection.id == conn.id), [])


def _checked_model(conn, model: str) -> None:
    if model not in _chat_models(conn):
        raise HTTPException(422, {"code": "model", "message": "пробный ответ — только у отмеченной модели чата"})


def _with_probes(conn, snap: tuple, source: str, fresh: dict) -> dict:
    """Пробы — в паспорт подключения (той же строки, что в снимке); паспорта нет — паспорт без проверки
    (`checked` None)."""
    base = connections.passport_at(snap) or {"checked": None, "source": source,
                                             "profile": capabilities.profile(conn), "routes": [], "models": [],
                                             "probes": {}}
    probes = {**(base.get("probes") or {}), **fresh}
    return base | {"probes": probes, "models": capabilities.with_probes(base["models"], probes)}


def _refusal(r: httpx.Response) -> str:
    if r.status_code in KEY_WORDS:
        return f"не ответила: ключ {KEY_WORDS[r.status_code]}"
    reason = upstream.message(r)
    return f"не ответила: код {r.status_code}" + (f" — {reason}" if reason else "")


async def probe(conn, model: str) -> dict:
    """`chat/completions` одним коротким сообщением — единственная тратящая проба, по отдельной кнопке.
    У srvllm её нет: роль local-gemma-abl заняла бы карту арендой, а release проверка не делает."""
    if conn.kind == "srvllm":
        raise HTTPException(409, {"code": "srvllm", "message": "у шлюза srvllm пробного ответа нет: он занял бы карту"})
    if conn.cleaner:
        raise HTTPException(409, {"code": "cleaner", "message": "Пробный ответ Cleaner — через предпросмотр в чате"})
    if connections.dropped(conn):
        raise connections.refused(conn, upstream.UpstreamRefused("убран"))
    if not conn.proxied:
        raise HTTPException(409, {"code": "direct", "message": "пробный ответ прямого подключения отправляет браузер"})
    if model.startswith(sections.LOCAL_PREFIX):
        # роль local-* шлюза, к которому ходят как к OpenAI-совместимому: та же аренда карты, что у srvllm
        raise HTTPException(409, {"code": "srvllm", "message": "у роли local-* пробного ответа нет: он занял бы карту srvllm"})
    _checked_model(conn, model)
    snap = connections.snapshot(conn)
    body = {"model": model, "messages": [{"role": "user", "content": PROBE_TEXT}], "max_tokens": PROBE_TOKENS,
            "stream": False}
    start = clock.NOW()
    try:
        r = await upstream.request(conn, "POST", connections.chat_url(conn), json=body, timeout=PROBE_TIMEOUT_SEC)
        if r.status_code == 400 and "max_tokens" in (upstream.message(r) or ""):
            # модели рассуждений OpenAI: max_tokens не принимают — один повтор
            body = {k: v for k, v in body.items() if k != "max_tokens"} | {"max_completion_tokens": PROBE_TOKENS}
            r = await upstream.request(conn, "POST", connections.chat_url(conn), json=body, timeout=PROBE_TIMEOUT_SEC)
    except upstream.UpstreamRefused as e:
        result = {"ok": False, "ms": None, "words": refused_words(e, conn)}
    except httpx.HTTPError as e:
        result = {"ok": False, "ms": None, "words": _down(e, PROBE_TIMEOUT_SEC).replace("не отвечает", "не ответила")}
    else:
        ms = _ms(start)
        result = {"ok": r.status_code == 200, "ms": ms,
                  "words": f"ответила за {ms} мс" if r.status_code == 200 else _refusal(r)[:WORDS_MAX]}
    result |= {"at": clock.NOW(), "source": "server"}
    built = _with_probes(conn, snap, "server", {model: result})
    if not connections.save_passport(snap, built):
        raise _changed()
    log.info("пользователь %d: подключение %d — пробный ответ %s", conn.user_id, conn.id,
             "ok" if result["ok"] else "отказ")
    return {"passport": built, "probe": result}


# ---- паспорт браузера --------------------------------------------------------------

Words = Annotated[str, Field(max_length=WORDS_MAX)]
ModelId = Annotated[str, Field(min_length=1, max_length=capabilities.ID_MAX)]
Modality = Annotated[str, Field(max_length=32)]


class ReachIn(BaseModel):
    ok: bool
    words: Words


class KeyIn(BaseModel):
    state: Literal["accepted", "refused", "limited", "unverified", "unknown"]
    code: int | None = Field(None, ge=100, le=599)
    words: Words


class RouteIn(BaseModel):
    path: str = Field(max_length=connections.URL_MAX)        # путь прямого адреса бывает длинным
    ms: int | None = Field(None, ge=0, le=3_600_000)
    status: int | None = Field(None, ge=100, le=599)
    count: int | None = Field(None, ge=0, le=1_000_000)
    reach: ReachIn
    key: KeyIn | None = None


class ArchIn(BaseModel):
    input_modalities: list[Modality] = Field(default_factory=list, max_length=16)
    output_modalities: list[Modality] = Field(default_factory=list, max_length=16)


class ModelIn(BaseModel):
    id: ModelId
    context_length: int | None = Field(None, ge=0)
    architecture: ArchIn | None = None


class ProbeIn(BaseModel):
    ok: bool
    ms: int | None = Field(None, ge=0, le=3_600_000)
    words: Words


class PassportIn(BaseModel):
    """Итог проверки браузером; время, источник и возможности ставит сервер."""
    routes: list[RouteIn] | None = Field(None, max_length=ROUTES_MAX)
    models: list[ModelIn] | None = Field(None, max_length=MODELS_MAX)
    probes: dict[ModelId, ProbeIn] | None = Field(None, max_length=16)


class ProbeBody(BaseModel):
    model: ModelId


def put_passport(conn, body: PassportIn) -> dict:
    """Проверка браузером — только у прямого подключения: подключение через сервер (или убранное из
    списка) браузер «проверенным» не сделает. Присланное — `source: browser`."""
    if conn.proxied or connections.dropped(conn):
        raise HTTPException(409, {"code": "server", "message": "это подключение проверяет сервер"})
    for model in body.probes or {}:
        _checked_model(conn, model)
    fresh = {m: p.model_dump() | {"at": clock.NOW(), "source": "browser"} for m, p in (body.probes or {}).items()}
    found = {}
    snap = connections.snapshot(conn)
    if body.routes is None and body.models is None:
        built = _with_probes(conn, snap, "browser", fresh)
    else:
        probes = {**_probes(snap), **fresh}
        listed = [capabilities.model_entry(m.model_dump(exclude_none=True)) for m in body.models or []]
        built = passport(conn, "browser", [r.model_dump() for r in body.routes or []],
                         capabilities.models(conn.kind, listed, None, MODELS_MAX), probes)
        if body.models is not None:
            ids = [m["id"] for m in listed]
            found = found_of(conn, ids, None, ids)
    if not connections.save_passport(snap, built):
        raise _changed()
    log.info("пользователь %d: подключение %d — проверка браузером %s", conn.user_id, conn.id,
             outcome(built["routes"]) if body.routes is not None else "пробный-ответ")
    return {"passport": built, "found": found}


def _uid(request: Request) -> int:
    return request.state.user.id


@router.post("/{conn_id}/check")
async def post_check(conn_id: int, request: Request):
    return await check(connections.get(_uid(request), conn_id))


@router.put("/{conn_id}/passport")
async def put_passport_route(conn_id: int, body: PassportIn, request: Request):
    return put_passport(connections.get(_uid(request), conn_id), body)


@router.post("/{conn_id}/probe")
async def post_probe(conn_id: int, body: ProbeBody, request: Request):
    return await probe(connections.get(_uid(request), conn_id), body.model)
