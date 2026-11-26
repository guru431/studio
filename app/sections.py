"""Разделы студии (спека 2026-10-20-studio-sections-voice-design, раздел 6.1): у пользователя в
каждом разделе — свои подключения, отмеченные модели каждого и одна модель по умолчанию.

- Подключение — общий адрес с ключом (connections.py); раздел ссылается на него строкой
  `section_links` (отмеченные модели — JSON-список), умолчание раздела — `section_defaults`.
  Удалили подключение — его связи и умолчания уходят каскадом.
- Виды по разделам — `connections.KINDS`. Встроенный перечень srvllm — CATALOG (короткие имена
  студии, первое — по умолчанию); чат srvllm — из `GET /v1/models` шлюза, при подключении
  отмечается CHAT_INITIAL.
- Кто ходит: разделы SERVER_ONLY — только сервер и только к адресам из списка разрешённых; чат — как
  раньше (вне списка — браузер).
- После каждой правки умолчание приводится в порядок (`_settle`: неверное — первая отмеченная
  модель первого подключения раздела или никакой), а прежние столбцы `connections.sections` и
  `users.default_*` — к связям (`_sync_legacy`): прежний код после отката `install.sh` видит те же
  подключения в чате, фото и видео и ту же модель чата.
- База — синхронно, с цикла событий; правка — одной транзакцией (`transaction`). Журнал — только id.
"""
import json
import logging
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import access
import cleaner_route
import clock
import connections
import state

SECTIONS = ("chat", "photo", "video", "speech", "transcribe", "ocr", "translate", "dictation")   # порядок вкладок
NAMES = {"chat": "Проекты", "photo": "Фото", "video": "Видео", "speech": "Озвучка", "transcribe": "Расшифровка", "ocr": "Сканы",
         "translate": "Перевод", "dictation": "Голосовой ввод",
         "help": "Справка", "trash": "Корзина", "myfiles": "Мои файлы", "owui": "Open WebUI"}
SERVER_ONLY = {"photo", "video", "transcribe", "ocr", "translate", "speech", "dictation"}
LEGACY = ("chat", "photo", "video")           # разделы прежнего столбца connections.sections
CATALOG = {"photo": ["snofs", "flux", "fineview", "jib-moss"], "video": ["wan", "h3"], "transcribe": ["whisperx"], "ocr": ["hunyuan-ocr"],
           "speech": ["qwen3-tts", "f5-tts"], "dictation": ["gigaam", "whisperx"]}
CHAT_INITIAL = ["local-gemma-abl"]            # = manage.DEFAULT_MODEL
S06_CATALOG = {"chat": ["qwen-27b", "qwen-27b-fast"], "translate": ["qwen-27b", "qwen-27b-fast"],
               "transcribe": ["whisperx"], "ocr": ["ocr-ru"], "dictation": ["whisperx"]}
