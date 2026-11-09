"""Invalidate in-flight creation when history is wiped or an account is disabled.

All checks and epoch changes run on the service event loop. Requests and their
background jobs retain an epoch through ContextVar; idle users leave no registry
entry. The DB identity also keeps independent application lifespans isolated.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from weakref import WeakValueDictionary

from fastapi import HTTPException

import state


@dataclass
class _Epoch:
    database: object
    user_id: int
    revision: int = 0
    barriers: int = 0


_EPOCHS: WeakValueDictionary[tuple[int, int], _Epoch] = WeakValueDictionary()
_CURRENT: ContextVar[tuple[_Epoch, int, bool] | None] = ContextVar("creation_operation", default=None)


def _epoch(user_id: int) -> _Epoch:
    key = (id(state.DB), user_id)
    epoch = _EPOCHS.get(key)
    if epoch is None:
        epoch = _Epoch(state.DB, user_id)
        _EPOCHS[key] = epoch
    return epoch


def check(user_id: int) -> None:
    """Call after the final await, before inserting user history or publishing media."""
    user = state.DB.execute("SELECT disabled FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or user["disabled"]:
        raise HTTPException(401, {"code": "login", "message": "нужен вход"})
    epoch = _epoch(user_id)
    current = _CURRENT.get()
    stale = current is not None and current[0].user_id == user_id and (
        current[0] is not epoch or current[1] != epoch.revision or current[2])
    if epoch.barriers or stale:
        raise HTTPException(409, {"code": "history_changed", "message": "история очищена или очищается — повторите действие"})


def snapshot(user_id: int) -> tuple:
    """The user's epoch as it is now."""
    epoch = _epoch(user_id)
    return (epoch, epoch.revision, bool(epoch.barriers))


def capture(user_id: int) -> tuple:
    """Epoch of a job put in a per-user queue: the request's one (`auth.guard`), outside a request — the
    current one. The queue runner runs each job under its own epoch (`restore`), not under the epoch of
    whoever started the runner."""
    current = _CURRENT.get()
    if current is not None and current[0].user_id == user_id:
        return current
    return snapshot(user_id)


@contextmanager
def restore(captured: tuple):
    token = _CURRENT.set(captured)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def bind(user_id: int):
    """Capture before reading a request body or awaiting conversion/upstream."""
    with restore(snapshot(user_id)):
        yield


@contextmanager
def barrier(user_id: int):
    """Invalidate previous creation and reject new writes until cleanup finishes."""
    epoch = _epoch(user_id)
    epoch.revision += 1
    epoch.barriers += 1
    try:
        yield
    finally:
        epoch.barriers -= 1
