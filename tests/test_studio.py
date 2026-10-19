"""Страница студии (app/server.py) — клиент API картинок шлюза: вход ключом
шлюза и фоновое задание «POST /slot/images/generations → PNG в память». Шлюз подменён
httpx.MockTransport, часы — srv.NOW, паузы — srv.SLEEP (спеки 2026-10-16-srvllm-studio и
2026-10-19-images-api-lustify, раздел 4)."""
import asyncio
import base64
import importlib.util
import json
import logging
import sys
import time
import threading
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import APP, DEPLOY

STUDIO = APP
TOKEN = "owner-images-test-key"
PROMPT = "секретный промпт: женщина у окна"
PNG = b"\x89PNG\r\n\x1a\nfake-image"
T0 = 1_800_000_000.0
GW = "http://gw.test/slot"
LUSTIFY, FLUX = "lustify-v10-krea2-turbo", "flux.2-klein-4b"


def load():
    """server.py студии — свежим экземпляром модуля."""
    spec = importlib.util.spec_from_file_location("studio_server", STUDIO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclass ищет свой модуль в sys.modules
    spec.loader.exec_module(mod)
    return mod


class Gateway:
    """Шлюз: /slot/models (проверка ключа), генерация, освобождение карты."""

    def __init__(self):
        self.down = False
        self.models_calls = 0
        self.generations = []            # (тело, заголовок авторизации, таймаут чтения)
        self.reply = None                # (код, тело) вместо картинки
        self.raise_on_generate = None
        self.hold = False                # генерация «идёт», пока не снимут
        self.aborted = 0                 # запрос генерации оборван клиентом
        self.completed = 0               # запрос генерации дошёл до ответа
        self.releases = []
        self.release_replies = [(200, {"ok": True, "slot": "comfy"})]   # последний повторяется

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("нет соединения", request=request)
        good = request.headers.get("authorization") == f"Bearer {TOKEN}"
        path = request.url.path
        if path == "/slot/models":
            self.models_calls += 1
            if not good:
                return httpx.Response(401, json={"error": {"message": "Authentication Error", "type": "auth_error"}})
            return httpx.Response(200, json={"object": "list", "data": [{"id": LUSTIFY}, {"id": FLUX}]})
        if path == "/slot/images/generations":
            body = json.loads(request.content)
            self.generations.append((body, request.headers.get("authorization"),
                                     request.extensions["timeout"]["read"]))
            try:
                while self.hold:
                    await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                self.aborted += 1
                raise
            self.completed += 1
            if self.raise_on_generate:
                raise self.raise_on_generate("нет ответа", request=request)
            if self.reply:
                return httpx.Response(self.reply[0], json=self.reply[1])
            return httpx.Response(200, json={"created": 1, "data": [
                {"b64_json": base64.b64encode(PNG).decode(), "seed": body.get("seed")}]})
        if path in ("/slot/images/release", "/slot/images/descriptions/release"):
            self.releases.append((json.loads(request.content)["model"],
                                  request.headers.get("authorization")))
            code, body = self.release_replies[min(len(self.releases), len(self.release_replies)) - 1]
            return httpx.Response(code, json=body)
        return httpx.Response(404)


@pytest.fixture
def studio(monkeypatch):
    srv = load()
    gw = Gateway()
    monkeypatch.setenv("STUDIO_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("STUDIO_GATEWAY_URL", GW + "/")
    monkeypatch.setattr(srv, "make_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(gw.handler)))
    clock = [T0]
    monkeypatch.setattr(srv, "NOW", lambda: clock[0])
    sleeps = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds              # пауза двигает подменённые часы: сроки повторов честные
        await asyncio.sleep(0.001)

    monkeypatch.setattr(srv, "SLEEP", fast_sleep)
    with TestClient(srv.app, base_url="https://testserver") as client:
        yield types.SimpleNamespace(srv=srv, gw=gw, c=client, clock=clock, sleeps=sleeps)


def login(s, token=TOKEN, ip="10.77.48.5", spoofed=None):
    """Apache дописывает настоящий адрес клиента В КОНЕЦ X-Forwarded-For; начало может прислать
    сам клиент (`spoofed`)."""
    forwarded = f"{spoofed}, {ip}" if spoofed else ip
    return s.c.post("/studio/login", data={"token": token},
                    headers={"X-Forwarded-For": forwarded}, follow_redirects=False)


def start(s, prompt=PROMPT, size="square", seed=None, model=None):
    body = {"prompt": prompt, "size": size, "seed": seed} | ({"model": model} if model else {})
    return s.c.post("/studio/api/jobs", json=body)


def wait_state(s, job_id, states, limit=3000):
    for _ in range(limit):
        body = s.c.get(f"/studio/api/jobs/{job_id}").json()
        if body["state"] in states:
            return body
    raise AssertionError(f"задание не дошло до {states}: {body}")


def wait_for(cond, limit=3000):
    for _ in range(limit):
        if cond():
            return
        time.sleep(0.001)
    raise AssertionError("условие не наступило")


# ---- вход ------------------------------------------------------------------

@pytest.mark.parametrize("model", ["lustify", "flux"])
def test_uploaded_photo_is_forwarded_to_edits_and_discarded(studio, monkeypatch, model):
    import io
    from PIL import Image
    from image_upload import decode_form

    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, "PNG")
    observed = []
    original = studio.gw.handler

    async def handler(request):
        if request.url.path == "/slot/images/edits":
            fields, photo = decode_form(request.content, request.headers["content-type"])
            observed.append(fields)
            assert photo.startswith(b"\x89PNG")
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        return await original(request)

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    r = studio.c.post("/studio/api/jobs", data={"prompt": PROMPT, "size": "square",
                      "model": model, "strength": "0.4"},
                      files={"image": ("photo.png", output.getvalue(), "image/png")})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert wait_state(studio, job_id, {"done", "error"})["state"] == "done"
    assert observed[0]["model"] == studio.srv.MODELS[model]
    assert (observed[0].get("strength") == "0.4") if model == "lustify" else "strength" not in observed[0]
    assert studio.srv.JOBS[job_id].image is None


def test_invalid_photo_creates_no_studio_job(studio):
    login(studio)
    r = studio.c.post("/studio/api/jobs", data={"prompt": PROMPT, "size": "square"},
                      files={"image": ("bad.png", b"bad", "image/png")})
    assert r.status_code == 400 and not studio.srv.JOBS


def test_masked_edit_preserves_source_size_and_pixels_without_forwarding_mask(studio, monkeypatch):
    import io
    from PIL import Image
    from image_upload import decode_form

    def png(image):
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    source = png(Image.new("RGB", (48, 32), "blue"))
    mask = Image.new("L", (48, 32), 0)
    mask.paste(255, (10, 8, 30, 24))
    original = studio.gw.handler
    observed = []

    async def handler(request):
        if request.url.path == "/slot/images/edits":
            fields, photo = decode_form(request.content, request.headers["content-type"])
            observed.append(fields)
            assert photo == source and fields["size"] == "48x32" and fields["model"] == FLUX
            assert "mask" not in fields and "strength" not in fields
            edited = png(Image.new("RGB", (96, 64), "red"))
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(edited).decode()}]})
        return await original(request)

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    r = studio.c.post("/studio/api/jobs", data={"model": "flux", "size": "original", "prompt": "make this red"},
                      files={"image": ("source.png", source, "image/png"),
                             "mask": ("mask.png", png(mask), "image/png")})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert wait_state(studio, job_id, {"done", "error"})["state"] == "done"
    image = studio.c.get(f"/studio/api/jobs/{job_id}/image")
    with Image.open(io.BytesIO(image.content)) as result:
        assert result.size == (48, 32)
        for y in range(32):
            for x in range(48):
                assert result.getpixel((x, y)) == ((255, 0, 0) if mask.getpixel((x, y)) else (0, 0, 255))
    assert len(observed) == 1
    job = studio.srv.JOBS[job_id]
    assert job.mask is None and job.image is None


