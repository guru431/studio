"""Подключения профиля (спека, раздел 7.9): OpenAI-совместимые и шлюз srvllm.

- Токен хранится зашифрованным (security.encrypt); наружу — только `token_hint`.
- `proxied` — адрес в списке администратора (upstream.is_allowed): такие подключения сервер
  зовёт сам, их токен браузеру не отдаётся никогда. Прочие (прямые) браузер зовёт сам: им
  нужен https (кроме localhost) — токен прямого подключения отдаётся только его владельцу.
- `server_only` в базе липкий: ставится, когда токен сохранён при адресе через сервер, и
  снимается, только когда новый токен сохранён при прямом. Пока он стоит, `/token` — отказ,
  даже если адрес потом убрали из списка.
- Смена вида, схемы, хоста, порта или пути адреса (или того, идёт ли подключение через сервер) —
  только с новым токеном: иначе ключ шлюза ушёл бы на другой адрес или в браузер. Снимок
  подключения у задания после такой смены (и после удаления) ключа не получает — token().
- Через сервер путь IP-адреса фиксирован: srvllm — без пути, openai — без пути или `/v1`; у имени
  из списка (публичный https-хост) путь любой.
- srvllm — всегда через сервер: вне списка такое подключение не сохраняется.
- Разделы подключения — его связи в `section_links` (sections.py); поле `sections` POST и PATCH
  ставит связи ровно на эти разделы. Виды по разделам — KINDS.
- Журнал — только id: имена, адреса и токены туда не пишутся.
"""
import json
import logging
import re
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import clock
import sections
import security
import state
import upstream

KINDS = {"openai": {"chat", "photo", "transcribe", "speech", "dictation"},
         "srvllm": {"chat", "photo", "video", "transcribe", "speech", "dictation"}}
NAME_MAX = 64
URL_MAX = 2048
TOKEN_RE = r"[\x20-\x7e]{1,512}"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}   # их браузер считает безопасными и без https
PROXIED_PATHS = {"srvllm": {"/"}, "openai": {"/", "/v1"}}   # httpx.URL: пустой путь — «/»

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/connections")


@dataclass(frozen=True)
class Connection:
    id: int
    user_id: int
    name: str
    kind: str
    base_url: str
    sections: frozenset[str]
    proxied: bool
    server_only: bool = False


class ConnIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    kind: str = Field(max_length=16)
    base_url: str = Field(max_length=URL_MAX)
    token: str = Field(max_length=4096)
    sections: list[str] = Field(default_factory=list, max_length=8)


class ConnPatch(BaseModel):
    name: str | None = Field(None, max_length=NAME_MAX)
    kind: str | None = Field(None, max_length=16)
    base_url: str | None = Field(None, max_length=URL_MAX)
    token: str | None = Field(None, max_length=4096)
    sections: list[str] | None = Field(None, max_length=8)


def _gone() -> HTTPException:
    return HTTPException(404, {"code": "gone", "message": "такого подключения нет"})


def _from_row(row) -> Connection:
    return Connection(id=row["id"], user_id=row["user_id"], name=row["name"], kind=row["kind"],
                      base_url=row["base_url"], sections=frozenset(json.loads(row["linked"])),
                      proxied=upstream.is_allowed(row["base_url"]), server_only=bool(row["server_only"]))


_COLS = ("id, user_id, name, kind, base_url, server_only,"
         " (SELECT json_group_array(l.section) FROM section_links l WHERE l.connection_id = connections.id) AS linked")


def list_for(user_id: int) -> list[Connection]:
    rows = state.DB.execute(f"SELECT {_COLS} FROM connections WHERE user_id = ? ORDER BY id", (user_id,))
    return [_from_row(r) for r in rows]


def get(user_id: int, conn_id: int) -> Connection:
    row = state.DB.execute(f"SELECT {_COLS} FROM connections WHERE id = ? AND user_id = ?",
                           (conn_id, user_id)).fetchone()
    if row is None:
        raise _gone()
    return _from_row(row)


def token(conn: Connection) -> str:
    """Токен — только той строке, что в снимке `conn`: задание держит снимок, а ключ читается свежим.
    Строку удалили (или id занят чужой) — ConnectionChanged(deleted); сменились вид или адрес —
    ConnectionChanged: новый ключ не уходит прежнему адресу. Новый ключ тому же адресу — можно."""
    row = state.DB.execute("SELECT user_id, kind, base_url, token_enc FROM connections WHERE id = ?",
                           (conn.id,)).fetchone()
    if row is None or row["user_id"] != conn.user_id:
        raise upstream.ConnectionChanged(conn, deleted=True)
    if row["kind"] != conn.kind or _address(row["base_url"]) != _address(conn.base_url):
        raise upstream.ConnectionChanged(conn, deleted=False)
    return security.decrypt(row["token_enc"])


