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
- `via_cleaner` — переключатель «Через Cleaner» (только внешние `openai`, разделы чат и перевод); `routed` — ход
  идёт через Cleaner (`cleaner_route.routes`: переключатель или принудительный режим). Такое подключение всегда
  `proxied` (сервер зовёт только Cleaner, провайдера проверяет Cleaner), его токен браузеру не отдаётся. Смена
  переключателя обнуляет паспорт и нового токена не требует.
- Через сервер путь IP-адреса фиксирован: srvllm — без пути, openai — без пути или `/v1`; у имени
  из списка (публичный https-хост) путь любой.
- srvllm — всегда через сервер: вне списка такое подключение не сохраняется.
- Разделы подключения — его связи в `section_links` (sections.py); поле `sections` POST и PATCH
  ставит связи ровно на эти разделы. Виды по разделам — KINDS.
- `dropped` — адрес убрали из списка, а подключение прямым не стало (srvllm, `server_only`, адрес без
  https): «Обновить модели» и «Проверить» говорят «убран из списка администратора», а не «нет связи».
  PATCH проверяет адрес (check_base_url), только если меняются вид или адрес: такое подключение можно
  переименовать и убрать из разделов, а новый раздел, кроме чата, ему — отказ `server`.
- Паспорт (`passport`, checks.py) — итог «Проверить» JSON; смена адреса, вида или токена его обнуляет;
  `out(…, passport=False)` — зеркала панели администратора.
