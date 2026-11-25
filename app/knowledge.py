"""Знания (спека 2026-11-21-studio-source-capabilities-design, п. 15): поиск по большим вложениям чата и базы знаний.

- Документ — строка `kb_documents`: базы (`kb_id`) или большого вложения диалога (`attachment_id`). Его текст режется на
  куски `chunks` (CHUNK_CHARS знаков с перекрытием OVERLAP) — `kb_chunks` с FTS5 `kb_fts` (триггеры шага 58 сворачивают
  «ё» в «е», `db.YO`; запрос — так же). Исходный файл не хранится: только куски.
- Источник эмбеддингов — только общее подключение через сервер (`settings` SOURCE_KEY = `{connection_id, embed_model,
  rerank_model | null}`, панель администратора). Нет источника — только BM25, всё работает: документ сразу `ready`.
  С источником `index` пишет куски сразу (BM25 находит их тотчас), а векторы считает фоновый обработчик (`kick`):
  `queued` → `ready` или `error` (куски остаются — BM25 работает). Смена модели эмбеддингов — векторы заново.
- Cleaner (cleaner_route.py): внешний источник при принудительном режиме не используется вовсе (`_source_conn`, у всех
  только BM25, панель предупреждает — `source_off`); диалог через Cleaner не отдаёт внешнему источнику ни запроса, ни
  кусков на переранжирование, ни своих больших вложений (`vectors_for`).
- `embed` — `POST <api>/embeddings` пачками по EMBED_BATCH, вектор — float32 `array('f')` единичной длины (близость —
  скалярное произведение); `rerank` — `POST <api>/rerank {model, query, documents}` → `results[{index,
  relevance_score}]`. Отказ источника при поиске — без векторов или без переранжирования, не ошибка.
- `search` — BM25 топ-CANDIDATES ∪ векторы топ-CANDIDATES → RRF (RRF_K) → переранжирование → TOP_K кусков `Hit`.
- Большое вложение: ход решает сам (`chat._excerpt_plan`) — целиком, если запрос помещается в контекст модели;
  иначе (или предел не известен, а текст длиннее INLINE_TOKENS) — документ с `attachment_id` (`adopt`), модель
  получает выдержки по последнему сообщению (`for_turn`, `excerpts`); снова целиком — документ удаляется (`release`).
  Базы, выбранные в диалоге (`chat_kbs`), — блоком `block` в сообщение пользователя хода; встроенный инструмент
  `knowledge_search` (chat_tools.py). Поиск хода — после начала ответа (`chat.StreamTask`), источник ждёт
  TURN_TIMEOUT_SEC.
- Базы: личные (`user_id` — владелец; видит и удаляет только он, администратор — нет) и общие (`user_id NULL`, панель
  администратора): правил `kb_rules` нет — всем, есть — участникам их групп, администратору — всегда. Удаление личной
  базы и документа — в корзину (trash.py, `deleted`); общих — из панели сразу.
- В журнал — только id, коды и числа: ни текстов, ни запросов, ни имён документов и баз, ни имён моделей.
"""
import asyncio
import json
import logging
import math
import operator
import unicodedata
import uuid
from array import array
from dataclasses import dataclass
from html import escape
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

import access
import auth
import cleaner_route
import clock
import connections
import documents
import operations
import search as history_search
import state
import upstream

