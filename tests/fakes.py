"""Подмена шлюза для быстрых тестов: httpx.MockTransport зовёт FakeGateway.handler.

Встроенное поведение — API картинок шлюза srvllm (перенос `Gateway` из tests/test_studio.py с путями
`/slot/...`): генерация и правка отдают настоящий PNG (`PNG_REAL`, Pillow — чтобы строилось превью),
release моделей, описание gemma-abl, список карты `GET /slot/models` (`slot_models`); API роликов `/slot/videos` (перенос фикстуры `videos` из
tests/test_studio.py): создание, статус, `content`, удаление; чат — `/v1/models` и
`/v1/chat/completions` (поток OpenAI SSE); речь — `/speech/asr`, `/speech/transcriptions`,
`/speech/lite/transcriptions` (`asr_replies` — очередь ответов, затем `asr_reply`) и `/speech/tts-qwen`
(`tts_replies`, последний повторяется); распознавание — `/pages/ocr` (`ocr_replies`, затем `ocr_reply`) и
`/pages/ocr-release` (в `releases` с моделью None; пока страница идёт — 409). Чужой ключ — 401: верен только ключ подключения `TOKEN`.
Запросы на хост `api.exa.ai` — Exa (`_exa`), мимо шлюза: ни `requests`, ни `down`, ни `reply`, ни `TOKEN`.

`FakeCloud` — облачный сервис для браузерных тестов: настоящий HTTP на localhost с CORS.
"""
import asyncio
import base64
import io
import json
import threading
import time
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
def _wav(seconds: float = 0.3, rate: int = 24000) -> bytes:
    """Настоящий WAV тишины: живая студия склеивает части озвучки настоящим ffmpeg."""
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return out.getvalue()


