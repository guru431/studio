"""PIN-protected, revocable chat snapshots. Public secrets travel only in request bodies.

The link token lives in the URL fragment (never Apache's request log). A successful
PIN check grants a separate, expiring read token, never a Studio account session.

Снимок переписки хранится только шифром STUDIO_DATA_KEY (`chat_shares.sealed`, db.SHARES_36), как токены
подключений: в базе и ночных копиях открытого текста нет. В прежнем столбце `snapshot` — заглушка `PLACEHOLDER`,
её прежний код (откат) покажет вместо снимка. Открытые снимки (до шага 36 или прежнего кода) шифрует `seal()` при
старте. Сменили ключ данных — снимок не расшифровать: ссылка «недоступна», владелец создаёт новую.

Срок ссылки — `days` при создании (1, 7, 30 дней или бессрочно, по умолчанию `DAYS_DEFAULT`): истёкшая удаляется
со снимком при первом же обращении к ссылкам или старте. Неверные PIN считаются и без окна (`fails`): `FAILS_MAX`
отзывает ссылку, владельцу — push/ntfy без содержимого и `share_locked` в журнале безопасности.

Кроме чатов — запись другого раздела (`ITEMS`, шаг 69): расшифровка, перевод, скан, картинка, ролик, чат «Озвучки»
(шаг 82); снимок — `item_snapshot`, маршруты — `…/<id>/share` под префиксом раздела (блокировка раздела — та же).
Переписка Open WebUI — свой вид `OWUI` (снимок и маршруты — owui_share.py). «Обновить снимок» (PUT) заменяет
содержимое под тем же адресом, сроком и PIN, счётчики остаются.
"""
import asyncio
import base64
import json
import logging
import uuid

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictBool, field_validator

import auth
import bridge
import chat
import chat_skills
import claude_code
import clock
import images
import media
import lock_policy
import ocr
import operations
import owui
import push
import security
import security_log
import speeches
import state
import transcripts
import translate
import videos

log = logging.getLogger("studio")
router = APIRouter()
PIN_FAILS = 5
IP_ATTEMPTS = 30
LOCK_SEC = 900
#: все неверные PIN ссылки, без окна: пауза LOCK_SEC сдерживает темп, а этот предел — сам подбор (4 цифры)
FAILS_MAX = 50
#: срок ссылки в днях; None — бессрочно. Неделя по умолчанию: ссылку обычно открывают в первые дни, а снимок
#: переписки и подбор PIN не должны жить дольше нужного — бессрочную владелец выбирает явно
DAYS = (1, 7, 30)
DAYS_DEFAULT = 7
#: в прежнем столбце `snapshot`: прежний код (откат) отдаст получателю это вместо зашифрованной переписки
PLACEHOLDER = {"title": "Ссылка недоступна", "messages": [{
    "role": "assistant", "attachments": [], "text": "Эту ссылку создала более новая версия Студии, и сейчас её не "
    "открыть. Попросите владельца прислать ссылку заново."}]}
PLACEHOLDER_RAW = json.dumps(PLACEHOLDER, ensure_ascii=False, separators=(",", ":"))
VIEW_SEC = 8 * 3600
MAX_MESSAGES = 10000
MAX_SNAPSHOT = 16 * 1024 * 1024
PUBLIC_BODY_MAX = 4096
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                  "X-Robots-Tag": "noindex, nofollow, noarchive"}
#: записи других разделов, которыми делятся ссылкой (шаг 69): таблица (`chat_shares.item_kind`) → раздел (блокировка,
#: «Доступы и ссылки», журнал) и префикс маршрутов раздела. Окончательное удаление записи уносит ссылку триггером
#: (db.SHARES_69), запись в корзине — `LIVE`
ITEMS = {"transcripts": ("transcribe", "/api/transcripts"), "translations": ("translate", "/api/translate"),
         "scans": ("ocr", "/api/ocr"), "images": ("photo", "/api/images"), "videos": ("video", "/api/videos"),
         # чат «Озвучки» (шаг 82): группа записей с одним `speeches.chat_id`, `item_id` — этот chat_id
         "speech_chats": ("speech", "/api/speeches/chats")}
#: своя запись не из корзины, иначе 404 (`_row` раздела; у чата «Озвучки» — его записи)
_ROWS = {"transcripts": transcripts._row, "translations": translate._row, "scans": ocr._row, "images": images._row,
         "videos": videos._row, "speech_chats": speeches._chat_rows}
#: переписка Open WebUI (шаг 82, owui_share.py): свой `item_kind`, а не вид «как у Агентов» — прежний код (откат) его
#: не знает и ссылку не открывает; `item_id` — "<owui_accounts.id>:<id чата>"
OWUI = "owui"
#: вид записи ссылки → раздел (блокировка, «Доступы и ссылки», журнал)
SECTIONS = {kind: section for kind, (section, _) in ITEMS.items()} | {OWUI: "owui"}


async def public_guard(request: Request, call_next):
    """Bound anonymous JSON before FastAPI parses it, including chunked requests (`/share/*` and `/f/*`, my_files)."""
    if request.method == "POST" and request.url.path.startswith(("/share/", "/f/")):
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > PUBLIC_BODY_MAX:
                return JSONResponse({"detail": {"code": "too_big", "message": "слишком большой запрос"}},
                                    status_code=413, headers=PUBLIC_HEADERS)
            body.extend(block)
        request._body = bytes(body)
    return await call_next(request)


