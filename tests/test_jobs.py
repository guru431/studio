"""«В работе» (app/jobs.py, спека 2026-10-21-studio-jobs-panel): свои идущие и готовые за час по
всем разделам — из базы и памяти модулей, без запросов к шлюзу."""
import logging
from types import SimpleNamespace

import images
import jobs
import ocr
import speeches
import state
import transcripts
import translate
from conftest import T0, add_gateway, make_user, signin

NAME = "Звонок Ивану.m4a"
TEXT = "Добрый вечер, это длинный текст для озвучки, который не войдёт в заголовок целиком"


def me(s, login="owner") -> int:
    uid = make_user(s, login)
    signin(s, login)
    return uid


def image(uid, iid, state_="working", kind="generation", model="lustify", created=T0 - 30, finished=None,
          detail="жду карту или генерирую", prompt="секретный кот"):
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created, finished)"
                     " VALUES (?, ?, ?, ?, ?, '832x1216', 1, ?, ?, ?, ?)",
                     (iid, uid, kind, model, prompt, state_, detail, created, finished))


def video(uid, vid, state_="queued", progress=0, created=T0 - 30, finished=None, detail="в очереди"):
    state.DB.execute("INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth,"
                     " state, progress, detail, created, finished) VALUES (?, ?, 1, 'm', 'секретный ролик', 5,"
                     " 'standard', 1, ?, ?, ?, ?, ?)", (vid, uid, state_, progress, detail, created, finished))


def transcript(uid, tid, state_="working", created=T0 - 30, finished=None, detail="готовлю запись"):
    state.DB.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created, finished)"
                     " VALUES (?, ?, 'whisperx', ?, 'm', ?, ?, ?, ?)", (tid, uid, NAME, state_, detail, created, finished))


def speech(uid, sid, state_="working", created=T0 - 30, finished=None, detail="готовлю"):
    state.DB.execute("INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created, finished)"
                     " VALUES (?, ?, 'qwen3-tts', 'v', ?, ?, ?, ?, ?)", (sid, uid, TEXT, state_, detail, created, finished))


def answer(uid, cid, mid, status="streaming", via="server", title="План отпуска"):
    if state.DB.execute("SELECT 1 FROM chats WHERE id = ?", (cid,)).fetchone() is None:
        state.DB.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, ?, ?, ?)",
                         (cid, uid, title, T0 - 60, T0 - 60))
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, created)"
                     " VALUES (?, ?, 'assistant', '', ?, ?, ?)", (mid, cid, status, via, T0 - 20))


def get(s, since=None):
    r = s.c.get("/api/jobs" + (f"?since={since}" if since is not None else ""))
    assert r.status_code == 200, r.text
    return r.json()


def by_id(items):
    return {i["id"]: i for i in items}


def test_running_jobs_of_every_section(studio):
    uid = me(studio)
    image(uid, "i1")
    image(uid, "i2", state_="cancelling", kind="edit", model="flux", detail="отменяю")
    image(uid, "i3", state_="queued", detail="в очереди серии")
    images.JOBS["i1"] = SimpleNamespace(state="working", detail="жду карту или генерирую", task=None, started=T0 - 30)
    video(uid, "v1")
    video(uid, "v2", state_="in_progress", progress=40, detail="генерирую кадры")
    transcript(uid, "t1")
    transcripts.JOBS["t1"] = SimpleNamespace(stage="распознаю", stage_at=T0 - 65, task=None)
    speech(uid, "s1")
    speeches.JOBS["s1"] = SimpleNamespace(stage="озвучиваю 2 из 5", task=None)
    answer(uid, "c1", "m1")
    out = get(studio)
    assert out["now"] == T0 and out["finished"] == []
    run = by_id(out["running"])
    assert set(run) == {"i1", "i2", "i3", "v1", "v2", "t1", "s1", "m1"}
    assert all(i["state"] == "running" and i["finished"] is None for i in run.values())
    assert run["i1"] == {"section": "photo", "id": "i1", "state": "running", "title": "Картинка · Lustify",
                         "detail": "жду карту или генерирую", "started": T0 - 30, "finished": None,
                         "href": "photo?id=i1"}
    assert run["i2"]["title"] == "Правка · FLUX" and run["i2"]["detail"] == "отменяю"
    assert run["i3"]["detail"] == "в очереди серии"
    assert run["v1"]["title"] == "Ролик · 5 с" and run["v1"]["href"] == "video?id=v1" and run["v1"]["progress"] == 0
    assert run["v2"]["progress"] == 40 and run["v2"]["detail"] == "генерирую кадры"
    assert run["t1"]["title"] == f"Расшифровка «{NAME}»" and run["t1"]["detail"] == "распознаю"   # время одно — у страницы
    assert run["t1"]["href"] == "transcribe?id=t1"
    assert run["s1"]["title"] == "Озвучка · " + TEXT[:40].rstrip() + "…" and run["s1"]["detail"] == "озвучиваю 2 из 5"
    assert run["s1"]["href"] == "speech?id=s1"
    assert run["m1"]["section"] == "chat" and run["m1"]["title"] == "Ответ · План отпуска"
    assert run["m1"]["href"] == "chat/c1"
    assert "секретный" not in str(out)