WAV_FAKE = _wav()
SPEECH_ASR = ("/speech/asr", "/speech/transcriptions", "/speech/lite/transcriptions")
RELEASES = ("/slot/images/release", "/slot/images/descriptions/release")
EXA_HOST = "api.exa.ai"
#: образец ответа `POST /search` Exa (форма — живая проба 2026-10-21)
EXA_SEARCH = {"requestId": "req-search", "results": [
    {"id": "https://www.python.org/downloads/", "title": "Download Python", "url": "https://www.python.org/downloads/",
     "publishedDate": "2025-10-07T00:00:00.000Z", "author": None,
     "highlights": ["Python 3.14.0 — Oct. 7, 2025", "Download the latest version for Windows"]},
    {"id": "https://docs.python.org/3/whatsnew/", "title": "What's New in Python", "url": "https://docs.python.org/3/whatsnew/",
     "highlights": ["What's New In Python 3.14"]}],
    "costDollars": {"total": 0.005}}


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
        self.video_fields = {}           # их поля создания: id → поля формы (для списка шлюза)
        self.video_status = "queued"     # статус любого ролика в ответе шлюза
        self.video_extra = {}            # прочие поля ответа статуса (progress, detail, error, prompt…)
        self.video_content = MP4
        self.video_deleted = []          # id роликов, снятых DELETE
        self.models = [{"id": "local-gemma-abl", "context_length": 131072}, {"id": "gemma4-abl"}]
        #: `GET /slot/models` — модели карты, отвечает сам slot-gate
        self.slot_models = [{"id": "flux-3-image-klein"}, {"id": "lustify-v10-krea2-turbo"}, {"id": VIDEO_MODEL}]
        self.chat_requests = []          # тела запросов /v1/chat/completions
        self.chat_auth = []              # их заголовки авторизации
        self.chat_reply = ["Здрав", "ствуйте!"]   # куски ответа модели
        #: ответы на очередные потоковые запросы: list[str] — куски текста, dict {"text": [...],
        #: "tool_calls": [(имя, аргументы JSON), …], "reasoning": [...]} — ещё вызовы инструментов и рассуждение
        #: (идёт до текста), "then": {"reasoning": [...], "text": [...]} — снова после текста; пусто — chat_reply
        self.chat_script = []
        self.reasoning_field = "reasoning_content"   # поле дельты рассуждения: vLLM через LiteLLM; иначе "reasoning"
        #: после первого куска рассуждения ответ «думает», пока не снимут; функция без аргументов — пока она истинна
        #: (живой службе браузерных тестов: сценарий страницы снимает его своим запросом)
        self.think_hold = False
        self.chat_error = None           # (код, тело) вместо ответа
        self.chat_delay = 0              # молчание до первого куска — по часам студии (их двигают другие)
        self.chat_hold = False           # после первого куска ответ «идёт», пока не снимут
        self.chat_ending = "stop"        # finish_reason конца потока; None — поток рвётся без него и без [DONE]
        self.asr_requests = []           # запросы распознавания (asr, transcriptions, lite)
        self.asr_replies = []            # (код, тело) по очереди, затем — (200, asr_reply)
        self.asr_reply = {"text": "распознано", "language": "ru", "segments": [
            {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "text": "Здравствуйте."},
            {"start": 3.0, "end": 5.0, "speaker": "SPEAKER_01", "text": "Добрый день."}]}
        self.tts_requests = []           # запросы озвучки /speech/tts-qwen
        self.tts_replies = [(200, WAV_FAKE)]   # (код, тело: bytes — звук, dict — JSON); последний повторяется
        self.ocr_requests = []           # запросы страниц /pages/ocr
        self.ocr_replies = []            # (код, тело) по очереди, затем — (200, ocr_reply)
        self.ocr_reply = {"choices": [{"message": {"content": "# Договор\n\nТекст страницы."}, "finish_reason": "stop"}]}
        #: ответ страницы ждёт, пока не снимут; функция от запроса — пока она истинна (держать один из нескольких)
        self.ocr_hold = False
        self.ocr_inflight = 0            # страницы, на которые шлюз ещё не ответил: release слота ocr тогда — 409
        self.exa_requests = []           # (путь, тело, заголовок x-api-key) запросов к Exa
        #: путь → (код, тело) ответа Exa; `/contents` без записи — образец по адресам запроса
        self.exa_replies = {"/search": (200, EXA_SEARCH)}
        self.exa_page_text = "Текст страницы."   # текст образца `/contents` (режется по maxCharacters, как у Exa)
        self.exa_html = False            # True — 403 text/html Cloudflare, как Exa отвечает debian
        self.exa_raise = None            # класс исключения httpx вместо ответа Exa

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == EXA_HOST:
            return self._exa(request)
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
        if path == "/pages/ocr" and request.method == "POST":
            return await self._ocr(request)
        if path == "/pages/ocr-release" and request.method == "POST":
            # тело — `{}`, модели нет; slot-gate отвечает 409, пока страница ещё считается
            self.releases.append((None, request.headers.get("authorization")))
            if self.ocr_inflight:
                return httpx.Response(409, json={"error": {"message": "slot_busy: идёт распознавание"}})
            code, body = self.release_replies[min(len(self.releases), len(self.release_replies)) - 1]
            return httpx.Response(code, json=body)
        if path == "/slot/images/descriptions":
            self.descriptions.append(json.loads(request.content))
            return httpx.Response(200, json=self.description_reply)
        if path == "/slot/videos" or path.startswith("/slot/videos/"):
            return self._video(request)
        if path == "/v1/models" and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": self.models})
        if path == "/slot/models" and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": self.slot_models})
        if path == "/v1/chat/completions" and request.method == "POST":
            return self._chat(request)
        if path in SPEECH_ASR and request.method == "POST":
            self.asr_requests.append(request)
            code, body = self.asr_replies.pop(0) if self.asr_replies else (200, self.asr_reply)
            return httpx.Response(code, json=body)
        if path == "/speech/tts-qwen" and request.method == "POST":
            self.tts_requests.append(request)
            code, body = self.tts_replies[min(len(self.tts_requests), len(self.tts_replies)) - 1]
            if isinstance(body, bytes):
                return httpx.Response(code, content=body, headers={"content-type": "audio/wav"})
            return httpx.Response(code, json=body)
        return httpx.Response(404)

    async def _ocr(self, request: httpx.Request) -> httpx.Response:
        """Страница HunyuanOCR через шлюз: ответ OpenAI chat completions (не поток)."""
        self.ocr_requests.append(request)
        self.ocr_inflight += 1
        try:
            while self.ocr_hold(request) if callable(self.ocr_hold) else self.ocr_hold:
                await asyncio.sleep(0.001)
        finally:
            self.ocr_inflight -= 1
        code, body = self.ocr_replies.pop(0) if self.ocr_replies else (200, self.ocr_reply)
        return httpx.Response(code, json=body)

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

        step = self.chat_script.pop(0) if self.chat_script else self.chat_reply
        pieces, calls, thoughts = ((step.get("text", []), step.get("tool_calls", []), step.get("reasoning", []))
                                   if isinstance(step, dict) else (step, [], []))
        then = step.get("then", {}) if isinstance(step, dict) else {}
        number = len(self.chat_requests)

        def data(delta, ending=None):
            chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": ending}]}
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

        async def chunks():
            # Молчание считается по часам студии, сам шлюз их не двигает: дальше его ведёт только
            # тот, кто ждёт (пинги чата) — без гонки двух пауз за одни часы. Предел проходов: без
            # пингов тест падает на счёте, а не висит до таймаута.
            start = clock.NOW()
            for _ in range(1000):
                if clock.NOW() - start >= self.chat_delay:
                    break
                await asyncio.sleep(0.001)
            for i, thought in enumerate(thoughts):
                yield data({self.reasoning_field: thought})
                while i == 0 and (self.think_hold() if callable(self.think_hold) else self.think_hold):
                    await asyncio.sleep(0.001)
            for i, piece in enumerate(pieces):
                yield data({"content": piece})
                while i == 0 and self.chat_hold:
                    await asyncio.sleep(0.001)
            for thought in then.get("reasoning", []):    # модель снова думает после текста того же круга
                yield data({self.reasoning_field: thought})
            for piece in then.get("text", []):
                yield data({"content": piece})
            # вызовы — как vLLM gemma-abl через шлюз (проба 2026-10-21): имя с id и index, затем
            # аргументы кусками; конец — finish_reason "tool_calls"
            for i, (name, arguments) in enumerate(calls):
                yield data({"tool_calls": [{"index": i, "id": f"chatcmpl-tool-{number}-{i}", "type": "function",
                                            "function": {"name": name}}]})
                half = len(arguments) // 2
                for part in (arguments[:half], arguments[half:]):
                    yield data({"tool_calls": [{"index": i, "function": {"arguments": part}}]})
            ending = "tool_calls" if calls else self.chat_ending
            if ending is not None:
                yield data({}, ending)
                yield b"data: [DONE]\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    def _exa(self, request: httpx.Request) -> httpx.Response:
        """Exa: `POST /search` и `/contents`. Без ключа — 402 x402, как живой Exa."""
        path, body = request.url.path, json.loads(request.content or b"{}")
        self.exa_requests.append((path, body, request.headers.get("x-api-key")))
        if self.exa_raise is not None:
            raise self.exa_raise("нет ответа", request=request)
        if self.exa_html:
            return httpx.Response(403, text="<!DOCTYPE html><title>Attention Required!</title>"
                                            "<h1>Sorry, you have been blocked</h1>",
                                  headers={"content-type": "text/html; charset=UTF-8"})
        if not request.headers.get("x-api-key"):
            return httpx.Response(402, json={"error": "Payment required", "tag": "X402_PAYMENT_REQUIRED"})
        if path in self.exa_replies:
            code, reply = self.exa_replies[path]
            return httpx.Response(code, json=reply)
        if path == "/contents" and request.method == "POST":
            limit = (body.get("text") or {}).get("maxCharacters")
            urls = body.get("urls") or []
            results = [{"id": u, "url": u, "title": f"Страница {httpx.URL(u).host}",
                        "text": self.exa_page_text[:limit]} for u in urls]
            statuses = [{"id": u, "status": "success", "source": "cached"} for u in urls]
            return httpx.Response(200, json={"requestId": "req-contents", "results": results, "statuses": statuses,
                                             "costDollars": {"total": 0.001}})
        return httpx.Response(404, json={"error": "Not found", "tag": "NOT_FOUND"})

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
            self.video_fields[video_id] = fields
            return httpx.Response(200, json=video_object(video_id, seed=self.videos[video_id]))
        if path == "/slot/videos" and method == "GET":
            # список шлюза — как у переводчика: все задания всех ключей, с длительностью, качеством,
            # дорисовкой и промптом (свой — `given`)
            data = []
            for vid, seed in self.videos.items():
                f = self.video_fields.get(vid, {})
                data.append(video_object(vid, status=self.video_status, seed=seed, seconds=f.get("seconds", "5"),
                                         quality=f.get("quality", "standard"), smooth=f.get("smooth", "true") == "true",
                                         prompt=f.get("prompt"), prompt_source="given" if f.get("prompt") else "auto"))
            return httpx.Response(200, json={"object": "list", "data": data, "has_more": False})
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


