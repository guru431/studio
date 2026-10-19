"""Подмена шлюза для быстрых тестов: httpx.MockTransport зовёт FakeGateway.handler.

Встроенное поведение — API картинок шлюза srvllm (перенос `Gateway` из tests/test_studio.py с путями
`/slot/...`): генерация и правка отдают настоящий PNG (`PNG_REAL`, Pillow — чтобы строилось превью),
release моделей, описание gemma-abl. Чужой ключ — 401: верен только ключ подключения `TOKEN`.
"""
import asyncio
import base64
import io
import json
from collections.abc import Awaitable, Callable

import httpx
from PIL import Image

from image_upload import decode_form

TOKEN = "owner-images-test-key"


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 90)).save(out, "PNG")
    return out.getvalue()


PNG_REAL = _png()
RELEASES = ("/slot/images/release", "/slot/images/descriptions/release")


class FakeGateway:
    TOKEN = TOKEN

    def __init__(self):
        self.requests: list[httpx.Request] = []                       # все полученные запросы по порядку
        self.reply: Callable[[httpx.Request], httpx.Response] | None = None  # ответ теста на любой запрос
        #: путь → обработчик теста вместо встроенного (встроенный — `builtin`)
        self.on: dict[str, Callable[[httpx.Request], Awaitable[httpx.Response]]] = {}
        self.down = False
        self.generations = []            # (тело или поля формы, заголовок авторизации, таймаут чтения) — и правки
        self.edits = []                  # (поля формы, фото) правок
        self.image_reply = None          # (код, тело) вместо картинки
        self.raise_on_generate = None
        self.hold = False                # генерация «идёт», пока не снимут
        self.aborted = 0                 # запрос генерации оборван клиентом
        self.completed = 0               # запрос генерации дошёл до ответа
        self.releases = []               # (модель, заголовок авторизации)
        self.release_replies = [(200, {"ok": True, "slot": "comfy"})]   # последний повторяется
        self.descriptions = []           # тела запросов описания
        self.description_reply = {"choices": [{"message": {"content": "A described photo."}, "finish_reason": "stop"}]}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("нет соединения", request=request)
        if self.reply is not None:
            return self.reply(request)
        return await self.on.get(request.url.path, self.builtin)(request)

    async def builtin(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": {"message": "Authentication Error", "type": "auth_error"}})
        path = request.url.path
        if path in ("/slot/images/generations", "/slot/images/edits"):
            return await self._image(request)
        if path in RELEASES:
            self.releases.append((json.loads(request.content)["model"], request.headers.get("authorization")))
            code, body = self.release_replies[min(len(self.releases), len(self.release_replies)) - 1]
            return httpx.Response(code, json=body)
        if path == "/slot/images/descriptions":
            self.descriptions.append(json.loads(request.content))
            return httpx.Response(200, json=self.description_reply)
        return httpx.Response(404)

    async def _image(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/edits"):
            body, photo = decode_form(request.content, request.headers["content-type"])
            self.edits.append((body, photo))
            seed = int(body["seed"]) if "seed" in body else None
        else:
            body = json.loads(request.content)
            seed = body.get("seed")
        self.generations.append((body, request.headers.get("authorization"), request.extensions["timeout"]["read"]))
        try:
            while self.hold:
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            self.aborted += 1
            raise
        self.completed += 1
        if self.raise_on_generate:
            raise self.raise_on_generate("нет ответа", request=request)
        if self.image_reply:
            return httpx.Response(self.image_reply[0], json=self.image_reply[1])
        return httpx.Response(200, json={"created": 1, "data": [
            {"b64_json": base64.b64encode(PNG_REAL).decode(), "seed": seed}]})