CHUNK_CHARS = 1500
OVERLAP = 200
INLINE_TOKENS = 30_000
TOP_K = 6
CANDIDATES = 30                  # кусков BM25 и векторов до слияния
RRF_K = 60
EMBED_BATCH = 64
QUERY_WORDS = 32                 # слов запроса в BM25
QUERY_MAX = 500                  # знаков запроса инструмента
NAME_MAX = 100                   # имя базы
NAME_WORDS = "имя базы — от 1 до 100 знаков"
PERSONAL_BASES_MAX = 50
PERSONAL_DOCS_MAX = 1000
PERSONAL_CHARS_MAX = 50_000_000
TIMEOUT_SEC = 120                # векторы кусков фоновой индексацией (`_vectors`)
#: вектор запроса и переранжирование в ходе и в `knowledge_search`: ответ ждёт; отказ источника в ходе запоминается
#: (`cache["down"]`) — дальше без векторов и без переранжирования
TURN_TIMEOUT_SEC = 15
SOURCE_KEY = "knowledge_source"
GONE = {"code": "gone", "message": "такой базы знаний нет"}
DOC_GONE = {"code": "gone", "message": "такого документа нет"}
EMPTY = "в документе нет текста"
NOTHING = "В базах знаний ничего не найдено."
QUOTA = {"code": "knowledge_quota", "message": "достигнут предел личных баз знаний — освободите место в корзине"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")
WORKER: list = []                # обработчик векторов (одна задача на цикл событий)


class SourceError(Exception):
    """Источник эмбеддингов не ответил как надо (код или форма ответа); `code` — для журнала."""

    def __init__(self, code: "int | str"):
        super().__init__(str(code))
        self.code = code


@dataclass(frozen=True)
class Hit:
    doc_name: str
    n: int                       # номер куска в документе, с 1
    text: str
    score: float
    doc_id: str = ""             # id документа (`kb_documents`) — источник под ответом (`hits_view`, `chunk`)


# ---- куски ----------------------------------------------------------------------

def chunks(text: str) -> list[str]:
    """Куски по CHUNK_CHARS, соседние перекрываются на OVERLAP; вместе (без перекрытий) — весь текст."""
    out, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        out.append(text[start:end])
        if end == len(text):
            break
        start = end - OVERLAP
    return out


# ---- источник -------------------------------------------------------------------

def source() -> "dict | None":
    row = state.DB.execute("SELECT value FROM settings WHERE key = ?", (SOURCE_KEY,)).fetchone()
    return json.loads(row[0]) if row is not None and row[0] else None


class SourceIn(BaseModel):
    connection_id: "int | None" = None           # None — без источника (только BM25)
    embed_model: str = Field("", max_length=200)
    rerank_model: "str | None" = Field(None, max_length=200)


def set_source(body: SourceIn) -> "dict | None":
    """Источник — общее подключение через сервер; смена подключения или модели эмбеддингов — векторы заново."""
    before = source()
    if body.connection_id is None:
        value = None
    else:
        conn = _shared(body.connection_id)
        if conn is None or not conn.proxied or conn.cleaner:
            raise HTTPException(422, {"code": "connection",
                                      "message": "нужно общее подключение через сервер (адрес — в списке разрешённых)"})
        embed_model = body.embed_model.strip()
        if not embed_model:
            raise HTTPException(422, {"code": "model", "message": "укажите модель эмбеддингов"})
        value = {"connection_id": conn.id, "embed_model": embed_model,
                 "rerank_model": (body.rerank_model or "").strip() or None}
    state.DB.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (SOURCE_KEY, json.dumps(value) if value else ""))
    old = (before or {}).get("connection_id"), (before or {}).get("embed_model")
    if value is not None and old != (value["connection_id"], value["embed_model"]):
        state.DB.execute("UPDATE kb_chunks SET vector = NULL")
        count = state.DB.execute("UPDATE kb_documents SET state = 'queued'").rowcount
        log.info("знания: источник сменён, документов на векторы — %d", count)
        kick()
    return value


def _shared(conn_id: int):
    try:
        return connections.get(None, conn_id)
    except HTTPException:
        return None


def _source_blocked(conn) -> bool:
    return conn.routed or (cleaner_route.forced() and cleaner_route.is_external(conn))


def _source_conn() -> "tuple[dict, object] | None":
    """Источник, которым можно пользоваться. Внешний адрес при принудительном Cleaner (или общее подключение с
    переключателем «Через Cleaner») даёт только BM25: Cleaner не проводит эмбеддинги и переранжирование."""
    src = source()
    if src is None:
        return None
    conn = _shared(src["connection_id"])
    if conn is None or not conn.proxied or _source_blocked(conn):
        return None
    return src, conn


def source_off() -> bool:
    """Источник задан, но не используется: он внешний, а Cleaner обязателен (панель предупреждает)."""
    src = source()
    conn = _shared(src["connection_id"]) if src is not None else None
    return conn is not None and _source_blocked(conn)


def vectors_for(conn) -> bool:
    """Векторы и переранжирование в ходе диалога: диалог через Cleaner (`conn.routed`) внешнему источнику текста не
    отдаёт — ни запроса, ни кусков на переранжирование, ни своих больших вложений; внутренний источник — можно."""
    if not conn.routed:
        return True
    found = _source_conn()
    return found is None or not cleaner_route.is_external(found[1])


