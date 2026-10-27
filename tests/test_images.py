"""Картинки на учётках (app/images.py): задания через фото-подключение srvllm, лента истории,
отмена с release модели, описание gemma-abl. Сценарии перенесены из tests/test_studio.py (прежний
server.py); шлюз — FakeGateway, часы — clock.NOW, паузы — clock.SLEEP."""
import asyncio
import base64
import io
import json
import logging
import struct
import threading
import time
import zlib

import httpx
import pytest
from PIL import Image

import forms
import images
import media
import state
import upstream
from conftest import T0, add_connection, add_gateway, login, make_user, signin
from fakes import PNG_REAL, TOKEN
from image_upload import decode_form, normalize_image

PROMPT = "секретный промпт: женщина у окна"
LUSTIFY, FLUX = "lustify-v10-krea2-turbo", "flux-3-image-klein"
GW = "http://10.77.1.15:4000"
SLOT_BUSY = (409, {"error": {"message": "busy", "type": "slot_busy"}})


def start(s, prompt=PROMPT, size="square", seed=None, model=None, **extra):
    body = {"prompt": prompt, "size": size, "seed": seed} | ({"model": model} if model else {}) | extra
    return s.c.post("/api/images", json=body)


def wait_state(s, job_id, states, limit=3000):
    body = None
    for _ in range(limit):
        body = s.c.get(f"/api/images/{job_id}").json()
        if body["state"] in states:
            return body
    raise AssertionError(f"задание не дошло до {states}: {body}")


def wait_description(s, job_id, limit=3000):
    """Первый ответ опроса описания с концом задания (готовое описание — им же)."""
    body = None
    for _ in range(limit):
        body = s.c.get(f"/api/images/descriptions/{job_id}").json()
        if body["state"] in images.TERMINAL:
            return body
    raise AssertionError(f"описание не дошло до конца: {body}")


def wait_for(cond, limit=3000):
    for _ in range(limit):
        if cond():
            return
        time.sleep(0.001)
    raise AssertionError("условие не наступило")


def png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def photo() -> bytes:
    return png(Image.new("RGB", (48, 32), "blue"))


def describe(s):
    return s.c.post("/api/images/descriptions", files={"image": ("source.png", photo(), "image/png")})


def media_rows() -> int:
    return state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0]


# ---- задание ---------------------------------------------------------------

def test_job_goes_through_the_gateway_with_the_connection_key(studio):
    add_gateway(studio)
    r = start(studio, size="portrait", seed=7)
    assert r.status_code == 202 and r.json()["seed"] == 7
    job = r.json()["id"]
    body = wait_state(studio, job, {"done", "error"})
    assert body["state"] == "done" and body["model"] == "lustify" and body["kind"] == "generation"
    assert body["prompt"] == PROMPT and body["size"] == "832x1216" and body["seed"] == 7
    image = studio.c.get(f"/api/media/{body['media_id']}")
    assert image.status_code == 200 and image.content == PNG_REAL and image.headers["content-type"] == "image/png"
    payload, auth, read_timeout = studio.gw.generations[0]
    assert payload == {"model": LUSTIFY, "prompt": PROMPT, "n": 1, "size": "832x1216", "seed": 7,
                       "response_format": "b64_json"}
    assert auth == f"Bearer {TOKEN}" and read_timeout == 1300
    assert [str(r.url) for r in studio.gw.requests] == [GW + "/slot/images/release", GW + "/slot/images/generations"]
    assert studio.gw.releases == [(FLUX, f"Bearer {TOKEN}")]      # другая модель — до запроса


@pytest.mark.parametrize("reply", [SLOT_BUSY, (500, {"error": {"message": "boom"}})], ids=["busy", "failed"])
def test_switching_models_releases_the_other_one_first(studio, reply):
    """Аренда Lustify (слот comfy) держит карту 600 с простоя, и FLUX её не вытесняет: без release
    шлюз ждал карту 300 с и отвечал 503 (приёмка 2026-10-20). Release занятой модели (409) или
    сбой release задание не останавливает — карту дождётся сам запрос."""
    add_gateway(studio)
    wait_state(studio, start(studio, model="lustify").json()["id"], {"done"})
    assert [m for m, _ in studio.gw.releases] == [FLUX]
    studio.gw.release_replies = [reply]
    body = wait_state(studio, start(studio, model="flux").json()["id"], {"done", "error"})
    assert body["state"] == "done"
    assert [m for m, _ in studio.gw.releases] == [FLUX, LUSTIFY]
    paths = [r.url.path for r in studio.gw.requests]
    assert paths[-2:] == ["/slot/images/release", "/slot/images/generations"]


def test_unreachable_gateway_is_named_by_the_request_not_the_release(studio):
    studio.gw.down = True
    add_gateway(studio)
    body = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert body["state"] == "error" and body["detail"] == "шлюз недоступен: ConnectError"


@pytest.mark.parametrize("model, gateway_id", [("flux", FLUX), ("lustify", LUSTIFY)])
def test_model_is_chosen_on_the_page(studio, model, gateway_id):
    add_gateway(studio)
    wait_state(studio, start(studio, model=model).json()["id"], {"done"})
    assert studio.gw.generations[0][0]["model"] == gateway_id


def test_seed_from_the_gateway_answer_is_shown(studio):
    studio.gw.image_reply = (200, {"data": [{"b64_json": base64.b64encode(PNG_REAL).decode(), "seed": 42}]})
    add_gateway(studio)
    body = wait_state(studio, start(studio, seed=7).json()["id"], {"done"})
    assert body["seed"] == 42
    download = studio.c.get(f"/api/media/{body['media_id']}?download=1")
    assert download.headers["content-disposition"] == 'attachment; filename="studio-lustify-42.png"'


@pytest.mark.parametrize("code, body, words", [
    (401, {"error": {"message": "Authentication Error"}}, "проверьте токен в профиле"),
    (403, {"error": {"message": "forbidden"}}, "закрыт"),
    (400, {"error": {"message": "unknown model", "type": "invalid_request_error"}}, "шлюз отклонил запрос: unknown model"),
    (503, {"error": "no_capacity", "detail": "x"}, "карта занята или недоступна: no_capacity"),
    (504, {"error": {"message": "картинка не готова за 300 с"}}, "не дождался службы"),
    (200, {"data": []}, "не вернул картинку"),
])
def test_gateway_failures_read_as_plain_words(studio, code, body, words):
    studio.gw.image_reply = (code, body)
    add_gateway(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and words in result["detail"] and result["media_id"] is None


def test_gateway_read_timeout_is_an_error_not_a_hang(studio):
    studio.gw.raise_on_generate = httpx.ReadTimeout
    add_gateway(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and "1300" in result["detail"]


def picture(fmt) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 90)).save(out, fmt)
    return out.getvalue()


def reply_with(raw: bytes, seed=5) -> tuple:
    return 200, {"data": [{"b64_json": base64.b64encode(raw).decode(), "seed": seed}]}