def test_original_size_requires_a_photo(studio):
    login(studio)
    r = studio.c.post("/studio/api/jobs", json={"prompt": "test", "size": "original"})
    assert r.status_code == 422 and not studio.srv.JOBS


def test_maximum_prompt_in_ascii_escaped_json_reaches_gateway(studio):
    login(studio)
    prompt = "я" * 24000
    response = studio.c.post("/studio/api/jobs", content=json.dumps({"prompt": prompt, "size": "square"}),
                             headers={"Content-Type": "application/json"})
    assert response.status_code == 202
    assert wait_state(studio, response.json()["id"], {"done", "error"})["state"] == "done"
    assert studio.gw.generations[0][0]["prompt"] == prompt


def description_photo():
    import io
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (48, 32), "blue").save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_description_uses_gemma_abl_and_returns_editable_text_once(studio, monkeypatch, caplog, finish):
    text = "A private detailed description. " * 300
    observed = []
    original = studio.gw.handler

    async def handler(request):
        if request.url.path == "/slot/images/descriptions":
            body = json.loads(request.content)
            observed.append(body)
            assert body["model"] == "gemma4-abl"
            assert body["chat_template_kwargs"] == {"enable_thinking": False}
            assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}]})
        return await original(request)

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    response = studio.c.post("/studio/api/descriptions", files={"image": ("source.png", description_photo(), "image/png")})
    assert response.status_code == 202
    job_id = response.json()["id"]
    status = wait_state(studio, job_id, {"done", "error"})
    assert status["state"] == "done" and status["kind"] == "description"
    result = studio.c.get(f"/studio/api/jobs/{job_id}/description")
    assert result.json() == {"text": text.strip(), "truncated": finish == "length", "model": "gemma-abl"}
    assert result.headers["cache-control"] == "no-store"
    assert studio.c.get(f"/studio/api/jobs/{job_id}/description").status_code == 404
    assert [model for model, _ in studio.gw.releases] == [LUSTIFY, FLUX, "gemma4-abl"]
    job = studio.srv.JOBS[job_id]
    assert job.image is None and job.key == "" and job.description is None
    assert len(observed) == 1 and text.strip() not in caplog.text


def test_description_cancellation_waits_then_discards_text_and_releases(studio, monkeypatch):
    original = studio.gw.handler
    arrived, finish = threading.Event(), threading.Event()

    async def handler(request):
        if request.url.path == "/slot/images/descriptions":
            arrived.set()
            while not finish.is_set():
                await asyncio.sleep(0.001)
            return httpx.Response(200, json={"choices": [{"message": {"content": "private description"}}]})
        return await original(request)

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    response = studio.c.post("/studio/api/descriptions", files={"image": ("source.png", description_photo(), "image/png")})
    job_id = response.json()["id"]
    assert arrived.wait(1)
    assert start(studio).status_code == 409
    assert studio.c.delete(f"/studio/api/jobs/{job_id}").json()["state"] == "cancelling"
    finish.set()
    assert wait_state(studio, job_id, {"cancelled", "error"})["state"] == "cancelled"
    assert studio.c.get(f"/studio/api/jobs/{job_id}/description").status_code == 404
    assert [model for model, _ in studio.gw.releases][-1] == "gemma4-abl"


