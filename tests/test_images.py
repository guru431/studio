"""Картинки на учётках (app/images.py): задания через фото-подключение srvllm, лента истории,
отмена с release модели, описание gemma-abl. Сценарии перенесены из tests/test_studio.py (прежний
server.py); шлюз — FakeGateway, часы — clock.NOW, паузы — clock.SLEEP."""
import asyncio
import base64
import io
import json
import logging
import threading
import time

import httpx
import pytest
from PIL import Image

import forms
import images
import media
import state
from conftest import T0, add_connection, add_gateway, login, make_user, signin
from fakes import PNG_REAL, TOKEN
from image_upload import decode_form, normalize_image

PROMPT = "секретный промпт: женщина у окна"
LUSTIFY, FLUX = "lustify-v10-krea2-turbo", "flux.2-klein-4b"
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
    """Первый ответ опроса описания с концом задания (готовое описание выдаётся один раз — им же)."""
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
    assert body["state"] == "done" and body["kind"] == "edit" and body["strength"] == 0.4
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
def test_description_uses_gemma_abl_and_returns_editable_text_once(studio, caplog, finish):
    text = "A private detailed description. " * 300
    studio.gw.description_reply = {"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    add_gateway(studio)
    response = describe(studio)
    assert response.status_code == 202
    job_id = response.json()["id"]
    result = wait_description(studio, job_id)
    assert result == {"state": "done", "text": text.strip(), "truncated": finish == "length"}
    assert studio.c.get(f"/api/images/descriptions/{job_id}").status_code == 404
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
    for image_id, status in (("a" * 32, "working"), ("b" * 32, "cancelling"), ("c" * 32, "done")):
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                         " VALUES (?, ?, 'generation', 'flux', 'p', '1024x1024', 1, ?, 'x', ?)",
                         (image_id, uid, status, T0))
    images.recover()
    for image_id, status in (("a" * 32, "error"), ("b" * 32, "error"), ("c" * 32, "done")):
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
    assert studio.c.delete("/api/history").status_code == 204
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
    assert studio.c.delete("/api/history").status_code == 204
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
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 204
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
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 204
    studio.gw.hold = False
    job = wait_job(job_id)
    assert job.state == "cancelled" and job.media_id is None
    assert media_rows() == 0 and share_files(env) == []
    assert make_user(studio, "carol") == bob and images._active(bob) is None   # новый id не наследует busy


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
], ids=["refused", "deleted"])
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
