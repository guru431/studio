"""Подмена шлюза для быстрых тестов: httpx.MockTransport зовёт FakeGateway.handler."""
import httpx

TOKEN = "owner-images-test-key"


class FakeGateway:
    TOKEN = TOKEN

    async def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