class RefreshIn(BaseModel):
    """«Обновить снимок»: то же содержимое, что при создании, — без PIN и срока (они у ссылки остаются)."""
    title: str = Field(default="Чат", max_length=200)
    #: рассуждение модели обычного чата — черновик, который владелец обычно не перечитывал: только по выбору
    reasoning: StrictBool = False
    #: запись раздела: приложить исходный файл (запись разговора, исходный документ) — только по выбору
    source: StrictBool = False


class ShareIn(RefreshIn):
    pin: str = Field(strict=True, pattern=r"^[0-9]{4}$", min_length=4, max_length=4)
    days: int | None = Field(default=DAYS_DEFAULT, strict=True)

    @field_validator("days")
    @classmethod
    def _days(cls, value):
        if value is not None and value not in DAYS:
            raise ValueError("срок ссылки — 1, 7, 30 дней или бессрочно")
        return value


class UnlockIn(BaseModel):
    token: str = Field(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$", max_length=43)
    pin: str = Field(strict=True, pattern=r"^[0-9]{4}$", min_length=4, max_length=4)


class ReadIn(BaseModel):
    access: str = Field(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$", max_length=43)


class FileIn(ReadIn):
    file: str = Field(pattern=r"^[0-9a-f]{32}$", max_length=32)


def _refuse(status=404, code="share_gone", message="ссылка недоступна или отключена"):
    return HTTPException(status, {"code": code, "message": message}, headers=PUBLIC_HEADERS)


def _gone():
    """«Обновить снимок» без ссылки: её отключили или она истекла — создать новую."""
    raise _refuse(404, "share_gone", "ссылка отключена или истекла — создайте новую")


def _owner_open(request: Request, section: str) -> None:
    if (auth._live(request.cookies.get(auth.COOKIE), renew=False) is None
            or lock_policy.locked(request.state.digest, section)):
        raise _refuse(409, "closed", "сеанс Студии закрыт")
    operations.check(request.state.user.id)


def _expire() -> None:
    """Истёкшие ссылки — прочь вместе со снимком, читателями и списком файлов (каскад)."""
    state.DB.execute("DELETE FROM chat_shares WHERE expires IS NOT NULL AND expires <= ?", (clock.NOW(),))


def seal() -> int:
    """При старте: снимки открытым текстом — шифром, на их место — заглушка. secure_delete на время записи: SQLite
    обнулит освобождённые страницы с прежним текстом, а не оставит их в файле базы и её копиях."""
    _expire()
    rows = state.DB.execute("SELECT id, snapshot FROM chat_shares WHERE sealed IS NULL").fetchall()
    if not rows:
        return 0
    before = state.DB.execute("PRAGMA secure_delete").fetchone()[0]
    state.DB.execute("PRAGMA secure_delete = ON")
    try:
        for row in rows:
            state.DB.execute("UPDATE chat_shares SET sealed = ?, snapshot = ? WHERE id = ? AND sealed IS NULL",
                             (security.encrypt(row["snapshot"]), PLACEHOLDER_RAW, row["id"]))
    finally:
        state.DB.execute(f"PRAGMA secure_delete = {int(before)}")
    state.DB.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log.info("ссылки «Поделиться»: зашифровано снимков — %d", len(rows))
    return len(rows)


def disown() -> int:
    """При старте: ссылки на агентские чаты, созданные не владельцем компьютера (участник проекта прежним кодом
    или до отката), — прочь. Владелец их не видел и не мог отключить, а уникальный индекс `chat_shares_agent`
    не давал ему поделиться тем же чатом."""
    gone = state.DB.execute("DELETE FROM chat_shares WHERE agent_id IS NOT NULL AND user_id IS NOT"
                            " (SELECT user_id FROM agents WHERE id = chat_shares.agent_id)").rowcount
    if gone:
        log.info("ссылки «Поделиться»: удалено ссылок участников на чужие компьютеры — %d", gone)
    return gone


def _own_computer(request: Request, aid: int) -> None:
    """Ссылку на агентский чат создаёт и отзывает только владелец компьютера: участник проекта видит его чаты,
    но публиковать их наружу от своего имени не может (CLAUDE.md «Пользователь видит своё и явно выданные…»)."""
    if claude_code.own_agent(request, aid)["user_id"] != request.state.user.id:
        raise _refuse(403, "owner_only", "ссылкой на чат проекта делится только владелец компьютера")


def _owned(request: Request, *, chat_id=None, agent_id=None, project=None, session_id=None, engine=None,
           item_kind=None, item_id=None):
    _expire()
    return state.DB.execute(
        "SELECT * FROM chat_shares WHERE user_id = ? AND chat_id IS ? AND agent_id IS ?"
        " AND project IS ? AND session_id IS ? AND engine IS ? AND item_kind IS ? AND item_id IS ?",
        (request.state.user.id, chat_id, agent_id, project, session_id, engine, item_kind, item_id)).fetchone()


def _detail(agent_id, item_kind) -> str:
    """Код подробности журнала: `chat`, `code` или раздел записи (`SECTIONS`)."""
    if agent_id:
        return "code"
    return SECTIONS[item_kind] if item_kind in SECTIONS else "chat"


def _out(row) -> dict:
    if row is None:
        return {"enabled": False}
    try:
        url = state.CFG.origin.rstrip("/") + "/share#" + security.decrypt(row["token_enc"])
    except InvalidToken:          # сменили STUDIO_DATA_KEY: ссылку не показать и не открыть — создать новую
        url = None
    return {"enabled": True, "url": url, "created": row["created"], "expires": row["expires"],
            "views": row["views"], "viewed": row["viewed"], "fails": row["fails"]}


def _encode(snapshot: dict) -> str:
    try:
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        size = len(raw.encode("utf-8"))
    except (ValueError, UnicodeEncodeError):
        raise _refuse(422, "invalid", "неверные данные переписки") from None
    if size > MAX_SNAPSHOT or len(snapshot["messages"]) > MAX_MESSAGES:
        raise _refuse(413, "share_size", "переписка слишком большая для ссылки")
    return raw


async def _store(request: Request, body: RefreshIn, snapshot: dict, expected, files=(), *, refresh=False, **source):
    """Создать ссылку (новый адрес, PIN и срок; прежняя отзывается) или, `refresh`, заменить снимок и файлы
    `expected` под тем же адресом, сроком и PIN — счётчики открытий и неверных PIN остаются."""
    sealed = await asyncio.to_thread(security.encrypt, _encode(snapshot))
    pin_hash = None if refresh else await asyncio.to_thread(security.hash_password, body.pin)
    if source.get("agent_id"):
        await claude_code.ask(request, source["agent_id"], "history", project=source["project"],
                              chat=source["session_id"], before=None, limit=1)
        _own_computer(request, source["agent_id"])
    kind = source.get("item_kind")
    _owner_open(request, "code" if source.get("agent_id") else SECTIONS[kind] if kind else "chat")
    if source.get("chat_id"):
        chat.unprotected(chat._chat_row(request.state.user.id, source["chat_id"]))
    if kind in _ROWS:
        _ROWS[kind](request.state.user.id, source["item_id"])     # запись не ушла в корзину, пока собирали снимок
    elif kind == OWUI:                                             # учётку Open WebUI не удалили, пока читали чат
        owui.load(request.state.user.id, int(source["item_id"].split(":", 1)[0]))
    current = _owned(request, **source)
    if (current["id"] if current else None) != (expected["id"] if expected else None):
        raise _refuse(409, "share_changed", "настройки ссылки изменились — откройте окно заново")
    if refresh and current is None:
        _gone()
    # название чата без своей строки в базе Студии — шифром у ссылки («Доступы и ссылки»)
    label = security.encrypt(snapshot["title"]) if source.get("agent_id") or kind == OWUI else None
    token, share_id, now = security.new_token(), uuid.uuid4().hex, clock.NOW()
    state.DB.execute("BEGIN")
    try:
        if refresh:
            share_id = current["id"]
            state.DB.execute("UPDATE chat_shares SET sealed = ?, snapshot = ?, label = ? WHERE id = ?",
                             (sealed, PLACEHOLDER_RAW, label, share_id))
            state.DB.execute("DELETE FROM chat_share_files WHERE share_id = ?", (share_id,))
        else:
            if current:
                state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (current["id"],))
            state.DB.execute(
                "INSERT INTO chat_shares(id,user_id,chat_id,agent_id,project,session_id,engine,token_hash,token_enc,"
                "pin_hash,snapshot,sealed,created,expires,label,item_kind,item_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (share_id, request.state.user.id, source.get("chat_id"), source.get("agent_id"),
                 source.get("project"), source.get("session_id"), source.get("engine"), security.digest(token),
                 security.encrypt(token), pin_hash, PLACEHOLDER_RAW, sealed, now,
                 None if body.days is None else now + body.days * 86400, label, kind, source.get("item_id")))
        state.DB.executemany("INSERT INTO chat_share_files(share_id,media_id) VALUES(?,?)",
                             [(share_id, mid) for mid in set(files)])
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    _journal(request, "share_refresh" if refresh else "share_create", source)
    return _out(_owned(request, **source))