def _routed_attachment(doc_id: str) -> bool:
    """Учитываем сохранённый маршрут вложения и действующее подключение его диалога."""
    row = state.DB.execute("SELECT attachment_id FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
    return row is not None and row["attachment_id"] is not None and cleaner_route.attachment_routed(row["attachment_id"])


def _pack(values) -> bytes:
    """Вектор → float32 единичной длины."""
    if not isinstance(values, list) or not values or not all(isinstance(v, (int, float)) for v in values):
        raise SourceError("shape")
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return array("f", (v / norm for v in values)).tobytes()


async def _post(conn, path: str, body: dict, timeout: float) -> dict:
    try:
        r = await upstream.request(conn, "POST", connections._api(conn) + path, json=body, timeout=timeout)
    except upstream.UpstreamRefused as e:
        raise SourceError(e.reason) from None
    except httpx.HTTPError as e:
        raise SourceError(type(e).__name__) from None
    if r.status_code != 200:
        raise SourceError(r.status_code)
    out = upstream.json_body(r)
    if not out:
        raise SourceError("shape")
    return out


async def embed(texts: list[str], timeout: float = TIMEOUT_SEC) -> list[bytes]:
    """Векторы текстов (`POST <api>/embeddings` пачками по EMBED_BATCH); нет источника или отказ — SourceError."""
    found = _source_conn()
    if found is None:
        raise SourceError("none")
    src, conn = found
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        data = (await _post(conn, "/embeddings", {"model": src["embed_model"], "input": batch}, timeout)).get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise SourceError("shape")
        try:
            ordered = sorted(data, key=lambda d: d["index"])
            out += [_pack(d["embedding"]) for d in ordered]
        except (KeyError, TypeError):
            raise SourceError("shape") from None
    return out


async def rerank(query: str, docs: list[str]) -> list[float]:
    """Оценки кусков по порядку `docs` (`POST <api>/rerank`, ждёт TURN_TIMEOUT_SEC); нет модели переранжирования —
    SourceError."""
    found = _source_conn()
    if found is None or not found[0].get("rerank_model"):
        raise SourceError("none")
    src, conn = found
    results = (await _post(conn, "/rerank", {"model": src["rerank_model"], "query": query, "documents": docs},
                           TURN_TIMEOUT_SEC)).get("results")
    scores = [None] * len(docs)
    try:
        for item in results:
            scores[item["index"]] = float(item["relevance_score"])
    except (KeyError, TypeError, ValueError, IndexError):
        raise SourceError("shape") from None
    if any(s is None for s in scores):
        raise SourceError("shape")
    return scores


# ---- индекс ---------------------------------------------------------------------

def index(doc_id: str, text: str, vectors: bool = True) -> None:
    """Куски документа — сразу (BM25 находит их тотчас); векторы — фоновый обработчик (`kick`): `queued` →
    `ready` / `error`. Без источника (или `vectors` False — вложение диалога через Cleaner при внешнем источнике) —
    сразу `ready`."""
    parts = chunks(text)
    state.DB.executemany("INSERT INTO kb_chunks(doc_id, n, text) VALUES (?, ?, ?)",
                         [(doc_id, n, part) for n, part in enumerate(parts, 1)])
    queued = vectors and _source_conn() is not None
    state.DB.execute("UPDATE kb_documents SET state = ? WHERE id = ?", ("queued" if queued else "ready", doc_id))
    log.info("знания: документ %s — кусков %d", doc_id[:8], len(parts))
    if queued:
        kick()


def kick() -> None:
    """Обработчик векторов, если он не идёт: документы `queued` по очереди. Строка документа пишется до `kick`, а
    обработчик кончается без await после пустой выборки — новый документ не потеряется."""
    if WORKER and not WORKER[0].done():
        return
    WORKER[:] = [asyncio.create_task(_drain())]


async def _drain() -> None:
    while True:
        row = state.DB.execute("SELECT id FROM kb_documents WHERE state = 'queued' ORDER BY created LIMIT 1").fetchone()
        if row is None:
            return
        await _vectors(row["id"])


async def _vectors(doc_id: str) -> None:
    """Векторы кусков документа пачками; документ удалили — пачки пишутся мимо, обработчик идёт дальше."""
    try:
        while True:
            found = _source_conn()
            if found is None or (cleaner_route.is_external(found[1]) and _routed_attachment(doc_id)):
                raise SourceError("none")
            rows = state.DB.execute("SELECT id, text FROM kb_chunks WHERE doc_id = ? AND vector IS NULL ORDER BY n"
                                    " LIMIT ?", (doc_id, EMBED_BATCH)).fetchall()
            if not rows:
                break
            vectors = await embed([r["text"] for r in rows])
            latest = _source_conn()
            if latest is None:
                raise SourceError("none")
            if latest[0] != found[0]:
                continue                       # смена источника уже сбросила векторы, не писать поверх них старые
            state.DB.executemany("UPDATE kb_chunks SET vector = ? WHERE id = ?",
                                 [(v, r["id"]) for v, r in zip(vectors, rows)])
        state.DB.execute("UPDATE kb_documents SET state = 'ready' WHERE id = ?", (doc_id,))
    except SourceError as e:
        state.DB.execute("UPDATE kb_documents SET state = ? WHERE id = ?",
                         ("ready" if e.code == "none" else "error", doc_id))
        if e.code != "none":
            log.warning("знания: документ %s — векторы не получены (%s)", doc_id[:8], e.code)


def resume() -> None:
    """При старте службы: документы, чьи векторы прервал перезапуск, — дальше."""
    if state.DB.execute("SELECT 1 FROM kb_documents WHERE state = 'queued' LIMIT 1").fetchone():
        kick()


def queue_missing_vectors() -> None:
    """После отключения обязательного Cleaner дополнить индексы баз. Вложения диалогов не переотправляем."""
    if _source_conn() is None:
        return
    count = state.DB.execute("UPDATE kb_documents SET state = 'queued' WHERE state = 'ready' AND attachment_id IS NULL"
                             " AND EXISTS ("
                             "SELECT 1 FROM kb_chunks WHERE doc_id = kb_documents.id AND vector IS NULL)").rowcount
    if count:
        kick()


async def settle() -> None:
    """Дождаться обработчика векторов (тесты)."""
    while WORKER and not WORKER[0].done():
        await asyncio.gather(WORKER[0], return_exceptions=True)
        await asyncio.sleep(0)


# ---- доступ ---------------------------------------------------------------------

#: общая база открыта учётке: правил нет или учётка — участник одной из их групп (администратору — всегда, `_open`)
_RULES = ("(NOT EXISTS (SELECT 1 FROM kb_rules r WHERE r.kb_id = b.id) OR EXISTS (SELECT 1 FROM kb_rules r"
          " JOIN access_group_members m ON m.group_id = r.group_id WHERE r.kb_id = b.id AND m.user_id = ?))")


def _open(uid: int) -> tuple[str, tuple]:
    """SQL-условие живой базы `b`, которую учётка видит: своя или открытая общая."""
    if access.is_admin(uid):
        return "b.deleted IS NULL AND (b.user_id = ? OR b.user_id IS NULL)", (uid,)
    return f"b.deleted IS NULL AND (b.user_id = ? OR (b.user_id IS NULL AND {_RULES}))", (uid, uid)


def visible_ids(uid: int, kb_ids) -> list[str]:
    """Из `kb_ids` — базы, которые учётка видит (порядок сохраняется)."""
    clause, args = _open(uid)
    found = {r[0] for r in state.DB.execute(
        f"SELECT b.id FROM knowledge_bases b WHERE b.id IN (SELECT value FROM json_each(?)) AND {clause}",
        (json.dumps(list(kb_ids)), *args))}
    return [k for k in dict.fromkeys(kb_ids) if k in found]


def _documents(where: str, args: tuple) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "chars": r["chars"], "state": r["state"], "created": r["created"]}
            for r in state.DB.execute(f"SELECT * FROM kb_documents WHERE {where} AND deleted IS NULL"
                                      " ORDER BY created, rowid", args)]