class FakeCloud:
    """Облачный OpenAI-совместимый сервис для браузерных тестов: настоящий HTTP-сервер на `localhost`
    с CORS — прямое подключение зовёт сам браузер. `/v1/models` и потоковый `/v1/chat/completions`
    (строки `data:` SSE и `data: [DONE]`); чужой ключ — 401: верен только `TOKEN`."""
    TOKEN = "cloud-key"

    def __init__(self):
        self.models = [{"id": "cloud-chat"}]
        self.chat_reply = ["Облако ", "на связи."]
        self.chat_requests = []          # тела запросов /v1/chat/completions
        self.chat_auth = []              # их заголовки авторизации
        self.models_auth = []            # заголовки авторизации /v1/models
        self.chunk_pause = 0.05          # пауза между кусками: браузер видит поток, а не готовый ответ
        self.chat_ending = "stop"        # finish_reason конца потока; None — поток рвётся без него и без [DONE]
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def _json(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self):
                if self.headers.get("Authorization") == f"Bearer {cloud.TOKEN}":
                    return True
                self._json(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}})
                return False

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()

            def do_GET(self):
                if self.path != "/v1/models":
                    return self._json(404, {"error": {"message": "not found"}})
                cloud.models_auth.append(self.headers.get("Authorization"))
                if self._authorized():
                    self._json(200, {"object": "list", "data": cloud.models})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path != "/v1/chat/completions":
                    return self._json(404, {"error": {"message": "not found"}})
                cloud.chat_auth.append(self.headers.get("Authorization"))
                if not self._authorized():
                    return
                cloud.chat_requests.append(body)
                if not body.get("stream"):
                    return self._json(200, {"choices": [{"message": {"role": "assistant",
                                                                     "content": "".join(cloud.chat_reply)},
                                                         "finish_reason": "stop"}]})
                # HTTP/1.0 без длины: конец потока — закрытие соединения
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for piece in cloud.chat_reply:
                    delta = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(delta, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(cloud.chunk_pause)
                if cloud.chat_ending is not None:
                    end = {"choices": [{"index": 0, "delta": {}, "finish_reason": cloud.chat_ending}]}
                    self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://localhost:{self.server.server_port}"
        self.base_url = self.url + "/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
