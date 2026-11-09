"""«В работе» (спека 2026-10-21-studio-jobs-panel): свои задания всех разделов одним списком.

`GET /api/jobs?since=<время>` → `{now, running, finished, push}` (`push` — подписка этой сессии). Идущие — картинки `working`,
`cancelling` и `queued` (серия), ролики `queued`/`in_progress`, расшифровки и озвучки `working`/`queued`,
распознавания и переводы `queued`/`working`/`cancelling`,
ответы чата через сервер (`streaming`, `via='server'`); готовые — закончившиеся не раньше `since` и
не старше JOBS_RECENT_SEC, не больше FINISHED_MAX (чат сюда не попадает: ответ виден в диалоге).
`since` — `now` прошлого ответа: часы устройства не участвуют; граница включена — задание, кончившееся
в тот же миг, не теряется, повтор страница отсекает по id. У идущих — `cancel`: адрес отмены своего раздела
(чат — остановка ответа), им пользуется «Отменить» панели; блокировка раздела закрывает его, как любой запрос.

Только база (строки своего `user_id`, индексы `*_user_created` и `messages_streaming`) и память
модулей (`detail` идущих — те же слова, что у раздела). Ни одного запроса к шлюзу. Журнал — ничего:
названия записей, имена файлов и тексты не пишутся.
"""
from fastapi import APIRouter, Request

import chat
import clock
import lock_policy
import images
import ocr
import speeches
import state
import transcripts
import translate

router = APIRouter(prefix="/api/jobs")

JOBS_RECENT_SEC = 3600
FINISHED_MAX = 50
SPEECH_TITLE = 40
MODEL_NAMES = {"lustify": "Lustify", "flux": "FLUX"}
STATES = {"done": "done", "error": "error", "failed": "error", "cancelled": "cancelled"}
TERMINAL = tuple(STATES)


#: «Отменить» идущей и ждущей строки — обычный адрес отмены раздела (защита раздела — та же: 423)
CANCEL = {"photo": "/api/images/{}/cancel", "video": "/api/videos/{}/cancel", "transcribe": "/api/transcripts/{}/cancel",
          "ocr": "/api/ocr/{}/cancel", "translate": "/api/translate/{}/cancel", "speech": "/api/speeches/{}/cancel"}


def _item(section: str, row, title: str, detail: str, href: str) -> dict:
    finished = row["finished"]
    state_ = STATES.get(row["state"], "running") if finished is not None else "running"
    out = {"section": section, "id": row["id"], "state": state_, "title": title, "detail": detail,
           "started": row["created"], "finished": finished, "href": href}
    if finished is None:
        out["cancel"] = CANCEL[section].format(row["id"])
    return out


def _rows(table: str, uid: int, running: tuple, after: float, columns: str = "*") -> tuple[list, list]:
    """Идущие и готовые после `after` строки таблицы истории пользователя."""
    marks = ", ".join("?" * len(running))
    going = state.DB.execute(f"SELECT {columns} FROM {table} WHERE user_id = ? AND state IN ({marks})"
                             " ORDER BY created DESC", (uid, *running)).fetchall()
    ends = ", ".join("?" * len(TERMINAL))
    done = state.DB.execute(f"SELECT {columns} FROM {table} WHERE user_id = ? AND state IN ({ends}) AND finished >= ?"
                            " ORDER BY finished DESC LIMIT ?", (uid, *TERMINAL, after, FINISHED_MAX)).fetchall()
    return going, done


def _photo(uid: int, after: float) -> list:
    going, done = _rows("images", uid, ("working", "cancelling", "queued"), after)
    out = []
    for row in going + done:
        job = images.JOBS.get(row["id"])
        detail = job.detail if job is not None and row["finished"] is None else row["detail"]
        kind = "Правка" if row["kind"] == "edit" else "Картинка"
        item = _item("photo", row, f"{kind} · {MODEL_NAMES.get(row['model'], row['model'])}", detail,
                     f"photo?id={row['id']}")
        item["started"] = images.started(row)        # строка серии — с перехода в работу, не с создания
        out.append(item)
    return out


def _video(uid: int, after: float) -> list:
    going, done = _rows("videos", uid, ("queued", "in_progress"), after)
    out = []
    for row in going + done:
        item = _item("video", row, f"Ролик · {row['seconds']} с", row["detail"], f"video?id={row['id']}")
        item["progress"] = row["progress"]
        out.append(item)
    return out


