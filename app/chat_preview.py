"""Окно HTML и SVG ответа «Чата» (п. 25 спеки 2026-11-25): блок кода ответа открывается в `iframe sandbox` без
`allow-same-origin` по короткоживущему адресу своей учётки. Документ — только в памяти процесса, политику содержимого
задаёт заголовок ответа (не `srcdoc`: унаследованная политика страницы сломала бы встроенные скрипты блока, а `'self'`
остался бы достижим). Сеть закрыта тремя слоями: CSP ответа (`default-src 'none'`, без `connect-src`), `sandbox` без
`allow-same-origin` (ни cookie, ни страницы Студии) и `default-src 'self'` самой страницы — переход кадра на чужой
адрес запрещён её `frame-src`. В защищённом диалоге окна нет (решение 17). Текст блока в журнал не пишется."""
import logging
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import chat
import clock
import state

PREVIEW_MAX = 2 * 1024 * 1024    # байты UTF-8 блока
TTL_SEC = 300
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
       "media-src data:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'self'; sandbox allow-scripts")
TOO_BIG = "блок больше 2 МиБ — в окне не открывается"
#: одно окно на учётку: uid → (токен, digest сессии, id диалога, документ, срок по clock.NOW())
VIEWS: dict[int, tuple] = {}
SVG_HEAD = '<!doctype html><meta charset="utf-8"><body style="margin:0">'

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class PreviewIn(BaseModel):
    kind: Literal["html", "svg"]
    text: str


def _closed() -> HTTPException:
    return HTTPException(404, {"code": "gone", "message": "окно закрыто — откройте его заново"})


@router.post("/chats/{chat_id}/preview", status_code=201)
async def open_preview(chat_id: str, body: PreviewIn, request: Request):
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    if row["protected_used"]:
        raise HTTPException(409, {"code": "protected", "message": chat.PROTECTED_PREVIEW})
    try:
        size = len(body.text.encode("utf-8"))
    except UnicodeEncodeError:                  # одиночный суррогат — не текст блока
        size = PREVIEW_MAX + 1
    if size > PREVIEW_MAX:
        raise HTTPException(413, {"code": "preview_size", "message": TOO_BIG})
    document = body.text if body.kind == "html" else SVG_HEAD + body.text
    token = secrets.token_hex(16)
    now = clock.NOW()
    for owner in [owner for owner, kept in VIEWS.items() if kept[4] <= now]:   # истёкшие окна всех учёток — из памяти
        del VIEWS[owner]
    VIEWS[uid] = (token, request.state.digest, chat_id, document, now + TTL_SEC)
    log.info("чат %s: окно HTML, %d байт", chat_id[:8], size)
    return {"token": token}


@router.get("/chats/preview/{token}")
async def preview(token: str, request: Request):
    uid = request.state.user.id
    kept = VIEWS.get(uid)
    if kept is None or kept[0] != token or kept[1] != request.state.digest:
        raise _closed()
    fresh = state.DB.execute("SELECT protected_used FROM chats WHERE id = ? AND user_id = ? AND deleted IS NULL",
                             (kept[2], uid)).fetchone()
    if kept[4] <= clock.NOW() or fresh is None or fresh["protected_used"]:
        del VIEWS[uid]
        raise _closed()
    return Response(kept[3], media_type="text/html", headers={
        "Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