def test_description_can_be_cancelled_during_model_release(studio, monkeypatch):
    original = studio.gw.handler
    releasing, finish = threading.Event(), threading.Event()

    async def handler(request):
        if request.url.path == "/slot/images/descriptions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "description"}}]})
        if request.url.path == "/slot/images/descriptions/release":
            releasing.set()
            while not finish.is_set():
                await asyncio.sleep(0.001)
        return await original(request)

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    response = studio.c.post("/studio/api/descriptions", files={"image": ("source.png", description_photo(), "image/png")})
    job_id = response.json()["id"]
    assert releasing.wait(1)
    studio.c.delete(f"/studio/api/jobs/{job_id}")
    finish.set()
    assert wait_state(studio, job_id, {"done", "cancelled"})["state"] == "cancelled"
    assert studio.c.get(f"/studio/api/jobs/{job_id}/description").status_code == 404


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": [{"message": {"content": ""}}]}])
def test_empty_description_errors_and_releases_model(studio, monkeypatch, body):
    original = studio.gw.handler

    async def handler(request):
        if request.url.path == "/slot/images/descriptions":
            return httpx.Response(200, json=body)
        return await original(request)

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    response = studio.c.post("/studio/api/descriptions", files={"image": ("source.png", description_photo(), "image/png")})
    status = wait_state(studio, response.json()["id"], {"done", "error"})
    assert status["state"] == "error" and "не вернула описание" in status["detail"]
    assert [model for model, _ in studio.gw.releases][-1] == "gemma4-abl"


def test_health_is_open_and_everything_else_needs_login(studio):
    assert studio.c.get("/studio/health").json() == {"status": "ok"}
    page = studio.c.get("/studio/")
    assert page.status_code == 200 and 'name="token"' in page.text and 'name="password"' not in page.text
    # переход с чужого сайта не несёт SameSite=Strict: вход сам проверяет сессию своим запросом
    assert 'fetch("api/jobs/current"' in page.text and "relogin" in page.text
    for method, url in (("post", "/studio/api/jobs"), ("get", "/studio/api/jobs/x"),
                        ("get", "/studio/api/jobs/x/image"), ("delete", "/studio/api/jobs/x"),
                        ("post", "/studio/api/release")):
        r = getattr(studio.c, method)(url)
        assert r.status_code == 401 and r.json()["detail"]["code"] == "login", url
    assert studio.gw.generations == [] and studio.gw.releases == []


def test_login_checks_the_token_at_the_gateway_and_sets_a_strict_secure_cookie(studio):
    r = login(studio)
    assert r.status_code == 303 and r.headers["location"] == "/studio/"
    assert studio.gw.models_calls == 1
    cookie = r.headers["set-cookie"]
    for flag in ("studio_session=", "HttpOnly", "Secure", "SameSite=strict", "Path=/studio/",
                 "Max-Age=2592000"):
        assert flag.lower() in cookie.lower(), flag
    assert TOKEN not in cookie
    page = studio.c.get("/studio/").text
    assert 'id="prompt"' in page and 'id="model"' in page
    assert 'studio_session="' not in cookie                    # без кавычек: браузер вернёт как есть
    b64 = base64.urlsafe_b64encode(TOKEN.encode()).decode().rstrip("=")
    assert TOKEN not in page and b64 not in page                 # ключ не в странице и не в её JS


def test_wrong_token_is_refused_and_counted(studio):
    r = login(studio, "sk-wrong")
    assert r.status_code == 401 and "Неверный токен" in r.text and "set-cookie" not in r.headers
    assert len(studio.srv.FAILS["10.77.48.5"]) == 1
    # не похоже на ключ (пробел, перевод строки) — отказ без вызова шлюза, но тоже неудача
    calls = studio.gw.models_calls
    assert login(studio, "a b\r\nX-Evil: 1").status_code == 401
    assert studio.gw.models_calls == calls and len(studio.srv.FAILS["10.77.48.5"]) == 2


def test_gateway_down_at_login_is_502_and_not_a_failure(studio):
    studio.gw.down = True
    r = login(studio)
    assert r.status_code == 502 and "Шлюз недоступен" in r.text
    assert studio.srv.FAILS == {}


def test_login_refuses_an_oversized_body_before_the_token_check(studio):
    """Вход открыт без сессии: тело сверх предела не читается целиком и неудачей не считается."""
    huge = "token=" + "x" * (studio.srv.LOGIN_BODY_MAX + 1)
    r = studio.c.post("/studio/login", content=huge,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "X-Forwarded-For": "10.77.48.5"}, follow_redirects=False)
    assert r.status_code == 413 and "set-cookie" not in r.headers
    assert studio.srv.FAILS == {} and studio.gw.models_calls == 0
    assert login(studio).status_code == 303


def test_five_bad_tokens_lock_the_address(studio):
    for _ in range(5):
        assert login(studio, "sk-no").status_code == 401
    calls = studio.gw.models_calls
    assert login(studio).status_code == 429                    # и верный токен не пускает
    assert studio.gw.models_calls == calls                     # шлюз под блокировкой не зовётся
    assert login(studio, ip="10.10.49.7").status_code == 303   # другой адрес — свой счёт
    studio.clock[0] = T0 + 15 * 60 + 1
    assert login(studio).status_code == 303