def _api(conn: Connection) -> str:
    return conn.base_url + ("/v1" if conn.kind == "srvllm" else "")


def chat_url(conn: Connection) -> str:
    return _api(conn) + "/chat/completions"


def models_url(conn: Connection) -> str:
    return _api(conn) + "/models"


def slot_url(conn: Connection, path: str) -> str:
    return f"{conn.base_url}/slot{path}"


def _direct_ok(base_url: str) -> bool:
    """Прямое подключение браузер откроет: https или петля."""
    url = httpx.URL(base_url)
    return url.scheme == "https" or url.host in LOCAL_HOSTS


def out(conn: Connection) -> dict:
    secret = token(conn)
    hint = "…" + secret[-4:] if len(secret) > 8 else "…"   # короткий токен по хвосту угадывается целиком
    return {"id": conn.id, "name": conn.name, "kind": conn.kind, "base_url": conn.base_url,
            "sections": [s for s in sections.SECTIONS if s in conn.sections], "proxied": conn.proxied,
            "token_hint": hint}


# ---- проверка при сохранении ---------------------------------------------------

def _bad(code: str, message: str) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message})


def check_name(text: str) -> str:
    name = text.strip()
    if not name:
        raise _bad("name", "нужно имя подключения")
    return name


def check_token(text: str) -> str:
    secret = text.strip()
    if not re.fullmatch(TOKEN_RE, secret):
        raise _bad("token", "токен — 1–512 печатных знаков ASCII")
    return secret


def check_base_url(kind: str, text: str) -> str:
    """Адрес без хвостового `/`; отказ 422, если подключение такого вида туда нельзя."""
    raw = text.strip().rstrip("/")
    try:
        url = httpx.URL(raw)
        url.host                     # имя IDNA httpx разбирает лениво: IDNAError — это UnicodeError
    except (httpx.InvalidURL, UnicodeError, ValueError):
        url = None
    if (url is None or url.scheme not in upstream.DEFAULT_PORTS or not url.host or url.userinfo
            or "?" in raw or "#" in raw):
        raise _bad("url", "адрес — http(s)://хост[:порт][/путь], без логина, ? и #")
    if upstream.is_allowed(raw):
        # ключ через сервер уходит только на известные пути, а не на любой префикс внутреннего хоста;
        # имя в списке — публичный хост целиком, путь у него любой
        if not upstream.named(url) and url.path not in PROXIED_PATHS.get(kind, {"/"}):
            raise _bad("path", "адрес через сервер — без пути" + (" или с /v1" if kind == "openai" else ""))
        return raw
    if kind == "srvllm":
        raise _bad("srvllm", "подключение srvllm — только к адресу из списка администратора")
    if not _direct_ok(raw):
        raise _bad("https", "адрес без https браузер не откроет; внутренний адрес должен быть в списке администратора")
    return raw


def check(kind: str, base_url: str, wanted: list[str]) -> tuple[str, list[str]]:
    if kind not in KINDS:
        raise _bad("kind", "вид подключения — openai или srvllm")
    if not set(wanted) <= KINDS[kind]:
        raise _bad("sections", "разделы — " + ", ".join(s for s in sections.SECTIONS if s in KINDS[kind]))
    return check_base_url(kind, base_url), [s for s in sections.SECTIONS if s in wanted]


# ---- общие действия (профиль и панель администратора) ------------------------

def create(user_id: int, body: ConnIn) -> dict:
    name, secret = check_name(body.name), check_token(body.token)
    base_url, wanted = check(body.kind, body.base_url, body.sections)
    with sections.transaction():               # подключение — вместе со связями или никак
        cid = state.DB.execute(
            "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only)"
            " VALUES (?, ?, ?, ?, ?, '[]', ?, ?)",
            (user_id, name, body.kind, base_url, security.encrypt(secret), clock.NOW(),
             int(upstream.is_allowed(base_url)))).lastrowid
        sections.set_sections(user_id, cid, wanted)
    log.info("пользователь %d: подключение %d создано", user_id, cid)
    return out(get(user_id, cid))


def _address(base_url: str) -> tuple:
    """Куда на самом деле идёт запрос: схема, хост, порт (с умолчанием) и путь."""
    url = httpx.URL(base_url)
    return url.scheme, url.host, url.port or upstream.DEFAULT_PORTS.get(url.scheme), url.path


