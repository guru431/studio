"""Канал к студии (протокол §2): исходящий WebSocket, знакомство, просьбы, переподключение.

Ход от канала не зависит: канал оборвался — ход идёт, его события пропадают, пока нет нового канала;
вернувшись, студия просит `watch`. Задачи просьб при обрыве не отменяются (запись файла не рвётся) —
их ответы уходят в очередь прежнего канала и пропадают."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from urllib.parse import urlsplit

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from . import VERSION, events, limits
from .agent import Agent

log = logging.getLogger(__name__)

PATH = "/api/agent/link"
LOCAL = ("localhost", "127.0.0.1", "::1")  # http — только для тестов на этой машине
WELCOME_SEC = limits.WELCOME_SEC
FLUSH_SEC = 2  # stop(): дослать очередь (`done` ходов) не дольше
CLOSE_PROTOCOL = 4400
CLOSE_REVOKED = 4401
CLOSE_REPLACED = 4409


def ws_url(url: str) -> str:
    """Адрес студии → адрес канала: `https://h` → `wss://h/api/agent/link`; `http` — только локальный."""
    u = urlsplit(url)
    host = u.hostname
    if not host or "@" in u.netloc or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("адрес студии")
    u.port  # неверный порт — ValueError
    if u.scheme == "https":
        return f"wss://{u.netloc}{PATH}"
    if u.scheme == "http" and host in LOCAL:
        return f"ws://{u.netloc}{PATH}"
    raise ValueError("адрес студии")


def _dumps(frame: dict) -> str:
    return json.dumps(frame, ensure_ascii=True, allow_nan=False)


def _parse(raw) -> dict | None:
    """Кадр студии → объект; не JSON или не объект — `None` (в журнал — только тип ошибки)."""
    if not isinstance(raw, str):
        log.info("канал: двоичный кадр пропущен")
        return None
    try:
        frame = json.loads(raw)
    except (ValueError, RecursionError) as e:
        log.info("канал: кадр не разобран: %s", type(e).__name__)
        return None
    if not isinstance(frame, dict):
        log.info("канал: кадр не объект")
        return None
    return frame


class Link:
    """Канал моста: подключается, знакомится, отвечает на просьбы и переподключается до отзыва, замены или `stop()`."""

    def __init__(self, url: str, key: str, agent: Agent, on_state: Callable[[str], None] = lambda s: None, *,
                 on_welcome: Callable[[dict], None] | None = None, connect=ws_connect, sleep=asyncio.sleep):
        self.uri = ws_url(url)
        self.key = key
        self.agent = agent
        self.on_state = on_state
        self.on_welcome = on_welcome  # кадр `welcome` наружу (логин и имя компьютера — ядру); зовётся в цикле
        self._connect = connect
        self._sleep = sleep
        self._queue: asyncio.Queue | None = None  # очередь отправки текущего канала
        self._requests: set[asyncio.Task] = set()  # задачи просьб — ссылки, чтобы их не собрал сборщик
        self._stop = asyncio.Event()
        self._finished = asyncio.Event()
        self._running = False

    def send(self, frame: dict) -> bool:
        """Кадр в текущий канал; канала нет — `False` (конец хода `Agent.missed` дошлёт после `welcome`)."""
        if self._queue is None:
            return False
        self._queue.put_nowait(frame)
        return True

    async def stop(self) -> None:
        """Закрыть канал и выйти из `run()` со `stopped`."""
        self._stop.set()
        if self._running:
            await self._finished.wait()

    async def run(self) -> str:
        """До `revoked` (4401), `replaced` (4409) или `stop()` — `stopped`."""
        self._running = True
        try:
            loop = asyncio.create_task(self._loop())
            stop = asyncio.create_task(self._stop.wait())
            await asyncio.wait({loop, stop}, return_when=asyncio.FIRST_COMPLETED)
            stop.cancel()
            if loop.done():
                state = loop.result()
            else:
                loop.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop
                state = "stopped"
            log.info("канал: %s", state)
            self.on_state(state)
            return state
        finally:
            self._running = False
            self._finished.set()

    async def _loop(self) -> str:
        fails = 0  # неудач подряд; после welcome — сначала
        while True:
            self.on_state("connecting")
            try:
                code, welcomed = await self._channel()
            except Exception as e:  # сбой вне сети (нечитаемый pin.bin, …) — пауза и снова, не конец канала
                log.warning("канал: сбой: %s", type(e).__name__)
                code, welcomed = None, False
            if code == CLOSE_REVOKED:
                return "revoked"
            if code == CLOSE_REPLACED:
                return "replaced"
            if welcomed:
                fails = 0
            self.on_state("offline")
            if code == CLOSE_PROTOCOL:
                pause = limits.PROTOCOL_PAUSE_SEC
            else:
                pause = limits.BACKOFF[min(fails, len(limits.BACKOFF) - 1)]
                fails += 1
            log.info("канал: пауза %d с", pause)
            await self._sleep(pause)

    async def _channel(self) -> tuple[int | None, bool]:
        """Один канал от подключения до закрытия → (код закрытия студии или `None`, был ли `welcome`)."""
        try:
            ws = await self._connect(self.uri, additional_headers={"Authorization": f"Bearer {self.key}"},
                                     max_size=limits.FRAME_MAX, ping_interval=20, ping_timeout=20, open_timeout=15,
                                     user_agent_header=f"studio-agent/{VERSION}")
        except InvalidStatus as e:
            log.info("канал: отказ рукопожатия %d", e.response.status_code)
            return None, False
        except (InvalidHandshake, InvalidURI, OSError, TimeoutError) as e:
            log.info("канал: подключение не удалось: %s", type(e).__name__)
            return None, False
        queue: asyncio.Queue = asyncio.Queue()
        writer = None
        welcomed = False
        try:
            await ws.send(_dumps(self.agent.hello()))
            async with asyncio.timeout(WELCOME_SEC):
                while (frame := _parse(await ws.recv())) is None or frame.get("t") != "welcome":
                    pass
            welcomed = True
            if self.on_welcome is not None:
                try:
                    self.on_welcome(frame)
                except Exception as e:
                    log.warning("канал: welcome: %s", type(e).__name__)
            self.agent.new_link(reconnect=True)
            # Ходы, кончившиеся без канала, — первым кадром, если студия их принимает (протокол §18).
            if frame.get("completions") is True and (missed := self.agent.completions()) is not None:
                queue.put_nowait(missed)
            self._queue = queue
            self.agent.send = self.send
            writer = asyncio.create_task(self._write(ws, queue))
            log.info("канал: на связи")
            self.on_state("online")
            while True:
                frame = _parse(await ws.recv())
                if frame is not None:
                    self._on_frame(frame, queue)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd is not None else None
            log.info("канал: закрыт %s", code if code is not None else "без кода")
            return code, welcomed
        except TimeoutError:
            log.info("канал: нет welcome")
            return None, welcomed
        finally:
            self.agent.desktop.revoke()
            if self._queue is queue:
                self._queue = None
            if writer is not None and self._stop.is_set():  # выход: дослать, что уже в очереди (`done` ходов)
                queue.put_nowait(None)
                await asyncio.wait({writer}, timeout=FLUSH_SEC)
            if writer is not None:
                writer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await writer
            if not self._stop.is_set():  # не ушедшие концы ходов — следующему каналу
                while not queue.empty():
                    if isinstance(left := queue.get_nowait(), dict):
                        self.agent.missed(left)
            with contextlib.suppress(Exception):
                await ws.close()

    def _on_frame(self, frame: dict, queue: asyncio.Queue) -> None:
        t = frame.get("t")
        if t == "req":
            task = asyncio.create_task(self._answer(frame, queue))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)
        elif t in ("close", "desktop_close"):
            session = frame.get("session")
            if isinstance(session, str):
                if t == "close":
                    self.agent.close_session(session)
                else:
                    self.agent.desktop.revoke(session)
        else:
            log.info("канал: кадр пропущен")

    async def _answer(self, frame: dict, queue: asyncio.Queue) -> None:
        """Ответ просьбы и кадры после него — подряд в очередь канала, по которому она пришла."""
        try:
            out = await self.agent.handle(frame)
        except Exception as e:
            log.warning("канал: просьба упала: %s", type(e).__name__)
            return
        for f in out:
            queue.put_nowait(f)

    @staticmethod
    async def _write(ws, queue: asyncio.Queue) -> None:
        """Писатель канала: один на канал, кадры — по порядку очереди (`events.Ready` — уже закодирован); `None` —
        очередь дослана, конец."""
        while True:
            frame = await queue.get()
            if frame is None:
                return
            try:
                text = frame.text if isinstance(frame, events.Ready) else _dumps(frame)
            except (TypeError, ValueError) as e:
                log.warning("канал: кадр не кодируется: %s", type(e).__name__)
                continue
            await ws.send(text)