def test_spoofed_forwarded_prefix_does_not_reset_the_lock(studio):
    """Начало X-Forwarded-For присылает клиент: счёт — по последнему элементу, который
    дописал Apache (как у панели, dashboard/app/auth.py::client_key)."""
    for n in range(5):
        assert login(studio, "sk-no", spoofed=f"203.0.113.{n}").status_code == 401
    assert login(studio, spoofed="203.0.113.99").status_code == 429


def test_failed_logins_from_quiet_addresses_are_forgotten(studio):
    """Адрес без свежих неудач не копится в FAILS навсегда (находка 2026-10-17)."""
    assert login(studio, "sk-no", ip="203.0.113.1").status_code == 401
    assert login(studio, "sk-no", ip="203.0.113.2").status_code == 401
    assert set(studio.srv.FAILS) == {"203.0.113.1", "203.0.113.2"}
    studio.clock[0] = T0 + 15 * 60 + 1
    assert login(studio, ip="203.0.113.3").status_code == 303
    assert studio.srv.FAILS == {}


def test_expired_session_gets_401_json(studio):
    login(studio)
    studio.clock[0] = T0 + 30 * 86400 + 1
    r = start(studio)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "login"


def test_session_survives_a_restart_and_rejects_tampering(studio):
    """Сессия — подписанная cookie: новый процесс с тем же секретом её принимает, правка
    ключа или срока и другой секрет — нет."""
    cookie = login(studio).cookies["studio_session"]
    fresh = load()
    fresh.CFG["STUDIO_SESSION_SECRET"] = "session-secret"
    fresh.NOW = lambda: T0 + 86400
    assert fresh.session_key(cookie) == TOKEN
    version, exp, packed, sig = cookie.split(".")
    other = base64.urlsafe_b64encode(b"sk-other").decode()
    for forged in (f"{version}.{exp}.{other}.{sig}", f"{version}.{int(exp) + 1}.{packed}.{sig}",
                   f"v2.{exp}.{packed}.{sig}", "garbage", ""):
        assert fresh.session_key(forged) is None, forged
    fresh.CFG["STUDIO_SESSION_SECRET"] = "rotated"
    assert fresh.session_key(cookie) is None


# ---- задание ---------------------------------------------------------------


def test_job_goes_through_the_gateway_with_the_session_key(studio):
    login(studio)
    r = start(studio, size="portrait", seed=7)
    assert r.status_code == 202 and r.json()["seed"] == 7
    job = r.json()["id"]
    body = wait_state(studio, job, {"done", "error"})
    assert body["state"] == "done" and body["model"] == "lustify"
    image = studio.c.get(f"/studio/api/jobs/{job}/image")
    assert image.status_code == 200 and image.content == PNG and image.headers["content-type"] == "image/png"
    assert studio.c.get(f"/studio/api/jobs/{job}/image").status_code == 404
    payload, auth, read_timeout = studio.gw.generations[0]
    assert payload == {"model": LUSTIFY, "prompt": PROMPT, "n": 1, "size": "832x1216", "seed": 7,
                       "response_format": "b64_json"}
    assert auth == f"Bearer {TOKEN}" and read_timeout == 1300


@pytest.mark.parametrize("model, gateway_id", [("flux", FLUX), ("lustify", LUSTIFY)])
def test_model_is_chosen_on_the_page(studio, model, gateway_id):
    login(studio)
    wait_state(studio, start(studio, model=model).json()["id"], {"done"})
    assert studio.gw.generations[0][0]["model"] == gateway_id


def test_seed_from_the_gateway_answer_is_shown(studio):
    studio.gw.reply = (200, {"data": [{"b64_json": base64.b64encode(PNG).decode(), "seed": 42}]})
    login(studio)
    job = start(studio, seed=7).json()["id"]
    assert wait_state(studio, job, {"done"})["seed"] == 42


