"""Область блокировки и серверная проверка доступа к разделам и общим данным."""
import json
from contextvars import ContextVar

from fastapi import HTTPException

import clock
import state

SECTIONS = ("chat", "photo", "video", "transcribe", "speech", "ocr", "translate", "code")
#: допуск доставки отметки: больше интервала отметок видимой вкладки (lock.js ALIVE_SEC = 15) и срока её
#: ответа (MARK_SEC = 8) — иначе таймер 0 («при возвращении») запирал бы работающую вкладку от опоздания на мгновение
HEARTBEAT_GRACE = 30
DEFAULT_TIMEOUT = 60
COMPUTER_TIMEOUT = 10800
RESTRICTED = ContextVar("studio_locked_sections", default=frozenset())


def require_media(uid: int, media_id: str) -> None:
    """Проверяется и при передаче файла источником в другой, незапертый раздел."""
    selected = RESTRICTED.get()
    if selected and media_sections(uid, media_id) & selected:
        raise HTTPException(423, {"code": "screen_locked", "message": "студия заперта"})


def session(digest: str):
    return state.DB.execute(
        "SELECT u.screen_lock, u.screen_lock_sections, u.screen_lock_timeout, s.seen"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.digest = ?", (digest,)).fetchone()


def expired(row, now: float | None = None) -> bool:
    now = clock.NOW() if now is None else now
    return bool(row and row["screen_lock"] and
                (row["seen"] is None or now - row["seen"] > row["screen_lock_timeout"] + HEARTBEAT_GRACE))


def protected(row, section: str | None) -> bool:
    selected = json.loads(row["screen_lock_sections"])
    return "*" in selected or section == "security" or section in selected


def locked(digest: str, section: str | None = None) -> bool:
    row = session(digest)
    return expired(row) and protected(row, section)


def hidden(digest: str) -> set[str]:
    row = session(digest)
    if not expired(row):
        return set()
    selected = set(json.loads(row["screen_lock_sections"]))
    return set(SECTIONS) if "*" in selected else selected


def scan_sections(uid: int, sid: str) -> set[str]:
    """Разделы документа «Сканов»: сам «Сканы» и тот, кто его завёл, — вложение чата (`origin = 'chat'`) и
    перевод (`translations.scan_id`). Защита любого из них закрывает текст, страницы и файлы скана."""
    row = state.DB.execute("SELECT origin FROM scans WHERE id = ? AND user_id = ?", (sid, uid)).fetchone()
    if row is None:
        return set()
    found = {"ocr"}
    if row["origin"] == "chat":
        found.add("chat")
    if state.DB.execute("SELECT 1 FROM translations WHERE user_id = ? AND scan_id = ? LIMIT 1", (uid, sid)).fetchone():
        found.add("translate")
    return found


def hidden_scans(uid: int, closed) -> tuple[str, tuple]:
    """Подзапрос id сканов закрытых разделов `closed` (как `scan_sections`, но для ленты и поиска) и его
    аргументы; нечего прятать — ('', ()). «Сканы» целиком закрывает сам маршрут/раздел, не этот подзапрос."""
    parts, args = [], []
    if "chat" in closed:
        parts.append("SELECT id FROM scans WHERE user_id = ? AND origin = 'chat'")
        args.append(uid)
    if "translate" in closed:
        parts.append("SELECT scan_id FROM translations WHERE user_id = ? AND scan_id IS NOT NULL")
        args.append(uid)
    return " UNION ".join(parts), tuple(args)


def media_sections(uid: int, media_id: str) -> set[str]:
    """Один файл может использоваться в нескольких разделах: защищённая связь имеет приоритет.
    Файл скана (исходный PDF, PNG страниц) — разделов его документа (`scan_sections`)."""
    sources = {
        "chat": (("attachments", ("media_id",)),),
        "photo": (("images", ("media_id", "source_media_id", "mask_media_id")),),
        "video": (("videos", ("media_id", "source_media_id")),),
        "transcribe": (("transcripts", ("source_media_id", "result_media_id")),),
        "speech": (("voices", ("media_id",)), ("speeches", ("media_id",))),
        "translate": (("translations", ("source_media_id",)),),
    }
    found = set()
    for section, tables in sources.items():
        for table, columns in tables:
            where = " OR ".join(f"{column} = ?" for column in columns)
            if state.DB.execute(f"SELECT 1 FROM {table} WHERE user_id = ? AND ({where}) LIMIT 1",
                                (uid, *([media_id] * len(columns)))).fetchone():
                found.add(section)
                break
    for row in state.DB.execute("SELECT id FROM scans WHERE user_id = ? AND source_media_id = ?"
                                " UNION SELECT scan_id FROM scan_pages WHERE user_id = ? AND media_id = ?",
                                (uid, media_id, uid, media_id)).fetchall():
        found |= scan_sections(uid, row[0])
    return found


def blocks(request, uid: int, digest: str) -> bool:
    row = session(digest)
    if not expired(row):
        return False
    if protected(row, None):
        return True
    path = request.url.path.rstrip("/")
    pieces = path.split("/")
    head = pieces[2] if len(pieces) > 2 else ""
    section = {"chats": "chat", "attachments": "chat", "images": "photo", "presets": "photo",
               "videos": "video", "transcripts": "transcribe", "transcribe": "transcribe",
               "voices": "speech", "speeches": "speech", "speech": "speech", "ocr": "ocr",
               "translate": "translate", "code": "code", "agents": "code"}.get(head)
    if section == "ocr" and not protected(row, section) and len(pieces) > 3:
        return bool(scan_sections(uid, pieces[3]) & hidden(digest))     # документ закрытого чата или перевода
    if section is not None:
        return protected(row, section)
    if head == "sections" and len(pieces) > 3:
        return protected(row, pieces[3])
    if head == "media" and len(pieces) > 3 and pieces[3] != "archives":
        return bool(media_sections(uid, pieces[3]) & hidden(digest))
    # Общие выгрузки, управление защитой, сессиями и ключами не должны обходить закрытый раздел; изменение
    # уведомлений (ntfy, push) увело бы на чужое устройство события закрытых разделов. Каждый маршрут /api/*
    # отнесён к разделу, закрываемым или открытым явно — реестр в tests/test_lock_policy.py.
    return (path.startswith(("/api/me/export", "/api/media/archives", "/api/passkeys", "/api/admin",
                             "/api/me/sessions", "/api/me/security-events"))
            or path in ("/api/history", "/api/me/screen-lock", "/api/me/pin", "/api/me/password", "/api/me/emergency",
                        "/api/me/password-reset")
            or (path in ("/api/me", "/api/me/ntfy", "/api/me/ntfy/test", "/api/me/push", "/api/me/exa-key")
                and request.method != "GET")
            or (head == "connections" and path.endswith("/token") and protected(row, "chat")))