def _base_out(row, groups: bool = False) -> dict:
    out = {"id": row["id"], "name": row["name"], "shared": row["user_id"] is None, "created": row["created"],
           "documents": _documents("kb_id = ?", (row["id"],))}
    if groups:
        out["groups"] = [r[0] for r in state.DB.execute("SELECT group_id FROM kb_rules WHERE kb_id = ? ORDER BY group_id",
                                                        (row["id"],))]
    return out


def bases(uid: int) -> list[dict]:
    """Базы учётки: свои и открытые ей общие (свои — первыми)."""
    clause, args = _open(uid)
    return [_base_out(r) for r in state.DB.execute(
        f"SELECT b.* FROM knowledge_bases b WHERE {clause} ORDER BY b.user_id IS NULL, b.created, b.rowid", args)]


def shared_bases() -> list[dict]:
    return [_base_out(r, groups=True) for r in state.DB.execute(
        "SELECT * FROM knowledge_bases WHERE user_id IS NULL AND deleted IS NULL ORDER BY created, rowid")]


def chat_kbs(uid: int, chat_id: str) -> list[str]:
    """Базы диалога, которые учётка видит сейчас (доступ мог закрыться, база — уйти в корзину)."""
    ids = [r[0] for r in state.DB.execute("SELECT kb_id FROM chat_kbs WHERE chat_id = ? ORDER BY rowid", (chat_id,))]
    return visible_ids(uid, ids) if ids else []


def big_attachments(chat_id: str) -> dict[str, str]:
    """Большие вложения диалога, которые модель получает выдержками (документ знаний есть): id вложения → id
    документа."""
    return {r["attachment_id"]: r["id"] for r in state.DB.execute(
        "SELECT d.id, d.attachment_id FROM kb_documents d JOIN attachments a ON a.id = d.attachment_id"
        " WHERE d.deleted IS NULL AND a.message_id IN (SELECT id FROM messages WHERE chat_id = ?)", (chat_id,))}


def offered(uid: int, chat_id: str) -> bool:
    """Инструмент `knowledge_search` в ходе: у учётки есть видимые базы или у диалога — большие вложения."""
    clause, args = _open(uid)
    return bool(state.DB.execute(f"SELECT 1 FROM knowledge_bases b WHERE {clause} LIMIT 1", args).fetchone()
                or big_attachments(chat_id))


# ---- поиск ----------------------------------------------------------------------

def _scope(uid: int, kb_ids, attachment_ids) -> dict[str, str]:
    """Документы поиска: живые документы видимых баз и большие вложения учётки; id → имя."""
    docs = {}
    kbs = visible_ids(uid, kb_ids) if kb_ids else []
    if kbs:
        docs |= {r[0]: r[1] for r in state.DB.execute(
            "SELECT id, name FROM kb_documents WHERE deleted IS NULL AND kb_id IN (SELECT value FROM json_each(?))",
            (json.dumps(kbs),))}
    if attachment_ids:
        docs |= {r[0]: r[1] for r in state.DB.execute(
            "SELECT id, name FROM kb_documents WHERE deleted IS NULL AND user_id = ?"
            " AND attachment_id IN (SELECT value FROM json_each(?))", (uid, json.dumps(list(attachment_ids))))}
    return docs