CLEANER = ("chat", "translate")               # разделы подключения Cleaner
LOCAL_PREFIX = "local-"                       # роли шлюза srvllm, что работают на его карте (is_local)
MODEL_MAX = 200
MODELS_MAX = 200
RETIRED_PHOTO_MODELS = {"lustify", "big-love", "muse", "fineporn", "moody-krea"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/sections")


@dataclass(frozen=True)
class Link:
    section: str
    connection: "connections.Connection"
    models: tuple[str, ...]


def _bad(code: str, message: str, status: int = 422) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def check_section(section: str) -> None:
    if section not in SECTIONS:
        raise _bad("section", "такого раздела нет", 404)


@contextmanager
def transaction():
    """Транзакция правки; внутри уже открытой (подключение создаётся вместе со связями) — та же."""
    db = state.DB
    if db.in_transaction:
        yield
        return
    db.execute("BEGIN")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


# ---- чтение ------------------------------------------------------------------------

def _rows(user_id: int | None, section: str, *, include_shared: bool = True, gate: bool = True):
    scope = "(user_id IS ? OR user_id IS NULL)" if include_shared else "user_id IS ?"
    clause, args = (access.shared_filter(user_id, "connection", "connection_id")
                    if user_id is not None and gate and include_shared else ("1", ()))
    return state.DB.execute(f"SELECT connection_id, models FROM section_links WHERE {scope} AND section = ?"
                            f" AND {clause} ORDER BY user_id IS NULL, created, rowid",
                            (user_id, section, *args)).fetchall()


def links(user_id: int | None, section: str, *, gate: bool = True) -> list[Link]:
    rows = _rows(user_id, section, gate=gate)
    if not rows:
        return []
    by_id = {c.id: c for c in connections.list_for(user_id, gate=gate)}
    return [Link(section, by_id[r["connection_id"]], tuple(json.loads(r["models"])))
            for r in rows if r["connection_id"] in by_id]


def sections_of(user_id: int | None, conn_id: int) -> list[str]:
    linked = {r["section"] for r in state.DB.execute(
        "SELECT section FROM section_links WHERE user_id IS ? AND connection_id = ?", (user_id, conn_id))}
    return [s for s in SECTIONS if s in linked]


def _default_row(user_id: int | None, section: str):
    if user_id is None:
        return None
    return state.DB.execute("SELECT connection_id, model FROM section_defaults WHERE user_id = ? AND section = ?",
                            (user_id, section)).fetchone()


def default(user_id: int | None, section: str, available: list[Link] | None = None) -> "tuple[connections.Connection, str] | None":
    available = links(user_id, section) if available is None else available
    row = _default_row(user_id, section)
    if row is not None:
        for link in available:
            conn = link.connection
            if conn.id == row["connection_id"] and row["model"] in link.models:
                if not conn.shared or not connections.dropped(conn):
                    return conn, row["model"]
    for link in available:
        if link.models and (not link.connection.shared or not connections.dropped(link.connection)):
            return link.connection, link.models[0]
    return None


def is_local(conn: "connections.Connection", model: str) -> bool:
    """Модель работает на карте студии — единственный признак локальной модели: подключение через сервер и шлюз s06
    (весь) или роль шлюза srvllm `local-*` (соглашение llm_routers). Остальные роли srvllm облачные, а адрес
    локальности не доказывает."""
    return conn.proxied and (conn.s06 or (conn.kind == "srvllm" and model.startswith(LOCAL_PREFIX)))


def transcript_local_models(conn: "connections.Connection", models: Iterable[str]) -> list[str]:
    """Модели для передачи расшифровки по внутренним IP шлюзов s06, srvllm и настроенного API Spark."""
    spark = conn.kind == "openai" and conn.base_url == state.CFG.spark_chat_url
    local = [model for model in models if spark or is_local(conn, model)]
    return local if local and not cleaner_route.is_external(conn) else []


def capabilities(user_id: int) -> dict[str, bool]:
    """Раздел готов: есть умолчание, а у раздела только через сервер — его адрес в списке; у чата — адрес не
    убран из списка (`connections.dropped`: ни сервер, ни браузер туда не пойдут)."""
    out = {}
    for section in SECTIONS:
        chosen = default(user_id, section)
        out[section] = chosen is not None and (chosen[0].proxied if section in SERVER_ONLY
                                               else not connections.dropped(chosen[0]))
    return out


def resolve(user_id: int, section: str, connection_id: "int | None",
            model: "str | None", *, gate: bool = True) -> "tuple[connections.Connection, str]":
    """Подключение и модель запроса раздела: ничего не задано — умолчание; только модель — первая
    связь (связь умолчания — первой), где она отмечена; только подключение — его умолчание или
    первая отмеченная; пара — связь с отмеченной моделью."""
    check_section(section)
    if connection_id is not None:
        connections.get(user_id, connection_id, gate=gate)    # чужое и неизвестное — 404 gone
    available = links(user_id, section, gate=gate)
    chosen = default(user_id, section, available)
    if connection_id is None and not model:
        if chosen is None:
            raise _bad("no_model", f"выберите модель в настройках раздела «{NAMES[section]}»", 409)
        conn, model = chosen
    else:
        found = sorted(available, key=lambda l: chosen is None or l.connection.id != chosen[0].id)
        if connection_id is not None:
            found = [l for l in found if l.connection.id == connection_id]
        if not model and found:
            model = chosen[1] if chosen and chosen[0].id == connection_id else (found[0].models or ("",))[0]
        link = next((l for l in found if model and model in l.models), None)
        if link is None:
            raise _bad("model", "этой модели нет среди отмеченных в настройках раздела")
        conn = link.connection
    if (section in SERVER_ONLY and not conn.proxied) or connections.dropped(conn):
        raise _bad("server", unlisted(conn), 409)
    cleaner_route.refuse_forced(conn, section, model)       # принудительный Cleaner: 409 `cleaner_forced`
    return conn, model


def unlisted(conn: "connections.Connection") -> str:
    """Слова: подключение раздела только через сервер есть, но его адрес убран из списка разрешённых."""
    return f"адрес подключения «{conn.name}» убран из списка разрешённых — попросите администратора внести его"


# ---- правка ------------------------------------------------------------------------

def _link_row(user_id: int | None, section: str, conn_id: int):
    return state.DB.execute("SELECT models FROM section_links WHERE user_id IS ? AND section = ? AND connection_id = ?",
                            (user_id, section, conn_id)).fetchone()


def accepts(conn: "connections.Connection") -> list[str]:
    """Разделы, которые подключение принимает по виду (Cleaner и подключение через Cleaner — только CLEANER, s06 — S06_CATALOG, иначе
    `connections.KINDS`); список разрешённых — отдельно (SERVER_ONLY). Страница по нему строит выбор «+ Адрес для раздела»."""
    kinds = S06_CATALOG if conn.s06 else connections.KINDS[conn.kind]
    return [s for s in SECTIONS if s in kinds and (not (conn.cleaner or conn.routed) or s in CLEANER)]


def check_link(conn: "connections.Connection", section: str) -> None:
    """Может ли подключение участвовать в разделе: принудительный Cleaner (внешнее — только чат и перевод, 409
    `cleaner_forced`), вид (`accepts`) и, для раздела только через сервер, список."""
    if section not in CLEANER:                  # роль srvllm связь не выбирает: её отказ — при запросе (`resolve`)
        cleaner_route.refuse_forced(conn, section, "")
    if section not in accepts(conn):
        if conn.cleaner:
            raise _bad("kind", "Cleaner поддерживает чат и перевод")
        if conn.routed:
            raise _bad("kind", "через Cleaner работают чат и перевод")
        if conn.s06:
            raise _bad("kind", "шлюз s06 поддерживает чат, перевод, расшифровку, сканы и голосовой ввод")
        raise _bad("kind", f"раздел «{NAMES[section]}» — только через шлюз srvllm")
    if not conn.proxied and (section in SERVER_ONLY or conn.shared):
        raise _bad("server", f"раздел «{NAMES[section]}» — только через сервер: попросите администратора"
                             " внести адрес в список разрешённых")


def link(user_id: int | None, section: str, conn_id: int) -> None:
    check_section(section)
    conn = connections.get(user_id, conn_id, own=True)
    check_link(conn, section)
    with transaction():
        if _link_row(user_id, section, conn_id) is not None:
            return
        models = catalog(conn, section)
        if models is None:
            models = CHAT_INITIAL if conn.kind == "srvllm" else []
        state.DB.execute("INSERT INTO section_links(user_id, section, connection_id, models, created)"
                         " VALUES (?, ?, ?, ?, ?)", (user_id, section, conn_id, json.dumps(models), clock.NOW()))
        _settle(user_id, section)
        _sync_legacy(user_id)
    log.info("владелец %s: подключение %d — в разделе %s", user_id, conn_id, section)


def unlink(user_id: int | None, section: str, conn_id: int) -> None:
    check_section(section)
    connections.get(user_id, conn_id, own=True)
    with transaction():
        state.DB.execute("DELETE FROM section_links WHERE user_id IS ? AND section = ? AND connection_id = ?",
                         (user_id, section, conn_id))
        _settle(user_id, section)
        _sync_legacy(user_id)
    log.info("владелец %s: подключение %d убрано из раздела %s", user_id, conn_id, section)


def _clean(conn: "connections.Connection", section: str, models: list[str]) -> list[str]:
    out = []
    for raw in models:
        model = raw.strip() if isinstance(raw, str) else ""
        if not model or len(model) > MODEL_MAX or model in out:
            raise _bad("model", f"модель — от 1 до {MODEL_MAX} знаков, без повторов")
        out.append(model)
    if len(out) > MODELS_MAX:
        raise _bad("model", f"отмечено больше {MODELS_MAX} моделей")
    choices = catalog(conn, section)
    if choices is not None and not set(out) <= set(choices):
        raise _bad("model", "у этого шлюза в этом разделе — " + ", ".join(choices))
    return out


def set_models(user_id: int | None, section: str, conn_id: int, models: list[str]) -> None:
    check_section(section)
    clean = _clean(connections.get(user_id, conn_id, own=True), section, models)
    if _link_row(user_id, section, conn_id) is None:
        raise _bad("link", "подключения нет в этом разделе", 404)
    with transaction():
        state.DB.execute("UPDATE section_links SET models = ? WHERE user_id IS ? AND section = ? AND connection_id = ?",
                         (json.dumps(clean), user_id, section, conn_id))
        _settle(user_id, section)
        _sync_legacy(user_id)
    log.info("владелец %s: раздел %s, подключение %d — отмечено моделей %d", user_id, section, conn_id, len(clean))


def set_default(user_id: int, section: str, conn_id: int, model: str) -> None:
    check_section(section)
    conn = connections.get(user_id, conn_id)
    if conn.shared and connections.dropped(conn):
        raise _bad("server", unlisted(conn), 409)
    row = _link_row(conn.owner_id, section, conn_id)
    if row is None or model not in json.loads(row["models"]):
        raise _bad("model", "по умолчанию — только отмеченная модель")
    with transaction():
        state.DB.execute("INSERT INTO section_defaults(user_id, section, connection_id, model) VALUES (?, ?, ?, ?)"
                         " ON CONFLICT(user_id, section) DO UPDATE SET connection_id = excluded.connection_id,"
                         " model = excluded.model", (user_id, section, conn_id, model))
        _sync_legacy(user_id)
    log.info("пользователь %d: раздел %s — умолчание у подключения %d", user_id, section, conn_id)


def set_sections(user_id: int | None, conn_id: int, wanted: list[str]) -> None:
    """Связи подключения — ровно эти разделы (поле `sections` прежнего API подключений)."""
    for section in wanted:
        check_section(section)
    have = set(sections_of(user_id, conn_id))
    with transaction():
        for section in [s for s in SECTIONS if s in have and s not in wanted]:
            unlink(user_id, section, conn_id)
        for section in [s for s in SECTIONS if s in wanted and s not in have]:
            link(user_id, section, conn_id)


def settle_all(user_id: int) -> None:
    """После удаления подключения: связи и умолчания ушли каскадом — порядок и прежние столбцы."""
    with transaction():
        for section in SECTIONS:
            _settle(user_id, section)
        _sync_legacy(user_id)


def retire_photo_models() -> None:
    """Prune retired srvllm choices and repair defaults without changing media history."""
    affected = set()
    with transaction():
        rows = state.DB.execute("SELECT l.user_id, l.connection_id, l.models FROM section_links l"
                                " JOIN connections c ON c.id=l.connection_id AND c.user_id IS l.user_id"
                                " WHERE l.section='photo' AND c.kind='srvllm'").fetchall()
        for row in rows:
            selected = json.loads(row["models"])
            models = [m for m in selected if m not in RETIRED_PHOTO_MODELS] or ["snofs"]
            uid, cid = row["user_id"], row["connection_id"]
            if models != selected:
                state.DB.execute("UPDATE section_links SET models=? WHERE user_id IS ? AND section='photo' AND connection_id=?",
                                 (json.dumps(models), uid, cid))
                affected.add(uid)
            chosen = _default_row(uid, "photo")
            if chosen and chosen["connection_id"] == cid and chosen["model"] in RETIRED_PHOTO_MODELS:
                replacement = "snofs" if "snofs" in models else models[0]
                state.DB.execute("UPDATE section_defaults SET model=? WHERE user_id=? AND section='photo'",
                                 (replacement, uid))
                affected.add(uid)
        for uid in affected:
            _settle(uid, "photo")
            _sync_legacy(uid)


def drop_cleaner_strays() -> None:
    """При запуске: связи Cleaner с разделами вне CLEANER (успели появиться до запрета в `check_link`) — прочь,
    умолчания и прежние столбцы — в порядок. Такие связи нерабочие: Cleaner обслуживает только чат и перевод.
    Прежний код после отката без этих строк просто не видит связи; созданные им снова уберёт следующий запуск."""
    affected = set()
    with transaction():
        rows = state.DB.execute(
            "SELECT l.user_id, l.section, l.connection_id FROM section_links l"
            " JOIN connections c ON c.id = l.connection_id AND c.user_id = l.user_id"
            f" WHERE c.cleaner = 1 AND l.section NOT IN ({', '.join('?' * len(CLEANER))})", CLEANER).fetchall()
        for row in rows:
            state.DB.execute("DELETE FROM section_links WHERE user_id = ? AND section = ? AND connection_id = ?",
                             (row["user_id"], row["section"], row["connection_id"]))
            _settle(row["user_id"], row["section"])
            affected.add(row["user_id"])
        for uid in affected:
            _sync_legacy(uid)
    if rows:
        log.info("связи Cleaner вне чата и перевода убраны: %d", len(rows))


def adopt(section: str, source: str) -> None:
    """Один раз на базу (флаг `settings` `adopted:<section>`, при старте службы): каждому пользователю — связи
    раздела `source`, чьё подключение идёт через сервер и вид которого допускает `section`, с теми же
    отмеченными моделями и порядком; умолчание `section` — умолчание `source`, если его подключение перенесено,
    иначе — `_settle`. Дальше разделы живут порознь: связь, которую пользователь убрал, второй вызов не вернёт.
    Новая база получает флаг тем же первым запуском — подключений у неё нет."""
    flag = f"adopted:{section}"
    moved = 0
    with transaction():
        if state.DB.execute("SELECT 1 FROM settings WHERE key = ?", (flag,)).fetchone() is not None:
            return
        users = [r[0] for r in state.DB.execute(
            "SELECT DISTINCT user_id FROM section_links WHERE section = ? AND user_id IS NOT NULL", (source,)).fetchall()]
        for user_id in users:
            by_id = {c.id: c for c in connections.list_for(user_id)}
            for row in state.DB.execute("SELECT connection_id, models, created FROM section_links"
                                        " WHERE user_id = ? AND section = ? ORDER BY created, rowid",
                                        (user_id, source)).fetchall():
                conn = by_id.get(row["connection_id"])
                if conn is None or not conn.proxied or section not in connections.KINDS[conn.kind]:
                    continue
                moved += state.DB.execute(
                    "INSERT OR IGNORE INTO section_links(user_id, section, connection_id, models, created)"
                    " VALUES (?, ?, ?, ?, ?)", (user_id, section, conn.id, row["models"], row["created"])).rowcount
            chosen = _default_row(user_id, source)
            if chosen is not None and _link_row(user_id, section, chosen["connection_id"]) is not None:
                state.DB.execute("INSERT INTO section_defaults(user_id, section, connection_id, model) VALUES (?, ?, ?, ?)"
                                 " ON CONFLICT(user_id, section) DO UPDATE SET connection_id = excluded.connection_id,"
                                 " model = excluded.model", (user_id, section, chosen["connection_id"], chosen["model"]))
            _settle(user_id, section)
        state.DB.execute("INSERT INTO settings(key, value) VALUES (?, '1')", (flag,))
    log.info("раздел %s: из %s перенесено связей %d", section, source, moved)


def _settle(user_id: int | None, section: str) -> None:
    if user_id is None:
        return
    row = _default_row(user_id, section)
    if row is not None:
        if any(r["connection_id"] == row["connection_id"] and row["model"] in json.loads(r["models"])
               for r in _rows(user_id, section, gate=False)):
            return
    state.DB.execute("DELETE FROM section_defaults WHERE user_id = ? AND section = ?", (user_id, section))
    for r in _rows(user_id, section, include_shared=False):
        models = json.loads(r["models"])
        if models:
            state.DB.execute("INSERT INTO section_defaults(user_id, section, connection_id, model) VALUES (?, ?, ?, ?)",
                             (user_id, section, r["connection_id"], models[0]))
            return


def _sync_legacy(user_id: int | None) -> None:
    if user_id is None:
        return
    for row in state.DB.execute("SELECT id FROM connections WHERE user_id = ?", (user_id,)).fetchall():
        linked = [s for s in sections_of(user_id, row["id"]) if s in LEGACY]
        state.DB.execute("UPDATE connections SET sections = ? WHERE id = ?", (json.dumps(sorted(linked)), row["id"]))
    chat = _default_row(user_id, "chat")
    if chat and _link_row(user_id, "chat", chat["connection_id"]) is None:
        chat = None  # The previous version only resolves personal connections.
    state.DB.execute("UPDATE users SET default_connection_id = ?, default_model = ? WHERE id = ?",
                     (chat["connection_id"] if chat else None, chat["model"] if chat else None, user_id))


# ---- API (профиль; панель администратора зовёт те же функции для `uid` пути) ----------

class LinkIn(BaseModel):
    connection_id: int


class ModelsIn(BaseModel):
    models: list[str] = Field(max_length=MODELS_MAX)


class DefaultIn(BaseModel):
    connection_id: int
    model: str = Field(max_length=MODEL_MAX)


def catalog(conn: "connections.Connection", section: str) -> list[str] | None:
    if conn.s06:
        return S06_CATALOG.get(section)
    return CATALOG.get(section) if conn.kind == "srvllm" else None


def _link_out(user_id: int | None, section: str, link: Link, passport: bool) -> dict:
    conn = link.connection
    item = connections.out(conn, passport)
    inherited = conn.shared and user_id is not None
    result = {"connection": item, "models": list(link.models),
              "catalog": None if inherited else catalog(conn, section),
              "local_models": [m for m in link.models if is_local(conn, m)]}
    if section == "chat":
        result["transcript_local_models"] = transcript_local_models(conn, link.models)
    if section == "chat" and passport:
        # Unverified models remain absent. Never expose the passport's routes, probes or ungranted model IDs.
        checked = connections.passport_of(conn) if inherited else item.get("passport")
        caps = {model["id"]: [cap["name"] for cap in model["caps"]]
                for model in (checked or {}).get("models", ()) if model["id"] in link.models}
        if conn.kind == "srvllm" and "local-gemma-abl" in link.models:
            # Existing passports predate the catalog entry for this role.
            caps["local-gemma-abl"] = list(dict.fromkeys([*caps.get("local-gemma-abl", ()), "vision"]))
        result["capabilities"] = caps
    return result


def section_out(user_id: int | None, section: str, passport: bool = True) -> dict:
    """passport=False — панель администратора: паспорт подключения туда не идёт."""
    available = links(user_id, section)
    chosen = default(user_id, section, available)
    return {"id": section, "name": NAMES[section],
            "links": [_link_out(user_id, section, link, passport) for link in available],
            "default": {"connection_id": chosen[0].id, "model": chosen[1]} if chosen else None}


def all_out(user_id: int | None, passport: bool = True) -> dict:
    return {"sections": [section_out(user_id, s, passport) for s in SECTIONS]}


async def found(user_id: int | None, section: str, conn_id: int) -> dict:
    """Найденные модели: srvllm вне чата — встроенный перечень; прочее — `GET …/models` через сервер.
    Адрес убран из списка — отказ словами и с перечнем (`connections.check_reachable`)."""
    check_section(section)
    conn = connections.get(user_id, conn_id, own=True)
    if connections.dropped(conn):
        connections.check_reachable(conn)
    choices = catalog(conn, section)
    if choices is not None:
        return {"items": list(choices)}
    return {"items": [m["id"] for m in await connections.fetch_models(conn)]}


def _uid(request: Request) -> int:
    return request.state.user.id


@router.get("")
async def get_sections(request: Request):
    return all_out(_uid(request))


@router.post("/{section}/links")
async def add_link(section: str, body: LinkIn, request: Request):
    link(_uid(request), section, body.connection_id)
    return section_out(_uid(request), section)


@router.delete("/{section}/links/{conn_id}", status_code=204)
async def remove_link(section: str, conn_id: int, request: Request):
    unlink(_uid(request), section, conn_id)
    return Response(status_code=204)


@router.put("/{section}/links/{conn_id}")
async def put_models(section: str, conn_id: int, body: ModelsIn, request: Request):
    set_models(_uid(request), section, conn_id, body.models)
    return section_out(_uid(request), section)


@router.put("/{section}/default")
async def put_default(section: str, body: DefaultIn, request: Request):
    set_default(_uid(request), section, body.connection_id, body.model)
    return section_out(_uid(request), section)


@router.get("/{section}/links/{conn_id}/found")
async def get_found(section: str, conn_id: int, request: Request):
    return await found(_uid(request), section, conn_id)
