"""Подмена шлюза для быстрых тестов: httpx.MockTransport зовёт FakeGateway.handler.

Встроенное поведение — API картинок шлюза srvllm (перенос `Gateway` из tests/test_studio.py с путями
`/slot/...`): генерация и правка отдают настоящий PNG (`PNG_REAL`, Pillow — чтобы строилось превью),
release моделей, описание gemma-abl; API роликов `/slot/videos` (перенос фикстуры `videos` из
tests/test_studio.py): создание, статус, `content`, удаление; чат — `/v1/models` и
`/v1/chat/completions` (поток OpenAI SSE). Чужой ключ — 401: верен только ключ подключения `TOKEN`.
"""
import asyncio
import base64
import io
import json
from collections.abc import Awaitable, Callable

import httpx
from PIL import Image

import clock
from image_upload import VIDEO_FIELDS, VIDEO_UPLOAD, decode_form

TOKEN = "owner-images-test-key"
VIDEO_MODEL = "wan2.2-remix-i2v-v3"
MP4 = bytes(range(256)) * 4


def video_object(video_id, **extra):
    """Объект ролика API шлюза (OpenAI videos + поля переводчика)."""
    return {"id": video_id, "object": "video", "model": VIDEO_MODEL, "status": "queued",
            "progress": 0, "created_at": 1_800_000_000, "completed_at": None, "expires_at": None,
            "seconds": "5", "size": "576x1024", "error": None, "seed": 42, "prompt": None,
            "prompt_source": "auto", "detail": "в очереди: перед ним 0", "queue_position": 0} | extra


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
        self.video_calls = []            # {"method", "path", "auth", "timeout"} запросов /slot/videos…
        self.video_forms = []            # (поля формы, картинка, сырое тело) созданий роликов
        self.video_create_reply = None   # (код, тело) вместо нового ролика
        self.video_ids = []              # id всех созданных роликов по порядку
        self.videos = {}                 # ролики, которые шлюз помнит: id → seed
        self.video_status = "queued"     # статус любого ролика в ответе шлюза
        self.video_extra = {}            # прочие поля ответа статуса (progress, detail, error, prompt…)
        self.video_content = MP4
        self.video_deleted = []          # id роликов, снятых DELETE
        self.models = [{"id": "local-gemma-abl", "context_length": 131072}, {"id": "gemma4-abl"}]
        self.chat_requests = []          # тела запросов /v1/chat/completions
        self.chat_auth = []              # их заголовки авторизации
        self.chat_reply = ["Здрав", "ствуйте!"]   # куски ответа модели
        self.chat_error = None           # (код, тело) вместо ответа
        self.chat_delay = 0              # молчание до первого куска — по часам студии (их двигают другие)
        self.chat_hold = False           # после первого куска ответ «идёт», пока не снимут

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
        if path == "/slot/videos" or path.startswith("/slot/videos/"):
            return self._video(request)
        if path == "/v1/models" and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": self.models})
        if path == "/v1/chat/completions" and request.method == "POST":
            return self._chat(request)
        return httpx.Response(404)

    def _chat(self, request: httpx.Request) -> httpx.Response:
        """OpenAI chat completions; `stream: true` — строки `data:` SSE и `data: [DONE]`."""
        body = json.loads(request.content)
        self.chat_requests.append(body)
        self.chat_auth.append(request.headers.get("authorization"))
        if self.chat_error is not None:
            code, error = self.chat_error
            return httpx.Response(code, json=error)
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                      "content": "".join(self.chat_reply)},
                                                          "finish_reason": "stop"}]})

        async def chunks():
            # Молчание считается по часам студии, сам шлюз их не двигает: дальше его ведёт только
            # тот, кто ждёт (пинги чата) — без гонки двух пауз за одни часы. Предел проходов: без
            # пингов тест падает на счёте, а не висит до таймаута.
            start = clock.NOW()
            for _ in range(1000):
                if clock.NOW() - start >= self.chat_delay:
                    break
                await asyncio.sleep(0.001)
            for i, piece in enumerate(self.chat_reply):
                delta = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                yield f"data: {json.dumps(delta, ensure_ascii=False)}\n\n".encode()
                while i == 0 and self.chat_hold:
                    await asyncio.sleep(0.001)
            yield b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    def _video(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.video_calls.append({"method": method, "path": path, "auth": request.headers.get("authorization"),
                                 "timeout": request.extensions["timeout"]["read"]})
        if path == "/slot/videos" and method == "POST":
            fields, image = decode_form(request.content, request.headers["content-type"],
                                        file_fields=VIDEO_FIELDS, max_upload=VIDEO_UPLOAD)
            self.video_forms.append((fields, image, request.content))
            if self.video_create_reply is not None:
                code, body = self.video_create_reply
                return httpx.Response(code, json=body)
            video_id = f"video_{len(self.video_ids) + 1:024x}"
            self.video_ids.append(video_id)
            self.videos[video_id] = int(fields["seed"]) if "seed" in fields else 42
            return httpx.Response(200, json=video_object(video_id, seed=self.videos[video_id]))
        video_id, _, tail = path.removeprefix("/slot/videos/").partition("/")
        if video_id not in self.videos:
            return httpx.Response(404, json={"error": {"message": "нет такого ролика", "type": "not_found"}})
        if method == "GET" and tail == "content":
            return httpx.Response(200, content=self.video_content, headers={"content-type": "video/mp4"})
        if method == "GET" and not tail:
            return httpx.Response(200, json=video_object(video_id, status=self.video_status,
                                                         seed=self.videos[video_id]) | self.video_extra)
        if method == "DELETE" and not tail:
            del self.videos[video_id]
            self.video_deleted.append(video_id)
            return httpx.Response(200, json={"id": video_id, "object": "video.deleted", "deleted": True})
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