def _match(query: str) -> str:
    """Запрос FTS5: слова (буквы и цифры, «ё» → «е», как в индексе) — каждое началом слова, через OR; синтаксис
    FTS5 не проходит (кавычки удваиваются, прочее — разделитель)."""
    words = history_search.WORD.findall(unicodedata.normalize("NFC", query))
    words = [w.replace("ё", "е").replace("Ё", "Е") for w in words if len(w) > 1]
    return " OR ".join('"{}"*'.format(w.replace('"', '""')) for w in list(dict.fromkeys(words))[:QUERY_WORDS])


def _bm25(query: str, docs: dict) -> list[int]:
    match = _match(query)
    if not match:
        return []
    return [r[0] for r in state.DB.execute(
        "SELECT c.id FROM kb_fts JOIN kb_chunks c ON c.id = kb_fts.rowid WHERE kb_fts MATCH ?"
        " AND c.doc_id IN (SELECT value FROM json_each(?)) ORDER BY bm25(kb_fts) LIMIT ?",
        (match, json.dumps(list(docs)), CANDIDATES))]


def _nearest(query: bytes, rows: list) -> list[int]:
    """id кусков по убыванию близости (скалярное произведение единичных векторов); без базы — для to_thread."""
    q = array("f")
    q.frombytes(query)
    scored = []
    for chunk_id, raw in rows:
        v = array("f")
        v.frombytes(raw)
        if len(v) == len(q):
            scored.append((sum(map(operator.mul, q, v)), chunk_id))
    scored.sort(reverse=True)
    return [chunk_id for _, chunk_id in scored[:CANDIDATES]]


async def _vector(query: str, docs: dict, cache: dict) -> list[int]:
    """Куски по близости векторов; нет источника или векторов — []. Вектор запроса — один на ход (`cache`: по
    тексту запроса); источник в этом ходе уже отказал (`cache["down"]`) — не спрашивается."""
    if _source_conn() is None or cache.get("down"):
        return []
    rows = state.DB.execute("SELECT id, vector FROM kb_chunks WHERE vector IS NOT NULL"
                            " AND doc_id IN (SELECT value FROM json_each(?))", (json.dumps(list(docs)),)).fetchall()
    if not rows:
        return []
    key = ("vector", query)
    if key not in cache:
        try:
            cache[key] = (await embed([query], TURN_TIMEOUT_SEC))[0]
        except SourceError as e:
            cache["down"] = True
            log.warning("знания: вектор запроса не получен (%s)", e.code)
            return []
    return await asyncio.to_thread(_nearest, cache[key], [(r[0], r[1]) for r in rows])


