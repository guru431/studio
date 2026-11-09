"""Ролики на учётках (app/videos.py): задание у шлюза через видео-подключение srvllm, наблюдатель
сам забирает готовый mp4 в историю, лента, отмена и удаление. Сценарии создания перенесены из
tests/test_studio.py (прежний server.py); шлюз — FakeGateway, часы — clock.NOW, паузы — clock.SLEEP.
Проход наблюдателя идёт в цикле приложения (там живёт state.HTTP) — `poll`."""
import asyncio
import io
import itertools
import json
import logging

import httpx
import pytest
from PIL import Image

import clock
import forms
import media
import state
import videos
from conftest import T0, add_connection, add_gateway, make_user, signin
from fakes import MP4, PNG_REAL, TOKEN, video_object
from image_upload import normalize_image

PROMPT = "секретное движение: она оборачивается"
PHOTO_NAME = "my-private-photo-name.png"
VIDEO_OUT = {"id", "state", "progress", "detail", "prompt", "prompt_source", "seconds", "quality", "smooth",
             "seed", "source_media_id", "media_id", "created", "elapsed", "steps", "chain"}


def photo(name=PHOTO_NAME, size=(40, 20)):
    out = io.BytesIO()
    Image.new("RGB", size, "red").save(out, "PNG")
    return {"image": (name, out.getvalue(), "image/png")}


def create_video(s, files=None, **data):
    return s.c.post("/api/videos", files=photo() if files is None else files, data=data)


def v_gateway_id(s):
    return s.gw.video_ids[-1]


def poll(s):
    s.c.portal.call(videos.poll_once)


@pytest.mark.parametrize("gone", [False, True], ids=["cancelled", "deleted"])
def test_cancelled_or_deleted_video_is_not_downloaded_after_a_stale_status(studio, gone):
    add_gateway(studio)
    video = create_video(studio).json()
    gateway_id = v_gateway_id(studio)

    async def completed(request):
        if gone:
            state.DB.execute("DELETE FROM videos WHERE id = ?", (video["id"],))
        else:
            state.DB.execute("UPDATE videos SET state = 'cancelled' WHERE id = ?", (video["id"],))
        return httpx.Response(200, json={"status": "completed", "seed": 42})

    studio.gw.on[f"/slot/videos/{gateway_id}"] = completed
    poll(studio)

    assert not any(r.url.path == f"/slot/videos/{gateway_id}/content" for r in studio.gw.requests)
    assert studio.gw.video_deleted == []


def item(s, video_id):
    return s.c.get(f"/api/videos/{video_id}").json()


def media_rows() -> int:
    return state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0]


def share_files(env):
    return sorted(p.name for p in env.media_dir.rglob("*") if p.is_file())


def owner_id() -> int:
    return state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]


# ---- создание ----------------------------------------------------------------