def patch(user_id: int, conn_id: int, body: ConnPatch) -> dict:
    conn = get(user_id, conn_id)
    sent = body.model_fields_set
    changes = {}
    if "name" in sent and body.name is not None:
        changes["name"] = check_name(body.name)
    kind = body.kind if "kind" in sent and body.kind is not None else conn.kind
    base_url = body.base_url if "base_url" in sent and body.base_url is not None else conn.base_url
    wanted = body.sections if "sections" in sent and body.sections is not None else list(conn.sections)
    changes["base_url"], chosen = check(kind, base_url, wanted)      # вид, адрес и разделы — вместе
    changes["kind"] = kind
    proxied = upstream.is_allowed(changes["base_url"])
    if not proxied and set(chosen) & sections.SERVER_ONLY:
        raise _bad("server", "адрес вне списка разрешённых — уберите подключение из разделов, кроме чата,"
                             " или попросите администратора внести адрес")
    moved = (kind != conn.kind or proxied != conn.proxied
             or _address(changes["base_url"]) != _address(conn.base_url))
    if "token" in sent and body.token is not None:
        changes["token_enc"] = security.encrypt(check_token(body.token))
        changes["server_only"] = int(proxied)    # снимается только токеном, введённым для прямого
    elif moved:                                  # прежний ключ — только прежнему адресу
        raise _bad("token", "новый адрес — введите токен заново")
    cols = ", ".join(f"{k} = ?" for k in changes)                    # имена — из кода выше, не из запроса
    with sections.transaction():
        state.DB.execute(f"UPDATE connections SET {cols} WHERE id = ? AND user_id = ?",
                         (*changes.values(), conn_id, user_id))
        sections.set_sections(user_id, conn_id, chosen)
    log.info("пользователь %d: подключение %d изменено", user_id, conn_id)
    return out(get(user_id, conn_id))


def delete(user_id: int, conn_id: int) -> None:
    get(user_id, conn_id)
    with sections.transaction():               # связи и умолчания — каскадом, затем порядок в разделах
        state.DB.execute("DELETE FROM connections WHERE id = ? AND user_id = ?", (conn_id, user_id))
        sections.settle_all(user_id)
    log.info("пользователь %d: подключение %d удалено", user_id, conn_id)


# ---- API профиля -------------------------------------------------------------

def _uid(request: Request) -> int:
    return request.state.user.id


@router.get("")
async def list_connections(request: Request):
    return {"items": [out(c) for c in list_for(_uid(request))]}


@router.post("", status_code=201)
async def create_connection(body: ConnIn, request: Request):
    return create(_uid(request), body)


@router.patch("/{conn_id}")
async def patch_connection(conn_id: int, body: ConnPatch, request: Request):
    return patch(_uid(request), conn_id, body)


@router.delete("/{conn_id}", status_code=204)
async def delete_connection(conn_id: int, request: Request):
    delete(_uid(request), conn_id)
    return Response(status_code=204)


async def fetch_models(conn: Connection) -> list[dict]:
    """Модели подключения через сервер (`GET …/models`): `{"id"[, "context_length"]}`; прямое — 409,
    его список берёт браузер; нет связи или отказ — 502 словами."""
    if not conn.proxied:
        raise HTTPException(409, {"code": "direct", "message": "список моделей прямого подключения берёт браузер"})
    try:
        r = await upstream.request(conn, "GET", models_url(conn))
    except (httpx.HTTPError, upstream.UpstreamRefused):
        raise HTTPException(502, {"code": "upstream", "message": f"нет связи с подключением «{conn.name}»"}) from None
    if not r.is_success:
        raise HTTPException(502, {"code": "upstream", "message": upstream.gateway_error(r, conn.name)})
    data = upstream.json_body(r).get("data")
    items = []
    for m in data if isinstance(data, list) else []:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            item = {"id": m["id"]}
            if isinstance(m.get("context_length"), int):
                item["context_length"] = m["context_length"]
            items.append(item)
    return items


@router.get("/{conn_id}/models")
async def models(conn_id: int, request: Request):
    return {"items": await fetch_models(get(_uid(request), conn_id))}


@router.get("/{conn_id}/token")
async def get_token(conn_id: int, request: Request):
    conn = get(_uid(request), conn_id)
    # server_only, srvllm и адрес, который браузер сам не откроет, — тоже отказ: такое подключение
    # было через сервер, пока адрес не убрали из списка, и его токен браузеру не предназначался
    if conn.server_only or conn.proxied or conn.kind == "srvllm" or not _direct_ok(conn.base_url):
        raise HTTPException(403, {"code": "proxied", "message": "токен подключения через сервер не выдаётся"})
    # адреса — вместе с токеном: браузер несёт его только туда, а не по списку, который мог устареть
    return {"token": token(conn), "chat_url": chat_url(conn), "models_url": models_url(conn)}
