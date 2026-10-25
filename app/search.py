"""Поиск по своей истории (спека 2026-10-21-studio-history-search-design).

- Индекс — `search_docs` и FTS5 `search` (db.SEARCH_13): их держат триггеры таблиц истории, здесь —
  только чтение. Ответ чата попадает в индекс после «готово»; расшифровка — по имени и тексту.
- `POST /api/search` — телом, а не строкой запроса (её пишет журнал Apache): `{q?, section?, model?,
  since?, until?, before?, limit?}`. Слова — буквы и цифры Unicode (прочее — разделитель, синтаксис
  FTS5 не проходит), до WORDS_MAX, каждое — начало слова, все вместе; «ё» = «е». Ни слов, ни фильтров —
  400. Дни — по часам хоста, «по» включительно, годы YEARS (иначе 422 словами). Документ чата и текст
  расшифровки — первые db.INDEX_CHARS знаков.
- Новые сверху (created, затем item по убыванию, у одной записи — позже проиндексированная); диалог —
  один раз, последним по времени совпадением (сообщение, документ или название; при одном времени —
  ответ, а не вопрос). Курсор `before` — `{created, item, part}` последней строки страницы.
- Ремонт индекса — `reindex`, не 'rebuild' FTS5 (он теряет «ё» = «е», db.YO).
- Ответ: `{items: [{section, id, part, title, snippet, model, created, href}], more}`; snippet — FTS5
  с маркерами MARK вокруг найденного (страница строит выделение узлами), без слов — пусто.
- `GET /api/search/models` — разные модели своей истории по разделам.
- Только `user_id` сессии. Журнал — «search: N», без слов и фильтров.
"""
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import db
import state

Q_MAX = 200
WORDS_MAX = 8
LIMIT_MAX = 50
TITLE_MAX = 80
SNIPPET_TOKENS = 16
MARK = ("\u0002", "\u0003")
WORD = re.compile(r"[^\W_]+")
DAY = r"^\d{4}-\d{2}-\d{2}$"
YEARS = (2000, 2100)
DAYS_WORDS = f"дни — с {YEARS[0]} по {YEARS[1]} год"
#: раздел → заголовок записи (таблица и столбец — из кода)
TITLES = {"chat": ("chats", "title"), "photo": ("images", "prompt"), "video": ("videos", "prompt"),
          "transcribe": ("transcripts", "name"), "speech": ("speeches", "text")}
EMPTY = {"code": "input", "message": "введите слова или выберите фильтр"}

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/search")


class Cursor(BaseModel):
    created: float
    item: str = Field(max_length=64)
    part: str = Field(max_length=64)


class SearchIn(BaseModel):
    q: str = Field("", max_length=Q_MAX)
    section: "Literal['chat', 'photo', 'video', 'transcribe', 'speech'] | None" = None
    model: "str | None" = Field(None, max_length=200)
    since: "str | None" = Field(None, pattern=DAY)
    until: "str | None" = Field(None, pattern=DAY)
    before: "Cursor | None" = None
    limit: int = Field(30, ge=1, le=LIMIT_MAX)


def words(q: str) -> list[str]:
    """Слова запроса: буквы и цифры, «ё» → «е» (как в индексе), не больше WORDS_MAX."""
    found = WORD.findall(unicodedata.normalize("NFC", q))
    return [w.replace("ё", "е").replace("Ё", "Е") for w in found][:WORDS_MAX]


