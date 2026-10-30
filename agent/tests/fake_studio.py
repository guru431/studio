"""Подставная студия: сервер канала `/api/agent/link` на 127.0.0.1 по протоколу v1 (как `app/bridge.py`)."""

import asyncio
import json
import secrets

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

SESSION = "3b1e" * 8
WAIT_SEC = 5  # ждать события подставной студии; дольше — тест упал


class FakeStudio:
    """`hellos` — кадры знакомства, `frames` — (номер канала, кадр) после `welcome`, `events` — кадры `ev`,
    `attempts` — принятые рукопожатия; `reject_handshake` — код HTTP отказа рукопожатия, `silent` — без `welcome`,
    `login` — логин учётки в `welcome` (`None` — прежняя студия, поля нет). Отвязку (`DELETE /api/agent/enroll`)
    подставляют тесты `test_enroll.py` и `test_app.py`: сервер `websockets` принимает только рукопожатия."""

    def __init__(self, key: str):
        self.key = key
        self.hellos: list[dict] = []
        self.frames: list[tuple[int, dict]] = []
        self.events: list[dict] = []
        self.attempts = 0
        self.reject_handshake: int | None = None
        self.silent = False
        self.login: str | None = "owner"
        self.links = []
        self._pending: dict[str, asyncio.Future] = {}
        self._changed = asyncio.Condition()
        self._server = None
        self.url = ""

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0, process_request=self._process_request)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    def _process_request(self, connection, request):
        if self.reject_handshake is not None:
            return connection.respond(self.reject_handshake, "отказ\n")
        return None

    async def _handler(self, ws):
        self.attempts += 1
        if ws.request.headers.get("Authorization") != f"Bearer {self.key}":
            await ws.close(4401)  # как сервер: принять и закрыть
            return
        try:
            self.hellos.append(json.loads(await ws.recv()))
            if self.silent:
                await ws.wait_closed()
                return
            welcome = {"t": "welcome", "agent_id": 7, "name": "Рабочий ПК"}
            if self.login is not None:
                welcome["login"] = self.login
            await ws.send(json.dumps(welcome))
            async with self._changed:
                n = len(self.links)
                self.links.append(ws)
                self._changed.notify_all()
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append((n, frame))
                if frame.get("t") == "res":
                    future = self._pending.pop(frame["id"], None)
                    if future is not None and not future.done():
                        future.set_result(frame)
                elif frame.get("t") == "ev":
                    self.events.append(frame)
        except ConnectionClosed:
            pass

    async def connected(self, n: int = 1) -> None:
        """Ждать `n`-й канал на связи (после `welcome`)."""
        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(lambda: len(self.links) >= n), WAIT_SEC)

    async def request(self, op: str, **fields) -> dict:
        """Просьба по последнему каналу → кадр `res` с тем же `id`."""
        rid = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        await self.links[-1].send(json.dumps({"t": "req", "id": rid, "op": op, "session": SESSION, **fields}))
        return await asyncio.wait_for(future, WAIT_SEC)

    async def send_raw(self, text: str) -> None:
        await self.links[-1].send(text)

    async def close(self, code: int) -> None:
        await self.links[-1].close(code)

    def drop(self) -> None:
        """Обрыв без закрытия (сеть пропала)."""
        self.links[-1].transport.abort()