def _journal(request: Request, kind: str, source: dict) -> None:
    security_log.record(request.state.user.id, kind, detail=_detail(source.get("agent_id"), source.get("item_kind")),
                        digest=request.state.digest, ip=auth.client_ip(request))


@router.get("/api/chats/{chat_id}/share")
async def chat_share(chat_id: str, request: Request):
    chat._chat_row(request.state.user.id, chat_id)
    return _out(_owned(request, chat_id=chat_id))


def regular_snapshot(user_id: int, chat_id: str, reasoning: bool = True) -> tuple[dict, list[str]]:
    """Снимок обычного чата; `reasoning=False` — без рассуждения модели (ссылка по умолчанию). Диалог, где сработал
    защищённый источник, — 409 `protected` (`chat.unprotected`): ни ссылки, ни экспорта, ни архива."""
    row = chat.unprotected(chat._chat_row(user_id, chat_id))
    atts = chat._attachments(chat_id)
    files, messages = [], []
    for message in chat._messages(chat_id):
        # what the chat page shows (chat._message_out): «Интернет»/MCP steps without text for the model, the words
        # of an answer that failed, the text of an answer still being written
        shown = chat._message_out(message, [])
        attachments = []
        for a in atts.get(message["id"], []):
            # Missing/deleted source files are never turned into broader media access.
            if state.DB.execute("SELECT 1 FROM media WHERE id = ? AND user_id = ?",
                                (a["media_id"], user_id)).fetchone():
                files.append(a["media_id"])
                attachments.append({"file": a["media_id"], "name": a["name"], "kind": a["kind"]})
        messages.append({"role": message["role"], "text": shown["content"], "attachments": attachments,
                         "reasoning": shown["reasoning"] if reasoning else None, "status": message["status"],
                         "tools": shown["tools"], "error": shown["error"], "todo": shown["todo"]})
        # навык (решение 18 спеки 2026-11-25): только имя и то, что дописал человек, — без текста навыка
        messages[-1]["skill"] = shown["skill"]["name"] if shown["skill"] else None
        if shown["skill"]:
            messages[-1]["text"] = chat_skills.human(message)
    # без названия — пусто: подпись «Чат» на языке читателя ставят страница ссылки (`tr`) и экспорт (`exports.word`)
    snapshot = {"title": row["title"] or "", "messages": messages}
    _encode(snapshot)
    return snapshot, files