def _transcribe(uid: int, after: float) -> list:
    going, done = _rows("transcripts", uid, ("working", "queued"), after)
    out = []
    for row in going + done:
        job = transcripts.JOBS.get(row["id"])
        # этап без своего времени: время одно — общее, его считает страница
        detail = (job.stage if job is not None and row["finished"] is None
                  else transcripts.waiting(row) if row["state"] == "queued" else row["detail"])
        out.append(_item("transcribe", row, f"Расшифровка «{row['name']}»", detail, f"transcribe?id={row['id']}"))
    return out


def _ocr(uid: int, after: float) -> list:
    going, done = _rows("scans", uid, ocr.RUNNING, after)
    out = []
    for row in going + done:
        job = ocr.JOBS.get(row["id"])
        # этап идущего — без своего времени (как у расшифровки); ждущий и отменяемый — слова строки
        detail = job.stage if job is not None and row["state"] == "working" else row["detail"]
        out.append(_item("ocr", row, f"Распознавание «{row['name']}»", detail, f"ocr?id={row['id']}"))
    return out


def _translate(uid: int, after: float) -> list:
    # без исходника и перевода: до 300 000 знаков каждой записи на каждый опрос
    going, done = _rows("translations", uid, translate.RUNNING, after, "id, name, state, detail, created, finished")
    out = []
    for row in going + done:
        job = translate.JOBS.get(row["id"])
        detail = job.stage if job is not None and row["state"] == "working" else row["detail"]
        out.append(_item("translate", row, f"Перевод «{row['name']}»", detail, f"translate?id={row['id']}"))
    return out


def _speech(uid: int, after: float) -> list:
    going, done = _rows("speeches", uid, ("working", "queued"), after)
    out = []
    for row in going + done:
        job = speeches.JOBS.get(row["id"])
        detail = (job.stage if job is not None and row["finished"] is None
                  else speeches.waiting(row) if row["state"] == "queued" else row["detail"])
        text = row["text"]
        head = text if len(text) <= SPEECH_TITLE else text[:SPEECH_TITLE].rstrip() + "…"
        out.append(_item("speech", row, f"Озвучка · {head}", detail, f"speech?id={row['id']}"))
    return out


def _chat(uid: int) -> list:
    """Идущие ответы через сервер своих диалогов (частичный индекс `messages_streaming`)."""
    rows = state.DB.execute("SELECT m.id, m.chat_id, m.created, c.title FROM messages m JOIN chats c ON c.id = m.chat_id"
                            " WHERE c.user_id = ? AND m.status = 'streaming' AND m.via = 'server'"
                            " ORDER BY m.created DESC", (uid,)).fetchall()
    out = []
    for row in rows:
        live = chat.STREAMS.get(row["id"])
        detail = "пишет ответ" if live is not None and live.content else "жду ответ модели"
        out.append({"section": "chat", "id": row["id"], "state": "running",
                    "title": f"Ответ · {row['title'] or 'новый диалог'}", "detail": detail,
                    "started": row["created"], "finished": None, "href": f"chat/{row['chat_id']}",
                    "cancel": f"/api/chats/{row['chat_id']}/stop"})
    return out


@router.get("")
async def list_jobs(request: Request, since: "float | None" = None):
    uid, now = request.state.user.id, clock.NOW()
    after = now - JOBS_RECENT_SEC if since is None else max(since, now - JOBS_RECENT_SEC)
    hidden = lock_policy.hidden(request.state.digest)
    items = (_photo(uid, after) + _video(uid, after) + _transcribe(uid, after) + _ocr(uid, after)
             + _translate(uid, after) + _speech(uid, after))
    items = [i for i in items if i["section"] not in hidden
             and not (i["section"] == "ocr" and hidden and lock_policy.scan_sections(uid, i["id"]) & hidden)]
    running = sorted([i for i in items if i["finished"] is None] + ([] if "chat" in hidden else _chat(uid)), key=lambda i: i["started"], reverse=True)
    finished = sorted((i for i in items if i["finished"] is not None), key=lambda i: i["finished"], reverse=True)
    subscribed = state.DB.execute("SELECT 1 FROM push_subs WHERE digest = ? UNION ALL"
                                  " SELECT 1 FROM ntfy_devices WHERE user_id = ? AND digest = ? AND enabled = 1",
                                  (request.state.digest, uid, request.state.digest)).fetchone()
    return {"now": now, "running": running, "finished": finished[:FINISHED_MAX], "push": subscribed is not None}
