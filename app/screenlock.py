"""Блокировка на телефоне (спека 2026-10-21-studio-screen-lock): отметки «на экране», «заперто»,
отпирание паролем учётки, включение и выключение.

Признак «заперто» считает auth.guard (auth.screen_locked); запертая сессия делает только запросы
auth.SCREEN_LOCK_PATHS. Сессия запроса — request.state.digest. Журнал — только события с id: пароль
туда не пишется."""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import auth
import clock
import state

PASSWORD_MAX = 1024

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


class Alive(BaseModel):
    touch: bool = False          # основной указатель — палец (matchMedia('(pointer: coarse)'))


class Password(BaseModel):
    password: str = Field(max_length=PASSWORD_MAX)


class ScreenLock(BaseModel):
    enabled: bool
    password: str | None = Field(default=None, max_length=PASSWORD_MAX)


def _has_keys(uid: int) -> bool:
    return state.DB.execute("SELECT 1 FROM passkeys WHERE user_id = ? LIMIT 1", (uid,)).fetchone() is not None


def _seen(request: Request, value: float | None) -> None:
    state.DB.execute("UPDATE sessions SET seen = ? WHERE digest = ?", (value, request.state.digest))


@router.get("/lock")
async def lock_state(request: Request):
    """Только «заперто ли», «есть ли ключи» и CSRF (без него не отправить отпирание) — профиля нет."""
    return {"locked": request.state.locked, "keys": _has_keys(request.state.user.id), "csrf": request.state.csrf}


@router.post("/lock/alive")
async def alive(body: Alive, request: Request):
    """Отметка «на экране». Сначала защёлка mobile (сбросить её может только новый вход), затем
    «заперто» заново по базе: иначе старая сессия телефона с пустым seen открылась бы первой же
    отметкой. Запертой seen не трогается."""
    if body.touch:
        state.DB.execute("UPDATE sessions SET mobile = 1 WHERE digest = ?", (request.state.digest,))
    row = state.DB.execute("SELECT u.screen_lock, s.mobile, s.seen FROM sessions s JOIN users u ON u.id = s.user_id"
                           " WHERE s.digest = ?", (request.state.digest,)).fetchone()
    if row is None:                  # сессию закрыли между guard и этим запросом
        raise HTTPException(401, {"code": "login", "message": "нужен вход"})
    now = clock.NOW()
    locked = auth.screen_locked(row["screen_lock"], row["mobile"], row["seen"], now)
    if not locked:
        _seen(request, now)
    return {"locked": locked}


@router.post("/lock/now", status_code=204)
async def lock_now(request: Request):
    """Студия на телефоне заперлась сама (ушла с экрана дольше минуты): сервер с ней согласен."""
    _seen(request, None)
    log.info("пользователь %d: заперто", request.state.user.id)
    return Response(status_code=204)


@router.post("/lock/password", status_code=204)
async def open_with_password(body: Password, request: Request):
    uid = request.state.user.id
    await auth.confirm_password(uid, body.password, "отпирание")
    _seen(request, clock.NOW())
    log.info("пользователь %d: открыто (пароль)", uid)
    return Response(status_code=204)


@router.post("/me/screen-lock")
async def set_screen_lock(body: ScreenLock, request: Request):
    """Включить — без пароля; выключить — только с паролем учётки: иначе получивший телефон открытым
    на минуту выключил бы её. Запертой сессии путь не открыт (guard)."""
    uid = request.state.user.id
    if not body.enabled:
        await auth.confirm_password(uid, body.password or "", "выключение блокировки")
    state.DB.execute("UPDATE users SET screen_lock = ? WHERE id = ?", (int(body.enabled), uid))
    log.info("пользователь %d: блокировка %s", uid, "включена" if body.enabled else "выключена")
    return {"screen_lock": body.enabled}