def png_header(width: int, height: int) -> bytes:
    """Заголовок PNG без пикселей: Image.open читает чанки до первого IDAT, размер — из IHDR."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", b""))


@pytest.mark.parametrize("raw, limit, words", [
    (b"not an image", None, "сервис вернул не картинку"),
    (PNG_REAL[:len(PNG_REAL) // 2], None, "сервис вернул не картинку"),
    (picture("GIF"), None, "сервис вернул не картинку"),
    (PNG_REAL, ("RESULT_PIXELS", 64 * 48 - 1), "больше предела"),
    (PNG_REAL, ("RESULT_MAX", len(PNG_REAL) - 1), "больше предела"),
    # больше двух MAX_IMAGE_PIXELS Pillow отказывает (DecompressionBombError) ещё в Image.open
    (png_header(14_000, 14_000), None, "больше предела"),
], ids=["text", "truncated", "gif", "pixels", "bytes", "bomb"])
def test_a_reply_that_is_not_a_proper_picture_is_an_error(studio, env, monkeypatch, raw, limit, words):
    """Корректный base64 ещё не картинка: текст, обрезанный файл, чужой формат и картинка больше
    пределов — ошибка словами, а не «готово» с текстом под видом PNG (находка аудита P2)."""
    if limit is not None:
        monkeypatch.setattr(images, *limit)
    studio.gw.image_reply = reply_with(raw)
    add_gateway(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and words in result["detail"] and result["media_id"] is None
    assert media_rows() == 0 and share_files(env) == []


def test_a_brush_edit_answered_with_garbage_is_an_error_in_words(studio):
    """Битый ответ правки кистью не доходит до слияния с исходником (там он был бы «сбоем службы»)."""
    studio.gw.image_reply = reply_with(b"not an image")
    add_gateway(studio)
    mask = Image.new("L", (48, 32), 0)
    mask.paste(255, (0, 0, 8, 8))
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "original"},
                      files={"image": ("p.png", photo(), "image/png"), "mask": ("m.png", png(mask), "image/png")})
    result = wait_state(studio, r.json()["id"], {"done", "error"})
    assert result["state"] == "error" and "сервис вернул не картинку" in result["detail"]


@pytest.mark.parametrize("fmt, mime, ext", [("JPEG", "image/jpeg", ".jpg"), ("WEBP", "image/webp", ".webp")])
def test_jpeg_and_webp_replies_keep_their_own_type(studio, fmt, mime, ext):
    """Ответ JPEG или WebP (совместимый сервис) ложится со своим типом и расширением, а не как PNG."""
    raw = picture(fmt)
    studio.gw.image_reply = reply_with(raw)
    add_gateway(studio)
    body = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert body["state"] == "done", body
    r = studio.c.get(f"/api/media/{body['media_id']}?download=1")
    assert r.content == raw and r.headers["content-type"] == mime
    assert r.headers["content-disposition"] == f'attachment; filename="studio-lustify-5{ext}"'
    assert studio.c.get(f"/api/media/{body['media_id']}?thumb=1").headers["content-type"] == "image/webp"


def test_working_status_shows_the_elapsed_time(studio):
    studio.gw.hold = True
    add_gateway(studio)
    job = start(studio).json()["id"]
    wait_for(lambda: studio.gw.generations)
    studio.clock[0] = T0 + 83
    body = studio.c.get(f"/api/images/{job}").json()
    assert body["state"] == "working" and body["elapsed"] == 83
    studio.gw.hold = False
    wait_state(studio, job, {"done"})


def _cancel_while_generating(s, model="flux"):
    """Задание висит на запросе к шлюзу, его отменяют: id задания."""
    s.gw.hold = True
    add_gateway(s)
    job = start(s, model=model).json()["id"]
    wait_for(lambda: s.gw.generations)
    s.gw.releases.clear()          # release другой модели перед запросом — свой тест; ответы — с начала
    assert s.c.post(f"/api/images/{job}/cancel").json() == {"state": "cancelling"}
    return job


def test_cancel_waits_for_the_gateway_request_then_releases_the_models_slot(studio):
    """Обрыв запроса до slot-gate не доходит (LiteLLM его не передаёт): отмена запрос не рвёт,
    ждёт его конца, картинку выбрасывает и сразу освобождает карту модели задания (ревью
    2026-10-19). Пока задание отменяется, новое получает busy."""
    job = _cancel_while_generating(studio)
    body = studio.c.get(f"/api/images/{job}").json()
    assert body["state"] == "cancelling" and "после текущей картинки" in body["detail"]
    assert "ждёт карту" in body["detail"]                     # честно: и ожидание карты дождёмся
    assert studio.c.post(f"/api/images/{job}/cancel").json() == {"state": "cancelling"}
    busy = start(studio)
    assert busy.status_code == 409 and busy.json()["detail"]["id"] == job
    assert busy.json()["detail"]["code"] == "busy" and "отменяется" in busy.json()["detail"]["message"]
    assert studio.gw.releases == []                           # пока запрос идёт, release — 409
    studio.gw.hold = False
    result = wait_state(studio, job, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and "карта освобождена" in result["detail"]
    assert studio.gw.aborted == 0 and studio.gw.completed == 1
    assert studio.gw.releases == [(FLUX, f"Bearer {TOKEN}")]
    assert result["media_id"] is None and media_rows() == 0   # картинка выброшена
    assert images.JOBS[job].prompt == ""
    nxt = start(studio)
    assert nxt.status_code == 202                             # отменённое не держит очередь
    wait_state(studio, nxt.json()["id"], {"done"})


def test_cancel_retries_release_while_the_slot_is_busy(studio):
    studio.gw.release_replies = [SLOT_BUSY, SLOT_BUSY, (200, {"ok": True, "slot": "comfy"})]
    job = _cancel_while_generating(studio, model="lustify")
    studio.gw.hold = False
    result = wait_state(studio, job, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and "карта освобождена" in result["detail"]
    assert [m for m, _ in studio.gw.releases] == [LUSTIFY] * 3
    assert studio.sleeps == [images.RELEASE_PAUSE_SEC] * 2


def test_cancel_gives_up_release_after_a_minute_of_409(studio):
    """Карту держит чужая генерация: дольше CANCEL_RELEASE_SEC не ждём — аренду снимет её срок."""
    studio.gw.release_replies = [SLOT_BUSY]
    job = _cancel_while_generating(studio)
    studio.gw.image_reply = (503, {"error": "no_capacity"})    # и запрос кончился ошибкой
    studio.gw.hold = False
    result = wait_state(studio, job, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and "освободится сама" in result["detail"]
    assert sum(studio.sleeps) >= images.CANCEL_RELEASE_SEC
    assert len(studio.gw.releases) == images.CANCEL_RELEASE_SEC // images.RELEASE_PAUSE_SEC + 1


def test_finished_job_is_not_cancelled(studio):
    add_gateway(studio)
    job = start(studio).json()["id"]
    wait_state(studio, job, {"done"})
    before = list(studio.gw.releases)
    assert studio.c.post(f"/api/images/{job}/cancel").json() == {"state": "done"}
    assert studio.gw.releases == before


# ---- освобождение карты ------------------------------------------------------

def test_release_reports_each_model_in_plain_words(studio):
    """409 без сообщения — прежние слова; с сообщением — см. test_release_shows_the_busy_reason."""
    studio.gw.release_replies = [(409, {"error": {"type": "slot_busy"}}), (500, {"error": {"message": "boom"}})]
    add_gateway(studio)
    r = studio.c.post("/api/images/release")
    assert r.status_code == 200
    results = r.json()["results"]
    assert results["lustify"] == {"state": "busy", "message": "занята генерацией"}
    assert results["flux"]["state"] == "error" and "boom" in results["flux"]["message"]
    assert results["gemma-abl"]["state"] == "error"


def test_release_shows_the_busy_reason(studio):
    """Lustify и видео — слот comfy: переводчик при идущих роликах отвечает 409 «идут ролики: N»,
    кнопка показывает его слова (спека 2026-10-19-srvllm-studio-video, раздел 6)."""
    studio.gw.release_replies = [(409, {"error": {"message": "идут ролики: 2", "type": "slot_busy"}}),
                                 (200, {"ok": True})]
    add_gateway(studio)
    results = studio.c.post("/api/images/release").json()["results"]
    assert results["lustify"] == {"state": "busy", "message": "идут ролики: 2"}
    assert results["flux"]["state"] == "released"
    assert [m for m, _ in studio.gw.releases] == [LUSTIFY, FLUX, "gemma4-abl"]   # видео отдельно не освобождается


def test_release_is_for_admins_only(studio):
    make_user(studio, "friend")
    signin(studio, "friend")
    add_gateway(studio)
    r = studio.c.post("/api/images/release")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "admin"
    assert studio.gw.releases == []


# ---- проверка ввода ----------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"prompt": "", "size": "square"},
    {"prompt": "x" * 24001, "size": "square"},
    {"prompt": "a cat", "size": "huge"},
    {"prompt": "a cat", "size": "square", "seed": -1},
    {"prompt": "a cat", "size": "square", "model": "dall-e-3"},
])
def test_prompt_size_seed_and_model_are_validated(studio, payload):
    add_gateway(studio)
    r = studio.c.post("/api/images", json=payload)
    assert r.status_code == 422 and r.json()["detail"]["code"] == ("model" if "model" in payload else "input")
    assert studio.gw.generations == [] and not images.JOBS


def test_photo_without_gateway_connection_is_explained(studio):
    add_connection(studio)                                    # только чат: фото-подключения нет
    r = start(studio)
    assert r.status_code == 409 and r.json()["detail"] == {
        "code": "no_model", "message": "выберите модель в настройках раздела «Фото»"}
    assert describe(studio).json()["detail"] == {
        "code": "no_gateway", "message": "добавьте шлюз srvllm в настройках раздела «Фото»"}
    assert studio.gw.requests == [] and not images.JOBS


def test_original_size_requires_a_photo(studio):
    add_gateway(studio)
    r = studio.c.post("/api/images", json={"prompt": "test", "size": "original"})
    assert r.status_code == 422 and not images.JOBS


def test_maximum_prompt_in_ascii_escaped_json_reaches_gateway(studio):
    add_gateway(studio)
    prompt = "я" * 24000
    response = studio.c.post("/api/images", content=json.dumps({"prompt": prompt, "size": "square"}),
                             headers={"Content-Type": "application/json"})
    assert response.status_code == 202
    assert wait_state(studio, response.json()["id"], {"done", "error"})["state"] == "done"
    assert studio.gw.generations[0][0]["prompt"] == prompt


# ---- правка фото -------------------------------------------------------------

@pytest.mark.parametrize("model", ["lustify", "flux"])
def test_uploaded_photo_is_forwarded_to_edits_and_discarded(studio, model):
    add_gateway(studio)
    source = png(Image.new("RGB", (32, 24), "red"))
    r = studio.c.post("/api/images", data={"prompt": PROMPT, "size": "square", "model": model, "strength": "0.4"},
                      files={"image": ("photo.png", source, "image/png")})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    body = wait_state(studio, job_id, {"done", "error"})
    # сила — только у Lustify: FLUX её не получает, и в строке и цепочке её нет (раздел 9 спеки фото)
    assert body["state"] == "done" and body["kind"] == "edit"
    assert body["strength"] == (0.4 if model == "lustify" else None) == body["chain"][-1]["strength"]
    (fields, sent), = studio.gw.edits
    assert fields["model"] == images.MODELS[model] and sent.startswith(b"\x89PNG")
    assert (fields.get("strength") == "0.4") if model == "lustify" else "strength" not in fields
    assert str(studio.gw.requests[-1].url) == GW + "/slot/images/edits"
    assert images.JOBS[job_id].image is None
    # исходник — в истории, как его получил шлюз
    assert studio.c.get(f"/api/media/{body['source_media_id']}").content == sent == normalize_image(source)


def test_invalid_photo_creates_no_studio_job(studio):
    add_gateway(studio)
    r = studio.c.post("/api/images", data={"prompt": PROMPT, "size": "square"},
                      files={"image": ("bad.png", b"bad", "image/png")})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "image" and not images.JOBS
    assert state.DB.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0 and media_rows() == 0


def test_masked_edit_preserves_source_size_and_pixels_without_forwarding_mask(studio):
    source = photo()
    mask = Image.new("L", (48, 32), 0)
    mask.paste(255, (10, 8, 30, 24))
    observed = []

    async def edits(request):
        fields, sent = decode_form(request.content, request.headers["content-type"])
        observed.append(fields)
        assert sent == source and fields["size"] == "48x32" and fields["model"] == FLUX
        assert "mask" not in fields and "strength" not in fields
        edited = png(Image.new("RGB", (96, 64), "red"))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(edited).decode()}]})

    studio.gw.on["/slot/images/edits"] = edits
    add_gateway(studio)
    r = studio.c.post("/api/images", data={"model": "flux", "size": "original", "prompt": "make this red"},
                      files={"image": ("source.png", source, "image/png"),
                             "mask": ("mask.png", png(mask), "image/png")})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    body = wait_state(studio, job_id, {"done", "error"})
    assert body["state"] == "done"
    image = studio.c.get(f"/api/media/{body['media_id']}")
    with Image.open(io.BytesIO(image.content)) as result:
        assert result.size == (48, 32)
        for y in range(32):
            for x in range(48):
                assert result.getpixel((x, y)) == ((255, 0, 0) if mask.getpixel((x, y)) else (0, 0, 255))
    assert len(observed) == 1
    job = images.JOBS[job_id]
    assert job.mask is None and job.image is None


def test_edit_from_history_uses_the_stored_source(studio):
    add_gateway(studio)
    first = wait_state(studio, start(studio).json()["id"], {"done"})
    stored = normalize_image(PNG_REAL)
    job = start(studio, size="original", model="flux", source_media_id=first["media_id"]).json()["id"]
    body = wait_state(studio, job, {"done", "error"})
    assert body["state"] == "done" and body["kind"] == "edit" and body["source_media_id"] == first["media_id"]
    fields, sent = studio.gw.edits[-1]
    assert sent == stored and fields["size"] == "64x48"
    # кисть по фото истории: multipart без файла image — поле source_media_id и маска
    mask = Image.new("L", (64, 48), 0)
    mask.paste(255, (0, 0, 16, 16))
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "original", "model": "flux",
                                           "source_media_id": first["media_id"]},
                      files={"mask": ("mask.png", png(mask), "image/png")})
    assert r.status_code == 202, r.text
    masked = wait_state(studio, r.json()["id"], {"done", "error"})
    assert masked["state"] == "done" and masked["mask_media_id"]
    # исходник истории не копируется; маска — в историю (повтор правки идёт с ней)
    assert studio.gw.edits[-1][1] == stored and media_rows() == 4
    # и файл, и фото истории — отказ; чужое или неизвестное фото — 404
    both = studio.c.post("/api/images", data={"prompt": "x", "size": "square", "source_media_id": first["media_id"]},
                         files={"image": ("p.png", photo(), "image/png")})
    assert both.status_code == 400
    assert start(studio, source_media_id="0" * 32).status_code == 404


def test_repeating_a_brush_edit_keeps_its_mask(studio, env):
    """«Повторить» правки кистью шлёт mask_media_id прежней правки: вне выделения — снова пиксели
    исходника, а не вся картинка заново (ревью B1-M1)."""
    async def edits(request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(
            png(Image.new("RGB", (64, 48), (0, 255, 0)))).decode()}]})

    add_gateway(studio)
    first = wait_state(studio, start(studio).json()["id"], {"done"})
    studio.gw.on["/slot/images/edits"] = edits
    mask = Image.new("L", (64, 48), 0)
    mask.paste(255, (0, 0, 16, 16))
    form = {"prompt": "x", "size": "original", "model": "flux", "source_media_id": first["media_id"]}
    edit = wait_state(studio, studio.c.post("/api/images", data=form, files={"mask": ("m.png", png(mask), "image/png")})
                      .json()["id"], {"done"})
    repeat = start(studio, prompt="x", size="original", model="flux", seed=edit["seed"],
                   source_media_id=first["media_id"], mask_media_id=edit["mask_media_id"])
    assert repeat.status_code == 202, repeat.text
    again = wait_state(studio, repeat.json()["id"], {"done"})
    assert again["mask_media_id"] == edit["mask_media_id"]
    source = Image.open(io.BytesIO(normalize_image(PNG_REAL))).convert("RGB")
    with Image.open(io.BytesIO(studio.c.get(f"/api/media/{again['media_id']}").content)) as result:
        assert result.getpixel((4, 4)) == (0, 255, 0)                          # внутри выделения — правка
        assert result.getpixel((40, 30)) == source.getpixel((40, 30))          # снаружи — исходник
    # маска общая: удаление первой правки её не трогает, удаление последней ссылки — удаляет
    assert studio.c.delete(f"/api/images/{edit['id']}").status_code == 204
    assert studio.c.get(f"/api/media/{edit['mask_media_id']}").status_code == 200
    assert studio.c.delete(f"/api/images/{again['id']}").status_code == 204
    assert studio.c.get(f"/api/media/{edit['mask_media_id']}").status_code == 404
    # маска — одна: файлом или из истории; чужая или неизвестная — 404
    both = studio.c.post("/api/images", data=form | {"mask_media_id": first["media_id"]},
                         files={"mask": ("m.png", png(mask), "image/png")})
    assert both.status_code == 400 and both.json()["detail"]["code"] == "image"
    unknown = start(studio, size="original", source_media_id=first["media_id"], mask_media_id="0" * 32)
    assert unknown.status_code == 404


def test_a_refused_masked_upload_leaves_no_files(studio, env, monkeypatch):
    """Исходник и маска записаны, а задание отказано (другая вкладка начала своё): оба файла удаляются."""
    add_gateway(studio)
    studio.gw.hold = True
    real, other = media.write, []

    def racing(*args, **kwargs):                  # поток записи: цикл свободен — другая вкладка успела
        if not other:
            other.append(start(studio).json()["id"])
        return real(*args, **kwargs)

    monkeypatch.setattr(media, "write", racing)
    mask = Image.new("L", (48, 32), 0)
    mask.paste(255, (0, 0, 8, 8))
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "original", "model": "flux"},
                      files={"image": ("p.png", photo(), "image/png"), "mask": ("m.png", png(mask), "image/png")})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert media_rows() == 0 and share_files(env) == []
    studio.gw.hold = False
    wait_state(studio, other[0], {"done"})


# ---- описание ----------------------------------------------------------------

@pytest.mark.parametrize("finish", ["stop", "length"])
def test_description_uses_gemma_abl_and_returns_editable_text_until_received(studio, caplog, finish):
    """Ответ с текстом мог потеряться в пути: повтор GET отдаёт тот же текст, пока страница не
    подтвердит получение (DELETE); после — 404, повтор подтверждения безопасен."""
    text = "A private detailed description. " * 300
    studio.gw.description_reply = {"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    add_gateway(studio)
    response = describe(studio)
    assert response.status_code == 202
    job_id = response.json()["id"]
    result = wait_description(studio, job_id)
    assert result == {"state": "done", "text": text.strip(), "truncated": finish == "length"}
    assert studio.c.get(f"/api/images/descriptions/{job_id}").json() == result
    for _ in range(2):
        assert studio.c.delete(f"/api/images/descriptions/{job_id}").status_code == 204
    r = studio.c.get(f"/api/images/descriptions/{job_id}")
    assert r.status_code == 404 and r.json()["detail"]["message"] == "описание уже выдано"
    (body,) = studio.gw.descriptions
    assert body["model"] == "gemma4-abl" and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["content"][0]["text"] == images.DESCRIPTION_PROMPT
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert [model for model, _ in studio.gw.releases] == [LUSTIFY, FLUX, "gemma4-abl"]
    job = images.JOBS[job_id]
    assert job.image is None and job.description is None
    assert text.strip() not in caplog.text
    assert state.DB.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0   # описания — не в истории


def test_description_from_history_photo(studio):
    add_gateway(studio)
    first = wait_state(studio, start(studio).json()["id"], {"done"})
    r = studio.c.post("/api/images/descriptions", files={"source_media_id": (None, first["media_id"])})
    assert r.status_code == 202, r.text
    assert wait_description(studio, r.json()["id"])["state"] == "done"
    url = studio.gw.descriptions[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == normalize_image(PNG_REAL)


def test_description_cancellation_waits_then_discards_text_and_releases(studio):
    arrived, finish = threading.Event(), threading.Event()

    async def descriptions(request):
        arrived.set()
        while not finish.is_set():
            await asyncio.sleep(0.001)
        return httpx.Response(200, json={"choices": [{"message": {"content": "private description"}}]})

    studio.gw.on["/slot/images/descriptions"] = descriptions
    add_gateway(studio)
    job_id = describe(studio).json()["id"]
    assert arrived.wait(1)
    assert start(studio).status_code == 409
    assert studio.c.post(f"/api/images/{job_id}/cancel").json()["state"] == "cancelling"
    finish.set()
    result = wait_description(studio, job_id)
    assert result["state"] == "cancelled" and "text" not in result
    assert [model for model, _ in studio.gw.releases][-1] == "gemma4-abl"


def test_description_can_be_cancelled_during_model_release(studio):
    releasing, finish = threading.Event(), threading.Event()

    async def release(request):
        releasing.set()
        while not finish.is_set():
            await asyncio.sleep(0.001)
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/descriptions/release"] = release
    add_gateway(studio)
    job_id = describe(studio).json()["id"]
    assert releasing.wait(1)
    studio.c.post(f"/api/images/{job_id}/cancel")
    finish.set()
    result = wait_description(studio, job_id)
    assert result["state"] == "cancelled" and "text" not in result


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": [{"message": {"content": ""}}]}])
def test_empty_description_errors_and_releases_model(studio, body):
    studio.gw.description_reply = body
    add_gateway(studio)
    status = wait_description(studio, describe(studio).json()["id"])
    assert status["state"] == "error" and "не вернула описание" in status["detail"]
    assert [model for model, _ in studio.gw.releases][-1] == "gemma4-abl"


def test_description_reports_running_videos(studio):
    studio.gw.release_replies = [(409, {"error": {"message": "идут ролики: 1", "type": "slot_busy"}})]
    add_gateway(studio)
    status = wait_description(studio, describe(studio).json()["id"])
    assert status["state"] == "error" and "идут ролики: 1" in status["detail"]


@pytest.mark.parametrize("kind", ["descriptions", "translations"])
def test_ready_text_does_not_wait_a_minute_for_a_busy_gemma(studio, kind):
    """409 release после готового ответа — чужой запрос к gemma-abl: текст отдаётся после короткого
    окна повторов DONE_RELEASE_SEC, а не через CANCEL_RELEASE_SEC; слот пользователя свободен."""
    ok = (200, {"ok": True})
    studio.gw.release_replies = [ok, ok, SLOT_BUSY]       # модели картинок — до запроса, gemma-abl — 409 всегда
    add_gateway(studio)
    if kind == "descriptions":
        job_id = describe(studio).json()["id"]
        result = wait_description(studio, job_id)
    else:
        job_id = studio.c.post("/api/images/translations", json={"prompt": PROMPT}).json()["id"]
        result = wait_translation(studio, job_id)
    assert result["state"] == "done" and result["text"]
    assert 0 < sum(studio.sleeps) <= images.DONE_RELEASE_SEC < images.CANCEL_RELEASE_SEC
    assert "освободится сама" in images.JOBS[job_id].detail
    assert start(studio).status_code == 202


# ---- перевод промпта ---------------------------------------------------------

def wait_translation(s, job_id, limit=3000):
    body = None
    for _ in range(limit):
        body = s.c.get(f"/api/images/translations/{job_id}").json()
        if body["state"] in images.TERMINAL:
            return body
    raise AssertionError(f"перевод не дошёл до конца: {body}")


def test_translation_goes_to_gemma_abl_as_text_and_is_given_until_received(studio, caplog):
    caplog.set_level(logging.DEBUG)
    studio.gw.description_reply = {"choices": [{"message": {"content": " a woman by the window \n"}, "finish_reason": "stop"}]}
    add_gateway(studio)
    r = studio.c.post("/api/images/translations", json={"prompt": PROMPT})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    done = {"state": "done", "text": "a woman by the window", "truncated": False}
    assert wait_translation(studio, job_id) == done
    assert studio.c.get(f"/api/images/descriptions/{job_id}").status_code == 404      # чужой вид задания
    assert studio.c.delete(f"/api/images/descriptions/{job_id}").status_code == 204   # и его подтверждение
    assert studio.c.get(f"/api/images/translations/{job_id}").json() == done
    assert studio.c.delete(f"/api/images/translations/{job_id}").status_code == 204
    assert studio.c.get(f"/api/images/translations/{job_id}").status_code == 404
    (body,) = studio.gw.descriptions
    assert body["model"] == "gemma4-abl" and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"] == [{"role": "user", "content": images.TRANSLATION_PROMPT + PROMPT}]
    assert [model for model, _ in studio.gw.releases] == [LUSTIFY, FLUX, "gemma4-abl"]
    assert images.JOBS[job_id].prompt == "" and PROMPT not in caplog.text and "a woman" not in caplog.text
    assert state.DB.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0


def test_cut_translation_is_an_error_not_half_a_prompt(studio):
    studio.gw.description_reply = {"choices": [{"message": {"content": "a woman by"}, "finish_reason": "length"}]}
    add_gateway(studio)
    status = wait_translation(studio, studio.c.post("/api/images/translations", json={"prompt": PROMPT}).json()["id"])
    assert status["state"] == "error" and "не уместился" in status["detail"] and "text" not in status


def test_translation_is_one_job_of_the_user_and_needs_the_gateway(studio):
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    r = studio.c.post("/api/images/translations", json={"prompt": PROMPT})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_gateway"
    add_gateway(studio)
    assert studio.c.post("/api/images/translations", json={"prompt": ""}).status_code == 422
    studio.gw.hold = True
    job = start(studio).json()
    r = studio.c.post("/api/images/translations", json={"prompt": PROMPT})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    studio.gw.hold = False
    wait_state(studio, job["id"], {"done"})


def test_unreceived_text_lives_a_short_while_after_it_was_given(studio):
    """Без подтверждения выданный текст живёт TEXT_KEEP_SEC от первой выдачи; невыданный срока выдачи
    не ждёт — его, как прежде, забывает KEEP_DONE_SEC. Подтверждение чужого задания текст не трогает."""
    add_gateway(studio)                                       # owner
    job_id = describe(studio).json()["id"]
    wait_for(lambda: images.JOBS[job_id].finished_at is not None)
    studio.clock[0] += images.TEXT_KEEP_SEC + 1               # ещё не выдан — срока выдачи нет
    assert wait_description(studio, job_id)["state"] == "done"
    make_user(studio, "friend")
    signin(studio, "friend")
    assert studio.c.delete(f"/api/images/descriptions/{job_id}").status_code == 204
    signin(studio, "owner")
    studio.clock[0] += images.TEXT_KEEP_SEC - 1
    assert studio.c.get(f"/api/images/descriptions/{job_id}").json()["state"] == "done"
    studio.clock[0] += 2
    r = studio.c.get(f"/api/images/descriptions/{job_id}")
    assert r.status_code == 404 and r.json()["detail"]["message"] == "описание уже выдано"
    assert images.JOBS[job_id].description is None
    assert images.TEXT_KEEP_SEC < images.KEEP_DONE_SEC


# ---- учётки и история --------------------------------------------------------

def test_finished_image_lands_in_history_with_a_thumb(studio):
    add_gateway(studio)
    job = start(studio).json()
    item = wait_state(studio, job["id"], {"done"})
    assert item["media_id"] and studio.c.get(f"/api/media/{item['media_id']}").content == PNG_REAL
    assert studio.c.get("/api/images").json()["items"][0]["id"] == job["id"]
    thumb = studio.c.get(f"/api/media/{item['media_id']}?thumb=1")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"


def test_one_job_per_user_not_per_service(studio):
    studio.gw.hold = True
    add_gateway(studio)                                       # owner
    mine = start(studio).json()["id"]
    make_user(studio, "friend")
    signin(studio, "friend")
    add_gateway(studio)
    theirs = start(studio)
    assert theirs.status_code == 202                          # чужое задание не мешает
    assert studio.c.get(f"/api/images/{mine}").status_code == 404   # и чужое не видно
    signin(studio, "owner")
    second = start(studio)
    assert second.status_code == 409 and second.json()["detail"] == {
        "code": "busy", "message": "предыдущее задание ещё выполняется", "id": mine}
    studio.gw.hold = False
    wait_state(studio, mine, {"done"})
    wait_for(lambda: images.JOBS[theirs.json()["id"]].state == "done")


def test_gateway_401_points_to_the_profile_not_the_login(studio):
    add_connection(studio, kind="srvllm", base_url=GW, token="revoked-key", name="srvllm")
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and "«srvllm»" in result["detail"] and "профиле" in result["detail"]
    assert studio.c.get("/api/me").status_code == 200


def test_unsaved_image_becomes_an_error(studio, monkeypatch):
    def broken(*args, **kwargs):
        raise media.StorageError()

    monkeypatch.setattr(media, "write", broken)
    add_gateway(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert result["state"] == "error" and "не удалось сохранить файл" in result["detail"]
    assert result["media_id"] is None


def test_restart_marks_running_images_interrupted(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    for image_id, status in (("a" * 32, "working"), ("b" * 32, "cancelling"), ("c" * 32, "done"), ("d" * 32, "queued")):
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                         " VALUES (?, ?, 'generation', 'flux', 'p', '1024x1024', 1, ?, 'x', ?)",
                         (image_id, uid, status, T0))
    images.recover()
    for image_id, status in (("a" * 32, "error"), ("b" * 32, "error"), ("c" * 32, "done"), ("d" * 32, "error")):
        body = studio.c.get(f"/api/images/{image_id}").json()
        assert body["state"] == status
    assert studio.c.get(f"/api/images/{'a' * 32}").json()["detail"] == "прервано перезапуском студии"


def test_running_image_cannot_be_deleted_and_done_one_can(studio, env, unlinks):
    studio.gw.hold = True
    add_gateway(studio)
    job = start(studio).json()["id"]
    r = studio.c.delete(f"/api/images/{job}")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "running", "message": "сначала отмените"}
    studio.gw.hold = False
    done = wait_state(studio, job, {"done"})
    assert studio.c.delete(f"/api/images/{job}").status_code == 204
    assert studio.c.get(f"/api/images/{job}").status_code == 404
    assert studio.c.get(f"/api/media/{done['media_id']}").status_code == 404
    assert [p for p in env.media_dir.rglob("*") if p.is_file()] == [] and media_rows() == 0
    assert unlinks and not any(unlinks)                  # файлы удалены не с цикла событий


def share_files(env):
    return [p for p in env.media_dir.rglob("*") if p.is_file()]


def wait_job(job_id):
    wait_for(lambda: images.JOBS[job_id].state in images.TERMINAL)
    return images.JOBS[job_id]


def test_history_wipe_during_a_generation_leaves_nothing(studio, env):
    """Стирание истории посреди генерации: картинка, пришедшая после, не ложится на шару (ревью A2-I1)."""
    studio.gw.hold = True
    add_gateway(studio)
    job_id = start(studio).json()["id"]
    wait_for(lambda: studio.gw.generations)
    assert studio.c.delete("/api/history").status_code == 200
    studio.gw.hold = False
    job = wait_job(job_id)
    assert job.state == "cancelled" and "карта освобождена" in job.detail
    assert studio.gw.releases == [(FLUX, f"Bearer {TOKEN}"), (LUSTIFY, f"Bearer {TOKEN}")]   # до запроса и после
    assert media_rows() == 0 and share_files(env) == []


def test_generation_started_during_the_wipe_leaves_nothing(studio, env, monkeypatch):
    """Задание, начатое из другой вкладки, пока стирание удаляет файлы (forget_user уже прошёл):
    его картинка не остаётся на шаре без строки images (ревью N1)."""
    add_gateway(studio)
    studio.gw.hold = True
    real, jobs = media.remove_files, []

    def racing(rows):                             # поток стирания: цикл свободен
        if not jobs:
            jobs.append(start(studio).json()["id"])
            wait_for(lambda: studio.gw.generations)
        return real(rows)

    monkeypatch.setattr(media, "remove_files", racing)
    media.save(state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0],
               photo(), "image/png", "old.png", thumb=True)
    assert studio.c.delete("/api/history").status_code == 200
    studio.gw.hold = False
    job = wait_job(jobs[0])
    assert job.state == "cancelled" and job.media_id is None
    assert media_rows() == 0 and share_files(env) == []


def test_deleting_a_user_releases_the_card_once_their_job_ends(studio, env):
    """Удаление учётки ждёт конца её отменённого задания: карту оно освобождает подключением учётки,
    которое каскад удалил бы раньше (ревью N4)."""
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    add_gateway(studio)

    async def generation(request):                # картинка досчитывается, когда удаление уже идёт
        for _ in range(3000):
            if state.DB.execute("SELECT disabled FROM users WHERE id = ?", (bob,)).fetchone()[0]:
                break
            await asyncio.sleep(0.001)
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/generations"] = generation
    job_id = start(studio).json()["id"]
    wait_for(lambda: studio.gw.requests and studio.gw.requests[-1].url.path == "/slot/images/generations")
    signin(studio, "owner")
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    job = images.JOBS[job_id]
    assert job.state == "cancelled" and "карта освобождена" in job.detail
    assert studio.gw.releases[-1] == (LUSTIFY, f"Bearer {TOKEN}")
    assert state.DB.execute("SELECT COUNT(*) FROM users WHERE id = ?", (bob,)).fetchone()[0] == 0
    assert media_rows() == 0 and share_files(env) == []


def test_deleting_a_user_during_their_generation_ends_the_job(studio, env, monkeypatch):
    """Удаление учётки посреди генерации: ни IntegrityError, ни задания, вечно занятого (ревью A2-I1).
    Задание не кончилось за SETTLE_SEC — учётка удаляется, карту освободит срок аренды."""
    monkeypatch.setattr(images, "SETTLE_SEC", 3)
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    add_gateway(studio)
    studio.gw.hold = True
    job_id = start(studio).json()["id"]
    wait_for(lambda: studio.gw.generations)
    signin(studio, "owner")
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    studio.gw.hold = False
    job = wait_job(job_id)
    assert job.state == "cancelled" and job.media_id is None
    assert media_rows() == 0 and share_files(env) == []
    assert make_user(studio, "carol") == bob and images._active(bob) is None   # новый id не наследует busy


def test_deleting_a_user_counts_the_files_queued_while_their_jobs_end(studio, env, monkeypatch):
    """Картинка, сохранение которой шло, пока стиралась история, отменяется уже во время ожидания
    заданий учётки: её файл, который шара не удалила, тоже в pending_files (хвост правки очереди)."""
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    add_gateway(studio)
    settling, writing = threading.Event(), threading.Event()
    real_write, real_settle = media.write, images.settle_user

    def held_write(*args, **kwargs):
        writing.set()
        settling.wait(5)                          # пишется до начала ожидания заданий учётки
        return real_write(*args, **kwargs)

    async def settle(uid):
        settling.set()
        return await real_settle(uid)

    def refused(media_id, rel_path):
        raise OSError("share went away")

    monkeypatch.setattr(media, "write", held_write)
    monkeypatch.setattr(images, "settle_user", settle)
    monkeypatch.setattr(media, "_remove", refused)
    job_id = start(studio).json()["id"]
    assert writing.wait(5)
    signin(studio, "owner")
    r = studio.c.delete(f"/api/admin/users/{bob}")
    assert r.status_code == 200 and r.json() == {"pending_files": 1}
    assert images.JOBS[job_id].state == "cancelled" and media_rows() == 0


def test_unexpected_save_failure_ends_the_job_as_an_error(studio, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(PROMPT)

    monkeypatch.setattr(media, "write", broken)
    add_gateway(studio)
    result = wait_state(studio, start(studio).json()["id"], {"done", "error"})
    assert (result["state"], result["detail"]) == ("error", "сбой службы: RuntimeError")


def test_cancel_during_the_save_discards_the_saved_image(studio, env, monkeypatch):
    """Отмена, пришедшая, пока картинка пишется на шару: записанное удаляется, карта освобождается."""
    writing, go = threading.Event(), threading.Event()
    real = media.write

    def slow(*args, **kwargs):
        writing.set()
        go.wait(2)
        return real(*args, **kwargs)

    monkeypatch.setattr(media, "write", slow)
    add_gateway(studio)
    job_id = start(studio, model="flux").json()["id"]
    assert writing.wait(2)
    assert studio.c.post(f"/api/images/{job_id}/cancel").json() == {"state": "cancelling"}
    go.set()
    result = wait_state(studio, job_id, {"cancelled", "done", "error"})
    assert result["state"] == "cancelled" and result["media_id"] is None
    assert media_rows() == 0 and share_files(env) == []
    assert studio.gw.releases == [(LUSTIFY, f"Bearer {TOKEN}"), (FLUX, f"Bearer {TOKEN}")]   # до запроса и после


def test_graceful_stop_during_the_cancel_delete_leaves_no_broken_row(studio, env, monkeypatch):
    """Остановка службы, пока путь отмены удаляет картинку: строка images не ссылается на файл,
    которого уже нет (ревью N3)."""
    writing, go_write = threading.Event(), threading.Event()
    removing, go_remove = threading.Event(), threading.Event()
    real_write, real_remove, real_stop = media.write, media._remove, images.stop

    def slow_write(*args, **kwargs):
        writing.set()
        go_write.wait(2)
        return real_write(*args, **kwargs)

    def slow_remove(media_id, rel_path):          # файл удаляется, а строка media — уже после потока
        real_remove(media_id, rel_path)
        removing.set()
        go_remove.wait(2)

    async def stop_then_release():
        await real_stop()
        go_remove.set()

    monkeypatch.setattr(media, "write", slow_write)
    monkeypatch.setattr(media, "_remove", slow_remove)
    monkeypatch.setattr(images, "stop", stop_then_release)
    add_gateway(studio)
    job_id = start(studio, model="flux").json()["id"]
    assert writing.wait(2)
    assert studio.c.post(f"/api/images/{job_id}/cancel").json() == {"state": "cancelling"}
    go_write.set()
    assert removing.wait(2)
    studio.c.__exit__(None, None, None)
    studio.c.__enter__()
    body = studio.c.get(f"/api/images/{job_id}").json()
    assert (body["state"], body["detail"]) == ("error", "прервано перезапуском студии")
    assert body["media_id"] is None and share_files(env) == []


def test_graceful_restart_marks_a_running_image_interrupted(studio):
    """Остановка службы (SIGTERM при выкладке) — тоже перезапуск, не отмена пользователем (ревью A2-I4)."""
    studio.gw.hold = True
    add_gateway(studio)
    job_id = start(studio).json()["id"]
    wait_for(lambda: studio.gw.generations)
    studio.c.__exit__(None, None, None)
    studio.gw.hold = False
    studio.c.__enter__()
    body = studio.c.get(f"/api/images/{job_id}").json()
    assert (body["state"], body["detail"]) == ("error", "прервано перезапуском студии")


def on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_forms_are_decoded_off_the_loop(studio, monkeypatch):
    """Разбор формы в 20 МиБ — доли секунды: не на цикле событий (ревью A2-M4)."""
    calls, real = [], forms.decode_form

    def spy(*args, **kwargs):
        calls.append(on_loop())
        return real(*args, **kwargs)

    monkeypatch.setattr(forms, "decode_form", spy)
    add_gateway(studio)
    r = studio.c.post("/api/images", data={"prompt": PROMPT, "size": "square"},
                      files={"image": ("photo.png", photo(), "image/png")})
    wait_state(studio, r.json()["id"], {"done"})
    wait_description(studio, describe(studio).json()["id"])
    assert len(calls) == 2 and not any(calls)


@pytest.mark.parametrize("change, words", [
    ("UPDATE settings SET value = '[]' WHERE key = 'allowlist'", "убран из списка администратора"),
    ("DELETE FROM connections", "«srvllm» удалено из профиля"),
    ("UPDATE connections SET base_url = 'http://10.77.1.16:4000'", "«srvllm» изменено — запустите заново"),
], ids=["refused", "deleted", "moved"])
def test_description_release_refusals_read_as_words(studio, change, words):
    """Подготовка карты к описанию: адрес убрали из списка или подключение удалили — словами (ревью A2-M6)."""
    async def release(request):
        state.DB.execute(change)                  # между первым и вторым release
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/release"] = release
    add_gateway(studio)
    status = wait_description(studio, describe(studio).json()["id"])
    assert status["state"] == "error" and words in status["detail"], status


def test_a_source_being_deleted_cannot_start_a_job(studio, monkeypatch):
    """Пока файл картинки удаляется, новое задание с ним как исходником — 404, а не строка без файла."""
    add_gateway(studio)
    first = wait_state(studio, start(studio).json()["id"], {"done"})
    real, answers = media._remove, []

    def racing(media_id, rel_path):               # поток удаления: файл ещё на месте
        answers.append(start(studio, size="original", model="flux", source_media_id=first["media_id"]))
        real(media_id, rel_path)

    monkeypatch.setattr(media, "_remove", racing)
    assert studio.c.delete(f"/api/images/{first['id']}").status_code == 204
    (r,) = answers
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    assert state.DB.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0


def test_feed_pages_by_created(studio):
    add_gateway(studio)
    ids = []
    for i in range(5):
        studio.clock[0] = T0 + i
        ids.append(start(studio).json()["id"])
        wait_state(studio, ids[-1], {"done"})
    page = studio.c.get("/api/images?limit=2").json()["items"]
    assert [x["id"] for x in page] == [ids[4], ids[3]]
    older = studio.c.get(f"/api/images?limit=2&before={page[-1]['created']}").json()["items"]
    assert [x["id"] for x in older] == [ids[2], ids[1]]
    assert len(studio.c.get("/api/images").json()["items"]) == 5
    for bad in (0, 101):
        assert studio.c.get(f"/api/images?limit={bad}").status_code == 422


def test_prompt_and_key_never_reach_the_log_or_the_answers(studio, caplog):
    caplog.set_level(logging.DEBUG)
    make_user(studio, "owner", is_admin=True)
    assert login(studio, "owner", "wrong-pass-7f3a").status_code == 401
    signin(studio, "owner")
    add_gateway(studio)
    job = start(studio).json()["id"]
    status = wait_state(studio, job, {"done"})
    studio.gw.image_reply = (400, {"error": {"message": "bad"}})
    wait_state(studio, start(studio).json()["id"], {"error"})
    assert caplog.records, "служба ничего не пишет в журнал"
    for secret in (PROMPT, TOKEN, "wrong-pass-7f3a", "password-123"):
        assert secret not in caplog.text, secret
    answers = str(status) + studio.c.get("/api/images").text + studio.c.get("/api/connections").text
    assert TOKEN not in answers
    assert images.JOBS[job].prompt == ""


def test_unchecked_model_is_refused_and_default_is_used(studio):
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["flux"]})
    assert start(studio, model="lustify").status_code == 422
    job = wait_state(studio, start(studio).json()["id"], {"done"})
    assert job["model"] == "flux" and studio.gw.generations[-1][0]["model"] == FLUX


def test_photo_without_a_checked_model_points_to_settings(studio):
    cid = add_gateway(studio)
    studio.c.delete(f"/api/sections/photo/links/{cid}")
    r = start(studio)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"


def cloud_photo(studio, monkeypatch):
    """Внешний сервис картинок по имени в списке: адрес имени подменён публичным."""
    state.DB.execute("UPDATE settings SET value = '[\"10.77.1.15:4000\", \"img.cloud.test:443\"]' WHERE key = 'allowlist'")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(studio, base_url="https://img.cloud.test/v1", token="cloud-key-1", sections=["photo"])
    studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["gpt-image-1"]})
    return cid


def test_external_photo_generation_takes_b64_only(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    seen = []
    async def reply(q):
        seen.append(q)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_REAL).decode()}]})
    studio.gw.on["/v1/images/generations"] = reply
    releases = len(studio.gw.releases)
    job = wait_state(studio, start(studio, model="gpt-image-1", connection_id=cid, size="portrait").json()["id"], {"done"})
    body = json.loads(seen[0].content)
    assert body == {"model": "gpt-image-1", "prompt": PROMPT, "n": 1, "size": "1024x1536", "response_format": "b64_json"}
    assert seen[0].headers["host"] == "img.cloud.test" and seen[0].headers["authorization"] == "Bearer cloud-key-1"
    assert job["media_id"] and len(studio.gw.releases) == releases          # release — только шлюзу


def test_external_photo_edit_sends_the_picture_without_strength(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    seen = []
    async def reply(q):
        seen.append(q)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_REAL).decode()}]})
    studio.gw.on["/v1/images/edits"] = reply
    r = studio.c.post("/api/images", data={"prompt": "ярче", "size": "original", "model": "gpt-image-1",
                                           "connection_id": str(cid)}, files={"image": ("p.png", photo(), "image/png")})
    wait_state(studio, r.json()["id"], {"done"})
    fields, image = decode_form(seen[0].content, seen[0].headers["content-type"])
    assert fields == {"model": "gpt-image-1", "prompt": "ярче", "n": "1", "response_format": "b64_json"} and image


def test_external_photo_url_answer_is_an_error_and_not_fetched(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    async def reply(q):
        return httpx.Response(200, json={"data": [{"url": "http://10.0.0.1/x.png"}]})
    studio.gw.on["/v1/images/generations"] = reply
    job = wait_state(studio, start(studio, model="gpt-image-1", connection_id=cid).json()["id"], {"error"})
    assert "ссылку" in job["detail"] and not any(q.url.host == "10.0.0.1" for q in studio.gw.requests)


def test_external_photo_garbage_is_an_error(studio, env, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    async def reply(q):
        return httpx.Response(200, json=reply_with(b"not an image")[1])
    studio.gw.on["/v1/images/generations"] = reply
    job = wait_state(studio, start(studio, model="gpt-image-1", connection_id=cid).json()["id"], {"done", "error"})
    assert job["state"] == "error" and "сервис вернул не картинку" in job["detail"]
    assert media_rows() == 0 and share_files(env) == []


def test_brush_is_srvllm_only(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    mask = Image.new("L", (48, 32), 0); mask.paste(255, (0, 0, 8, 8))
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "original", "model": "gpt-image-1", "connection_id": str(cid)},
                      files={"image": ("p.png", photo(), "image/png"), "mask": ("m.png", png(mask), "image/png")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "brush"
    assert not state.DB.execute("SELECT count(*) FROM media").fetchone()[0]


# ---- серии -------------------------------------------------------------------

def rows_of(ids) -> list:
    return [state.DB.execute("SELECT state, detail, seed, strength FROM images WHERE id = ?", (i,)).fetchone()[:]
            for i in ids]


def held_gateway(s, path="/slot/images/generations"):
    """Запрос `path` «идёт», пока событие не поднято: (поднять, сколько запросов сразу в пути — максимум)."""
    go, now, peak = threading.Event(), [0], [0]

    async def held(request):
        now[0] += 1
        peak[0] = max(peak[0], now[0])
        try:
            while not go.is_set():
                await asyncio.sleep(0.001)
            return await s.gw.builtin(request)
        finally:
            now[0] -= 1

    s.gw.on[path] = held
    return go, peak


def generating(s) -> bool:
    """Запрос генерации дошёл до шлюза (и висит у held_gateway)."""
    return any(r.url.path == "/slot/images/generations" for r in s.gw.requests)


def first_photo(s) -> str:
    return wait_state(s, start(s).json()["id"], {"done"})["media_id"]


def test_seed_series_goes_one_by_one_in_the_users_slot(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    go, peak = held_gateway(studio)
    r = start(studio, seed=7, series={"count": 3, "vary": "seed"})
    assert r.status_code == 202, r.text
    out = r.json()
    ids = out["ids"]
    assert len(ids) == 3 and out["id"] == ids[0] and out["seed"] == 7
    wait_for(lambda: generating(studio))
    assert rows_of(ids) == [("working", "жду карту или генерирую", 7, None),
                            ("queued", "в очереди серии · 2 из 3", 8, None),
                            ("queued", "в очереди серии · 3 из 3", 9, None)]
    feed = [i["id"] for i in studio.c.get("/api/images").json()["items"]]
    assert feed == ids                                         # первая сверху, по порядку серии
    busy = start(studio)
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "busy"
    go.set()
    for i in ids:
        wait_state(studio, i, {"done"})
    assert [g[0]["seed"] for g in studio.gw.generations] == [7, 8, 9] and peak[0] == 1
    assert [m for m, _ in studio.gw.releases] == [FLUX] * 3   # до каждого запроса; карта после — не освобождается
    assert PROMPT not in caplog.text
    assert start(studio).status_code == 202


def test_random_seed_series_has_distinct_seeds(studio):
    add_gateway(studio)
    ids = start(studio, series={"count": 4, "vary": "seed"}).json()["ids"]
    seeds = [wait_state(studio, i, {"done"})["seed"] for i in ids]
    assert len(set(seeds)) == 4 and [g[0]["seed"] for g in studio.gw.generations] == seeds


def test_seed_series_wraps_at_two_to_the_32(studio):
    add_gateway(studio)
    ids = start(studio, seed=2**32 - 1, series={"count": 2, "vary": "seed"}).json()["ids"]
    assert [wait_state(studio, i, {"done"})["seed"] for i in ids] == [2**32 - 1, 0]


def test_strength_series_values(studio):
    add_gateway(studio)
    source = first_photo(studio)
    edit = dict(size="original", source_media_id=source)
    r = start(studio, strength=0.25, series={"count": 3, "vary": "strength", "strength_to": 0.75}, **edit)
    assert r.status_code == 202, r.text
    ids = r.json()["ids"]
    assert [wait_state(studio, i, {"done"})["strength"] for i in ids] == [0.25, 0.5, 0.75]
    assert [f["strength"] for f, _ in studio.gw.edits] == ["0.25", "0.5", "0.75"]
    assert [s["strength"] for s in (studio.c.get(f"/api/images/{i}").json()["chain"][-1] for i in ids)] == [0.25, 0.5, 0.75]
    assert len({g[0]["seed"] for g in studio.gw.generations[1:]}) == 1     # по силе — один seed на всю серию


def test_strength_series_refusals(studio):
    add_gateway(studio)
    edit = dict(size="original", source_media_id=first_photo(studio))
    narrow = start(studio, strength=0.25, series={"count": 4, "vary": "strength", "strength_to": 0.3}, **edit)
    assert narrow.status_code == 422 and narrow.json()["detail"] == {
        "code": "series", "message": "слишком узкий диапазон для 4 вариантов"}
    for refused in (start(studio, model="flux", series={"count": 2, "vary": "strength", "strength_to": 1}, **edit),
                    start(studio, series={"count": 2, "vary": "strength", "strength_to": 1}),
                    start(studio, series={"count": 2, "vary": "strength"}, **edit)):
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "series", refused.text
    for bad in ({"count": 1, "vary": "seed"}, {"count": 5, "vary": "seed"}, {"count": 2, "vary": "prompt"},
                {"count": 2, "vary": "strength", "strength_to": 0}):
        assert start(studio, series=bad, **edit).status_code == 422
    assert studio.gw.edits == [] and len(images.JOBS) == 1


def test_series_with_a_mask_is_refused(studio, env):
    add_gateway(studio)
    mask = Image.new("L", (48, 32), 0)
    mask.paste(255, (0, 0, 8, 8))
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "original", "model": "flux",
                                           "series": json.dumps({"count": 2, "vary": "seed"})},
                      files={"image": ("p.png", photo(), "image/png"), "mask": ("m.png", png(mask), "image/png")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "series"
    assert media_rows() == 0 and share_files(env) == [] and not images.JOBS


def test_series_field_in_a_form_is_json(studio, env):
    add_gateway(studio)
    form = {"prompt": "x", "size": "original", "model": "lustify", "strength": "0.25"}
    bad = studio.c.post("/api/images", data=form | {"series": "{broken"}, files={"image": ("p.png", photo(), "image/png")})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "input"
    assert media_rows() == 0 and share_files(env) == []
    r = studio.c.post("/api/images", data=form | {"series": json.dumps({"count": 2, "vary": "strength", "strength_to": 0.5})},
                      files={"image": ("p.png", photo(), "image/png")})
    assert r.status_code == 202, r.text
    rows = [wait_state(studio, i, {"done"}) for i in r.json()["ids"]]
    assert [x["strength"] for x in rows] == [0.25, 0.5]
    assert len({x["source_media_id"] for x in rows}) == 1        # исходник — один файл на всю серию


def test_external_series_sends_no_seed(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    seen = []

    async def reply(q):
        seen.append(json.loads(q.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_REAL).decode()}]})

    studio.gw.on["/v1/images/generations"] = reply
    r = start(studio, model="gpt-image-1", connection_id=cid, seed=5, series={"count": 2, "vary": "seed"})
    for i in r.json()["ids"]:
        wait_state(studio, i, {"done"})
    assert len(seen) == 2 and not any("seed" in body for body in seen)
    refused = start(studio, model="gpt-image-1", connection_id=cid, size="original", source_media_id=first_photo(studio),
                    series={"count": 2, "vary": "strength", "strength_to": 1})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "series"


def series_held(s, model="flux", count=3):
    """Серия, первая строка которой висит на запросе к шлюзу: (ids, поднять)."""
    add_gateway(s)
    go, _ = held_gateway(s)
    ids = start(s, model=model, series={"count": count, "vary": "seed"}).json()["ids"]
    wait_for(lambda: generating(s))
    s.gw.releases.clear()
    return ids, go


@pytest.mark.parametrize("which", [0, 2], ids=["running", "queued"])
def test_series_cancel_stops_the_whole_rest_and_frees_the_card(studio, which):
    """«Отменить» у любой строки серии: идущая — как одиночная отмена (ждёт конца запроса, release
    модели), все в очереди — сразу «отменено»; новое задание до конца — 409."""
    ids, go = series_held(studio)
    expected = "cancelling" if which == 0 else "cancelled"
    assert studio.c.post(f"/api/images/{ids[which]}/cancel").json() == {"state": expected}
    assert [r[0] for r in rows_of(ids)] == ["cancelling", "cancelled", "cancelled"]
    assert rows_of(ids)[1][1] == "отменено"
    busy = start(studio)
    assert busy.status_code == 409 and "отменяется" in busy.json()["detail"]["message"]
    assert studio.c.post(f"/api/images/{ids[1]}/cancel").json() == {"state": "cancelled"}   # повтор — без перемен
    go.set()
    first = wait_state(studio, ids[0], {"cancelled", "done", "error"})
    assert first["state"] == "cancelled" and "карта освобождена" in first["detail"]
    assert studio.gw.releases == [(FLUX, f"Bearer {TOKEN}")] and len(studio.gw.generations) == 1
    assert media_rows() == 0
    nxt = start(studio)
    assert nxt.status_code == 202
    wait_state(studio, nxt.json()["id"], {"done"})


def test_series_error_of_one_does_not_stop_the_next(studio):
    calls = []

    async def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/generations"] = flaky
    add_gateway(studio)
    ids = start(studio, series={"count": 3, "vary": "seed"}).json()["ids"]
    assert [wait_state(studio, i, images.TERMINAL)["state"] for i in ids] == ["error", "done", "done"]


def test_series_keeps_the_slot_until_the_last(studio):
    """Между строками серии нет мгновения без занятости: вторая стартует, не отпуская слот."""
    add_gateway(studio)
    go, calls = threading.Event(), []

    async def second_held(request):
        calls.append(1)
        while len(calls) == 2 and not go.is_set():
            await asyncio.sleep(0.001)
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/generations"] = second_held
    ids = start(studio, series={"count": 2, "vary": "seed"}).json()["ids"]
    wait_state(studio, ids[0], {"done"})
    assert start(studio).status_code == 409
    assert studio.c.delete(f"/api/images/{ids[1]}").json()["detail"]["code"] == "running"
    go.set()
    wait_state(studio, ids[1], {"done"})
    assert start(studio).status_code == 202


def second_of_series_held(s, count=2):
    """Серия, первая строка которой готова, а вторая висит на запросе к шлюзу: (ids, поднять)."""
    add_gateway(s)
    go, calls = threading.Event(), []

    async def second_held(request):
        calls.append(1)
        while len(calls) == 2 and not go.is_set():
            await asyncio.sleep(0.001)
        return await s.gw.builtin(request)

    s.gw.on["/slot/images/generations"] = second_held
    ids = start(s, series={"count": count, "vary": "seed"}).json()["ids"]
    wait_state(s, ids[0], {"done"})
    wait_for(lambda: len(calls) == 2)
    return ids, go


def test_series_row_time_counts_from_its_own_start(studio):
    """Время строки серии — от её перехода из очереди в работу, а не от создания серии: и в ленте,
    и в «В работе»."""
    add_gateway(studio)
    go, calls = threading.Event(), []

    async def first_held(request):
        calls.append(1)
        while len(calls) == 1 and not go.is_set():
            await asyncio.sleep(0.001)
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/generations"] = first_held
    studio.gw.hold = False
    ids = start(studio, series={"count": 2, "vary": "seed"}).json()["ids"]
    wait_for(lambda: calls)
    studio.clock[0] = T0 + 40                                   # первая строка шла 40 с
    hold_second = threading.Event()

    async def second_held(request):
        calls.append(1)
        while not hold_second.is_set():
            await asyncio.sleep(0.001)
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/images/generations"] = second_held
    go.set()
    wait_state(studio, ids[0], {"done"})
    wait_for(lambda: len(calls) == 2)
    studio.clock[0] = T0 + 45
    second = studio.c.get(f"/api/images/{ids[1]}").json()
    assert second["state"] == "working" and second["elapsed"] == 5
    assert [i["elapsed"] for i in studio.c.get("/api/images").json()["items"] if i["id"] == ids[1]] == [5]
    running = {i["id"]: i for i in studio.c.get("/api/jobs").json()["running"]}
    assert running[ids[1]]["started"] == T0 + 40
    hold_second.set()
    wait_state(studio, ids[1], {"done"})


def test_cancel_on_a_just_finished_series_row_stops_the_rest(studio):
    """«Отменить» на строке, которая только что кончилась: остаток серии снимается, кончившаяся —
    как есть."""
    ids, go = second_of_series_held(studio, count=3)
    assert studio.c.post(f"/api/images/{ids[0]}/cancel").json() == {"state": "done"}
    assert [r[0] for r in rows_of(ids)] == ["done", "cancelling", "cancelled"]
    go.set()
    assert wait_state(studio, ids[1], images.TERMINAL)["state"] == "cancelled"
    assert rows_of(ids)[0][0] == "done" and len(studio.gw.generations) == 2


def test_deeply_nested_series_field_is_refused(studio, env):
    """Поле `series` с глубокой вложенностью — 422, а не 500 (RecursionError json.loads)."""
    add_gateway(studio)
    deep = "[" * 50_000 + "]" * 50_000                          # 100 КБ — в пределе поля формы
    form = {"prompt": "x", "size": "original", "model": "lustify", "strength": "0.25", "series": deep}
    r = studio.c.post("/api/images", data=form, files={"image": ("p.png", photo(), "image/png")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "series", r.text
    assert media_rows() == 0 and share_files(env) == [] and not images.JOBS
    r = studio.c.post("/api/images", content='{"prompt": "x", "size": "square", "series": ' + deep + "}",
                      headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text


def test_queued_row_cannot_be_deleted(studio):
    ids, go = series_held(studio, count=2)
    r = studio.c.delete(f"/api/images/{ids[1]}")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "running", "message": "сначала отмените"}
    go.set()
    wait_state(studio, ids[1], {"done"})
    assert studio.c.delete(f"/api/images/{ids[1]}").status_code == 204


def test_stop_marks_queued_rows_interrupted(studio, monkeypatch):
    """Остановка службы посреди серии: строки в очереди — «прервано перезапуском» самой остановкой
    (прежний код после отката install.sh о queued не знает и оставил бы их неудаляемыми)."""
    ids, go = series_held(studio)
    studio.c.__exit__(None, None, None)
    go.set()
    monkeypatch.setattr(images, "recover", lambda: None)
    studio.c.__enter__()
    assert [r[:2] for r in rows_of(ids)] == [("error", "прервано перезапуском студии")] * 3
    assert all(images.JOBS[i].prompt == "" for i in ids)


def test_history_wipe_during_a_series_starts_nothing_more(studio, env):
    ids, go = series_held(studio)
    assert studio.c.delete("/api/history").status_code == 200
    go.set()
    job = wait_job(ids[0])
    assert job.state == "cancelled" and "карта освобождена" in job.detail
    assert [images.JOBS[i].state for i in ids[1:]] == ["cancelled", "cancelled"]
    assert len(studio.gw.generations) == 1 and media_rows() == 0 and share_files(env) == []


def test_deleting_a_user_during_a_series_ends_it(studio, env, monkeypatch):
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    ids, go = series_held(studio)
    signin(studio, "owner")
    real_settle = images.settle_user

    async def settle(uid):                        # конец задания приходит, пока удаление его ждёт
        go.set()
        return await real_settle(uid)

    monkeypatch.setattr(images, "settle_user", settle)
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    assert [images.JOBS[i].state for i in ids] == ["cancelled"] * 3
    assert "карта освобождена" in images.JOBS[ids[0]].detail and len(studio.gw.generations) == 1
    assert media_rows() == 0 and share_files(env) == []


# ---- варианты ----------------------------------------------------------------

def edit_of(s, media_id, prompt="x", **extra):
    return wait_state(s, start(s, prompt=prompt, size="original", model="flux", source_media_id=media_id, **extra)
                      .json()["id"], {"done"})


def test_variants_are_siblings_and_children(studio):
    add_gateway(studio)
    g = wait_state(studio, start(studio).json()["id"], {"done"})
    studio.clock[0] += 1
    a = edit_of(studio, g["media_id"], "a")
    studio.clock[0] += 1
    b = edit_of(studio, g["media_id"], "b")
    studio.clock[0] += 1
    c = edit_of(studio, a["media_id"], "c")
    studio.gw.image_reply = (500, {"error": {"message": "boom"}})
    wait_state(studio, start(studio, size="original", source_media_id=g["media_id"]).json()["id"], {"error"})
    ids = lambda xs: [x["id"] for x in xs]  # noqa: E731
    va = studio.c.get(f"/api/images/{a['id']}/variants").json()
    assert ids(va["siblings"]) == [b["id"]] and ids(va["children"]) == [c["id"]]
    assert "chain" not in va["siblings"][0] and va["siblings"][0]["prompt"] == "b" and va["siblings"][0]["media_id"]
    vg = studio.c.get(f"/api/images/{g['id']}/variants").json()
    assert vg == {"siblings": [], "children": vg["children"]} and ids(vg["children"]) == [b["id"], a["id"]]
    vc = studio.c.get(f"/api/images/{c['id']}/variants").json()
    assert vc == {"siblings": [], "children": []}


def test_variants_of_another_user_are_404(studio):
    add_gateway(studio)
    g = wait_state(studio, start(studio).json()["id"], {"done"})
    edit_of(studio, g["media_id"])
    make_user(studio, "friend")
    signin(studio, "friend")
    r = studio.c.get(f"/api/images/{g['id']}/variants")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_variants_are_capped(studio):
    add_gateway(studio)
    g = wait_state(studio, start(studio).json()["id"], {"done"})
    uid = state.DB.execute("SELECT user_id FROM images").fetchone()[0]
    for i in range(images.VARIANTS_MAX + 1):          # строками: 25 правок через шлюз — дольше секунды
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                         " state, detail, created) VALUES (?, ?, 'edit', 'flux', 'p', '64x48', 1, ?, ?, 'done', 'готово', ?)",
                         (f"{i:032x}", uid, g["media_id"], f"{i + 100:032x}", T0 + i))
    children = studio.c.get(f"/api/images/{g['id']}/variants").json()["children"]
    assert len(children) == images.VARIANTS_MAX == 24 and children[0]["id"] == f"{images.VARIANTS_MAX:032x}"


# ---- пресеты -----------------------------------------------------------------

PRESET = {"model": "lustify", "connection_id": None, "size": "portrait", "seed": 7, "strength": 0.4,
          "prompt": "портрет, студийный свет"}


def save_preset(s, name="Портрет студия", **params):
    return s.c.post("/api/images/presets", json={"name": name, "params": PRESET | params})


def test_presets_crud_and_replace_by_name(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    first = save_preset(studio)
    assert first.status_code == 201, first.text
    item = first.json()
    assert item["name"] == "Портрет студия" and item["params"] == PRESET and item["id"]
    studio.clock[0] += 5
    other = save_preset(studio, "Альбом", size="landscape", seed=None, prompt=None).json()
    listed = studio.c.get("/api/images/presets").json()["items"]
    assert [p["id"] for p in listed] == [other["id"], item["id"]]           # новые сверху
    studio.clock[0] += 5
    again = save_preset(studio, "Портрет студия", size="square")
    assert again.status_code == 201 and again.json()["id"] == item["id"]   # то же имя — замена
    listed = studio.c.get("/api/images/presets").json()["items"]
    assert len(listed) == 2 and listed[0]["params"]["size"] == "square"
    assert studio.c.delete(f"/api/images/presets/{item['id']}").status_code == 204
    assert [p["id"] for p in studio.c.get("/api/images/presets").json()["items"]] == [other["id"]]
    assert studio.c.delete(f"/api/images/presets/{item['id']}").status_code == 404
    assert "студийный" not in caplog.text and "Портрет" not in caplog.text


def test_presets_limit_is_50(studio):
    add_gateway(studio)
    assert save_preset(studio, "p0").status_code == 201
    uid = state.DB.execute("SELECT user_id FROM presets").fetchone()[0]
    for i in range(1, 50):                        # остальные — строками: 50 запросов — больше секунды
        state.DB.execute("INSERT INTO presets(id, user_id, section, name, params, created) VALUES (?, ?, 'photo', ?, '{}', ?)",
                         (f"{i:032x}", uid, f"p{i}", T0))
    r = save_preset(studio, "лишний")
    assert r.status_code == 409 and r.json()["detail"] == {
        "code": "presets", "message": "не больше 50 пресетов — удалите ненужные"}
    assert save_preset(studio, "p3", seed=1).status_code == 201             # замена — не новый


@pytest.mark.parametrize("ensure_ascii", [False, True], ids=["utf8", "escaped"])
def test_preset_with_the_longest_prompt_of_four_byte_chars_is_saved(studio, ensure_ascii):
    """24000 знаков по 4 байта UTF-8 (или по 12 — `\\uXXXX\\uXXXX`) — допустимый промпт: предел тела
    его не отбивает раньше проверки полей."""
    add_gateway(studio)
    prompt = "\U0001F600" * 24000
    body = json.dumps({"name": "эмодзи", "params": PRESET | {"prompt": prompt}}, ensure_ascii=ensure_ascii).encode()
    assert len(body) > 64 * 1024
    r = studio.c.post("/api/images/presets", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text[:200]
    assert studio.c.get("/api/images/presets").json()["items"][0]["params"]["prompt"] == prompt


def test_presets_are_own(studio):
    add_gateway(studio)
    mine = save_preset(studio).json()
    make_user(studio, "friend")
    signin(studio, "friend")
    assert studio.c.get("/api/images/presets").json()["items"] == []
    assert studio.c.delete(f"/api/images/presets/{mine['id']}").status_code == 404
    signin(studio, "owner")
    assert len(studio.c.get("/api/images/presets").json()["items"]) == 1


@pytest.mark.parametrize("body", [
    {"name": "", "params": PRESET},
    {"name": "   ", "params": PRESET},
    {"name": "x" * 61, "params": PRESET},
    {"name": "x", "params": PRESET | {"seed": -1}},
    {"name": "x", "params": PRESET | {"strength": 2}},
    {"name": "x", "params": PRESET | {"size": "huge"}},
    {"name": "x", "params": PRESET | {"prompt": "я" * 24001}},
    {"name": "x", "params": PRESET | {"extra": 1}},
    {"name": "x"},
])
def test_presets_input_is_validated(studio, body):
    add_gateway(studio)
    r = studio.c.post("/api/images/presets", json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "input"
    assert studio.c.get("/api/images/presets").json()["items"] == []


def test_presets_outlive_the_history_wipe_but_not_the_account(studio):
    """Пресеты — настройки, а не история: стирание истории их не трогает, удаление учётки — каскадом."""
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    add_gateway(studio)
    save_preset(studio)
    assert studio.c.delete("/api/history").status_code == 200
    assert len(studio.c.get("/api/images/presets").json()["items"]) == 1
    signin(studio, "owner")
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    assert state.DB.execute("SELECT COUNT(*) FROM presets").fetchone()[0] == 0