def test_other_users_jobs_are_not_listed(studio):
    other = make_user(studio, "friend")
    image(other, "i9")
    video(other, "v9", state_="done", finished=T0 - 5)
    transcript(other, "t9")
    speech(other, "s9", state_="done", finished=T0 - 5)
    answer(other, "c9", "m9")
    me(studio)
    assert get(studio) == {"now": T0, "running": [], "finished": [], "push": False}


def test_finished_window_since_and_limit(studio):
    uid = me(studio)
    image(uid, "old", state_="done", detail="готово", finished=T0 - 3601)
    image(uid, "hour", state_="done", detail="готово", finished=T0 - 3500)
    for n in range(60):
        transcript(uid, f"t{n:02d}", state_="done", detail="готово", finished=T0 - 100 + n)
    out = get(studio)
    ids = [i["id"] for i in out["finished"]]
    assert len(ids) == 50 and ids[0] == "t59" and "old" not in ids and "hour" not in ids
    assert [i["finished"] for i in out["finished"]] == sorted((i["finished"] for i in out["finished"]), reverse=True)
    assert [i["id"] for i in get(studio, since=T0 - 42)["finished"]] == [f"t{n}" for n in range(59, 57, -1)]
    # since старше часа окно не расширяет
    assert "old" not in [i["id"] for i in get(studio, since=0)["finished"]]


def test_finished_since_includes_the_boundary(studio):
    """Кончилось в тот же миг, что прошлый опрос (`now`): попадает в следующий — повтор страница отсечёт."""
    uid = me(studio)
    video(uid, "v1", state_="done", finished=T0 - 10)
    assert [i["id"] for i in get(studio, since=T0 - 10)["finished"]] == ["v1"]


def test_states_are_normalized(studio):
    uid = me(studio)
    video(uid, "vf", state_="failed", detail="ролик не получился", finished=T0 - 5)
    image(uid, "ie", state_="error", detail="шлюз недоступен", finished=T0 - 4)
    speech(uid, "sc", state_="cancelled", detail="отменено", finished=T0 - 3)
    transcript(uid, "td", state_="done", detail="готово", finished=T0 - 2)
    answer(uid, "c1", "m1", status="done")
    fin = by_id(get(studio)["finished"])
    assert {k: v["state"] for k, v in fin.items()} == {"vf": "error", "ie": "error", "sc": "cancelled", "td": "done"}
    assert fin["vf"]["detail"] == "ролик не получился" and fin["td"]["finished"] == T0 - 2


def test_chat_only_server_streaming_answers(studio):
    uid = me(studio)
    answer(uid, "c1", "m1", via="browser")
    answer(uid, "c1", "m2", status="stopped")
    answer(uid, "c2", "m3", title=None)
    out = get(studio)
    assert [i["id"] for i in out["running"]] == ["m3"] and out["running"][0]["title"] == "Ответ · новый диалог"


def test_running_picture_via_api_and_no_gateway_call(studio):
    add_gateway(studio)
    studio.gw.hold = True
    job_id = studio.c.post("/api/images", json={"prompt": "секретный кот", "size": "square"}).json()["id"]
    try:
        seen = len(studio.gw.requests)
        out = get(studio)
        assert [i["id"] for i in out["running"]] == [job_id] and out["running"][0]["title"] == "Картинка · Lustify"
        assert len(studio.gw.requests) == seen
    finally:
        studio.gw.hold = False