def _day(value: str) -> datetime:
    """День запроса; годы — YEARS: за краями timestamp() и «+1 день» — OverflowError (Linux) или OSError
    (Windows), то есть 500."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, {"code": "invalid", "message": "неверные данные запроса"}) from None
    if not YEARS[0] <= day.year <= YEARS[1]:
        raise HTTPException(422, {"code": "invalid", "message": DAYS_WORDS})
    return day


def reindex(conn) -> None:
    """Ремонт индекса: FTS5 заново из search_docs с той же заменой «ё», что у триггеров (db.YO), одной
    транзакцией. Штатный 'rebuild' — никогда: он берёт тело как есть, и «елка» перестаёт находить «Ёлку»."""
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT INTO search(search) VALUES ('delete-all')")
        conn.execute(f"INSERT INTO search(rowid, body) SELECT id, {db.YO.format('body')} FROM search_docs")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _title(section: str, item: str, user_id: int) -> str:
    table, column = TITLES[section]
    row = state.DB.execute(f"SELECT {column} FROM {table} WHERE id = ? AND user_id = ?", (item, user_id)).fetchone()
    text = " ".join(((row[0] if row is not None else None) or "").split())
    if not text:
        return "Новый диалог" if section == "chat" else ""
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX].rstrip() + "…"


def _href(section: str, item: str) -> str:
    return f"chat/{item}" if section == "chat" else f"{section}?id={item}"


@router.post("")
async def find(body: SearchIn, request: Request):
    uid = request.state.user.id
    terms = words(body.q)
    if not terms and not (body.section or body.model or body.since or body.until):
        raise HTTPException(400, EMPTY)
    where, args = ["d.user_id = ?"], [uid]
    for column, value in (("section", body.section), ("model", body.model)):
        if value:
            where.append(f"d.{column} = ?")
            args.append(value)
    if body.since:
        where.append("d.created >= ?")
        args.append(_day(body.since).timestamp())
    if body.until:
        where.append("d.created < ?")
        args.append((_day(body.until) + timedelta(days=1)).timestamp())
    match = " AND ".join('"{}"*'.format(t.replace('"', '""')) for t in terms)
    if terms:
        # CROSS JOIN держит FTS5 внешним циклом и при курсоре, и при фильтрах: иначе планировщик идёт от
        # индекса search_docs и гоняет MATCH заново на каждую строку истории (вторая страница — секунды)
        source = "search CROSS JOIN search_docs d ON d.id = search.rowid"
        where.insert(0, "search MATCH ?")
        args.insert(0, match)
    else:
        source = "search_docs d"
    cond = " AND ".join(where)
    cursor, tail, shown = "", [], set()
    if body.before is not None:
        cursor = " AND (d.created, d.item, d.part) < (?, ?, ?)"
        tail = [body.before.created, body.before.item, body.before.part]
        # диалоги прежних страниц: их последнее совпадение — выше курсора
        shown = {r[0] for r in state.DB.execute(
            f"SELECT DISTINCT d.item FROM {source} WHERE {cond} AND d.section = 'chat'"
            " AND (d.created, d.item, d.part) >= (?, ?, ?)", (*args, *tail))}
    # по порядку, без LIMIT: строки читаются, пока страница не наберётся; диалог — первым (последним по
    # времени) совпадением. Окно row_number() по всем совпадениям — впятеро дольше. При одном created
    # строки одной записи — по d.id (порядку в индексе): ход пишет вопрос и ответ с одним временем, ответ
    # индексируется после «готово» — он и «последний». Курсор (created, item, part) этим не сбит: строка
    # другой записи отличается created или item, а прочие строки диалога курсора — в shown
    rows = []
    found = state.DB.execute(f"SELECT d.id AS doc, d.section, d.item, d.part, d.model, d.created FROM {source}"
                             f" WHERE {cond}{cursor} ORDER BY d.created DESC, d.item DESC, d.id DESC", (*args, *tail))
    try:
        for row in found:
            if row["section"] == "chat":
                if row["item"] in shown:
                    continue
                shown.add(row["item"])
            rows.append(row)
            if len(rows) > body.limit:
                break
    finally:
        found.close()
    items = []
    for row in rows[:body.limit]:
        snippet = ""
        if terms:
            snippet = state.DB.execute("SELECT snippet(search, 0, ?, ?, '…', ?) FROM search"
                                       " WHERE search MATCH ? AND rowid = ?",
                                       (*MARK, SNIPPET_TOKENS, match, row["doc"])).fetchone()[0]
        items.append({"section": row["section"], "id": row["item"], "part": row["part"],
                      "title": _title(row["section"], row["item"], uid), "snippet": snippet, "model": row["model"],
                      "created": row["created"], "href": _href(row["section"], row["item"])})
    log.info("search: %d", len(items))
    return {"items": items, "more": len(rows) > body.limit}


@router.get("/models")
async def models(request: Request):
    out: dict[str, list[str]] = {}
    for row in state.DB.execute("SELECT DISTINCT section, model FROM search_docs WHERE user_id = ? AND model IS NOT NULL"
                                " ORDER BY section, model", (request.state.user.id,)):
        out.setdefault(row["section"], []).append(row["model"])
    return {"models": out}
