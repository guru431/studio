"""PIN моста (scrypt, счёт неверных на диске) и открытые сессии студии (только в памяти)."""

import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import dpapi
from .limits import OPEN_IDLE_SEC, PIN_TRIES
from .store import write_atomic

PIN_RE = re.compile(r"[0-9]{4,8}")
FILE = "pin.bin"

# Один на процесс: параллельные проверки иначе прочли бы один и тот же счёт и обошли предел.
_LOCK = threading.Lock()


def _scrypt(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(value.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


class PinStore:
    """`pin.bin` — DPAPI от JSON {"hash": "scrypt$<соль hex>$<хеш hex>", "fails": n}; нет файла — PIN не задан."""

    def __init__(self, h: Path):
        self.path = h / FILE

    def _load(self) -> dict:
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return {"hash": None, "fails": 0}
        return json.loads(dpapi.unprotect(blob))

    def _save(self, state: dict) -> None:
        write_atomic(self.path, dpapi.protect(json.dumps(state).encode("ascii")))

    @property
    def has_pin(self) -> bool:
        return self._load()["hash"] is not None

    @property
    def fails(self) -> int:
        return self._load()["fails"]

    @property
    def locked(self) -> bool:
        return self.fails >= PIN_TRIES

    def set_pin(self, value: str | None) -> None:
        """Задать PIN (4–8 цифр ASCII) или убрать (None); счёт неверных — с нуля."""
        with _LOCK:
            if value is None:
                self.path.unlink(missing_ok=True)
                return
            if not (isinstance(value, str) and value.isascii() and PIN_RE.fullmatch(value)):
                raise ValueError("PIN — от 4 до 8 цифр")
            salt = os.urandom(16)
            self._save({"hash": f"scrypt${salt.hex()}${_scrypt(value, salt).hex()}", "fails": 0})

    def check(self, value: str) -> tuple[str, int]:
        """("ok", 0), ("pin", осталось попыток) или ("pin_locked", 0). Попытка пишется на диск до scrypt."""
        with _LOCK:
            state = self._load()
            if state["hash"] is None:
                return "ok", 0
            if state["fails"] >= PIN_TRIES:
                return "pin_locked", 0
            state["fails"] += 1
            self._save(state)
            _, salt, digest = state["hash"].split("$")
            if hmac.compare_digest(_scrypt(value, bytes.fromhex(salt)), bytes.fromhex(digest)):
                state["fails"] = 0
                self._save(state)
                return "ok", 0
            left = PIN_TRIES - state["fails"]
            return ("pin", left) if left else ("pin_locked", 0)

    def reset(self) -> None:
        """Снять блок (только на самой машине: трей или окно настройки)."""
        with _LOCK:
            state = self._load()
            if state["hash"] is not None and state["fails"]:
                state["fails"] = 0
                self._save(state)


class OpenSessions:
    """Сессии студии, открытые PIN: только в памяти, простой дольше OPEN_IDLE_SEC закрывает."""

    def __init__(self, now: Callable[[], float] = time.monotonic):
        self._now = now
        self._seen: dict[str, float] = {}

    def open(self, session: str) -> None:
        self._seen[session] = self._now()

    def touch(self, session: str) -> bool:
        """Открыта и не простаивала дольше OPEN_IDLE_SEC — продлить; иначе убрать и False."""
        seen = self._seen.get(session)
        now = self._now()
        if seen is None or now - seen > OPEN_IDLE_SEC:
            self._seen.pop(session, None)
            return False
        self._seen[session] = now
        return True

    def close(self, session: str) -> None:
        self._seen.pop(session, None)

    def clear(self) -> None:
        self._seen.clear()