@pytest.mark.parametrize("code, body, words", [
    (401, {"error": {"message": "Authentication Error"}}, "войдите заново"),
    (403, {"error": {"message": "forbidden"}}, "закрыт"),
    (400, {"error": {"message": "unknown model", "type": "invalid_request_error"}}, "шлюз отклонил запрос: unknown model"),
    (503, {"error": "no_capacity", "detail": "x"}, "карта занята или недоступна: no_capacity"),
    (504, {"error": {"message": "картинка не готова за 300 с"}}, "не дождался службы"),
    (200, {"data": []}, "не вернул картинку"),
])
def test_gateway_failures_read_as_plain_words(studio, code, body, words):
    studio.gw.reply = (code, body)
    login(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and words in result["detail"]


def test_gateway_read_timeout_is_an_error_not_a_hang(studio):
    studio.gw.raise_on_generate = httpx.ReadTimeout
    login(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and "1300" in result["detail"]


def test_working_status_shows_the_elapsed_time(studio):
    studio.gw.hold = True
    login(studio)
    job = start(studio).json()["id"]
    wait_for(lambda: studio.gw.generations)
    studio.clock[0] = T0 + 83
    body = studio.c.get(f"/studio/api/jobs/{job}").json()
    assert body["state"] == "working" and body["elapsed"] == 83
    assert "blocked_by" not in body
    studio.gw.hold = False
    wait_state(studio, job, {"done"})


def test_second_job_while_one_runs_is_busy(studio):
    studio.gw.hold = True
    login(studio)
    first = start(studio).json()["id"]
    r = start(studio)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert r.json()["detail"]["id"] == first                  # страница может подхватить его
    studio.gw.hold = False
    wait_state(studio, first, {"done"})


def test_current_job_survives_a_page_reload(studio):
    studio.gw.hold = True
    login(studio)
    assert studio.c.get("/studio/api/jobs/current").status_code == 404
    job = start(studio, model="flux").json()["id"]
    current = studio.c.get("/studio/api/jobs/current")
    assert current.status_code == 200 and current.json()["id"] == job
    assert current.json()["state"] == "working" and current.json()["model"] == "flux"
    studio.gw.hold = False
    wait_state(studio, job, {"done"})
    assert studio.c.get("/studio/api/jobs/current").status_code == 404


SLOT_BUSY = (409, {"error": {"message": "busy", "type": "slot_busy"}})


def _cancel_while_generating(s, model="flux"):
    """Задание висит на запросе к шлюзу, его отменяют: id задания."""
    s.gw.hold = True
    login(s)
    job = start(s, model=model).json()["id"]
    wait_for(lambda: s.gw.generations)
    assert s.c.delete(f"/studio/api/jobs/{job}").json() == {"state": "cancelling"}
    return job


def test_cancel_waits_for_the_gateway_request_then_releases_the_models_slot(studio):
    """Обрыв запроса до slot-gate не доходит (LiteLLM его не передаёт): отмена запрос не рвёт,
    ждёт его конца, картинку выбрасывает и сразу освобождает карту модели задания (ревью
    2026-10-19). Пока задание отменяется, новое получает busy."""
    job = _cancel_while_generating(studio)
    body = studio.c.get(f"/studio/api/jobs/{job}").json()
    assert body["state"] == "cancelling" and "после текущей картинки" in body["detail"]
    assert "ждёт карту" in body["detail"]                     # честно: и ожидание карты дождёмся
    assert studio.c.delete(f"/studio/api/jobs/{job}").json() == {"state": "cancelling"}
    busy = start(studio)
    assert busy.status_code == 409 and busy.json()["detail"]["id"] == job
    assert "отменяется" in busy.json()["detail"]["message"]
    assert studio.gw.releases == []                           # пока запрос идёт, release — 409
    studio.gw.hold = False
    result = wait_state(studio, job, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and "карта освобождена" in result["detail"]
    assert studio.gw.aborted == 0 and studio.gw.completed == 1
    assert studio.gw.releases == [(FLUX, f"Bearer {TOKEN}")]
    assert studio.c.get(f"/studio/api/jobs/{job}/image").status_code == 404
    assert studio.srv.JOBS[job].png is None and studio.srv.JOBS[job].key == ""
    assert start(studio).status_code == 202                   # отменённое не держит очередь


def test_cancel_retries_release_while_the_slot_is_busy(studio):
    studio.gw.release_replies = [SLOT_BUSY, SLOT_BUSY, (200, {"ok": True, "slot": "comfy"})]
    job = _cancel_while_generating(studio, model="lustify")
    studio.gw.hold = False
    result = wait_state(studio, job, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and "карта освобождена" in result["detail"]
    assert [m for m, _ in studio.gw.releases] == [LUSTIFY] * 3
    assert studio.sleeps == [studio.srv.RELEASE_PAUSE_SEC] * 2


def test_cancel_gives_up_release_after_a_minute_of_409(studio):
    """Карту держит чужая генерация: дольше CANCEL_RELEASE_SEC не ждём — аренду снимет её срок."""
    studio.gw.release_replies = [SLOT_BUSY]
    job = _cancel_while_generating(studio)
    studio.gw.reply = (503, {"error": "no_capacity"})          # и запрос кончился ошибкой
    studio.gw.hold = False
    result = wait_state(studio, job, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and "освободится сама" in result["detail"]
    assert sum(studio.sleeps) >= studio.srv.CANCEL_RELEASE_SEC
    assert len(studio.gw.releases) == studio.srv.CANCEL_RELEASE_SEC // studio.srv.RELEASE_PAUSE_SEC + 1


def test_finished_job_is_not_cancelled(studio):
    login(studio)
    job = start(studio).json()["id"]
    wait_state(studio, job, {"done"})
    assert studio.c.delete(f"/studio/api/jobs/{job}").json() == {"state": "done"}
    assert studio.gw.releases == []


def test_release_button_frees_image_and_description_models(studio):
    login(studio)
    r = studio.c.post("/studio/api/release")
    assert r.status_code == 200
    assert {k: v["state"] for k, v in r.json()["results"].items()} == {"lustify": "released", "flux": "released", "gemma-abl": "released"}
    assert sorted(m for m, _ in studio.gw.releases) == sorted([LUSTIFY, FLUX, "gemma4-abl"])


def test_release_reports_each_model_in_plain_words(studio):
    """409 без сообщения — прежние слова; с сообщением — см. test_release_shows_the_busy_reason."""
    studio.gw.release_replies = [(409, {"error": {"type": "slot_busy"}}), (500, {"error": {"message": "boom"}})]
    login(studio)
    r = studio.c.post("/studio/api/release")
    assert r.status_code == 200
    results = r.json()["results"]
    assert results["lustify"] == {"state": "busy", "message": "занята генерацией"}
    assert results["flux"]["state"] == "error" and "boom" in results["flux"]["message"]


def test_release_with_a_revoked_key_asks_for_login(studio):
    studio.gw.release_replies = [(401, {"error": {"message": "Authentication Error"}})]
    login(studio)
    assert studio.c.post("/studio/api/release").status_code == 401


@pytest.mark.parametrize("payload", [
    {"prompt": "", "size": "square"},
    {"prompt": "x" * 24001, "size": "square"},
    {"prompt": "a cat", "size": "huge"},
    {"prompt": "a cat", "size": "square", "seed": -1},
    {"prompt": "a cat", "size": "square", "model": "dall-e-3"},
])
def test_prompt_size_seed_and_model_are_validated(studio, payload):
    login(studio)
    assert studio.c.post("/studio/api/jobs", json=payload).status_code == 422
    assert studio.gw.generations == []


def test_unclaimed_image_is_forgotten_after_ten_minutes(studio):
    login(studio)
    job = start(studio).json()["id"]
    wait_state(studio, job, {"done"})
    studio.clock[0] = T0 + 601
    assert studio.c.get(f"/studio/api/jobs/{job}").status_code == 404


def test_prompt_and_key_never_reach_the_log_or_the_answers(studio, caplog):
    caplog.set_level(logging.DEBUG)
    login(studio, "sk-wrong-7f3a")
    login(studio)
    job = start(studio).json()["id"]
    status = wait_state(studio, job, {"done"})
    studio.gw.reply = (400, {"error": {"message": "bad"}})
    wait_state(studio, start(studio).json()["id"], {"error"})
    assert caplog.records, "служба ничего не пишет в журнал"
    for secret in (PROMPT, TOKEN, "sk-wrong-7f3a"):
        assert secret not in caplog.text, secret
    assert TOKEN not in str(status) and TOKEN not in studio.c.get("/studio/api/jobs/current").text
    assert studio.srv.JOBS[job].key == "" and studio.srv.JOBS[job].prompt == ""


def test_release_shows_the_busy_reason(studio):
    """Lustify и видео — слот comfy: переводчик при идущих роликах отвечает 409 «идут ролики: N»,
    кнопка показывает его слова (спека 2026-10-19-srvllm-studio-video, раздел 6)."""
    studio.gw.release_replies = [(409, {"error": {"message": "идут ролики: 2", "type": "slot_busy"}}),
                                 (200, {"ok": True})]
    login(studio)
    results = studio.c.post("/studio/api/release").json()["results"]
    assert results["lustify"] == {"state": "busy", "message": "идут ролики: 2"}
    assert results["flux"]["state"] == "released"
    assert [m for m, _ in studio.gw.releases] == [LUSTIFY, FLUX, "gemma4-abl"]   # видео отдельно не освобождается


def test_description_reports_running_videos(studio):
    studio.gw.release_replies = [(409, {"error": {"message": "идут ролики: 1", "type": "slot_busy"}})]
    login(studio)
    response = studio.c.post("/studio/api/descriptions", files={"image": ("source.png", description_photo(), "image/png")})
    status = wait_state(studio, response.json()["id"], {"done", "error"})
    assert status["state"] == "error" and "идут ролики: 1" in status["detail"]


# ---- видео -----------------------------------------------------------------

VIDEO_ID = "video_" + "0123456789abcdef01234567"
MP4 = bytes(range(256)) * 4
VIDEO_PROMPT = "секретное движение: она оборачивается"
PHOTO_NAME = "my-private-photo-name.png"


def video_object(**extra):
    return {"id": VIDEO_ID, "object": "video", "model": "wan2.2-remix-i2v-v3", "status": "queued",
            "progress": 0, "created_at": int(T0), "completed_at": None, "expires_at": None,
            "seconds": "5", "size": "576x1024", "error": None, "seed": 42, "prompt": None,
            "prompt_source": "auto", "detail": "в очереди: перед ним 0", "queue_position": 0} | extra


@pytest.fixture
def videos(studio, monkeypatch):
    """Шлюз видео поверх шлюза картинок: запросы /slot/videos… копятся в `calls`."""
    from image_upload import VIDEO_UPLOAD, decode_form
    calls, original = [], studio.gw.handler
    state = {"content": (200, MP4), "create": (200, video_object())}

    async def handler(request):
        path, method = request.url.path, request.method
        if not path.startswith("/slot/videos"):
            return await original(request)
        call = {"method": method, "path": path, "auth": request.headers.get("authorization"),
                "timeout": request.extensions["timeout"]["read"]}
        calls.append(call)
        if path == "/slot/videos" and method == "POST":
            call["raw"] = request.content
            call["fields"], call["image"] = decode_form(request.content, request.headers["content-type"],
                                                        file_fields=("input_reference",), max_upload=VIDEO_UPLOAD)
            code, body = state["create"]
            return httpx.Response(code, json=body)
        if path == "/slot/videos" and method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [video_object()], "has_more": False})
        if path == f"/slot/videos/{VIDEO_ID}/content":
            code, body = state["content"]
            return httpx.Response(code, content=body) if code == 200 else httpx.Response(code, json=body)
        if path == f"/slot/videos/{VIDEO_ID}":
            if method == "DELETE":
                return httpx.Response(200, json={"id": VIDEO_ID, "object": "video.deleted", "deleted": True})
            return httpx.Response(200, json=video_object(status="completed", progress=100))
        return httpx.Response(404, json={"error": {"message": "нет такого ролика", "type": "not_found"}})

    monkeypatch.setattr(studio.srv.HTTP, "_transport", httpx.MockTransport(handler))
    login(studio)
    return types.SimpleNamespace(calls=calls, state=state)


def photo(name=PHOTO_NAME, size=(40, 20)):
    import io
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, "PNG")
    return {"image": (name, output.getvalue(), "image/png")}


def test_video_is_created_as_gateway_multipart_without_private_names(studio, videos, caplog):
    caplog.set_level(logging.DEBUG)
    r = studio.c.post("/studio/api/videos", files=photo(),
                      data={"prompt": VIDEO_PROMPT, "seconds": "7", "quality": "high", "smooth": "false",
                            "seed": ""})
    assert r.status_code == 200 and r.json() == video_object()
    (call,) = videos.calls
    assert call["auth"] == f"Bearer {TOKEN}" and call["timeout"] == 120
    assert call["fields"] == {"model": "wan2.2-remix-i2v-v3", "prompt": VIDEO_PROMPT, "seconds": "7",
                              "quality": "high", "smooth": "false"}       # пустой seed не уходит
    assert call["image"].startswith(b"\x89PNG")
    assert b'name="input_reference"; filename="input.png"' in call["raw"]
    assert PHOTO_NAME.encode() not in call["raw"]
    for secret in (VIDEO_PROMPT, PHOTO_NAME, TOKEN):
        assert secret not in caplog.text, secret
    assert caplog.records, "служба ничего не пишет в журнал"


def test_video_defaults_and_seed_reach_the_gateway(studio, videos):
    r = studio.c.post("/studio/api/videos", files=photo(), data={"prompt": "  ", "seed": "4294967295"})
    assert r.status_code == 200
    assert videos.calls[0]["fields"] == {"model": "wan2.2-remix-i2v-v3", "seconds": "5", "quality": "standard",
                                         "smooth": "true", "seed": "4294967295"}   # пустой промпт — описание по фото


@pytest.mark.parametrize("fields", [
    {"seconds": "0"}, {"seconds": "31"}, {"seconds": "2.5"}, {"seconds": "abc"},
    {"quality": "ultra"}, {"smooth": "maybe"}, {"seed": "-1"}, {"seed": str(2**32)},
    {"prompt": "x" * 4001},
])
def test_video_fields_are_validated_before_the_gateway(studio, videos, fields):
    r = studio.c.post("/studio/api/videos", files=photo(), data=fields)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "input"
    assert videos.calls == []


def test_video_needs_a_readable_image(studio, videos):
    no_image = studio.c.post("/studio/api/videos", data={"prompt": "x"}, files={"note": ("a.txt", b"x", "text/plain")})
    assert no_image.status_code == 400
    assert studio.c.post("/studio/api/videos", files={"image": ("bad.png", b"bad", "image/png")}).status_code == 400
    assert videos.calls == []


def test_video_upload_limit_is_twenty_megabytes_and_json_jobs_keep_theirs(studio, videos):
    from image_upload import MAX_BODY, MAX_UPLOAD, VIDEO_UPLOAD
    padded = photo()["image"][1]
    padded += b"\0" * (VIDEO_UPLOAD - 1024 - len(padded))     # PNG с хвостом: ~20 МБ, декодируется быстро
    assert studio.c.post("/studio/api/videos", files={"image": ("p.png", padded, "image/png")}).status_code == 200
    huge = b"\0" * (VIDEO_UPLOAD + (MAX_BODY - MAX_UPLOAD) + 1)
    r = studio.c.post("/studio/api/videos", files={"image": ("p.png", huge, "image/png")})
    assert r.status_code == 413 and len(videos.calls) == 1
    json_job = json.dumps({"prompt": "x" * (600 * 1024), "size": "square"})
    assert studio.c.post("/studio/api/jobs", content=json_job, headers={"Content-Type": "application/json"}).status_code == 413


@pytest.mark.parametrize("code, words", [(429, "очередь роликов полна"), (400, "шлюз отклонил запрос: bad model"),
                                         (503, "карта занята или недоступна")])
def test_video_gateway_refusals_keep_their_code(studio, videos, code, words):
    videos.state["create"] = (code, {"error": {"message": "bad model" if code == 400 else "full", "type": "x"}})
    r = studio.c.post("/studio/api/videos", files=photo())
    assert r.status_code == code and words in r.json()["detail"]["message"]


def test_revoked_key_on_videos_ends_the_session(studio, videos):
    videos.state["create"] = (401, {"error": {"message": "Authentication Error"}})
    r = studio.c.post("/studio/api/videos", files=photo())
    assert r.status_code == 401 and r.json()["detail"]["code"] == "login"
    assert "studio_session=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()


def test_video_list_status_and_delete_are_proxied(studio, videos):
    listing = studio.c.get("/studio/api/videos")
    assert listing.status_code == 200 and listing.json()["data"][0]["id"] == VIDEO_ID
    assert listing.headers["cache-control"] == "no-store"
    assert studio.c.get(f"/studio/api/videos/{VIDEO_ID}").json()["status"] == "completed"
    assert studio.c.delete(f"/studio/api/videos/{VIDEO_ID}").json()["deleted"] is True
    assert [(c["method"], c["path"]) for c in videos.calls] == [
        ("GET", "/slot/videos"), ("GET", f"/slot/videos/{VIDEO_ID}"), ("DELETE", f"/slot/videos/{VIDEO_ID}")]
    assert all(c["auth"] == f"Bearer {TOKEN}" for c in videos.calls)


@pytest.mark.parametrize("bad", ["abc", "video_123", "video_" + "g" * 24, "video_" + "A" * 24,
                                 "video_" + "0" * 25, "..%2Fmodels"])
def test_bad_video_id_never_reaches_the_gateway(studio, videos, bad):
    for method, suffix in (("get", ""), ("delete", ""), ("get", "/content")):
        assert getattr(studio.c, method)(f"/studio/api/videos/{bad}{suffix}").status_code == 404
    assert videos.calls == []


def test_videos_need_a_session(studio):
    for method, url in (("post", "/studio/api/videos"), ("get", "/studio/api/videos"),
                        ("get", f"/studio/api/videos/{VIDEO_ID}"), ("delete", f"/studio/api/videos/{VIDEO_ID}"),
                        ("get", f"/studio/api/videos/{VIDEO_ID}/content")):
        r = getattr(studio.c, method)(url)
        assert r.status_code == 401 and r.json()["detail"]["code"] == "login", url


def content_calls(videos):
    return sum(c["path"].endswith("/content") for c in videos.calls)


def test_video_content_answers_ranges_from_one_download(studio, videos):
    url = f"/studio/api/videos/{VIDEO_ID}/content"
    full = studio.c.get(url)
    assert full.status_code == 200 and full.content == MP4
    assert full.headers["content-type"] == "video/mp4" and full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "no-store" and "content-disposition" not in full.headers
    for spec, start, end in (("bytes=0-99", 0, 99), ("bytes=100-", 100, 1023), ("bytes=-50", 974, 1023),
                             ("bytes=1000-5000", 1000, 1023)):
        part = studio.c.get(url, headers={"Range": spec})
        assert part.status_code == 206, spec
        assert part.content == MP4[start:end + 1] and part.headers["content-range"] == f"bytes {start}-{end}/1024"
        assert part.headers["content-length"] == str(end - start + 1)
    for spec in ("bytes=1024-", "bytes=5-2", "bytes=-0", "bytes=-", "items=0-1", "bytes=0-1,5-6"):
        bad = studio.c.get(url, headers={"Range": spec})
        assert bad.status_code == 416 and bad.headers["content-range"] == "bytes */1024", spec
    assert content_calls(videos) == 1                                  # всё — из памяти студии
    assert studio.c.delete(f"/studio/api/videos/{VIDEO_ID}").status_code == 200
    assert VIDEO_ID not in studio.srv.VIDEO_CACHE
    assert studio.c.get(url).content == MP4 and content_calls(videos) == 2


def test_video_download_is_named_by_seed(studio, videos):
    r = studio.c.get(f"/studio/api/videos/{VIDEO_ID}/content?download=1")
    assert r.status_code == 200 and r.headers["content-disposition"] == 'attachment; filename="studio-video-42.mp4"'


def test_video_cache_holds_three_for_ten_minutes(studio, videos):
    url = f"/studio/api/videos/{VIDEO_ID}/content"
    studio.c.get(url)
    studio.clock[0] = T0 + 601
    studio.c.get(url)
    assert content_calls(videos) == 2                                  # старше 10 минут — заново у шлюза
    cache = studio.srv.VIDEO_CACHE
    cache.clear()
    for n in range(3):
        cache[f"video_{n:024x}"] = (studio.clock[0], "owner", b"x")
    studio.c.get(url)
    assert list(cache) == [f"video_{1:024x}", f"video_{2:024x}", VIDEO_ID]   # давний вытеснен


def test_video_content_of_another_session_is_not_served_from_cache(studio, videos):
    url = f"/studio/api/videos/{VIDEO_ID}/content"
    studio.c.get(url)
    owner, data = studio.srv.VIDEO_CACHE[VIDEO_ID][1:]
    studio.srv.VIDEO_CACHE[VIDEO_ID] = (studio.clock[0], "0" * 64, data)
    studio.c.get(url)
    assert content_calls(videos) == 2 and studio.srv.VIDEO_CACHE[VIDEO_ID][1] == owner


def test_video_content_not_ready_or_too_big(studio, videos, monkeypatch):
    url = f"/studio/api/videos/{VIDEO_ID}/content"
    videos.state["content"] = (409, {"error": {"message": "ролик ещё не готов", "type": "not_ready"}})
    r = studio.c.get(url)
    assert r.status_code == 409 and r.json()["detail"]["message"] == "ролик ещё не готов"
    assert VIDEO_ID not in studio.srv.VIDEO_CACHE
    videos.state["content"] = (200, MP4)
    monkeypatch.setattr(studio.srv, "VIDEO_CONTENT_MAX", 1000)
    assert studio.c.get(url).status_code == 502 and VIDEO_ID not in studio.srv.VIDEO_CACHE


def test_apache_lets_through_the_largest_body_the_studio_accepts():
    """Предел тела `/studio/` у Apache debian — не меньше предела службы студии: иначе картинка
    для видео (до 20 МБ) или фото с маской отбиваются 413 ещё на debian (до 2026-10-19 там было
    65536 — настоящая фотография в правку не проходила)."""
    import re
    import image_upload
    conf = (DEPLOY / "apache-location.conf").read_text(encoding="utf-8")
    block = re.search(r"<Location /studio/>(.*?)</Location>", conf, re.S).group(1)
    limit = int(re.search(r"^\s*LimitRequestBody (\d+)", block, re.M).group(1))
    extra = image_upload.MAX_BODY - image_upload.MAX_UPLOAD
    assert limit >= image_upload.VIDEO_UPLOAD + extra            # видео: одна картинка
    assert limit >= image_upload.MAX_BODY + image_upload.MAX_UPLOAD   # правка: фото и маска