def test_video_is_created_as_gateway_multipart_without_private_names(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    r = create_video(studio, prompt=PROMPT, seconds="7", quality="high", smooth="false", seed="")
    assert r.status_code == 201, r.text
    v = r.json()
    assert set(v) == VIDEO_OUT
    (call,) = studio.gw.video_calls
    assert (call["method"], call["path"]) == ("POST", "/slot/videos")
    assert call["auth"] == f"Bearer {TOKEN}" and call["timeout"] == 120
    (fields, image, raw), = studio.gw.video_forms
    # пустой seed — случайный, но выбирает его студия: по нему она найдёт ролик, если ответ потеряется
    seed = fields.pop("seed")
    assert seed.isdigit() and int(seed) < 2**32
    assert fields == {"model": "wan2.2-remix-i2v-v3", "prompt": PROMPT, "seconds": "7",
                      "quality": "high", "smooth": "false"}
    assert {k: v[k] for k in VIDEO_OUT - {"id", "source_media_id", "chain"}} == {
        "state": "queued", "progress": 0, "detail": "в очереди: перед ним 0", "prompt": PROMPT,
        "prompt_source": "auto", "seconds": 7, "quality": "high", "smooth": False, "seed": int(seed),
        "media_id": None, "created": T0, "elapsed": 0, "steps": 2}
    assert image.startswith(b"\x89PNG")
    assert b'name="input_reference"; filename="input.png"' in raw
    assert PHOTO_NAME.encode() not in raw
    # исходник — в истории с превью, как его получил шлюз
    assert studio.c.get(f"/api/media/{v['source_media_id']}").content == image
    assert studio.c.get(f"/api/media/{v['source_media_id']}?thumb=1").headers["content-type"] == "image/webp"
    for secret in (PROMPT, PHOTO_NAME, TOKEN):
        assert secret not in caplog.text, secret
    assert caplog.records, "служба ничего не пишет в журнал"


def test_video_defaults_and_seed_reach_the_gateway(studio):
    add_gateway(studio)
    r = create_video(studio, prompt="  ", seed="4294967295")
    assert r.status_code == 201
    assert studio.gw.video_forms[0][0] == {"model": "wan2.2-remix-i2v-v3", "seconds": "5", "quality": "standard",
                                           "smooth": "true", "seed": "4294967295"}   # пустой промпт — описание по фото
    assert r.json()["seed"] == 4294967295 and r.json()["prompt"] == ""


@pytest.mark.parametrize("fields", [
    {"seconds": "0"}, {"seconds": "31"}, {"seconds": "2.5"}, {"seconds": "abc"},
    {"quality": "ultra"}, {"smooth": "maybe"}, {"seed": "-1"}, {"seed": str(2**32)},
    {"prompt": "x" * 4001},
])
def test_video_fields_are_validated_before_the_gateway(studio, fields):
    add_gateway(studio)
    r = create_video(studio, **fields)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "input"
    assert studio.gw.video_calls == [] and media_rows() == 0


def test_video_needs_a_readable_image(studio):
    add_gateway(studio)
    no_image = studio.c.post("/api/videos", data={"prompt": "x"}, files={"note": ("a.txt", b"x", "text/plain")})
    assert no_image.status_code == 400
    assert create_video(studio, files={"image": ("bad.png", b"bad", "image/png")}).status_code == 400
    assert studio.gw.video_calls == [] and media_rows() == 0


def test_video_upload_limit_is_twenty_megabytes_and_json_jobs_keep_theirs(studio):
    from image_upload import MAX_BODY, MAX_UPLOAD, VIDEO_UPLOAD
    add_gateway(studio)
    padded = photo()["image"][1]
    # PNG с хвостом: ~20 МБ, декодируется быстро; хвост — строками по 1 КиБ: разбор multipart вдвое быстрее
    # одной сплошной строки (тест укладывается в 1 с)
    line = b"\0" * 1023 + b"\n"
    padded += line * ((VIDEO_UPLOAD - 1024 - len(padded)) // len(line))
    assert len(padded) > VIDEO_UPLOAD - 2048
    assert create_video(studio, files={"image": ("p.png", padded, "image/png")}).status_code == 201
    huge = b"\0" * (VIDEO_UPLOAD + (MAX_BODY - MAX_UPLOAD) + 1)
    r = create_video(studio, files={"image": ("p.png", huge, "image/png")})
    assert r.status_code == 413 and len(studio.gw.video_calls) == 1
    json_job = json.dumps({"prompt": "x" * (600 * 1024), "size": "square"})
    assert studio.c.post("/api/images", content=json_job, headers={"Content-Type": "application/json"}).status_code == 413


@pytest.mark.parametrize("code, words", [(429, "очередь роликов полна"), (400, "шлюз отклонил запрос: bad model"),
                                         (503, "карта занята или недоступна")])
def test_video_gateway_refusals_keep_their_code(studio, code, words):
    studio.gw.video_create_reply = (code, {"error": {"message": "bad model" if code == 400 else "full", "type": "x"}})
    add_gateway(studio)
    r = create_video(studio)
    assert r.status_code == code and words in r.json()["detail"]["message"]
    assert studio.c.get("/api/videos").json()["items"] == [] and media_rows() == 0   # исходник не остался


def test_video_without_gateway_connection_is_explained(studio):
    add_connection(studio, kind="srvllm", base_url="http://10.77.1.15:4000", token=TOKEN, sections=["photo"])
    r = create_video(studio)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"
    assert studio.gw.video_calls == []


@pytest.mark.parametrize("bad", ["abc", "0" * 31, "g" * 32, "A" * 32, "0" * 32, "video_0123456789abcdef01234567",
                                 "..%2Fmodels"])
def test_bad_video_id_never_reaches_the_gateway(studio, bad):
    add_gateway(studio)
    for method, suffix in (("get", ""), ("delete", ""), ("post", "/cancel")):
        assert getattr(studio.c, method)(f"/api/videos/{bad}{suffix}").status_code == 404
    assert studio.gw.video_calls == []


def test_gateway_id_of_a_strange_form_is_not_kept(studio):
    studio.gw.video_create_reply = (200, video_object("../models"))
    add_gateway(studio)
    r = create_video(studio)
    assert r.status_code == 502 and studio.c.get("/api/videos").json()["items"] == []
    poll(studio)
    assert len(studio.gw.video_calls) == 1


def _answer_lost(s, error=httpx.ReadTimeout, accepted=True):
    """Ответ на создание до студии не доходит (ревью A2-M8); accepted — задание шлюз всё же принял."""
    async def create(request):
        if request.method != "POST":
            return await s.gw.builtin(request)
        if accepted:
            await s.gw.builtin(request)
        raise error("ответ потерялся", request=request)

    s.gw.on["/slot/videos"] = create


def _foreign(s, seed, quality="standard", prompt=None):
    """Задание в очереди шлюза, которого у студии нет (скрипт или другой клиент того же ключа)."""
    gid = f"video_{len(s.gw.videos) + 900:024x}"
    s.gw.videos[gid] = seed
    s.gw.video_fields[gid] = {"seconds": "5", "quality": quality, "smooth": "true"} | (
        {"prompt": prompt} if prompt else {})
    return gid


def _studio_seed(monkeypatch, seed):
    """Seed, который «выберет» студия, когда пользователь его не задал."""
    monkeypatch.setattr(videos.secrets, "randbelow", lambda _n: seed)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_video_whose_answer_was_lost_is_found_in_the_gateway_list(studio, error):
    add_gateway(studio)
    _answer_lost(studio, error)
    r = create_video(studio, prompt=PROMPT, seconds="7", quality="high")
    assert r.status_code == 201, r.text
    v = r.json()
    gid = v_gateway_id(studio)
    assert state.DB.execute("SELECT gateway_id FROM videos WHERE id = ?", (v["id"],)).fetchone()[0] == gid
    assert [(c["method"], c["path"]) for c in studio.gw.video_calls] == [("POST", "/slot/videos"), ("GET", "/slot/videos")]
    studio.gw.video_status = "completed"
    poll(studio)
    assert item(studio, v["id"])["state"] == "done" and studio.gw.video_deleted == [gid]


def test_lost_answer_adopts_only_its_own_job(studio, monkeypatch):
    """Шлюз задание не принял, а в его списке — seed студии у ролика другой строки, у чужого задания
    другого качества и у чужого с другим промптом: ни одно не своё — 504 со словами о неясном
    исходе, исходник не остаётся, чужое не тронуто. Два подходящих чужих — тоже не своё: не угадываем."""
    _studio_seed(monkeypatch, 7)
    add_gateway(studio)
    first = create_video(studio, prompt=PROMPT).json()
    _foreign(studio, 7, quality="fast", prompt=PROMPT)
    _foreign(studio, 7, prompt="чужой промпт")
    _answer_lost(studio, accepted=False)
    r = create_video(studio, prompt=PROMPT)
    message = r.json()["detail"]["message"]
    assert r.status_code == 504 and "не ответил" in message and "мог начаться" in message
    assert [c["method"] for c in studio.gw.video_calls][-2:] == ["POST", "GET"]
    assert [x["id"] for x in studio.c.get("/api/videos").json()["items"]] == [first["id"]]
    assert media_rows() == 1 and studio.gw.video_deleted == []
    _studio_seed(monkeypatch, 8)
    _foreign(studio, 8), _foreign(studio, 8)
    assert create_video(studio).status_code == 504
    assert media_rows() == 1


@pytest.mark.parametrize("error, code", [(httpx.ReadTimeout, 504), (httpx.RemoteProtocolError, 502)])
def test_lost_answer_with_the_users_seed_adopts_nothing(studio, error, code):
    """Seed задал пользователь — его повторяют скрипты и другие учётки (42 и т. п.): единственное
    подходящее задание в общем списке шлюза своим не доказано (аудит 2026-10-21, P1). Список не
    читается, чужое не скачано и не снято, ответ — неопределённый исход словами."""
    add_gateway(studio)
    gid = _foreign(studio, 42)
    _answer_lost(studio, error, accepted=False)
    r = create_video(studio, seed="42")
    message = r.json()["detail"]["message"]
    assert r.status_code == code and "мог начаться" in message and "без seed" in message
    assert [(q.method, q.url.path) for q in studio.gw.requests] == [("POST", "/slot/videos")]
    assert studio.c.get("/api/videos").json()["items"] == [] and media_rows() == 0
    studio.gw.video_status = "completed"
    poll(studio)
    assert gid in studio.gw.videos and studio.gw.video_deleted == []


def test_unsent_video_is_not_looked_up(studio):
    """Соединения не было — задания у шлюза нет: список не читается, отказ 502."""
    add_gateway(studio)
    studio.gw.down = True
    r = create_video(studio)
    assert r.status_code == 502 and [(q.method, q.url.path) for q in studio.gw.requests] == [("POST", "/slot/videos")]
    studio.gw.down = False
    assert studio.c.get("/api/videos").json()["items"] == [] and media_rows() == 0


def _stopped_while_sending(s, accepted=True):
    """Выкладка посреди создания: --timeout-graceful-shutdown отменяет запрос к шлюзу (CancelledError),
    а шлюз задание уже принял (accepted) или ещё нет."""
    async def create(request):
        if request.method != "POST":
            return await s.gw.builtin(request)
        if accepted:
            await s.gw.builtin(request)
        raise asyncio.CancelledError()

    s.gw.on["/slot/videos"] = create


def _restart(s):
    s.c.__exit__(None, None, None)
    s.gw.on.pop("/slot/videos", None)
    s.clock[0] += 5
    s.c.__enter__()


def test_video_accepted_before_a_shutdown_is_found_after_restart(studio):
    """Находка 2026-11-09: ролик, принятый шлюзом до обрыва службы, генерировался на карте без учёта —
    строки не было. Теперь строка (seed студии) встаёт до запроса, а после перезапуска наблюдатель находит
    задание в списке шлюза и доводит ролик до истории."""
    add_gateway(studio)
    _stopped_while_sending(studio)
    with pytest.raises(BaseException):
        create_video(studio, prompt=PROMPT, seconds="7")
    (row,) = state.DB.execute("SELECT * FROM videos").fetchall()
    assert row["gateway_id"] is None and row["state"] == "queued" and row["detail"] == videos.SENDING
    gid = v_gateway_id(studio)
    _restart(studio)
    poll(studio)
    assert state.DB.execute("SELECT gateway_id FROM videos").fetchone()[0] == gid
    studio.gw.video_status = "completed"
    poll(studio)
    got = item(studio, row["id"])
    assert got["state"] == "done" and got["seed"] == row["seed"] and studio.gw.video_deleted == [gid]
    assert got["prompt"] == PROMPT and got["seconds"] == 7


def test_video_not_accepted_before_a_shutdown_fails_in_words(studio):
    add_gateway(studio)
    foreign = _foreign(studio, 7)                                      # чужое — не трогается
    _stopped_while_sending(studio, accepted=False)
    with pytest.raises(BaseException):
        create_video(studio)
    _restart(studio)
    poll(studio)
    (got,) = studio.c.get("/api/videos").json()["items"]
    assert got["state"] == "failed" and got["detail"].startswith(videos.INTERRUPTED)
    assert foreign in studio.gw.videos and studio.gw.video_deleted == []


def test_a_sending_video_is_left_to_its_request_and_the_users_seed_is_not_written_early(studio):
    """Идущую отправку (моложе SEND_STALE_SEC, после старта службы) наблюдатель не трогает; с seed
    пользователя строки до ответа нет — ему нечем сверить своё задание."""
    add_gateway(studio)
    seen = []

    async def create(request):
        seen.append([tuple(r) for r in state.DB.execute("SELECT state, gateway_id FROM videos")])
        return await studio.gw.builtin(request)

    studio.gw.on["/slot/videos"] = create
    assert create_video(studio).status_code == 201
    assert create_video(studio, seed="42").status_code == 201
    assert seen[0] == [("queued", None)] and len(seen[1]) == 1           # вторая строка — только после ответа
    videos.BOOT = 0.0
    sending = "f" * 32                                                  # отправка этого процесса ещё идёт
    state.DB.execute("INSERT INTO videos(id, user_id, connection_id, gateway_id, source_media_id, prompt, seconds,"
                     " quality, smooth, seed, state, detail, created) SELECT ?, user_id, connection_id, NULL,"
                     " source_media_id, prompt, seconds, quality, smooth, 1, 'queued', ?, ? FROM videos LIMIT 1",
                     (sending, videos.SENDING, T0))
    poll(studio)
    assert [c["path"] for c in studio.gw.video_calls].count("/slot/videos") == 2   # список не читался
    assert item(studio, sending)["state"] == "queued"
    studio.clock[0] = T0 + videos.SEND_STALE_SEC + 1                    # давно — запрос оборвался
    poll(studio)
    assert item(studio, sending)["state"] == "failed"


@pytest.mark.parametrize("gone", [False, True], ids=["cancelled", "deleted"])
def test_video_cancelled_or_deleted_while_sending_is_dropped_at_the_gateway(studio, gone):
    add_gateway(studio)

    async def create(request):
        (vid,) = state.DB.execute("SELECT id FROM videos").fetchone()
        answer = await studio.gw.builtin(request)
        # пока шлюз принимал, ролик отменили или удалили на другой вкладке
        if gone:
            state.DB.execute("DELETE FROM videos WHERE id = ?", (vid,))
        else:
            state.DB.execute("UPDATE videos SET state = 'cancelled', detail = 'отменено' WHERE id = ?", (vid,))
        return answer

    studio.gw.on["/slot/videos"] = create
    r = create_video(studio)
    gid = v_gateway_id(studio)
    assert studio.gw.video_deleted == [gid]
    if gone:
        assert r.status_code == 404
    else:
        assert r.status_code == 201 and r.json()["state"] == "cancelled"
        assert state.DB.execute("SELECT gateway_id FROM videos").fetchone()[0] is None


# ---- наблюдатель -------------------------------------------------------------

def test_watcher_brings_a_finished_video_into_history(studio):
    add_gateway(studio)
    v = create_video(studio).json()
    studio.gw.video_status = "completed"
    poll(studio)
    item_ = item(studio, v["id"])
    assert item_["state"] == "done" and item_["media_id"] and item_["progress"] == 100
    assert studio.gw.video_deleted == [v_gateway_id(studio)]
    r = studio.c.get(f"/api/media/{item_['media_id']}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.headers["content-type"] == "video/mp4" and r.content == MP4[:10]
    download = studio.c.get(f"/api/media/{item_['media_id']}?download=1")
    assert download.content == MP4
    assert download.headers["content-disposition"] == f'attachment; filename="studio-video-{v["seed"]}.mp4"'
    calls = [(c["method"], c["path"]) for c in studio.gw.video_calls]
    gid = v_gateway_id(studio)
    assert calls == [("POST", "/slot/videos"), ("GET", f"/slot/videos/{gid}"), ("GET", f"/slot/videos/{gid}/content"),
                     ("DELETE", f"/slot/videos/{gid}")]                # список шлюза студия не читает
    assert all(c["auth"] == f"Bearer {TOKEN}" for c in studio.gw.video_calls)
    poll(studio)
    assert len(studio.gw.video_calls) == 4                             # готовый больше не опрашивается


def test_progress_and_detail_follow_the_gateway(studio):
    add_gateway(studio)
    v = create_video(studio, prompt="").json()
    studio.gw.video_status = "in_progress"
    studio.gw.video_extra = {"progress": 40, "detail": "генерирую: 40%", "prompt": "she turns around",
                             "prompt_source": "auto", "seed": 7}
    studio.clock[0] = T0 + 30
    poll(studio)
    got = item(studio, v["id"])
    assert (got["state"], got["progress"], got["detail"]) == ("in_progress", 40, "генерирую: 40%")
    assert (got["prompt"], got["prompt_source"], got["seed"], got["elapsed"]) == ("she turns around", "auto", 7, 30)
    studio.gw.video_extra = {"progress": 80, "detail": "генерирую: 80%", "prompt": None}
    poll(studio)
    got = item(studio, v["id"])
    assert got["progress"] == 80 and got["prompt"] == "she turns around"   # пустой ответ промпт не стирает


def test_failed_and_forgotten_jobs_become_failed(studio):
    add_gateway(studio)
    first = create_video(studio).json()
    studio.gw.video_status = "failed"
    studio.gw.video_extra = {"error": {"code": "oom", "message": "не хватило памяти карты"}}
    studio.clock[0] = T0 + 10
    poll(studio)
    got = item(studio, first["id"])
    assert got["state"] == "failed" and "не хватило памяти карты" in got["detail"] and got["media_id"] is None
    assert studio.gw.video_deleted == [v_gateway_id(studio)]           # шлюз не держит неудачный в очереди
    studio.clock[0] = T0 + 50
    assert item(studio, first["id"])["elapsed"] == 10                  # время остановилось на конце
    studio.gw.video_status, studio.gw.video_extra = "queued", {}
    second = create_video(studio).json()
    del studio.gw.videos[v_gateway_id(studio)]                         # перезапуск шлюза забыл задание
    poll(studio)
    got = item(studio, second["id"])
    assert got["state"] == "failed" and got["detail"] == "шлюз забыл задание"
    assert "не хватило памяти карты" in item(studio, first["id"])["detail"]     # законченный не опрашивается


def test_video_gateway_401_keeps_the_session(studio):
    conn = add_connection(studio, kind="srvllm", base_url="http://10.77.1.15:4000", token="revoked-key",
                          name="srvllm")
    r = create_video(studio)
    assert r.status_code == 502 and "«srvllm»" in r.json()["detail"]["message"]
    assert "профиле" in r.json()["detail"]["message"] and media_rows() == 0
    assert studio.c.get("/api/me").status_code == 200
    assert studio.c.patch(f"/api/connections/{conn}", json={"token": TOKEN}).status_code == 200
    v = create_video(studio).json()
    assert studio.c.patch(f"/api/connections/{conn}", json={"token": "revoked-key"}).status_code == 200
    poll(studio)
    got = item(studio, v["id"])
    assert got["state"] == "failed" and "«srvllm»" in got["detail"] and "профиле" in got["detail"]
    assert studio.c.get("/api/me").status_code == 200


def test_watcher_resumes_queued_videos_after_restart(studio):
    add_gateway(studio)
    v = create_video(studio).json()
    studio.c.__exit__(None, None, None)                                # служба остановилась
    studio.c.__enter__()                                               # и запустилась снова
    studio.gw.video_status = "completed"
    poll(studio)
    got = item(studio, v["id"])
    assert got["state"] == "done" and studio.c.get(f"/api/media/{got['media_id']}").content == MP4


def test_lifespan_runs_the_watcher_by_itself(studio, monkeypatch):
    """Вкладка не нужна: наблюдатель службы сам доводит ролик до истории и гасится при остановке."""
    add_gateway(studio)
    v = create_video(studio).json()
    studio.gw.video_status = "completed"
    studio.c.__exit__(None, None, None)
    monkeypatch.setattr(state, "WATCHER", True)
    studio.c.__enter__()
    for _ in range(3000):
        if item(studio, v["id"])["state"] == "done":
            break
    assert item(studio, v["id"])["state"] == "done"
    assert videos.POLL_SEC in studio.sleeps


def test_watcher_survives_a_gateway_outage(studio, caplog):
    """Шлюз не ответил по сети — его подключение ждёт PAUSE_SEC: второй ролик того же подключения
    не ждёт второго таймаута, следующие проходы его не трогают (аудит 2026-10-21, P2)."""
    add_gateway(studio)
    v = create_video(studio, prompt=PROMPT).json()
    other = create_video(studio).json()
    studio.gw.down = True
    sent = len(studio.gw.requests)
    poll(studio)                                                       # исключения наружу нет
    assert item(studio, v["id"]) == v and len(studio.gw.requests) == sent + 1
    studio.gw.down = False
    studio.gw.video_status = "completed"
    studio.clock[0] += videos.PAUSE_SEC - 1
    poll(studio)
    assert len(studio.gw.requests) == sent + 1                         # пауза ещё идёт
    studio.clock[0] += 1
    poll(studio)
    assert item(studio, v["id"])["state"] == "done" and item(studio, other["id"])["state"] == "done"
    assert PROMPT not in caplog.text


def _second_user_gateway(s):
    """Вторая учётка со своим подключением к шлюзу; клиент дальше ходит ею."""
    make_user(s, "guest")
    signin(s, "guest")
    return add_gateway(s)


def _hang_on(s, path):
    """Шлюз молчит на этот путь (до отмены задачи), как недоступный адрес до таймаута."""
    async def hang(request):
        s.gw.hung.append(request.url.path)
        await asyncio.sleep(30)

    s.gw.hung = []
    s.gw.on[path] = hang


def _video_state(video_id):
    return state.DB.execute("SELECT state FROM videos WHERE id = ?", (video_id,)).fetchone()["state"]


def test_a_hung_gateway_does_not_hold_up_another_connections_clip(studio):
    """Подключение, которое не отвечает, держит только свои ролики: готовый ролик другого
    подключения ложится в историю, пока первое висит; висящее проход не начинает второй раз
    (аудит 2026-10-21, P2)."""
    add_gateway(studio)
    stuck = create_video(studio).json()
    _hang_on(studio, f"/slot/videos/{v_gateway_id(studio)}")
    _second_user_gateway(studio)
    ready = create_video(studio).json()
    studio.gw.video_status = "completed"

    async def run():
        tasks = videos.start_pass()
        for _ in range(3000):
            if _video_state(ready["id"]) == "done":
                break
            await asyncio.sleep(0.001)
        again = videos.start_pass()                # висящее подключение в работе — второй задачи нет
        hung = sum(not t.done() for t in tasks)
        for task in tasks + again:
            task.cancel()
        await asyncio.gather(*tasks, *again, return_exceptions=True)
        return len(tasks), again, hung

    started, again, hung = studio.c.portal.call(run)
    assert (started, again, hung) == (2, [], 1)
    assert _video_state(ready["id"]) == "done" and _video_state(stuck["id"]) == "queued"
    assert studio.gw.hung == [f"/slot/videos/{studio.gw.video_ids[0]}"] and videos.WATCHING == {}


def test_watcher_polls_at_most_poll_max_connections_at_once(studio, monkeypatch):
    monkeypatch.setattr(videos, "POLL_MAX", 1)
    add_gateway(studio)
    create_video(studio)
    _second_user_gateway(studio)
    create_video(studio)

    async def run():
        tasks = videos.start_pass()
        await asyncio.gather(*tasks)
        return len(tasks)

    assert studio.c.portal.call(run) == 1


def test_full_watcher_takes_connections_in_turn_and_forgets_old_pauses(studio, monkeypatch):
    """Все места заняты — следующий проход начинает другое подключение, а не снова самое старое;
    истёкшая пауза из PAUSED уходит (ревью)."""
    monkeypatch.setattr(videos, "POLL_MAX", 1)
    monkeypatch.setattr(videos, "_TURNS", itertools.count())
    polled = []

    async def poll_rows(rows):
        polled.append(rows[0]["connection_id"])

    monkeypatch.setattr(videos, "_poll_rows", poll_rows)
    add_gateway(studio)
    create_video(studio)
    _second_user_gateway(studio)
    create_video(studio)
    videos.PAUSED[424242] = studio.clock[0] - 1

    async def run():
        for _ in range(2):
            await asyncio.gather(*videos.start_pass())

    studio.c.portal.call(run)
    assert len(polled) == 2 and polled[0] != polled[1]
    assert 424242 not in videos.PAUSED


def test_stopping_the_watcher_stops_its_polls(studio):
    """Остановка службы гасит наблюдателя вместе с его задачами: ни один опрос не переживает
    закрытия state.HTTP и базы."""
    add_gateway(studio)
    create_video(studio)
    _hang_on(studio, f"/slot/videos/{v_gateway_id(studio)}")

    async def run():
        watcher = videos.start_watcher()
        for _ in range(3000):
            if studio.gw.hung:
                break
            await asyncio.sleep(0.001)
        child, = videos.WATCHING.values()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        return child

    child = studio.c.portal.call(run)
    assert child.cancelled() and videos.WATCHING == {}


def test_slow_download_gives_up_by_the_whole_clip_deadline(studio, env, monkeypatch):
    """Срок на кусок медленную передачу не ограничивает: весь mp4 — за FETCH_SEC, иначе скачанное
    выброшено и строка ждёт следующего прохода (аудит 2026-10-21, P2). Срок без настоящего времени: подменённый
    `clock.TIMEOUT` истекает ровно тогда, когда первый кусок записан, а передача встала."""
    deadlines = []

    def deadline(seconds):
        assert seconds == videos.FETCH_SEC
        deadlines.append(asyncio.timeout(None))
        return deadlines[-1]

    monkeypatch.setattr(clock, "TIMEOUT", deadline)
    add_gateway(studio)
    v = create_video(studio).json()
    before = share_files(env)

    async def trickle():
        yield MP4[:10]
        deadlines[-1].reschedule(asyncio.get_running_loop().time())   # срок истёк посреди ролика
        await asyncio.sleep(30)
        yield MP4[10:]

    async def slow(request):
        return httpx.Response(200, content=trickle(), headers={"content-type": "video/mp4"})

    async def saved():                       # уборка недописанного — в потоке, после отмены записи
        while media.SAVING:
            await asyncio.sleep(0.01)

    path = f"/slot/videos/{v_gateway_id(studio)}/content"
    studio.gw.on[path] = slow
    studio.gw.video_status = "completed"
    poll(studio)
    studio.c.portal.call(saved)
    assert len(deadlines) == 1
    assert item(studio, v["id"])["state"] == "queued" and share_files(env) == before and media_rows() == 1
    assert studio.gw.video_deleted == []
    del studio.gw.on[path]
    poll(studio)
    assert item(studio, v["id"])["state"] == "done"


@pytest.mark.parametrize("change, words", [
    ("UPDATE connections SET base_url = 'http://10.77.1.16:4000'", "подключение «srvllm» изменено — запустите заново"),
    ("DELETE FROM connections", "подключение «srvllm» удалено из профиля")], ids=["changed", "deleted"])
def test_connection_changed_before_the_download_ends_the_clip_in_words(studio, caplog, change, words):
    """Подключение сменили или удалили между опросом и скачиванием: ключ не уходит (connections.token),
    ролик кончается словами, а не «сбоем прохода»."""
    add_gateway(studio)
    v = create_video(studio).json()
    gid = v_gateway_id(studio)

    async def status(request):
        response = await studio.gw.builtin(request)
        state.DB.execute(change)
        return response

    studio.gw.on[f"/slot/videos/{gid}"] = status
    studio.gw.video_status = "completed"
    poll(studio)
    got = state.DB.execute("SELECT state, detail FROM videos WHERE id = ?", (v["id"],)).fetchone()
    assert (got["state"], got["detail"]) == ("failed", words)
    assert [c["path"] for c in studio.gw.video_calls][-1] == f"/slot/videos/{gid}"   # mp4 не запрошен
    assert "сбой прохода" not in caplog.text


def test_watcher_loop_logs_a_failed_pass_and_goes_on(studio, monkeypatch, caplog):
    passes = []

    def flaky():
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError(PROMPT)
        return []

    async def run():
        task = videos.start_watcher()
        while len(passes) < 3:
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    monkeypatch.setattr(videos, "start_pass", flaky)
    task = studio.c.portal.call(run)
    assert task.cancelled() and studio.sleeps[:2] == [videos.POLL_SEC] * 2
    assert "RuntimeError" in caplog.text and PROMPT not in caplog.text


def test_too_big_video_fails_cleanly(studio, env, monkeypatch):
    monkeypatch.setattr(videos, "VIDEO_CONTENT_MAX", len(MP4) - 1)
    add_gateway(studio)
    v = create_video(studio).json()
    before = share_files(env)
    studio.gw.video_status = "completed"
    poll(studio)
    got = item(studio, v["id"])
    assert got["state"] == "failed" and "больше" in got["detail"] and got["media_id"] is None
    assert share_files(env) == before and media_rows() == 1            # только исходник
    assert studio.gw.video_deleted == [v_gateway_id(studio)]


def test_unreachable_share_keeps_the_job_for_the_next_pass(studio, monkeypatch):
    add_gateway(studio)
    v = create_video(studio).json()
    studio.gw.video_status = "completed"
    real = media.save_stream

    async def broken(*args, **kwargs):
        raise media.StorageError()

    monkeypatch.setattr(media, "save_stream", broken)
    poll(studio)
    assert item(studio, v["id"])["state"] == "queued" and studio.gw.video_deleted == []
    monkeypatch.setattr(media, "save_stream", real)
    poll(studio)
    assert item(studio, v["id"])["state"] == "done"


def test_unsaved_clip_says_so_when_the_gateway_forgets_it(studio, monkeypatch):
    """Шара лежит, пока шлюз не забыл ролик: итог — о хранилище, а не «шлюз забыл» (ревью A2-M3)."""
    add_gateway(studio)
    v = create_video(studio).json()
    studio.gw.video_status = "completed"

    async def broken(*args, **kwargs):
        raise media.StorageError()

    monkeypatch.setattr(media, "save_stream", broken)
    for _ in range(2):
        poll(studio)
        got = item(studio, v["id"])
        assert (got["state"], got["detail"]) == ("queued", "не удалось сохранить файл — повторяю")
    del studio.gw.videos[v_gateway_id(studio)]                         # через 30 мин шлюз забыл ролик
    poll(studio)
    got = item(studio, v["id"])
    assert got["state"] == "failed" and got["detail"].startswith("не удалось сохранить файл")


def test_finished_clip_is_kept_before_the_gateway_drops_it(studio):
    """Сначала строка «готово», потом снятие у шлюза: обрыв между ними не теряет ролик (ревью A2-M2)."""
    add_gateway(studio)
    v = create_video(studio).json()
    seen = []

    async def watch(request):
        if request.method == "DELETE":
            seen.append(state.DB.execute("SELECT state FROM videos WHERE id = ?", (v["id"],)).fetchone()["state"])
        return await studio.gw.builtin(request)

    studio.gw.on[f"/slot/videos/{v_gateway_id(studio)}"] = watch
    studio.gw.video_status = "completed"
    poll(studio)
    assert seen == ["done"] and item(studio, v["id"])["state"] == "done"


def test_video_form_is_decoded_off_the_loop(studio, monkeypatch):
    """Форма ролика — до 20 МиБ: разбор не на цикле событий (ревью A2-M4)."""
    calls, real = [], forms.decode_form

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            calls.append(True)
        except RuntimeError:
            calls.append(False)
        return real(*args, **kwargs)

    monkeypatch.setattr(forms, "decode_form", spy)
    add_gateway(studio)
    assert create_video(studio).status_code == 201
    assert calls == [False]


def test_a_source_being_deleted_cannot_start_a_video(studio, monkeypatch):
    """Пока файл фото истории удаляется, ролик с ним — 404; задание у шлюза снимается."""
    add_gateway(studio)
    mid = media.save(owner_id(), normalize_image(PNG_REAL), "image/png", "studio-flux-1.png", thumb=True)
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created)"
                     " VALUES (?, ?, 'generation', 'flux', 'p', '64x48', 1, ?, 'done', 'готово', ?)",
                     ("a" * 32, owner_id(), mid, T0))
    real, answers = media._remove, []

    def racing(media_id, rel_path):               # поток удаления: файл ещё на месте
        answers.append(studio.c.post("/api/videos", data={"prompt": "x"}, files={"source_media_id": (None, mid)}))
        real(media_id, rel_path)

    monkeypatch.setattr(media, "_remove", racing)
    assert studio.c.delete(f"/api/images/{'a' * 32}").status_code == 204
    (r,) = answers
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    assert studio.c.get("/api/videos").json()["items"] == [] and studio.gw.videos == {}


# ---- история и учётки --------------------------------------------------------

def test_video_from_history_image(studio):
    add_gateway(studio)
    stored = normalize_image(PNG_REAL)
    mid = media.save(owner_id(), stored, "image/png", "studio-flux-1.png", thumb=True)
    r = studio.c.post("/api/videos", data={"prompt": "x"}, files={"source_media_id": (None, mid)})
    assert r.status_code == 201, r.text
    assert r.json()["source_media_id"] == mid and media_rows() == 1   # исходник истории не копируется
    assert studio.gw.video_forms[0][1] == stored
    both = studio.c.post("/api/videos", data={"source_media_id": mid}, files=photo())
    assert both.status_code == 400
    unknown = studio.c.post("/api/videos", files={"source_media_id": (None, "0" * 32)})
    assert unknown.status_code == 404 and len(studio.gw.video_forms) == 1


def test_users_see_only_their_videos(studio):
    add_gateway(studio)                                                # owner
    mine = create_video(studio).json()["id"]
    make_user(studio, "friend")
    signin(studio, "friend")
    add_gateway(studio)
    assert studio.c.get("/api/videos").json()["items"] == []
    for method, suffix in (("get", ""), ("post", "/cancel"), ("delete", "")):
        assert getattr(studio.c, method)(f"/api/videos/{mine}{suffix}").status_code == 404
    theirs = create_video(studio).json()["id"]
    assert [x["id"] for x in studio.c.get("/api/videos").json()["items"]] == [theirs]
    signin(studio, "owner")
    assert [x["id"] for x in studio.c.get("/api/videos").json()["items"]] == [mine]
    assert studio.gw.video_deleted == []


def test_cancel_and_delete(studio, env, unlinks):
    add_gateway(studio)
    v = create_video(studio).json()
    gid = v_gateway_id(studio)
    studio.gw.down = True
    r = studio.c.post(f"/api/videos/{v['id']}/cancel")
    assert r.status_code == 502 and item(studio, v["id"])["state"] == "queued"   # не снят — не отменён
    studio.gw.down = False
    r = studio.c.post(f"/api/videos/{v['id']}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert studio.gw.video_deleted == [gid]
    calls = len(studio.gw.video_calls)
    poll(studio)
    assert studio.c.post(f"/api/videos/{v['id']}/cancel").json()["state"] == "cancelled"
    assert len(studio.gw.video_calls) == calls                         # отменённый шлюз больше не видит
    assert studio.c.delete(f"/api/videos/{v['id']}").status_code == 204
    assert studio.c.get(f"/api/videos/{v['id']}").status_code == 404
    # идущий — сначала снимается у шлюза; готовый уносит и mp4
    running = create_video(studio).json()
    assert studio.c.delete(f"/api/videos/{running['id']}").status_code == 204
    assert studio.gw.video_deleted == [gid, v_gateway_id(studio)]
    done = create_video(studio).json()
    studio.gw.video_status = "completed"
    poll(studio)
    assert item(studio, done["id"])["state"] == "done"
    assert studio.c.delete(f"/api/videos/{done['id']}").status_code == 204
    assert share_files(env) == [] and media_rows() == 0
    assert studio.c.get("/api/videos").json()["items"] == []
    assert unlinks and not any(unlinks)                  # файлы удалены не с цикла событий


def test_history_wipe_cancels_running_videos_at_the_gateway(studio, env):
    add_gateway(studio)
    create_video(studio)
    assert studio.c.delete("/api/history").status_code == 200
    assert studio.gw.video_deleted == [v_gateway_id(studio)]
    assert studio.c.get("/api/videos").json()["items"] == [] and share_files(env) == []


def test_clip_landing_during_the_wipe_leaves_no_orphan(studio, env, monkeypatch):
    """Наблюдатель дописал ролик, пока стирание удаляет файлы (шлюз дочитывал уже идущую выдачу
    после DELETE): mp4 не остаётся на шаре без строки (ревью N1)."""
    add_gateway(studio)
    create_video(studio)
    gid = v_gateway_id(studio)
    studio.gw.video_status = "completed"

    async def keep(request):                      # DELETE шлюза не обрывает уже идущую выдачу
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": gid, "deleted": True})
        return await studio.gw.builtin(request)

    studio.gw.on[f"/slot/videos/{gid}"] = keep
    real, polls = media.remove_files, []

    def racing(rows):                             # поток стирания: цикл свободен
        if not polls:
            polls.append(poll(studio))
        return real(rows)

    monkeypatch.setattr(media, "remove_files", racing)
    assert studio.c.delete("/api/history").status_code == 200
    assert polls and media_rows() == 0
    assert state.DB.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    assert share_files(env) == []


def test_feed_pages_by_created(studio):
    add_gateway(studio)
    ids = []
    for i in range(5):
        studio.clock[0] = T0 + i
        ids.append(create_video(studio).json()["id"])
    page = studio.c.get("/api/videos?limit=2").json()["items"]
    assert [x["id"] for x in page] == [ids[4], ids[3]]
    older = studio.c.get(f"/api/videos?limit=2&before={page[-1]['created']}").json()["items"]
    assert [x["id"] for x in older] == [ids[2], ids[1]]
    for bad in (0, 101):
        assert studio.c.get(f"/api/videos?limit={bad}").status_code == 422


def test_video_needs_a_linked_gateway(studio):
    cid = add_gateway(studio)
    studio.c.delete(f"/api/sections/video/links/{cid}")
    r = create_video(studio, prompt=PROMPT)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"
