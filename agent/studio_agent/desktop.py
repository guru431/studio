"""Локальный выключатель и один короткоживущий сеанс рабочего стола.

Протокол умеет читать выключатель, но не менять его. Windows-объекты живут в одном
рабочем потоке; кадры не накапливаются, а потерявший связь зритель теряет ввод.
"""
import asyncio
import concurrent.futures
import math
import re
import secrets
import threading
import time

from .errors import Refused

CONTROL = re.compile(r"[0-9a-f]{32}")
LEASE_SEC = 10
OP_SEC = 5
CLIPBOARD_MAX = 65536
TEXT_MAX = 4096
EVENTS_MAX = 32


def validate_events(events) -> list[dict]:
    if not isinstance(events, list) or not 1 <= len(events) <= EVENTS_MAX:
        raise Refused("invalid")
    text_units = 0
    for event in events:
        if not isinstance(event, dict):
            raise Refused("invalid")
        kind = event.get("type")
        if not isinstance(kind, str):
            raise Refused("invalid")
        fields = {"move": {"type", "monitor", "x", "y"}, "button": {"type", "button", "down"},
                  "wheel": {"type", "dx", "dy"}, "key": {"type", "code", "down"},
                  "text": {"type", "text"}}.get(kind)
        if fields is None or set(event) != fields:
            raise Refused("invalid")
        if kind == "move":
            if type(event["monitor"]) is not int or not 0 <= event["monitor"] < 32:
                raise Refused("invalid")
            if any(type(event[k]) not in (int, float) or not math.isfinite(event[k])
                   or not 0 <= event[k] <= 1 for k in ("x", "y")):
                raise Refused("invalid")
        if kind in ("key", "button") and type(event["down"]) is not bool:
            raise Refused("invalid")
        if kind == "button" and event["button"] not in ("left", "right", "middle"):
            raise Refused("invalid")
        if kind == "key" and (not isinstance(event["code"], str)
                              or not re.fullmatch(r"[A-Za-z0-9]{2,24}", event["code"])):
            raise Refused("invalid")
        if kind == "key":
            from .desktop_native import KEYS
            if event["code"] not in KEYS:
                raise Refused("unsupported", "Эта клавиша не поддерживается")
        if kind == "wheel" and any(type(event[k]) is not int or abs(event[k]) > 1200 for k in ("dx", "dy")):
            raise Refused("invalid")
        if kind == "text" and (not isinstance(event["text"], str) or not 1 <= len(event["text"]) <= TEXT_MAX
                               or "\x00" in event["text"]):
            raise Refused("invalid")
        if kind == "text":
            try:
                text_units += len(event["text"].encode("utf-16-le")) // 2
            except UnicodeError:
                raise Refused("invalid") from None
            if text_units > TEXT_MAX:
                raise Refused("too_large", "Текст в пакете длиннее 4096 UTF-16 единиц")
    return events