- Профиль (profiles.py): `out` отдаёт подсказки (и у общих), `profile_set` — только у своих.
- Журнал — только id: имена, адреса и токены туда не пишутся.
"""
import json
import logging
import re
from dataclasses import dataclass, replace

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

import access
import cleaner_route
import clock
import sections
import security
import state
import upstream

KINDS = {"openai": {"chat", "photo", "transcribe", "translate", "speech", "dictation"},
         "srvllm": {"chat", "photo", "video", "transcribe", "ocr", "translate", "speech", "dictation"}}
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
    user_id: int | None
    name: str
    kind: str
    base_url: str
    sections: frozenset[str]
    proxied: bool
    server_only: bool = False
    cleaner: bool = False
    s06: bool = False
    shared: bool = False
    via_cleaner: bool = False
    routed: bool = False

    @property
    def owner_id(self) -> int | None:
        return None if self.shared else self.user_id


def _few_sections(wanted: list[str] | None) -> list[str] | None:
    # предел тела — число разделов этого кода; читается при запросе, а не при импорте: sections импортирует
    # этот модуль, и при обратном порядке импорта sections.SECTIONS ещё не было бы
    if wanted is not None and len(wanted) > len(sections.SECTIONS):
        raise ValueError("разделов больше, чем их есть")
    return wanted


class ConnIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    kind: str = Field(max_length=16)
    base_url: str = Field(max_length=URL_MAX)
    token: str = Field(max_length=4096)
    cleaner: bool = False
    s06: bool = False
    via_cleaner: bool = False
    sections: list[str] = Field(default_factory=list)
    _few = field_validator("sections")(classmethod(lambda cls, v: _few_sections(v)))


class ConnPatch(BaseModel):
    name: str | None = Field(None, max_length=NAME_MAX)
    kind: str | None = Field(None, max_length=16)
    base_url: str | None = Field(None, max_length=URL_MAX)
    token: str | None = Field(None, max_length=4096)
    cleaner: bool | None = None
    s06: bool | None = None
    via_cleaner: bool | None = None
    sections: list[str] | None = None
    _few = field_validator("sections")(classmethod(lambda cls, v: _few_sections(v)))


def _gone() -> HTTPException:
    return HTTPException(404, {"code": "gone", "message": "такого подключения нет"})


def _from_row(row, user_id: int | None) -> Connection:
    # раздел новее этого кода (откат после шага схемы с новым разделом) — не виден: PATCH без `sections`
    # иначе отказал бы 422 «разделы — …», а set_sections такие строки не трогает
    linked = frozenset(json.loads(row["linked"])) & frozenset(sections.SECTIONS)
    conn = Connection(id=row["id"], user_id=user_id, name=row["name"], kind=row["kind"],
                      base_url=row["base_url"], sections=linked,
                      proxied=upstream.is_allowed(row["base_url"]), server_only=bool(row["server_only"]),
                      cleaner=bool(row["cleaner"]), s06=bool(row["s06"]), shared=row["user_id"] is None,
                      via_cleaner=bool(row["via_cleaner"]))
    # подключение через Cleaner — всегда через сервер, даже если адреса провайдера нет в списке
    return replace(conn, routed=True, proxied=True) if cleaner_route.routes(conn) else conn


_COLS = ("id, user_id, name, kind, base_url, server_only, cleaner, s06, via_cleaner,"
         " (SELECT json_group_array(l.section) FROM section_links l WHERE l.connection_id = connections.id) AS linked")


def _gate(user_id: int | None, gate: bool) -> tuple[str, tuple]:
    """Общее подключение, закрытое учётке группами доступа (access.py), — как отсутствующее. Без фильтра
    (`gate=False`) — только фоновые пути работы, принятой до смены правил (снимок задания, release карты)."""
    if user_id is None or not gate:
        return "", ()
    clause, args = access.shared_filter(user_id, "connection", "connections.id")
    return f" AND {clause}", args


def list_for(user_id: int | None, *, gate: bool = True) -> list[Connection]:
    clause, args = _gate(user_id, gate)
    rows = state.DB.execute(f"SELECT {_COLS} FROM connections WHERE (user_id IS ? OR user_id IS NULL){clause}"
                            " ORDER BY user_id IS NULL, id", (user_id, *args))
    return [_from_row(r, user_id) for r in rows]


def get(user_id: int | None, conn_id: int, *, own: bool = False, gate: bool = True) -> Connection:
    scope = "user_id IS ?" if own else "(user_id IS ? OR user_id IS NULL)"
    clause, args = _gate(user_id, gate)
    row = state.DB.execute(f"SELECT {_COLS} FROM connections WHERE id = ? AND {scope}{clause}",
                           (conn_id, user_id, *args)).fetchone()
    if row is None:
        raise _gone()
    return _from_row(row, user_id)


def model_endpoint(conn: Connection) -> tuple:
    """Параметры получателя запросов. Имя карточки и список разделов модель не меняют."""
    return (conn.id, conn.owner_id, conn.kind, conn.base_url, conn.cleaner, conn.s06, conn.via_cleaner,
            conn.routed, conn.proxied)


def current_model_endpoint(conn: Connection) -> bool:
    """Снимок всё ещё на месте после ожидания ответа шлюза."""
    try:
        return model_endpoint(get(conn.user_id, conn.id, gate=False)) == model_endpoint(conn)
    except HTTPException:
        return False


def token(conn: Connection, *, route: bool = True) -> str:
    """Токен — только той строке, что в снимке `conn`: задание держит снимок, а ключ читается свежим.
    Строку удалили (или id занят чужой) — ConnectionChanged(deleted); сменились вид или адрес —
    ConnectionChanged: новый ключ не уходит прежнему адресу. Новый ключ тому же адресу — можно. Маршрут через
    Cleaner (`cleaner_route.routes`) сейчас не тот, что в снимке (`routed`), или флажок администратора включён, а
    подключение внешнее и не через Cleaner (внешний шлюз s06 или srvllm), — ConnectionChanged(route): работа,
    начатая до флажка, дальше не идёт. `route=False` — только подсказка ключа карточки (`out`), запроса нет."""
    row = state.DB.execute("SELECT user_id, kind, base_url, token_enc, cleaner, s06, via_cleaner FROM connections WHERE id = ?",
                           (conn.id,)).fetchone()
    if row is None or row["user_id"] != conn.owner_id:
        raise upstream.ConnectionChanged(conn, deleted=True)
    if (row["kind"] != conn.kind or bool(row["cleaner"]) != conn.cleaner or bool(row["s06"]) != conn.s06
            or bool(row["via_cleaner"]) != conn.via_cleaner or _address(row["base_url"]) != _address(conn.base_url)):
        raise upstream.ConnectionChanged(conn, deleted=False)
    # маршрут — по свежим флажку администратора и списку: снимок задания, взятый до включения флажка, ключ провайдеру
    # мимо Cleaner не понесёт, а взятый до выключения — в Cleaner (работа, идущая при смене, отказывает словами)
    if route and (cleaner_route.routes(conn) != conn.routed
                  or (not conn.routed and cleaner_route.forced() and cleaner_route.is_external(conn))):
        raise upstream.ConnectionChanged(conn, deleted=False, route=True)
    try:
        return security.decrypt(row["token_enc"])
    except InvalidToken:
        raise upstream.UpstreamRefused(f"подключение {conn.id}: токен не расшифрован", "token") from None


def _api(conn: Connection) -> str:
    if conn.cleaner:
        return conn.base_url + "/api/v1"
    return conn.base_url + ("/v1" if conn.kind == "srvllm" or conn.s06 else "")


def chat_url(conn: Connection) -> str:
    return _api(conn) + "/chat/completions"


def models_url(conn: Connection) -> str:
    return _api(conn) + "/models"


#: поля предела контекста в записи `GET …/models` по порядку: OpenRouter и подобные, vLLM, LiteLLM
CONTEXT_FIELDS = ("context_length", "max_model_len", "max_input_tokens")


def context_size(raw: dict) -> int | None:
    """Предел контекста модели из записи списка моделей: первое из CONTEXT_FIELDS — целое больше нуля."""
    for name in CONTEXT_FIELDS:
        value = raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def slot_url(conn: Connection, path: str) -> str:
    return f"{conn.base_url}/slot{path}"


def _direct_ok(base_url: str) -> bool:
    """Прямое подключение браузер откроет: https или петля."""
    url = httpx.URL(base_url)
    return url.scheme == "https" or url.host in LOCAL_HOSTS


def dropped(conn: Connection) -> bool:
    """Не через сервер, но и не прямое: адрес убрали из списка, а токен вводили для сервера (или это
    srvllm, или адрес без https) — браузеру его не выдать, серверу туда нельзя."""
    return not conn.proxied and (conn.server_only or conn.kind == "srvllm" or conn.s06 or not _direct_ok(conn.base_url))


def passport_of(conn: Connection) -> dict | None:
    row = state.DB.execute("SELECT passport FROM connections WHERE id = ? AND user_id IS ?",
                           (conn.id, conn.owner_id)).fetchone()
    return json.loads(row["passport"]) if row is not None and row["passport"] else None


#: строка подключения, какой её видела проверка: id, владелец, вид, адрес и ключ (шифром)
_SAME = "id = ? AND user_id IS ? AND kind = ? AND base_url = ? AND token_enc IS ? AND cleaner = ? AND s06 = ? AND via_cleaner = ?"


def snapshot(conn: Connection) -> tuple:
    """Снимок для паспорта: проверка ждёт ответа до десятков секунд, а за это время токен могли сменить
    (паспорт обнулён), подключение — удалить, а его id — отдать новому подключению другого пользователя."""
    row = state.DB.execute("SELECT token_enc FROM connections WHERE id = ? AND user_id IS ?",
                           (conn.id, conn.owner_id)).fetchone()
    return (conn.id, conn.owner_id, conn.kind, conn.base_url, row["token_enc"] if row is not None else None,
            int(conn.cleaner), int(conn.s06), int(conn.via_cleaner))


def passport_at(snap: tuple) -> dict | None:
    """Паспорт строки, если она всё ещё та, что в снимке; иначе None."""
    row = state.DB.execute(f"SELECT passport FROM connections WHERE {_SAME}", snap).fetchone()
    return json.loads(row["passport"]) if row is not None and row["passport"] else None


def save_passport(snap: tuple, passport: dict) -> bool:
    """Записать паспорт только той же строке, что в снимке; False — подключение изменили или удалили."""
    return state.DB.execute(f"UPDATE connections SET passport = ? WHERE {_SAME}",
                            (json.dumps(passport, ensure_ascii=False), *snap)).rowcount == 1


def out(conn: Connection, passport: bool = True) -> dict:
    """passport=False — зеркала панели администратора: паспорт чужого подключения туда не идёт."""
    inherited = conn.shared and conn.user_id is not None
    hint = ""
    if not inherited:
        try:
            secret = token(conn, route=False)
            hint = "…" + secret[-4:] if len(secret) > 8 else "…"
        except upstream.UpstreamRefused as e:
            if e.reason != "token":
                raise
            hint = "нужно ввести заново"
    item = {"id": conn.id, "name": conn.name, "kind": conn.kind, "base_url": "" if inherited else conn.base_url,
            "sections": [s for s in sections.SECTIONS if s in conn.sections], "accepts": sections.accepts(conn),
            "proxied": conn.proxied,
            "dropped": dropped(conn), "token_hint": hint, "cleaner": conn.cleaner, "s06": conn.s06,
            "via_cleaner": conn.via_cleaner, "routed": conn.routed}
    external = cleaner_route.is_external(conn)
    item["external"] = external
    # переключатель есть только у `openai` (не шлюза s06): внешний шлюз s06 или srvllm через Cleaner не идёт
    switchable = external and conn.kind == "openai" and not conn.s06
    item["cleaner_locked"] = switchable and cleaner_route.forced()
    # переключатель «Через Cleaner» на странице: внешний `openai`, пока Cleaner настроен (включённый — и без него,
    # чтобы его можно было выключить)
    item["cleaner_offer"] = switchable and (conn.via_cleaner or cleaner_route.configured())
    # profiles импортирует capabilities → images, а тот при загрузке берёт `connections.Connection`
    import profiles
    item["suggestions"] = profiles.suggestions_of(conn)
    item["effort"] = profiles.effort_ok(conn)    # выбор уровня рассуждения у нового диалога (спека п. 40)
    if not inherited:                            # текст и параметры общего профиля пользователю не видны
        item["profile_set"] = profiles.is_set(conn)
    if conn.shared:
        item["shared"] = True
    if passport and not inherited:
        item["passport"] = passport_of(conn)
    return item


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


def check_sections(kind: str, wanted: list[str], s06: bool = False) -> list[str]:
    if kind not in KINDS:
        raise _bad("kind", "вид подключения — openai или srvllm")
    supported = set(sections.S06_CATALOG) if s06 else KINDS[kind]
    if not set(wanted) <= supported:
        raise _bad("sections", "разделы — " + ", ".join(s for s in sections.SECTIONS if s in supported))
    return [s for s in sections.SECTIONS if s in wanted]


# ---- общие действия (профиль и панель администратора) ------------------------

def _check_shared(user_id: int | None, base_url: str, cleaner: bool) -> None:
    if user_id is None and (cleaner or not upstream.is_allowed(base_url)):
        raise _bad("server", "общий API моделей — через сервер по адресу из списка разрешённых, кроме Cleaner")


def create(user_id: int | None, body: ConnIn) -> dict:
    name, secret = check_name(body.name), check_token(body.token)
    wanted = check_sections(body.kind, body.sections, body.s06)
    base_url = check_base_url(body.kind, body.base_url)
    _check_shared(user_id, base_url, body.cleaner)
    if body.s06:
        _check_s06(body.kind, base_url, body.cleaner)
    if body.cleaner:
        _check_cleaner(body.kind, base_url, wanted)
    if body.via_cleaner:
        _check_via_cleaner(body.kind, base_url, body.cleaner, body.s06, wanted)
    with sections.transaction():               # подключение — вместе со связями или никак
        cid = state.DB.execute(
            "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, cleaner, s06,"
            " via_cleaner) VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
            (user_id, name, body.kind, base_url, security.encrypt(secret), clock.NOW(),
             int(upstream.is_allowed(base_url)), int(body.cleaner), int(body.s06), int(body.via_cleaner))).lastrowid
        sections.set_sections(user_id, cid, wanted)
    log.info("владелец %s: подключение %d создано", user_id, cid)
    return out(get(user_id, cid))


def _address(base_url: str) -> tuple:
    """Куда на самом деле идёт запрос: схема, хост, порт (с умолчанием) и путь."""
    url = httpx.URL(base_url)
    return url.scheme, url.host, url.port or upstream.DEFAULT_PORTS.get(url.scheme), url.path


def _check_cleaner(kind: str, base_url: str, wanted: list[str]) -> None:
    if kind != "openai" or set(wanted) - set(sections.CLEANER):
        raise _bad("cleaner", "Cleaner поддерживает чат и перевод")
    if not upstream.is_allowed(base_url):
        raise _bad("cleaner", "адрес Cleaner должен быть в списке администратора; запросы идут только через сервер")
    if httpx.URL(base_url).path != "/":
        raise _bad("path", "адрес Cleaner — без пути /api/v1")


def _check_via_cleaner(kind: str, base_url: str, cleaner: bool, s06: bool, wanted: list[str]) -> None:
    """Переключатель «Через Cleaner»: только внешнее подключение вида openai и только чат и перевод."""
    probe = Connection(0, None, "", kind, base_url, frozenset(), False, cleaner=cleaner, s06=s06)
    if kind != "openai" or cleaner or s06 or not cleaner_route.is_external(probe):
        raise _bad("via_cleaner", "«Через Cleaner» — только для внешних подключений вида openai;"
                                  " внутренним (srvllm, s06, Cleaner, частный адрес из списка) он не нужен")
    if set(wanted) - set(sections.CLEANER):
        raise _bad("via_cleaner", "через Cleaner работают чат и перевод — уберите подключение из прочих разделов")


def _check_s06(kind: str, base_url: str, cleaner: bool) -> None:
    if kind != "openai" or cleaner:
        raise _bad("s06", "шлюз s06 — отдельный вид OpenAI-совместимого подключения")
    if not upstream.is_allowed(base_url):
        raise _bad("s06", "адрес шлюза s06 должен быть в списке администратора")
    if httpx.URL(base_url).path != "/":
        raise _bad("path", "адрес шлюза s06 — без пути /v1")


def patch(user_id: int | None, conn_id: int, body: ConnPatch) -> dict:
    conn = get(user_id, conn_id, own=True)
    sent = body.model_fields_set
    changes = {}
    if "name" in sent and body.name is not None:
        changes["name"] = check_name(body.name)
    kind = body.kind if "kind" in sent and body.kind is not None else conn.kind
    base_url = body.base_url if "base_url" in sent and body.base_url is not None else conn.base_url
    wanted = body.sections if "sections" in sent and body.sections is not None else list(conn.sections)
    s06 = body.s06 if body.s06 is not None else conn.s06
    chosen = check_sections(kind, wanted, s06)                      # разделы — против вида всегда
    cleaner = body.cleaner if body.cleaner is not None else conn.cleaner
    if s06 and (not conn.s06 or kind != conn.kind or cleaner != conn.cleaner
                or base_url.strip().rstrip("/") != conn.base_url):
        _check_s06(kind, base_url.strip().rstrip("/"), cleaner)
    changes["s06"] = int(s06)
    if cleaner and (not (kind == conn.kind and base_url == conn.base_url and set(chosen) <= conn.sections)
                    or not conn.cleaner):
        _check_cleaner(kind, base_url.strip().rstrip("/"), chosen)
    changes["cleaner"] = int(cleaner)
    via = body.via_cleaner if body.via_cleaner is not None else conn.via_cleaner
    if body.via_cleaner is not None and cleaner_route.forced() and cleaner_route.is_external(conn):
        raise _bad("via_cleaner", "администратор требует Cleaner для внешних моделей: переключатель заблокирован")
    # вид и адрес прежние — адрес не перепроверяется: его могли убрать из списка (dropped), и тогда отказ
    # получало бы любое изменение, даже имя. Серверные разделы такому — только прежние, новых нет
    kept = kind == conn.kind and base_url.strip().rstrip("/") == conn.base_url
    changes["base_url"] = conn.base_url if kept else check_base_url(kind, base_url)
    changes["kind"] = kind
    proxied = upstream.is_allowed(changes["base_url"])
    if via and (not conn.via_cleaner or not kept or cleaner != conn.cleaner or s06 != conn.s06
                or not set(chosen) <= conn.sections):     # прежнее включённое не перепроверяется: адрес мог стать внутренним
        _check_via_cleaner(kind, changes["base_url"], cleaner, s06, chosen)
    changes["via_cleaner"] = int(via)
    if not kept or cleaner or body.token is not None:
        _check_shared(user_id, changes["base_url"], cleaner)
    after = Connection(conn.id, conn.user_id, conn.name, kind, changes["base_url"], frozenset(chosen), proxied,
                       cleaner=cleaner, s06=s06, shared=conn.shared, via_cleaner=via)
    reachable = proxied or cleaner_route.routes(after)          # через Cleaner — серверные разделы чата и перевода открыты
    if not reachable and (set(chosen) - (conn.sections if kept else set())) & sections.SERVER_ONLY:
        raise _bad("server", "адрес вне списка разрешённых — уберите подключение из разделов, кроме чата,"
                             " или попросите администратора внести адрес")
    moved = (kind != conn.kind or cleaner != conn.cleaner or s06 != conn.s06
             or proxied != upstream.is_allowed(conn.base_url)      # список, а не `conn.proxied`: переключатель ключ не меняет
             or _address(changes["base_url"]) != _address(conn.base_url))
    if "token" in sent and body.token is not None:
        changes["token_enc"] = security.encrypt(check_token(body.token))
        changes["server_only"] = int(proxied)    # снимается только токеном, введённым для прямого
    elif moved:                                  # прежний ключ — только прежнему адресу
        raise _bad("token", "новый адрес — введите токен заново")
    if ("token_enc" in changes or kind != conn.kind or cleaner != conn.cleaner or s06 != conn.s06
            or via != conn.via_cleaner or changes["base_url"] != conn.base_url):
        changes["passport"] = None               # проверяли другой адрес, вид или ключ
    cols = ", ".join(f"{k} = ?" for k in changes)                    # имена — из кода выше, не из запроса
    with sections.transaction():
        state.DB.execute(f"UPDATE connections SET {cols} WHERE id = ? AND user_id IS ?",
                         (*changes.values(), conn_id, user_id))
        sections.set_sections(user_id, conn_id, chosen)
        if not kept or cleaner != conn.cleaner or s06 != conn.s06 or via != conn.via_cleaner:
            state.DB.execute("DELETE FROM model_facts WHERE connection_id = ?", (conn_id,))
    log.info("владелец %s: подключение %d изменено", user_id, conn_id)
    return out(get(user_id, conn_id))


def delete(user_id: int | None, conn_id: int) -> None:
    get(user_id, conn_id, own=True)
    with sections.transaction():               # связи и умолчания — каскадом, затем порядок в разделах
        state.DB.execute("DELETE FROM connections WHERE id = ? AND user_id IS ?", (conn_id, user_id))
        # диалог без подключения просит выбрать новое; ждущие задания разделов отказывают сами (id не повторяется)
        state.DB.execute("UPDATE chats SET connection_id = NULL WHERE connection_id = ? AND (? IS NULL OR user_id = ?)",
                         (conn_id, user_id, user_id))
        if user_id is not None:
            sections.settle_all(user_id)
    log.info("владелец %s: подключение %d удалено", user_id, conn_id)


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


def refused(conn: Connection, e: upstream.UpstreamRefused) -> HTTPException:
    return HTTPException(409, {"code": "refused", "message": upstream.refused_words(e, conn)})


def check_reachable(conn: Connection) -> None:
    """Подключение, убранное из списка, — 409 `refused` этими словами; прямое — 409 `direct`."""
    if dropped(conn):
        raise refused(conn, upstream.UpstreamRefused(f"подключение {conn.id}: адрес не из списка администратора"))
    if not conn.proxied:
        raise HTTPException(409, {"code": "direct", "message": "список моделей прямого подключения берёт браузер"})


async def fetch_models(conn: Connection) -> list[dict]:
    """Модели подключения через сервер (`GET …/models`): `{"id"[, "context_length"]}` (предел — `context_size`); прямое — 409,
    его список берёт браузер; адрес убран из списка (или имя ведёт внутрь) — 409 `refused` словами;
    нет связи или отказ сервиса — 502 словами."""
    check_reachable(conn)
    if conn.routed:                                  # через Cleaner: провайдеру мимо него не ходим
        return await cleaner_route.models(conn)
    try:
        r = await upstream.request(conn, "GET", models_url(conn))
    except upstream.UpstreamRefused as e:
        raise refused(conn, e) from None
    except httpx.HTTPError:
        raise HTTPException(502, {"code": "upstream", "message": f"нет связи с подключением «{conn.name}»"}) from None
    if not r.is_success:
        raise HTTPException(502, {"code": "upstream", "message": upstream.gateway_error(r, conn)})
    return model_items(upstream.json_body(r).get("data"))


def model_items(data) -> list[dict]:
    """Записи `data` списка моделей — `{"id"[, "context_length"]}`; прочее пропускается."""
    items = []
    for m in data if isinstance(data, list) else []:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            item = {"id": m["id"]}
            if (length := context_size(m)) is not None:
                item["context_length"] = length
            items.append(item)
    return items


@router.get("/{conn_id}/models")
async def models(conn_id: int, request: Request):
    return {"items": await fetch_models(get(_uid(request), conn_id, own=True))}


@router.get("/{conn_id}/token")
async def get_token(conn_id: int, request: Request):
    conn = get(_uid(request), conn_id)
    # server_only, srvllm и адрес, который браузер сам не откроет, — тоже отказ: такое подключение
    # было через сервер, пока адрес не убрали из списка, и его токен браузеру не предназначался
    if conn.shared or conn.server_only or conn.proxied or conn.kind == "srvllm" or conn.s06 or not _direct_ok(conn.base_url):
        raise HTTPException(403, {"code": "proxied", "message": "токен подключения через сервер не выдаётся"})
    # адреса — вместе с токеном: браузер несёт его только туда, а не по списку, который мог устареть
    try:
        secret = token(conn)
    except upstream.UpstreamRefused as e:
        raise refused(conn, e) from None
    return {"token": secret, "chat_url": chat_url(conn), "models_url": models_url(conn)}
