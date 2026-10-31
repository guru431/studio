"""Область блокировки и серверная проверка доступа к разделам и общим данным."""
import json
from contextvars import ContextVar

from fastapi import HTTPException

import clock
import state

SECTIONS = ("chat", "photo", "video", "transcribe", "speech", "ocr", "translate", "code")
HEARTBEAT_GRACE = 15
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


def media_sections(uid: int, media_id: str) -> set[str]:
    """Один файл может использоваться в нескольких разделах: защищённая связь имеет приоритет."""
    sources = {
        "chat": (("attachments", ("media_id",)),),
        "photo": (("images", ("media_id", "source_media_id", "mask_media_id")),),
        "video": (("videos", ("media_id", "source_media_id")),),
        "transcribe": (("transcripts", ("source_media_id", "result_media_id")),),
        "speech": (("voices", ("media_id",)), ("speeches", ("media_id",))),
        "ocr": (("scans", ("source_media_id",)), ("scan_pages", ("media_id",))),
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
    if section is not None:
        return protected(row, section)
    if head == "sections" and len(pieces) > 3:
        return protected(row, pieces[3])
    if head == "media" and len(pieces) > 3 and pieces[3] != "archives":
        return bool(media_sections(uid, pieces[3]) & hidden(digest))
    # Общие выгрузки, управление защитой и ключи не должны обходить закрытый раздел.
    return (path.startswith(("/api/me/export", "/api/media/archives", "/api/passkeys", "/api/admin"))
            or path in ("/api/history", "/api/me/screen-lock", "/api/me/pin", "/api/me/password")
            or (path == "/api/me" and request.method != "GET")
            or (head == "connections" and path.endswith("/token") and protected(row, "chat")))
