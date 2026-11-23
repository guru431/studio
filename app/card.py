"""Очередь карты (первая ступень — без шлюза): что из заданий студии всех учёток держит карту шлюза и кто её ждёт.

Карту делит шлюз (srvllm, s06 — по хосту и порту адреса), о чужих клиентах студия не знает: она видит только свои
задания в памяти модулей и ролики в базе. Запись — вид, модель, фаза и её начало:
- `busy` — точно на карте: «распознаю» расшифровки, «озвучиваю» озвучки, идущие распознавание и перевод, ролик
  `in_progress`, ответ чата через шлюз без ожидания; ответ чата и перевод — только локальной моделью (`_local`:
  облачная роль srvllm карту не держит), перевод файла, что ждёт свой скан, — не в счёт (держит распознавание);
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
import asyncio
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
#: медиана вида и модели живёт минуту (`typical`): опрос «В работе» не читает историю на каждую строку
TYPICAL_SEC = 60
TYPICAL: dict[tuple, tuple] = {}
#: снимок `status` шага цикла событий: задача, снимок (`info` — один на все строки запроса)
_TICK: "tuple | None" = None


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

    def add(section, job_id, conn, kind, model, phase, since, role=None):
        gpu = _gpu(conn)
        if gpu is not None and (role is None or _local(conn, role)):     # role — модель чата или перевода
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
    for job_id, job in list(ocr.JOBS.items()):
        if job.stage not in (ocr.QUEUED, "готовлю страницы"):
            add("ocr", job_id, job.conn, "scan", None, "wait" if job.stage == "жду карту" else "busy", job.stage_at)
    for job_id, job in list(translate.JOBS.items()):
        if job.stage in (translate.QUEUED, "готовлю") or job.stage.startswith(translate.SCANNING):
            continue                                 # ждёт свой скан: карту держит запись ocr.JOBS
        add("translate", job_id, job.conn, "translate", None, "wait" if job.stage == "жду карту" else "busy",
            job.stage_at, None if job.stage == "распознаю снимок" else job.model)
    now = clock.NOW()
    for message_id, stream in list(chat.STREAMS.items()):
        wait = stream.card_wait
        add("chat", message_id, stream.conn, "chat", stream.model, "busy" if wait is None else "wait",
            now if wait is None else wait[0], stream.model)
    return out


def _local(conn, model: str) -> bool:
    """Модель чата или перевода на карте: s06 — весь, srvllm — только роль `local-*` (`sections.is_local`)."""
    import sections
    return getattr(conn, "s06", False) or sections.is_local(conn, model)


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
    now = clock.NOW()
    if (kept := TYPICAL.get((kind, model))) is not None and now - kept[0] < TYPICAL_SEC:
        return kept[1]
    table, column = HISTORY[kind]
    where, args = (f" AND {column} = ?", (model,)) if column and model else ("", ())
    rows = state.DB.execute(
        "SELECT finished - MAX(created, COALESCE(LAG(finished) OVER (PARTITION BY user_id ORDER BY finished), created))"
        f" FROM (SELECT user_id, created, finished FROM {table} WHERE state = 'done' AND finished IS NOT NULL{where}"
        " ORDER BY finished DESC LIMIT ?)", (*args, RECENT)).fetchall()
    found = [r[0] for r in rows if r[0] is not None and r[0] > 0]
    TYPICAL[(kind, model)] = (now, statistics.median(found) if found else None)
    return TYPICAL[(kind, model)][1]


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
    """Очередь карты задания (`status`); не ждёт её — None. В цикле событий снимок один на шаг задачи: строки одного
    запроса не пересчитывают очередь, следующий шаг (`call_soon`) — заново."""
    global _TICK
    try:
        task = asyncio.current_task()
    except RuntimeError:                         # вне цикла событий — без снимка
        task = None
    if task is None:
        return status().get((section, job_id))
    if _TICK is None or _TICK[0] is not task:
        _TICK = (task, status())
        asyncio.get_running_loop().call_soon(_forget, _TICK)
    return _TICK[1].get((section, job_id))


def _forget(tick: tuple) -> None:
    global _TICK
    if _TICK is tick:
        _TICK = None


def mark(item: dict, section: str, row) -> dict:
    """Идущей записи раздела, что ждёт карту, — поле `card` (`info`); прочим — как есть."""
    if row["finished"] is None and (queue := info(section, row["id"])) is not None:
        item["card"] = queue
    return item
