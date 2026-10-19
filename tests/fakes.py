"""Подмена шлюза для быстрых тестов: httpx.MockTransport зовёт FakeGateway.handler."""
from collections.abc import Callable

import httpx

TOKEN = "owner-images-test-key"


class FakeGateway:
    TOKEN = TOKEN

    def __init__(self):
        self.requests: list[httpx.Request] = []                       # все полученные запросы по порядку
        self.reply: Callable[[httpx.Request], httpx.Response] | None = None  # ответ теста на любой запрос

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.reply is not None:
            return self.reply(request)
        return httpx.Response(404)
