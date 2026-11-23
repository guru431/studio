"""Очередь карты (первая ступень — без шлюза): что из заданий студии всех учёток держит карту шлюза и кто её ждёт.

Карту делит шлюз (srvllm, s06 — по хосту и порту адреса), о чужих клиентах студия не знает: она видит только свои
задания в памяти модулей и ролики в базе. Запись — вид, модель, фаза и её начало:
- `busy` — точно на карте: «распознаю» расшифровки, «озвучиваю» озвучки, идущие распознавание и перевод, ролик
  `in_progress`, ответ чата через шлюз без ожидания;
- `maybe` — запрос у шлюза, который держит его, пока карта занята (картинки, описание и перевод промпта «Фото»):
  работает, если на карте нет никого точнее, иначе ждёт;
- `wait` — ждёт карту: «жду карту» после 503 шлюза;
- `queued` — ролик в очереди шлюза: ждёт, но занятость карты не доказывает (только что поставлен).
Держит карту самое раннее `busy`, без него — самое раннее `maybe`. Ждущему `info` отдаёт `{busy, minutes, ahead}`:
вид держащего (None — студия ничего не запускала, а шлюз ответил 503: карта занята другим клиентом; ролику без
держащего — ничего), сколько ему примерно осталось
(медиана RECENT недавних готовых того же вида и модели минус уже прошедшее; оценки нет или срок вышел — None) и
сколько ждущих этой карты встало раньше. Чужие задания — только видом и числами: ни учётки, ни содержания.
Длительность готового — от создания или от конца предыдущего готового той же учётки, что позже (очередь серии и
раздела не в счёт). Запросов к шлюзу нет, журнал — ничего."""
import math
import statistics
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

import clock
import state

#: вид → таблица истории и столбец модели (оценка); описание и перевод промпта, ответ чата — без оценки
HISTORY = {"image": ("images", "model"), "video": ("videos", "model"), "transcript": ("transcripts", None),
           "speech": ("speeches", None), "scan": ("scans", None), "translate": ("translations", None)}
RECENT = 20


@dataclass
class Entry:
    key: tuple          # (раздел, id) — как у строки «В работе»
    gpu: tuple          # хост и порт шлюза
    kind: str
    model: "str | None"
    phase: str          # busy | maybe | wait | queued
    since: float


def _gpu(conn) -> "tuple | None":
    """Карта подключения: шлюз студии (srvllm, s06) — хост и порт; облако и прочее — None (не карта)."""
    if conn is None or not (conn.kind == "srvllm" or getattr(conn, "s06", False)):
        return None
    url = httpx.URL(conn.base_url)
    return url.host, url.port


def _entries() -> list[Entry]:
    import chat
    import connections
    import images
    import ocr
    import speeches
    import transcripts
    import translate
    out: list[Entry] = []

    def add(section, job_id, conn, kind, model, phase, since):
        gpu = _gpu(conn)
        if gpu is not None:
            out.append(Entry((section, job_id), gpu, kind, model, phase, since))

    for job_id, job in list(images.JOBS.items()):
        if job.finished_at is None and job.state == "working":
            add("photo", job_id, job.conn, job.kind if job.kind in images.TEXT_KINDS else "image", job.model,
                "maybe", job.started)
    for row in state.DB.execute("SELECT id, user_id, connection_id, model, state, created FROM videos"
                                " WHERE state IN ('queued', 'in_progress')").fetchall():
        try:
            conn = connections.get(row["user_id"], row["connection_id"], gate=False)
        except HTTPException:                        # подключение удалено: ролик своё доживает без нас
            continue
        add("video", row["id"], conn, "video", row["model"], "busy" if row["state"] == "in_progress" else "queued",
            row["created"])
    for job_id, job in list(transcripts.JOBS.items()):
        if job.stage in ("распознаю", "жду карту"):
            add("transcribe", job_id, job.conn, "transcript", None, "wait" if job.stage == "жду карту" else "busy",
                job.stage_at)
    for job_id, job in list(speeches.JOBS.items()):
        if job.stage.startswith(("озвучиваю", "жду карту")):
            add("speech", job_id, job.conn, "speech", None, "wait" if job.stage.startswith("жду") else "busy",
                job.stage_at)
    for module, section, kind, ready in ((ocr, "ocr", "scan", "готовлю страницы"),
                                         (translate, "translate", "translate", "готовлю")):
        for job_id, job in list(module.JOBS.items()):
            if job.stage not in (module.QUEUED, ready):
                add(section, job_id, job.conn, kind, None, "wait" if job.stage == "жду карту" else "busy", job.stage_at)
    now = clock.NOW()
    for message_id, stream in list(chat.STREAMS.items()):
        wait = stream.card_wait
        add("chat", message_id, stream.conn, "chat", stream.model, "busy" if wait is None else "wait",
            now if wait is None else wait[0])
    return out


def _holder(entries: list[Entry]) -> "Entry | None":
    for phase in ("busy", "maybe"):
        found = [e for e in entries if e.phase == phase]
        if found:
            return min(found, key=lambda e: e.since)
    return None


def typical(kind: str, model: "str | None") -> "float | None":
    """Медиана длительности RECENT недавних готовых вида (и модели, где она в истории); нет истории — None."""
    if kind not in HISTORY:
        return None
    table, column = HISTORY[kind]
    where, args = (f" AND {column} = ?", (model,)) if column and model else ("", ())
    rows = state.DB.execute(
        "SELECT finished - MAX(created, COALESCE(LAG(finished) OVER (PARTITION BY user_id ORDER BY finished), created))"
        f" FROM (SELECT user_id, created, finished FROM {table} WHERE state = 'done' AND finished IS NOT NULL{where}"
        " ORDER BY finished DESC LIMIT ?)", (*args, RECENT)).fetchall()
    found = [r[0] for r in rows if r[0] is not None and r[0] > 0]
    return statistics.median(found) if found else None


def _minutes(entry: Entry) -> "int | None":
    usual = typical(entry.kind, entry.model)
    left = None if usual is None else usual - (clock.NOW() - entry.since)
    return math.ceil(left / 60) if left is not None and left > 0 else None


def status() -> dict:
    """(раздел, id) → `{busy, minutes, ahead}` каждого задания студии, которое ждёт карту."""
    entries = _entries()
    out = {}
    for gpu in {e.gpu for e in entries}:
        same = [e for e in entries if e.gpu == gpu]
        holder = _holder(same)
        waiting = sorted((e for e in same if e is not holder and e.phase != "busy"), key=lambda e: e.since)
        minutes = _minutes(holder) if holder is not None and waiting else None
        for ahead, e in enumerate(waiting):
            if holder is not None or e.phase != "queued":
                out[e.key] = {"busy": holder.kind if holder is not None else None, "minutes": minutes, "ahead": ahead}
    return out


def info(section: str, job_id: str) -> "dict | None":
    """Очередь карты задания (`status`); не ждёт её — None."""
    return status().get((section, job_id))


def mark(item: dict, section: str, row) -> dict:
    """Идущей записи раздела, что ждёт карту, — поле `card` (`info`); прочим — как есть."""
    if row["finished"] is None and (queue := info(section, row["id"])) is not None:
        item["card"] = queue
    return item