async def _search(uid: int, query: str, docs: dict, k: int, cache: dict, vectors: bool = True) -> list[Hit]:
    """`vectors` False — только BM25: ни вектора запроса, ни переранжирования (`vectors_for`)."""
    if not docs or not query.strip():
        return []
    fused: dict[int, float] = {}
    for ranked in (_bm25(query, docs), await _vector(query, docs, cache) if vectors else []):
        for rank, chunk_id in enumerate(ranked):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1 / (RRF_K + rank + 1)
    best = sorted(fused, key=fused.get, reverse=True)[:CANDIDATES]
    if not best:
        return []
    rows = {r["id"]: r for r in state.DB.execute(
        "SELECT id, doc_id, n, text FROM kb_chunks WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(best),))}
    found = [(rows[i], fused[i]) for i in best if i in rows]
    src = _source_conn() if vectors else None
    if src and src[0].get("rerank_model") and len(found) > 1 and not cache.get("down"):
        try:
            scores = await rerank(query, [row["text"] for row, _ in found])
            found = sorted(((row, s) for (row, _), s in zip(found, scores)), key=lambda x: x[1], reverse=True)
        except SourceError as e:
            cache["down"] = True
            log.warning("знания: переранжирование не прошло (%s)", e.code)
    return [Hit(docs[row["doc_id"]], row["n"], row["text"], score, row["doc_id"]) for row, score in found[:k]]


async def search(uid: int, query: str, *, kb_ids=(), attachment_ids=(), k: int = TOP_K,
                 cache: "dict | None" = None, vectors: bool = True) -> list[Hit]:
    """Куски документов видимых баз `kb_ids` и больших вложений учётки `attachment_ids` по запросу: BM25 топ-30 ∪
    векторы топ-30 → RRF → переранжирование; не больше `k`. `cache` — общий на ход (отказ источника, векторы);
    `vectors` False — только BM25 (`vectors_for`)."""
    return await _search(uid, query, _scope(uid, kb_ids, attachment_ids), k, {} if cache is None else cache, vectors)


def hits_view(hits: list[Hit]) -> list[dict]:
    """Источники шага «Знания» под ответом (спека 2026-11-25, п. 28): `[{doc, n, id}]` — имя документа, номер куска
    и id документа, без повторов пары (id, n); текст куска страница читает маршрутом (`chat_sources`)."""
    seen, out = set(), []
    for h in hits:
        if (h.doc_id, h.n) not in seen:
            seen.add((h.doc_id, h.n))
            out.append({"doc": h.doc_name, "n": h.n, "id": h.doc_id})
    return out


def chunk(uid: int, chat_id: str, doc_id: str, n: int) -> "dict | None":
    """Кусок `n` документа `doc_id` для источника под ответом: документ жив и лежит в базе, которую учётка видит
    (`visible_ids`), или это документ большого вложения этого диалога и этой учётки; иначе None."""
    doc = state.DB.execute("SELECT id, kb_id, attachment_id, user_id, name FROM kb_documents"
                           " WHERE id = ? AND deleted IS NULL", (doc_id,)).fetchone()
    if doc is None:
        return None
    if doc["kb_id"] is not None:
        if not visible_ids(uid, [doc["kb_id"]]):
            return None
    elif doc["user_id"] != uid or doc["attachment_id"] not in big_attachments(chat_id):
        return None
    row = state.DB.execute("SELECT text FROM kb_chunks WHERE doc_id = ? AND n = ?", (doc_id, n)).fetchone()
    return None if row is None else {"doc": doc["name"], "n": n, "text": row["text"]}


def render(hits: list[Hit]) -> str:
    """Найденное для модели: кусок — `<excerpt source n>`."""
    return "".join(f'<excerpt source="{escape(h.doc_name, quote=True)}" n="{h.n}">\n{h.text}\n</excerpt>\n'
                   for h in hits)


def block(hits: list[Hit]) -> str:
    """Найденное в базах диалога — в начало сообщения пользователя хода."""
    return f"<knowledge>\n{render(hits)}</knowledge>\n\n" if hits else ""


def excerpts(hits: list[Hit]) -> str:
    """Выдержки большого вложения (по номеру куска) вместо всего текста."""
    return "\n\n".join(f"[кусок {h.n}]\n{h.text}" for h in sorted(hits, key=lambda h: h.n))


def _head(doc_id: str, name: str, k: int) -> list[Hit]:
    """Начало документа — когда по сообщению ничего не нашлось (модель хотя бы знает, о чём он)."""
    return [Hit(name, r["n"], r["text"], 0.0, doc_id) for r in state.DB.execute(
        "SELECT n, text FROM kb_chunks WHERE doc_id = ? ORDER BY n LIMIT ?", (doc_id, k))]


def _adopted(att_id: str):
    return state.DB.execute("SELECT id, name FROM kb_documents WHERE attachment_id = ? AND deleted IS NULL",
                            (att_id,)).fetchone()


def heads(att_ids) -> dict[str, str]:
    """Начало больших вложений `att_ids` (первые TOP_K кусков, `excerpts`): id вложения → текст. Не зависит от
    вопроса — запрос следующего хода повторяет старые сообщения байт в байт (спека п. 34)."""
    out = {}
    for att_id in att_ids:
        row = _adopted(att_id)
        if row is not None:
            out[att_id] = excerpts(_head(row["id"], row["name"], TOP_K))
    return out


async def for_turn(uid: int, chat_id: str, query: str, attachment_ids=(),
                   cache: "dict | None" = None, vectors: bool = True, own=(),
                   found: "list | None" = None) -> tuple[dict[str, str], str]:
    """Знания хода по его сообщению `query`: выдержки вложений `attachment_ids`, которые модель получает выдержками
    (`adopt`; id вложения → текст выдержек), и блок найденного (пусто — ничего не нашлось). Вложения сообщения хода
    (`own`) — выдержками по вопросу (ничего — начало документа); прочие — началом (`heads`), а найденное по вопросу в
    них — в блок вместе с найденным в базах диалога: старое сообщение от нового вопроса не меняется (спека п. 34).
    `cache` — общий на ход (`_vector`, отказ источника); `vectors` False — только BM25 (`vectors_for`). `found`
    (список) получает куски, найденные по вопросу, — выдержки вложений хода и блок; начало документа, взятое за
    неимением совпадений, — нет (источники шага «Знания», спека 2026-11-25 п. 28)."""
    cache = {} if cache is None else cache
    out, history = {}, {}
    for att_id in attachment_ids:
        row = _adopted(att_id)
        if row is None:
            continue
        if att_id not in own:
            history[row["id"]] = row["name"]
            out[att_id] = excerpts(_head(row["id"], row["name"], TOP_K))
            continue
        hits = await _search(uid, query, {row["id"]: row["name"]}, TOP_K, cache, vectors)
        if found is not None:
            found.extend(hits)
        out[att_id] = excerpts(hits or _head(row["id"], row["name"], TOP_K))
    blocked = await _search(uid, query, history, TOP_K, cache, vectors) if history else []
    kbs = chat_kbs(uid, chat_id)
    blocked += await _search(uid, query, _scope(uid, kbs, ()), TOP_K, cache, vectors) if kbs else []
    if found is not None:
        found.extend(blocked)
    if out or blocked:
        log.info("знания: чат %s — выдержек вложений %d, кусков баз и истории %d", chat_id[:8], len(out), len(blocked))
    return out, block(blocked)


def adopt(uid: int, att_id: str, name: str, text: str, vectors: bool = True) -> str:
    """Вложение, которое ход отдаёт модели выдержками, — документ знаний (повтор — тот же документ). Итог — id
    документа. `vectors` False — без векторов (диалог через Cleaner при внешнем источнике, `vectors_for`)."""
    row = state.DB.execute("SELECT id FROM kb_documents WHERE attachment_id = ?", (att_id,)).fetchone()
    if row is not None:
        return row["id"]
    doc_id = uuid.uuid4().hex
    state.DB.execute("INSERT INTO kb_documents(id, attachment_id, user_id, name, chars, state, created)"
                     " VALUES (?, ?, ?, ?, ?, 'queued', ?)", (doc_id, att_id, uid, name, len(text), clock.NOW()))
    index(doc_id, text, vectors)
    log.info("знания: вложение %s — документ %s", att_id[:8], doc_id[:8])
    return doc_id


def release(att_ids) -> None:
    """Вложения, которые ход отдаёт модели целиком: их документы знаний не нужны (куски — каскадом); отметка
    «модель видит выдержки» (`big_attachments`) снимается."""
    count = state.DB.execute("DELETE FROM kb_documents WHERE attachment_id IN (SELECT value FROM json_each(?))",
                             (json.dumps(list(att_ids)),)).rowcount
    if count:
        log.info("знания: документов вложений целиком — %d", count)


# ---- базы -----------------------------------------------------------------------

class BaseIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)


class ChatKbsIn(BaseModel):
    kb_ids: list[str] = Field(max_length=32)


def _name(text: str) -> str:
    clean = " ".join(text.split())
    if not clean:
        raise HTTPException(422, {"code": "name", "message": NAME_WORDS})
    return clean


def create_base(owner: "int | None", name: str) -> dict:
    if owner is not None and state.DB.execute(
            "SELECT count(*) FROM knowledge_bases WHERE user_id = ?", (owner,)).fetchone()[0] >= PERSONAL_BASES_MAX:
        raise HTTPException(409, QUOTA)
    kb_id = uuid.uuid4().hex
    state.DB.execute("INSERT INTO knowledge_bases(id, user_id, name, created) VALUES (?, ?, ?, ?)",
                     (kb_id, owner, _name(name), clock.NOW()))
    log.info("знания: база %s создана (%s)", kb_id[:8], "общая" if owner is None else f"учётка {owner}")
    return _base_out(state.DB.execute("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,)).fetchone())


def _base(owner: "int | None", kb_id: str):
    """Живая база владельца (`None` — общая); иначе 404."""
    row = state.DB.execute("SELECT * FROM knowledge_bases WHERE id = ? AND user_id IS ? AND deleted IS NULL",
                           (kb_id, owner)).fetchone()
    if row is None:
        raise HTTPException(404, GONE)
    return row


async def _upload_body(request: Request) -> tuple[str, bytes]:
    """Тело — сам файл до documents.DOC_MAX, имя — заголовком `X-Studio-Name`."""
    too_big = HTTPException(413, {"code": "document", "message": f"файл должен быть непустым и не больше "
                                                                 f"{documents.DOC_MAX >> 20} МиБ"})
    length = request.headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > documents.DOC_MAX:
        raise too_big
    raw = bytearray()
    async for piece in request.stream():
        raw.extend(piece)
        if len(raw) > documents.DOC_MAX:
            raise too_big
    if not raw:
        raise too_big
    auth.recheck(request)
    return documents.clean_name(unquote(request.headers.get("x-studio-name", ""))), bytes(raw)


async def add_document(owner: "int | None", kb_id: str, request: Request) -> dict:
    _base(owner, kb_id)
    name, data = await _upload_body(request)
    try:
        text = await asyncio.to_thread(documents.extract, name, data)
    except documents.DocumentError as e:
        raise HTTPException(e.status, {"code": "document", "message": str(e)}) from None
    if not text.strip():
        raise HTTPException(422, {"code": "document", "message": EMPTY})
    if owner is not None:
        operations.check(owner)
    _base(owner, kb_id)                              # базу удалили, пока читался файл
    if owner is not None:
        count, chars = state.DB.execute(
            "SELECT count(*), coalesce(sum(chars), 0) FROM kb_documents WHERE user_id = ? AND kb_id IS NOT NULL",
            (owner,)).fetchone()
        if count >= PERSONAL_DOCS_MAX or chars + len(text) > PERSONAL_CHARS_MAX:
            raise HTTPException(409, QUOTA)
    doc_id = uuid.uuid4().hex
    state.DB.execute("INSERT INTO kb_documents(id, kb_id, user_id, name, chars, state, created)"
                     " VALUES (?, ?, ?, ?, ?, 'queued', ?)", (doc_id, kb_id, owner, name, len(text), clock.NOW()))
    index(doc_id, text)
    return _documents("id = ?", (doc_id,))[0]


def _doc(owner: "int | None", kb_id: str, doc_id: str):
    _base(owner, kb_id)
    row = state.DB.execute("SELECT * FROM kb_documents WHERE id = ? AND kb_id = ? AND deleted IS NULL",
                           (doc_id, kb_id)).fetchone()
    if row is None:
        raise HTTPException(404, DOC_GONE)
    return row


def delete_shared(kb_id: str) -> None:
    """Общая база — из панели сразу и окончательно (куски — каскадом)."""
    _base(None, kb_id)
    state.DB.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
    log.info("знания: общая база %s удалена", kb_id[:8])


def delete_shared_document(kb_id: str, doc_id: str) -> None:
    _doc(None, kb_id, doc_id)
    state.DB.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
    log.info("знания: документ %s общей базы удалён", doc_id[:8])


def set_access(kb_id: str, groups: "list[int] | None") -> list[int]:
    """None — всем; список групп (не пустой) — их участникам; администратору — всегда."""
    _base(None, kb_id)
    if groups is not None:
        if not groups:
            raise HTTPException(422, {"code": "group", "message": "выберите хотя бы одну группу"})
        clean = sorted(set(groups))
        known = {r[0] for r in state.DB.execute("SELECT id FROM access_groups WHERE id IN (SELECT value FROM json_each(?))",
                                                (json.dumps(clean),))}
        if known != set(clean):
            raise HTTPException(422, {"code": "group", "message": "такой группы нет"})
    state.DB.execute("DELETE FROM kb_rules WHERE kb_id = ?", (kb_id,))
    state.DB.executemany("INSERT INTO kb_rules(kb_id, group_id) VALUES (?, ?)", [(kb_id, g) for g in groups or []])
    log.info("знания: доступ к базе %s — %s", kb_id[:8], "все" if groups is None else f"групп {len(groups)}")
    return sorted(set(groups or []))


async def purge_base(user_id: "int | None", kb_id: str) -> None:
    """Окончательно (корзина): база с документами и кусками — каскадом."""
    if state.DB.execute("DELETE FROM knowledge_bases WHERE id = ? AND user_id IS ?", (kb_id, user_id)).rowcount:
        log.info("знания: база %s удалена окончательно", kb_id[:8])


async def purge_document(user_id: "int | None", doc_id: str) -> None:
    if state.DB.execute("DELETE FROM kb_documents WHERE id = ? AND user_id IS ?", (doc_id, user_id)).rowcount:
        log.info("знания: документ %s удалён окончательно", doc_id[:8])


@router.get("/knowledge")
async def list_bases(request: Request):
    return {"items": bases(request.state.user.id)}


@router.post("/knowledge", status_code=201)
async def post_base(body: BaseIn, request: Request):
    operations.check(request.state.user.id)
    return create_base(request.state.user.id, body.name)


@router.delete("/knowledge/{kb_id}", status_code=204)
async def delete_base(kb_id: str, request: Request):
    """Своя база — в корзину (с документами: они часть её строки)."""
    uid = request.state.user.id
    _base(uid, kb_id)
    state.DB.execute("UPDATE knowledge_bases SET deleted = ? WHERE id = ?", (clock.NOW(), kb_id))
    log.info("знания: база %s в корзине", kb_id[:8])
    return Response(status_code=204)


@router.post("/knowledge/{kb_id}/documents", status_code=201)
async def post_document(kb_id: str, request: Request):
    return await add_document(request.state.user.id, kb_id, request)


@router.delete("/knowledge/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_document(kb_id: str, doc_id: str, request: Request):
    _doc(request.state.user.id, kb_id, doc_id)
    state.DB.execute("UPDATE kb_documents SET deleted = ? WHERE id = ?", (clock.NOW(), doc_id))
    log.info("знания: документ %s в корзине", doc_id[:8])
    return Response(status_code=204)


@router.put("/chats/{chat_id}/knowledge")
async def put_chat_kbs(chat_id: str, body: ChatKbsIn, request: Request):
    """Базы диалога — полной заменой; база, которой учётка не видит, — 404."""
    uid = request.state.user.id
    if state.DB.execute("SELECT 1 FROM chats WHERE id = ? AND user_id = ? AND deleted IS NULL",
                        (chat_id, uid)).fetchone() is None:
        raise HTTPException(404, {"code": "gone", "message": "такого диалога нет"})
    wanted = list(dict.fromkeys(body.kb_ids))
    if len(visible_ids(uid, wanted)) != len(wanted):
        raise HTTPException(404, GONE)
    state.DB.execute("DELETE FROM chat_kbs WHERE chat_id = ?", (chat_id,))
    state.DB.executemany("INSERT INTO chat_kbs(chat_id, kb_id) VALUES (?, ?)", [(chat_id, k) for k in wanted])
    log.info("чат %s: баз знаний %d", chat_id[:8], len(wanted))
    return {"kb_ids": wanted}