@router.post("/api/chats/{chat_id}/share", status_code=201)
async def create_chat_share(chat_id: str, body: ShareIn, request: Request):
    expected = _owned(request, chat_id=chat_id)
    snapshot, files = regular_snapshot(request.state.user.id, chat_id, body.reasoning)
    return await _store(request, body, snapshot, expected, files, chat_id=chat_id)


@router.put("/api/chats/{chat_id}/share")
async def refresh_chat_share(chat_id: str, body: RefreshIn, request: Request):
    expected = _owned(request, chat_id=chat_id) or _gone()
    snapshot, files = regular_snapshot(request.state.user.id, chat_id, body.reasoning)
    return await _store(request, body, snapshot, expected, files, refresh=True, chat_id=chat_id)


@router.delete("/api/chats/{chat_id}/share", status_code=204)
async def revoke_chat_share(chat_id: str, request: Request):
    chat._chat_row(request.state.user.id, chat_id)
    if state.DB.execute("DELETE FROM chat_shares WHERE chat_id = ? AND user_id = ?",
                        (chat_id, request.state.user.id)).rowcount:
        _journal(request, "share_revoke", {})


def _agent_source(request, aid, pid, session_id):
    claude_code.own_agent(request, aid)
    claude_code.check_pid(pid)
    claude_code.check_chat(session_id)
    return {"agent_id": aid, "project": pid, "session_id": session_id,
            "engine": claude_code.chat_engine(request, aid, session_id)}