def test_jobs_need_a_session_and_wait_for_the_lock(studio):
    assert studio.c.get("/api/jobs").status_code == 401
    uid = me(studio)
    state.DB.execute("UPDATE users SET screen_lock = 1 WHERE id = ?", (uid,))
    state.DB.execute("UPDATE sessions SET mobile = 1")
    studio.clock[0] += 3600
    r = studio.c.get("/api/jobs")
    assert r.status_code == 423 and r.json()["detail"]["code"] == "screen_locked"


def test_jobs_log_nothing(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid = me(studio)
    transcript(uid, "t1")
    speech(uid, "s1", state_="done", finished=T0 - 1)
    answer(uid, "c1", "m1")
    get(studio)
    for secret in (NAME, TEXT[:20], "План отпуска"):
        assert secret not in caplog.text


def scan(uid, sid, state_="working", created=T0 - 30, finished=None, detail="готовлю страницы"):
    state.DB.execute("INSERT INTO scans(id, user_id, model, name, origin, pages, state, detail, created, finished)"
                     " VALUES (?, ?, 'hunyuan-ocr', ?, 'section', 3, ?, ?, ?, ?)",
                     (sid, uid, SCAN, state_, detail, created, finished))


SCAN = "Договор аренды.pdf"


def test_jobs_panel_lists_running_and_done_scans(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid = me(studio)
    scan(uid, "o1")
    ocr.JOBS["o1"] = SimpleNamespace(stage="распознаю · стр. 2 из 3", stage_at=T0 - 10)
    scan(uid, "o2", state_="queued", detail="в очереди")
    ocr.JOBS["o2"] = SimpleNamespace(stage="в очереди", stage_at=T0 - 5)
    scan(uid, "o3", state_="cancelling", detail="отменяю")
    ocr.JOBS["o3"] = SimpleNamespace(stage="распознаю · стр. 1 из 3", stage_at=T0 - 5)
    scan(uid, "o4", state_="done", detail="готово", finished=T0 - 2)
    scan(uid, "o5", state_="error", detail="файл документа удалён", finished=T0 - 1)
    scan(make_user(studio, "friend"), "o9")
    out = get(studio)
    run, fin = by_id(out["running"]), by_id(out["finished"])
    assert set(run) == {"o1", "o2", "o3"} and set(fin) == {"o4", "o5"}
    assert run["o1"] == {"section": "ocr", "id": "o1", "state": "running", "title": f"Распознавание «{SCAN}»",
                         "detail": "распознаю · стр. 2 из 3", "started": T0 - 30, "finished": None, "href": "ocr?id=o1"}
    assert run["o2"]["detail"] == "в очереди" and run["o3"]["detail"] == "отменяю"
    assert (fin["o4"]["state"], fin["o5"]["state"], fin["o5"]["detail"]) == ("done", "error", "файл документа удалён")
    assert SCAN not in caplog.text


def translation(uid, tid, state_="working", created=T0 - 30, finished=None, detail="готовлю"):
    state.DB.execute("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name, source, parts,"
                     " state, detail, created, finished) VALUES (?, ?, 'file', 'local-gemma-abl', 'en', 'ru', ?,"
                     " 'секретный исходник', 3, ?, ?, ?, ?)", (tid, uid, SCAN, state_, detail, created, finished))


def test_jobs_list_translations(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid = me(studio)
    translation(uid, "r1")
    translate.JOBS["r1"] = SimpleNamespace(stage="перевожу · 2 из 3", stage_at=T0 - 10)
    translation(uid, "r2", state_="queued", detail="в очереди")
    translation(uid, "r3", state_="done", detail="готово", finished=T0 - 2)
    translation(make_user(studio, "friend"), "r9")
    out = get(studio)
    run, fin = by_id(out["running"]), by_id(out["finished"])
    assert set(run) == {"r1", "r2"} and set(fin) == {"r3"}
    assert run["r1"] == {"section": "translate", "id": "r1", "state": "running", "title": f"Перевод «{SCAN}»",
                         "detail": "перевожу · 2 из 3", "started": T0 - 30, "finished": None,
                         "href": "translate?id=r1"}
    assert run["r2"]["detail"] == "в очереди" and fin["r3"]["state"] == "done"
    assert "секретный" not in str(out) and SCAN not in caplog.text


def test_window_constants():
    assert jobs.JOBS_RECENT_SEC == 3600 and jobs.FINISHED_MAX == 50