class Desktop:
    def __init__(self, enabled=False, *, factory=None, now=time.monotonic):
        self._enabled = enabled is True
        self._factory = factory
        self._now = now
        self._guard = threading.RLock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-desktop")
        self._native = None
        self._owner = self._control = None
        self._deadline = 0
        self._generation = 0
        self._pending = None
        self._serial = asyncio.Lock()
        self._requests = 0
        self._watchdog = None
        self._closed = False

    def set_enabled(self, enabled: bool) -> None:
        """Только Controller из локального окна. Не является операцией протокола."""
        if type(enabled) is not bool:
            raise ValueError("desktop_enabled")
        with self._guard:
            if self._enabled == enabled:
                return
            self._enabled = enabled
            self.revoke()

    def revoke(self, session=None, control=None) -> None:
        with self._guard:
            if session is not None and self._owner != session:
                return
            if control is not None and self._control != control:
                return
            self._generation += 1
            self._owner = self._control = None
            self._deadline = 0
            if not self._closed and (self._native is not None
                                     or self._pending is not None and not self._pending.done()):
                self._pool.submit(self._release)

    def _release(self):
        if self._native is not None:
            try:
                self._native.release()
            except Exception:
                pass  # выключатель уже закрыт; в журнал не попадают данные Windows

    def expire(self):
        with self._guard:
            if self._control is not None and self._now() >= self._deadline:
                self.revoke()

    def status(self) -> dict:
        with self._guard:
            self.expire()
            return {"enabled": self._enabled, "active": self._control is not None}

    def _check(self, session, control, generation):
        self.expire()
        if not self._enabled or self._closed:
            raise Refused("desktop_disabled", "Рабочий стол выключен. Включите его в агенте на компьютере")
        if generation != self._generation:
            raise Refused("gone", "Сеанс рабочего стола завершён")
        if control is not None and (self._owner != session or self._control is None
                                    or not secrets.compare_digest(self._control, control)):
            raise Refused("owned", "Сеанс рабочего стола завершён или открыт в другом окне")

    async def _run(self, session, control, action):
        if self._requests >= 4:
            raise Refused("busy", "Рабочий стол занят — повторите действие")
        self._requests += 1
        try:
            async with self._serial:
                return await self._run_one(session, control, action)
        finally:
            self._requests -= 1

    async def _run_one(self, session, control, action):
        with self._guard:
            generation = self._generation
            self._check(session, control, generation)
            if self._pending is not None and not self._pending.done():
                raise Refused("busy", "Рабочий стол занят — повторите действие")

            def work():
                with self._guard:
                    self._check(session, control, generation)
                    if self._native is None:
                        if self._factory is None:
                            from .desktop_native import NativeDesktop
                            self._factory = NativeDesktop
                        self._native = self._factory()
                return action(self._native, generation)

            future = self._pending = self._pool.submit(work)
        try:
            wrapped = asyncio.wrap_future(future)
            wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            result = await asyncio.wait_for(asyncio.shield(wrapped), OP_SEC)
        except asyncio.CancelledError:
            self.revoke(session, control)
            raise
        except TimeoutError:
            self.revoke(session, control)
            raise Refused("desktop_unavailable", "Windows не ответила — подключитесь к рабочему столу снова") from None
        except Refused as error:
            if error.code == "desktop_unavailable":
                self.revoke(session, control)
            raise
        with self._guard:
            self._check(session, control, generation)
            if control is not None:
                self._deadline = self._now() + LEASE_SEC
        return result

    async def start(self, session) -> dict:
        with self._guard:
            self.expire()
            self._check(session, None, self._generation)
            if self._control is not None:
                raise Refused("busy", "Рабочий стол уже открыт — завершите другой сеанс")
            # Занять место до первого await: две вкладки не получают один сеанс.
            self._owner, self._control = session, secrets.token_hex(16)
            control = self._control
            self._deadline = self._now() + LEASE_SEC
        try:
            monitors = await self._run(session, control, lambda native, _: native.monitors())
        except BaseException:
            self.revoke(session, control)
            raise
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch())
        return {"control": control, "monitors": monitors,
                "monitor": next((m["id"] for m in monitors if m["primary"]), monitors[0]["id"])}

    async def _watch(self):
        while self.status()["active"]:
            await asyncio.sleep(1)
            self.expire()

    async def frame(self, session, control, monitor):
        return await self._run(session, control, lambda native, _: native.frame(monitor))

    async def input(self, session, control, events):
        validate_events(events)

        def send(native, generation):
            for event in events:
                with self._guard:
                    self._check(session, control, generation)
                    native.input([event])
            return {}

        return await self._run(session, control, send)

    async def clipboard(self, session, control, text):
        if text is not None and (not isinstance(text, str) or len(text) > CLIPBOARD_MAX or "\x00" in text):
            raise Refused("invalid")

        def transfer(native, generation):
            with self._guard:
                self._check(session, control, generation)
                value = native.clipboard(text)
                if not isinstance(value, str) or len(value) > CLIPBOARD_MAX:
                    raise Refused("too_large", "В буфере больше 65 536 знаков")
                return {"text": value}

        return await self._run(session, control, transfer)

    def stop(self, session, control):
        with self._guard:
            self._check(session, control, self._generation)
            self.revoke(session)
        return {}

    async def close(self):
        self.revoke()
        with self._guard:
            self._closed = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
        await asyncio.get_running_loop().run_in_executor(None, self._pool.shutdown, True)