@router.get("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share")
async def agent_share(aid: int, pid: str, session_id: str, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    await claude_code.ask(request, aid, "history", project=pid, chat=session_id, before=None, limit=1)
    return _out(_owned(request, **source))


def _changes(value) -> list:
    """Правки шага для диффа на странице ссылки: только путь и тексты правки, как их отдал мост."""
    return [{key: change[key] for key in ("path", "patch", "before", "after") if isinstance(change.get(key), str)}
            | ({"truncated": True} if change.get("truncated") is True else {})
            for change in (value if isinstance(value, list) else []) if isinstance(change, dict)
            and isinstance(change.get("path"), str)]


def _parts(item: dict, text: str, steps: list) -> list | None:
    """Порядок текста и шагов ответа по `timeline` моста (протокол, мост 0.15.0): `[{text}, {step: номер}]` — без id
    шагов. Те же правила, что у страницы (`code-chat.js` renderTimeline): куски текста подряд от начала до конца в
    единицах UTF-16, каждый шаг ровно раз; иначе None — прежний показ «текст, затем шаги»."""
    timeline = item.get("timeline")
    ids = {step.get("id"): n for n, step in enumerate(steps)}
    if not isinstance(timeline, list) or not timeline or len(timeline) > 2000 or len(ids) != len(steps) or None in ids:
        return None
    units = text.encode("utf-16-le", "surrogatepass")
    parts, position, seen = [], 0, set()
    try:
        for part in timeline:
            if not isinstance(part, dict):
                return None
            if part.get("kind") == "step" and part.get("id") in ids and part["id"] not in seen:
                seen.add(part["id"])
                parts.append({"step": ids[part["id"]]})
            elif (part.get("kind") == "text" and type(part.get("start")) is int and type(part.get("end")) is int
                    and part["start"] == position <= part["end"] <= len(units) // 2):
                parts.append({"text": units[2 * position:2 * part["end"]].decode("utf-16-le", "surrogatepass")})
                position = part["end"]
            else:
                return None
    except UnicodeDecodeError:
        return None
    return parts if position == len(units) // 2 and len(seen) == len(steps) else None


#: картинок истории, которые ссылка «Поделиться» дочитывает у моста (по просьбе `history_image` на каждую)
SHARE_IMAGES_MAX = 50


async def _history_images(request: Request, aid: int, pid: str, session_id: str, snapshot: dict, wanted: list) -> None:
    """Байты картинок истории — в снимок ссылки, пока он не больше `MAX_SNAPSHOT`; не вошедшие остаются
    `{name, missing: true}` — получатель видит, что картинка была. Мост без `hello.history_images` — все заглушки."""
    link = bridge.LINKS.get(aid)
    if not wanted or link is None or not link.hello.get("history_images"):
        return
    size = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
    for image, message_id, index in wanted[:SHARE_IMAGES_MAX]:
        try:
            data = await claude_code.ask(request, aid, "history_image", quiet=True, passive=True, project=pid,
                                         chat=session_id, message_id=message_id, index=index)
            raw, mime = await asyncio.to_thread(claude_code._image_bytes, data, max_bytes=claude_code.IMAGE_MAX)
        except HTTPException as error:
            if error.detail.get("code") in ("closed", "offline"):
                raise
            continue
        encoded = base64.b64encode(raw).decode("ascii")
        if size + len(encoded) + 64 > MAX_SNAPSHOT:
            continue
        size += len(encoded) + 64
        image.pop("missing")
        image.update(mime=mime, data=encoded)


UNREADABLE = "Историю этой версии DeepSeek Harness студия пока не читает"
OTHER_FOLDER = "Папка этого чата DeepSeek Harness не совпадает с проектом — историю студия не показывает"


def unreadable(data) -> None:
    """Мост не прочёл историю (`notice`, DeepSeek Harness): пустой снимок не делаем — 409 со словами заметки."""
    notice = data.get("notice") if isinstance(data, dict) else None
    if notice in ("unsupported_format", "cwd_mismatch"):
        raise _refuse(409, "history_unavailable", OTHER_FOLDER if notice == "cwd_mismatch" else UNREADABLE)


async def agent_snapshot(request: Request, aid: int, pid: str, session_id: str, title: str, *,
                         images: bool = False) -> dict:
    """Снимок агентского чата. `images` — ссылка «Поделиться»: картинки истории дочитываются у моста
    (`_history_images`); экспорт их не читает."""
    _agent_source(request, aid, pid, session_id)
    pages, before, total_size, count = [], None, 0, 0
    while True:
        data = await claude_code.ask(request, aid, "history", project=pid, chat=session_id, before=before, limit=100)
        unreadable(data)
        items, start = data.get("items"), data.get("start")
        if not isinstance(items, list) or type(start) is not int or start < 0 or (before is not None and start >= before):
            raise _refuse(502, "bridge", "мост ответил непонятно")
        total_size += len(json.dumps(items, ensure_ascii=True))
        count += len(items)
        if total_size > MAX_SNAPSHOT or count > MAX_MESSAGES:
            raise _refuse(413, "share_size", "переписка слишком большая для ссылки")
        pages.append(items)
        if start == 0:
            break
        before = start
    messages, wanted = [], []
    for page in reversed(pages):
        for item in page:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                continue
            # Explicit fields: never publish project paths, machine/session metadata or live controls.
            text = item.get("text") if isinstance(item.get("text"), str) else ""
            steps = [step for step in (item["steps"] if isinstance(item.get("steps"), list) else [])
                     if isinstance(step, dict)]
            message = {"role": item["role"], "text": text,
                       "steps": [{key: step[key] for key in ("name", "tool", "input", "text", "error") if key in step}
                                 | ({"changes": changes} if not step.get("error") and (changes := _changes(step.get("changes")))
                                    else {}) for step in steps], "images": []}
            for image in (item["images"] if isinstance(item.get("images"), list) else []):
                if not isinstance(image, dict):
                    continue
                kept = {key: image[key] for key in ("mime", "name", "data") if key in image}
                if "data" not in kept:
                    # Картинка истории — ссылкой {index, name} (протокол §2.2): байты дочитываются ниже или её нет.
                    kept = {**({"name": image["name"]} if isinstance(image.get("name"), str) else {}), "missing": True}
                    if isinstance(item.get("id"), str) and type(image.get("index")) is int and image["index"] >= 0:
                        wanted.append((kept, item["id"], image["index"]))
                message["images"].append(kept)
            if (parts := _parts(item, text, steps)) is not None:
                message["parts"] = parts
            messages.append(message)
    snapshot = {"title": title, "messages": messages}
    _encode(snapshot)
    if images:
        await _history_images(request, aid, pid, session_id, snapshot, wanted)
    return snapshot


@router.post("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share", status_code=201)
async def create_agent_share(aid: int, pid: str, session_id: str, body: ShareIn, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    _own_computer(request, aid)
    expected = _owned(request, **source)
    snapshot = await agent_snapshot(request, aid, pid, session_id, body.title, images=True)
    return await _store(request, body, snapshot, expected, **source)


@router.put("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share")
async def refresh_agent_share(aid: int, pid: str, session_id: str, body: RefreshIn, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    _own_computer(request, aid)
    expected = _owned(request, **source) or _gone()
    snapshot = await agent_snapshot(request, aid, pid, session_id, body.title, images=True)
    return await _store(request, body, snapshot, expected, refresh=True, **source)


@router.delete("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/share", status_code=204)
async def revoke_agent_share(aid: int, pid: str, session_id: str, request: Request):
    source = _agent_source(request, aid, pid, session_id)
    _own_computer(request, aid)
    row = _owned(request, **source)
    if row:
        state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (row["id"],))
        _journal(request, "share_revoke", source)


def _media(uid: int, media_id) -> "dict | None":
    """Файл записи для снимка — `{file, name, mime}` (получатель берёт его `/share/file` по read-токену); нет — None."""
    row = state.DB.execute("SELECT id, mime, download_name FROM media WHERE id = ? AND user_id = ?",
                           (media_id, uid)).fetchone() if media_id else None
    return None if row is None else {"file": row["id"], "name": row["download_name"], "mime": row["mime"]}


def _not_ready(message: str) -> HTTPException:
    return HTTPException(409, {"code": "running", "message": message})


async def item_snapshot(uid: int, kind: str, item_id: str, source: bool = False) -> tuple[dict, list[str]]:
    """Снимок записи раздела (`ITEMS`) и id его файлов. Что видит получатель: у расшифровки — реплики с собеседником
    и временем (с правками владельца), у перевода — исходный текст и перевод, у скана — текст страниц, у картинки и
    ролика — сам файл, промпт и модель, у чата «Озвучки» — готовые записи по порядку (текст, голос, mp3; запись
    вне `speeches.TERMINAL` — 409 `running`). `source` — ещё исходный файл записи (запись разговора, документ) для
    скачивания. `messages` — заглушка `PLACEHOLDER`: её покажет прежний код (откат), новый смотрит `item`."""
    row = _ROWS[kind](uid, item_id)
    item, title, attach = {"kind": kind}, "", None
    if kind == "transcripts":
        row = transcripts._done(uid, item_id)
        segments = transcripts.view(row, await transcripts.result_of(uid, row))
        item |= {"duration": row["duration"], "segments": [
            {key: s[key] for key in ("start", "end", "speaker", "text")} | ({"system": True} if s.get("system") else {})
            for s in segments]}
        title, attach = row["name"], row["listen_media_id"] or row["source_media_id"]
    elif kind == "translations":
        text = translate.joined(item_id) if row["state"] in translate.TERMINAL else ""
        if not text:
            raise _not_ready("перевода ещё нет")
        original = state.DB.execute("SELECT source FROM translations WHERE id = ?", (item_id,)).fetchone()[0]
        if original is None:          # снимок: исходник — блоки надписей
            parts = [x for p in state.DB.execute("SELECT source, sep FROM translation_parts WHERE translation_id = ?"
                                                 " ORDER BY n", (item_id,)) for x in (p["source"] or "", p["sep"])]
            original = "".join(parts[:-1])
        item |= {"source": original, "text": text, "source_lang": row["detected_lang"] or row["source_lang"],
                 "target_lang": row["result_lang"] or row["target_lang"], "markdown": row["scan_id"] is not None}
        title, attach = row["name"], row["source_media_id"]
    elif kind == "scans":
        row = ocr._readable(uid, item_id)
        item["pages"] = [{"n": p["n"], "state": p["state"], "text": p["text"]} for p in ocr._page_list(item_id)]
        title, attach = row["name"], row["source_media_id"]
    elif kind == "speech_chats":
        if any(r["state"] not in speeches.TERMINAL for r in row):
            raise _not_ready("дождитесь озвучки или отмените её")
        records = [{"text": r["text"], "voice": r["voice_name"] or r["voice"], "duration": r["duration"], "file": shown}
                   for r in row if r["state"] == "done" and (shown := _media(uid, r["media_id"]))]
        if not records:
            raise _not_ready("в чате ещё нет готовых озвучек")
        item["records"] = records
        title = records[0]["text"][:80]
    else:
        if row["state"] != "done" or not row["media_id"]:
            raise _not_ready("картинка ещё не готова" if kind == "images" else "ролик ещё не готов")
        shown = _media(uid, row["media_id"])
        if shown is None:
            raise HTTPException(404, media.GONE)
        item |= {"prompt": row["prompt"], "model": row["model"], "file": shown}
        item |= {"size": row["size"]} if kind == "images" else {"seconds": row["seconds"]}
    files = [item["file"]["file"]] if "file" in item else [r["file"]["file"] for r in item.get("records", ())]
    if source and (extra := _media(uid, attach)):
        item["attachments"] = [extra]
        files.append(extra["file"])
    snapshot = {"title": title, "messages": PLACEHOLDER["messages"], "item": item}
    _encode(snapshot)
    return snapshot, files


def _item_routes(kind: str) -> None:
    """`GET/POST/PUT/DELETE <префикс раздела>/<id>/share`: под префиксом раздела маршрут закрывает та же блокировка
    раздела (lock_policy.blocks), что и саму запись."""
    path = ITEMS[kind][1] + "/{item_id}/share"

    async def get_share(item_id: str, request: Request):
        _ROWS[kind](request.state.user.id, item_id)
        return _out(_owned(request, item_kind=kind, item_id=item_id))

    async def create_share(item_id: str, body: ShareIn, request: Request):
        expected = _owned(request, item_kind=kind, item_id=item_id)
        snapshot, files = await item_snapshot(request.state.user.id, kind, item_id, body.source)
        return await _store(request, body, snapshot, expected, files, item_kind=kind, item_id=item_id)

    async def refresh_share(item_id: str, body: RefreshIn, request: Request):
        _ROWS[kind](request.state.user.id, item_id)
        expected = _owned(request, item_kind=kind, item_id=item_id) or _gone()
        snapshot, files = await item_snapshot(request.state.user.id, kind, item_id, body.source)
        return await _store(request, body, snapshot, expected, files, refresh=True, item_kind=kind, item_id=item_id)

    async def revoke_share(item_id: str, request: Request):
        _ROWS[kind](request.state.user.id, item_id)
        if state.DB.execute("DELETE FROM chat_shares WHERE item_kind = ? AND item_id = ? AND user_id = ?",
                            (kind, item_id, request.state.user.id)).rowcount:
            _journal(request, "share_revoke", {"item_kind": kind})

    router.add_api_route(path, get_share, methods=["GET"], name=f"{kind}_share")
    router.add_api_route(path, create_share, methods=["POST"], status_code=201, name=f"create_{kind}_share")
    router.add_api_route(path, refresh_share, methods=["PUT"], name=f"refresh_{kind}_share")
    router.add_api_route(path, revoke_share, methods=["DELETE"], status_code=204, name=f"revoke_{kind}_share")


for _kind in ITEMS:
    _item_routes(_kind)


@router.api_route("/share", methods=["GET", "HEAD"])
async def share_page():
    return HTMLResponse(auth.page("share.html"), headers=PUBLIC_HEADERS)


def _same_origin(request):
    if auth.foreign_origin(request) or request.headers.get("sec-fetch-site") == "cross-site":
        raise _refuse(403, "origin", "откройте ссылку в браузере")


#: ссылка обычного чата в корзине «недоступна», после «Вернуть» — снова открывается (строка и снимок — на месте);
#: чата, где сработал защищённый источник (`chats.protected_used`), — «недоступна» с того же мгновения и навсегда;
#: ссылка записи раздела (`ITEMS`) — так же, пока запись в корзине; чата «Озвучки» — пока у него нет живых записей;
#: переписки Open WebUI — пока учётка Open WebUI подключена. Вид, которого здесь нет, недоступен — так прежний код
#: (откат) не открывает ссылки новых видов
_ALIVE = {**{t: f"s.item_id IN (SELECT id FROM {t} WHERE deleted IS NULL)"
             for t in ("transcripts", "translations", "scans", "images", "videos")},
          "speech_chats": "s.item_id IN (SELECT chat_id FROM speeches WHERE deleted IS NULL AND user_id = s.user_id)",
          OWUI: "CAST(substr(s.item_id, 1, instr(s.item_id, ':') - 1) AS INTEGER) IN"
                " (SELECT id FROM owui_accounts WHERE user_id = s.user_id)"}
LIVE = (" AND (s.chat_id IS NULL OR s.chat_id IN (SELECT id FROM chats WHERE deleted IS NULL"
        " AND protected_used = 0)) AND (s.item_kind IS NULL OR "
        + " OR ".join(f"(s.item_kind = '{kind}' AND {alive})" for kind, alive in _ALIVE.items()) + ")")


def _public_share(token_hash):
    row = state.DB.execute("SELECT s.* FROM chat_shares s JOIN users u ON u.id = s.user_id"
                            " WHERE s.token_hash = ? AND u.disabled = 0 AND (s.expires IS NULL OR s.expires > ?)"
                            + LIVE, (token_hash, clock.NOW())).fetchone()
    if row is None:
        raise _refuse()
    return row


def _open(row, pin: str) -> "dict | None":
    """В потоке: верен ли PIN и, если да, снимок (scrypt и расшифровка многомегабайтного шифра — мимо цикла
    событий). Снимок не расшифровать (сменили STUDIO_DATA_KEY) — `_refuse`, как отозванную ссылку."""
    if not security.verify_password(pin, row["pin_hash"]):
        return None
    try:
        raw = row["snapshot"] if row["sealed"] is None else security.decrypt(row["sealed"])
        return json.loads(raw)
    except (InvalidToken, ValueError):
        raise _refuse(404, "share_gone", "ссылка недоступна — попросите владельца создать новую") from None


def _wrong_pin(request: Request, row, reserved: int) -> None:
    """Неверный PIN: в журнал владельца (повторы свернёт security_log), общий счёт ссылки; FAILS_MAX — отзыв."""
    uid, ip = row["user_id"], auth.client_ip(request)
    detail = _detail(row["agent_id"], row["item_kind"])
    fails =state.DB.execute("UPDATE chat_shares SET fails = fails + 1 WHERE id = ? RETURNING fails",
                             (row["id"],)).fetchone()[0]
    security_log.record(uid, "share_pin_fail", detail=detail, ip=ip)
    if fails >= FAILS_MAX:
        state.DB.execute("DELETE FROM chat_shares WHERE id = ?", (row["id"],))
        security_log.record(uid, "share_locked", detail="revoked", ip=ip)
        push.notify_later(uid, {"ru": f"Ссылка «Поделиться» отключена: {FAILS_MAX} неверных PIN",
                                "en": f"Shared link disabled: {FAILS_MAX} incorrect PINs"}, "settings/security",
                          "share-" + row["id"], title=push.STUDIO)
        log.info("пользователь %d: ссылка отозвана после %d неверных PIN", uid, fails)
        raise _refuse()
    if reserved == PIN_FAILS:
        security_log.record(uid, "share_locked", detail="pause", ip=ip)


@router.post("/share/unlock")
async def unlock(body: UnlockIn, request: Request):
    _same_origin(request)
    now = clock.NOW()
    _expire()
    state.DB.execute("DELETE FROM chat_share_attempts WHERE until <= ?", (now,))
    # истёкший просмотр помнится ещё VIEW_SEC: его страница услышит «введите PIN снова» (`_visitor`)
    state.DB.execute("DELETE FROM chat_share_visitors WHERE expires <= ?", (now - VIEW_SEC,))
    ip_hash = security.digest(auth.client_ip(request))
    state.DB.execute("INSERT INTO chat_share_attempts VALUES(?,1,?) ON CONFLICT(ip_hash)"
                     " DO UPDATE SET attempts = attempts + 1", (ip_hash, now + LOCK_SEC))
    ip = state.DB.execute("SELECT attempts FROM chat_share_attempts WHERE ip_hash = ?", (ip_hash,)).fetchone()
    if ip[0] > IP_ATTEMPTS:
        raise _refuse(429, "share_locked", "слишком много попыток — подождите 15 минут")
    row = _public_share(security.digest(body.token))
    if row["attempt_until"] <= now:
        state.DB.execute("UPDATE chat_shares SET attempts = 0, attempt_until = ? WHERE id = ?",
                         (now + LOCK_SEC, row["id"]))
    elif row["attempts"] >= PIN_FAILS:
        raise _refuse(429, "share_locked", "слишком много попыток — подождите 15 минут")
    # Reserve before awaiting the hash: a concurrent batch cannot bypass the limit.
    reserved = state.DB.execute("UPDATE chat_shares SET attempts = attempts + 1 WHERE id = ? RETURNING attempts",
                                (row["id"],)).fetchone()[0]
    try:
        snapshot = await asyncio.to_thread(_open, row, body.pin)
    except HTTPException:
        # верный PIN, а снимок не расшифровать (`share_gone`): не неудача — резервы ссылки и адреса снять
        state.DB.execute("UPDATE chat_shares SET attempts = MAX(0, attempts - 1) WHERE id = ?", (row["id"],))
        state.DB.execute("UPDATE chat_share_attempts SET attempts = MAX(0, attempts - 1) WHERE ip_hash = ?", (ip_hash,))
        raise
    current = _public_share(row["token_hash"])
    if current["id"] != row["id"]:
        raise _refuse()
    if snapshot is None:
        _wrong_pin(request, current, reserved)
        raise _refuse(403, "share_pin", "неверный PIN")
    state.DB.execute("UPDATE chat_shares SET attempts = MAX(0, attempts - 1), views = views + 1, viewed = ?"
                     " WHERE id = ?", (now, row["id"]))
    state.DB.execute("UPDATE chat_share_attempts SET attempts = MAX(0, attempts - 1) WHERE ip_hash = ?", (ip_hash,))
    access = security.new_token()
    # Bound successful visitor sessions, retaining the newest 100 per link.
    state.DB.execute("DELETE FROM chat_share_visitors WHERE digest IN (SELECT digest FROM chat_share_visitors"
                     " WHERE share_id = ? ORDER BY expires DESC LIMIT -1 OFFSET 99)", (row["id"],))
    state.DB.execute("INSERT INTO chat_share_visitors VALUES(?,?,?)",
                     (security.digest(access), row["id"], now + VIEW_SEC))
    return JSONResponse({"access": access, **snapshot}, headers=PUBLIC_HEADERS)


def _visitor(access):
    row = state.DB.execute(
        "SELECT s.id, s.user_id FROM chat_share_visitors v JOIN chat_shares s ON s.id = v.share_id"
        " JOIN users u ON u.id = s.user_id WHERE v.digest = ? AND v.expires > ? AND u.disabled = 0"
        " AND (s.expires IS NULL OR s.expires > ?)" + LIVE,
        (security.digest(access), clock.NOW(), clock.NOW())).fetchone()
    if row is None:
        # VIEW_SEC прошли, а ссылка жива: хватит снова ввести PIN — не «ссылка недоступна»
        if state.DB.execute(
                "SELECT 1 FROM chat_share_visitors v JOIN chat_shares s ON s.id = v.share_id JOIN users u ON u.id = s.user_id"
                " WHERE v.digest = ? AND u.disabled = 0 AND (s.expires IS NULL OR s.expires > ?)" + LIVE,
                (security.digest(access), clock.NOW())).fetchone():
            raise _refuse(404, "share_view_expired", "время просмотра истекло — введите PIN снова")
        raise _refuse()
    return row


@router.post("/share/check", status_code=204)
async def check_access(body: ReadIn, request: Request):
    _same_origin(request)
    _visitor(body.access)
    return Response(status_code=204, headers=PUBLIC_HEADERS)


@router.post("/share/file")
async def shared_file(body: FileIn, request: Request):
    _same_origin(request)
    visitor = _visitor(body.access)
    row = state.DB.execute(
        "SELECT m.* FROM chat_share_files f JOIN media m ON m.id = f.media_id"
        " WHERE f.share_id = ? AND m.id = ? AND m.user_id = ?",
        (visitor["id"], body.file, visitor["user_id"])).fetchone()
    if row is None:
        raise _refuse()
    path = media._file(row, False)
    try:
        size = (await asyncio.to_thread(path.stat)).st_size
    except FileNotFoundError:
        raise _refuse() from None
    except OSError:
        raise _refuse(503, "storage", "хранилище недоступно") from None
    _visitor(body.access)

    async def chunks():
        reader = media._read(path, 0, size)
        try:
            while True:
                block = await asyncio.to_thread(next, reader, b"")
                _visitor(body.access)
                if not block:
                    break
                yield block
        finally:
            await asyncio.to_thread(reader.close)

    # Always a download; potentially active HTML/SVG must never become a same-origin document.
    return StreamingResponse(chunks(), media_type="application/octet-stream", headers=PUBLIC_HEADERS | {
        "Content-Disposition": media.disposition(row["download_name"]), "Content-Length": str(size),
        "Content-Security-Policy": "default-src 'none'; sandbox"})
