"""Перевод (app/translate.py, спека 2026-10-28-studio-translator-design, 6.3): куски по очереди, две полосы
заданий на пользователя, ожидание карты, обрезанный кусок — половинками, ошибка куска — со своим номером,
«Остановить» и перезапуск с готовыми кусками, «Продолжить» только оставшегося; текст — не в журнал."""
import asyncio
import json
import logging

import httpx

import clock
import images
import media
import sections
import state
import translate
import translate_text
from conftest import add_connection, add_gateway, make_user, signin
from fakes import FakeGateway

MODEL = "local-gemma-abl"


def on_loop(s, fn, *args):
    return s.c.portal.call(fn, *args)


def wait_until(s, cond, limit=3000):
    for _ in range(limit):
        if on_loop(s, cond):
            return
        s.c.portal.call(asyncio.sleep, 0.001)
    raise AssertionError("не дождались")


def row_of(s, tid):
    return on_loop(s, lambda: (r := state.DB.execute("SELECT * FROM translations WHERE id = ?", (tid,)).fetchone())
                   and dict(r))


def wait_row(s, tid, states=translate.TERMINAL):
    wait_until(s, lambda: (r := state.DB.execute("SELECT state FROM translations WHERE id = ?", (tid,)).fetchone())
               is not None and r["state"] in states)
    return row_of(s, tid)


def parts_of(s, tid):
    return on_loop(s, lambda: [dict(r) for r in state.DB.execute(
        "SELECT * FROM translation_parts WHERE translation_id = ? ORDER BY n", (tid,))])


def post(s, text, source="en", target="ru", **extra):
    return s.c.post("/api/translate", json={"text": text, "source_lang": source, "target_lang": target} | extra)


def start(s, text, source="en", target="ru", **extra) -> str:
    r = post(s, text, source, target, **extra)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def owner_of(cid: int) -> int:
    return state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]


def hold_chat(s, when=lambda text: True):
    """Ответ модели не начинается, пока held[0] истинно, — у запросов, чей текст подходит под `when`."""
    held = [True]

    async def chat(request):
        text = json.loads(request.content)["messages"][-1]["content"]
        while held[0] and when(text):
            await asyncio.sleep(0.001)
        return await s.gw.builtin(request)
    s.gw.on["/v1/chat/completions"] = chat
    return held


def streamed(s) -> list[str]:
    """Тексты потоковых запросов к модели (переводы кусков) по порядку."""
    return [b["messages"][-1]["content"] for b in s.gw.chat_requests if b.get("stream")]


THREE = "First one is here.\n\nSecond one is here.\n\nThird one is here."


# ---- перевод ------------------------------------------------------------------------------------

def test_text_is_translated_in_chunks_and_lands_in_history(studio, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    add_gateway(studio)
    studio.gw.chat_reply = ["en"]                                       # определение языка — без потока
    studio.gw.chat_script = [["Пер", "вый."], ["Второй."]]
    r = post(studio, "First one.\r\n\r\nSecond one.", source="auto")
    assert r.status_code == 202 and r.json()["state"] == "queued"
    row = wait_row(studio, r.json()["id"])
    assert (row["state"], row["detected_lang"], row["result_lang"], row["parts"], row["done_parts"]) == ("done", "en", "ru", 2, 2)
    assert row["text"] == "Первый.\n\nВторой." and row["name"] == "First one." and row["kind"] == "text"
    assert row["source"] == "First one.\n\nSecond one." and row["detail"] == translate.DONE
    assert streamed(studio) == ["First one.", "Second one."]
    item = studio.c.get("/api/translate").json()["items"][0]
    assert item["text_head"] == "Первый.\n\nВторой." and item["source_head"] == "First one.\n\nSecond one."
    assert item["can_continue"] is False and "text" not in item and "source" not in item
    assert "First" not in caplog.text and "Первый" not in caplog.text and "Second" not in caplog.text
    assert translate.JOBS == {}


def test_same_language_goes_to_english(studio):
    add_gateway(studio)
    studio.gw.chat_reply = ["ru"]
    studio.gw.chat_script = [["Hello."]]
    row = wait_row(studio, start(studio, "Привет.", source="auto", target="ru"))
    assert (row["state"], row["detected_lang"], row["result_lang"], row["text"]) == ("done", "ru", "en", "Hello.")
    system = studio.gw.chat_requests[-1]["messages"][0]["content"]
    assert system == translate_text.system_prompt("ru", "en") and "into English" in system


def test_explicit_source_skips_detection(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    add_gateway(studio)
    row = wait_row(studio, start(studio, THREE, source="de"))
    assert row["state"] == "done" and row["detected_lang"] is None and row["result_lang"] == "ru"
    assert len(studio.gw.chat_requests) == 3 and all(b["stream"] for b in studio.gw.chat_requests)
    assert studio.gw.chat_requests[0]["messages"][0]["content"] == translate_text.system_prompt("de", "ru")


def test_local_model_frees_image_models_once(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    add_gateway(studio)
    wait_row(studio, start(studio, THREE))
    assert [m for m, _ in studio.gw.releases] == list(images.MODELS.values())
    sent = [r.url.path for r in studio.gw.requests]
    assert sent.index("/v1/chat/completions") > max(i for i, p in enumerate(sent) if p == "/slot/images/release")

    studio.gw.releases.clear()
    cloud = add_connection(studio, kind="openai", base_url="http://10.77.1.15:4000/v1", token=FakeGateway.TOKEN,
                           sections=["translate"], name="cloud")
    sections.set_models(owner_of(cloud), "translate", cloud, ["gpt-5"])
    row = wait_row(studio, start(studio, THREE, connection_id=cloud, model="gpt-5"))
    assert row["state"] == "done" and row["model"] == "gpt-5" and studio.gw.releases == []


def test_cut_chunk_is_retried_in_halves(studio):
    add_gateway(studio)
    studio.gw.chat_script = [["Обры"], ["Первое."], ["Второе."]]
    calls = []

    async def chat(request):
        calls.append(json.loads(request.content)["messages"][-1]["content"])
        studio.gw.chat_ending = "length" if len(calls) == 1 else "stop"
        return await studio.gw.builtin(request)
    studio.gw.on["/v1/chat/completions"] = chat
    row = wait_row(studio, start(studio, "First sentence. Second sentence."))
    assert calls == ["First sentence. Second sentence.", "First sentence.", "Second sentence."]
    assert (row["state"], row["parts"], row["done_parts"], row["text"]) == ("done", 1, 1, "Первое. Второе.")


def test_cut_half_is_an_error(studio):
    add_gateway(studio)
    studio.gw.chat_ending = "length"
    row = wait_row(studio, start(studio, "First sentence. Second sentence."))
    assert row["state"] == "error" and row["detail"] == "кусок 1 из 1: ответ модели обрезан"
    assert len(studio.gw.chat_requests) == 2                            # кусок и первая половина


def failing_second(s):
    """Перевод второго куска THREE — 400, остальные запросы — встроенный ответ."""
    async def chat(request):
        if json.loads(request.content)["messages"][-1]["content"] == "Second one is here.":
            return httpx.Response(400, json={"error": {"message": "плохой запрос"}})
        return await s.gw.builtin(request)
    s.gw.on["/v1/chat/completions"] = chat


def test_failed_chunk_stops_with_its_number_and_keeps_done_parts(studio, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    add_gateway(studio)
    studio.gw.chat_script = [["Первый."], ["Третий."]]
    failing_second(studio)
    tid = start(studio, THREE)
    row = wait_row(studio, tid)
    assert row["state"] == "error" and row["detail"].startswith("кусок 2 из 3: шлюз отклонил запрос")
    assert row["text"] == "Первый." and row["done_parts"] == 1
    assert [(p["n"], p["state"]) for p in parts_of(studio, tid)] == [(1, "done"), (2, "failed"), (3, "waiting")]
    out = studio.c.get(f"/api/translate/{tid}").json()
    assert out["can_continue"] is True and out["text_head"] == "Первый."
    assert "плохой запрос" not in caplog.text and "Second" not in caplog.text
    assert f"перевод {tid[:8]}: ошибка TranslateError" in caplog.text


def test_continue_translates_only_the_rest(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    add_gateway(studio)
    studio.gw.chat_reply = ["en"]
    studio.gw.chat_script = [["Первый."], ["Второй."], ["Третий."]]
    failing_second(studio)
    tid = start(studio, THREE, source="auto")
    assert wait_row(studio, tid)["state"] == "error"
    studio.gw.on.clear()
    studio.gw.chat_requests.clear()
    r = studio.c.post(f"/api/translate/{tid}/continue")
    assert r.status_code == 200 and r.json()["state"] == "queued" and r.json()["finished"] is None
    row = wait_row(studio, tid)
    assert streamed(studio) == ["Second one is here.", "Third one is here."]
    assert len(studio.gw.chat_requests) == 2                            # язык не определяется заново
    assert (row["state"], row["detected_lang"], row["done_parts"]) == ("done", "en", 3)
    assert row["text"] == "Первый.\n\nВторой.\n\nТретий."
    assert [p["detail"] for p in parts_of(studio, tid)] == [None, None, None]
    r = studio.c.post(f"/api/translate/{tid}/continue")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nothing"


def test_continue_refuses_when_the_model_is_gone(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    cid = add_gateway(studio)
    failing_second(studio)
    tid = start(studio, THREE)
    before = wait_row(studio, tid)
    sections.set_models(owner_of(cid), "translate", cid, ["gemma4-abl"])
    r = studio.c.post(f"/api/translate/{tid}/continue")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "model"
    assert row_of(studio, tid) == before and tid not in translate.JOBS


def test_busy_gateway_waits_and_says_so(studio, monkeypatch):
    add_gateway(studio)
    calls, gate = [], [True]

    async def chat(request):
        calls.append(request)
        if len(calls) <= 2:
            return httpx.Response(503, json={"error": {"message": "карта занята"}})
        return await studio.gw.builtin(request)
    studio.gw.on["/v1/chat/completions"] = chat
    fast = clock.SLEEP

    async def gated(sec):
        await fast(sec)
        while gate[0]:
            await asyncio.sleep(0.001)
    monkeypatch.setattr(clock, "SLEEP", gated)
    tid = start(studio, "Hello.")
    wait_until(studio, lambda: len(studio.sleeps) == 1)
    out = studio.c.get(f"/api/translate/{tid}").json()
    assert out["state"] == "working" and out["detail"] == "жду карту · 0:15"
    gate[0] = False
    row = wait_row(studio, tid)
    assert row["state"] == "done" and studio.sleeps == [15, 15] and row["text"] == "Здравствуйте!"


def test_busy_gateway_gives_up_after_the_limit(studio, monkeypatch):
    assert translate.WAIT_MAX_SEC == 1800 and translate.RETRY_SEC == 15
    monkeypatch.setattr(translate, "WAIT_MAX_SEC", 120)                 # 120 пауз по 15 с — дольше секунды
    add_gateway(studio)
    studio.gw.chat_error = (503, {"error": {"message": "карта занята"}})
    row = wait_row(studio, start(studio, "Hello."))
    assert row["state"] == "error" and row["detail"] == "кусок 1 из 1: карта занята дольше 2 минут — попробуйте позже"
    assert studio.sleeps == [15] * 8


# ---- полосы, остановка, перезапуск ---------------------------------------------------------------------

def file_job(s, uid, conn, text="Contract text.", kick=True, **extra):
    async def go():
        tid = translate.create(uid, conn, MODEL, kind="file", name="Договор.pdf", source_lang="en", target_lang="ru",
                               source=text, **extra)
        if kick:
            translate.kick(uid, "file")
        return tid
    return on_loop(s, go)


def gateway_conn(s):
    cid = add_gateway(s)
    uid = owner_of(cid)
    return uid, on_loop(s, lambda: sections.resolve(uid, "translate", cid, MODEL)[0])


def test_text_lane_does_not_wait_for_the_file_lane(studio):
    uid, conn = gateway_conn(studio)
    held = hold_chat(studio, when=lambda text: text == "Contract text.")
    fid = file_job(studio, uid, conn)
    wait_until(studio, lambda: len(studio.gw.requests) and studio.gw.requests[-1].url.path == "/v1/chat/completions")
    tid = start(studio, "Hello.")
    assert wait_row(studio, tid)["state"] == "done"
    assert row_of(studio, fid)["state"] == "working"
    held[0] = False
    assert wait_row(studio, fid)["state"] == "done"


def test_lane_queue_is_capped(studio):
    uid, conn = gateway_conn(studio)
    held = hold_chat(studio)
    first = start(studio, "Hello.")
    wait_row(studio, first, ("working",))
    waiting = [start(studio, f"Hello {n}.") for n in range(translate.QUEUE_MAX)]
    r = post(studio, "One more.")
    assert r.status_code == 409 and r.json()["detail"] == {
        "code": "busy", "message": "в очереди уже 5 переводов — дождитесь их или остановите"}
    fid = file_job(studio, uid, conn, kick=False)                       # файловая полоса принимает
    assert row_of(studio, fid)["state"] == "queued"
    held[0] = False
    for tid in (first, *waiting):
        assert wait_row(studio, tid)["state"] == "done"


def test_stop_keeps_done_parts_and_restart_interrupts(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    uid, conn = gateway_conn(studio)
    studio.gw.chat_script = [["Первый."], ["Второй."]]
    held = hold_chat(studio, when=lambda text: text == "Second one is here.")
    tid = start(studio, THREE)
    wait_until(studio, lambda: state.DB.execute("SELECT done_parts FROM translations WHERE id = ?",
                                                (tid,)).fetchone()[0] == 1)
    r = studio.c.post(f"/api/translate/{tid}/cancel")
    assert r.json() == {"state": "cancelling"}
    assert studio.c.get(f"/api/translate/{tid}").json()["detail"] == translate.CANCELLING
    held[0] = False
    row = wait_row(studio, tid)
    assert (row["state"], row["detail"], row["text"], row["done_parts"]) == ("cancelled", translate.CANCELLED, "Первый.", 1)
    assert studio.c.get(f"/api/translate/{tid}").json()["can_continue"] is True

    queued = file_job(studio, uid, conn, kick=False)
    assert studio.c.post(f"/api/translate/{queued}/cancel").json() == {"state": "cancelled"}
    other = file_job(studio, uid, conn, kick=False)
    on_loop(studio, translate.recover)
    row = row_of(studio, other)
    assert (row["state"], row["detail"]) == ("error", translate.INTERRUPTED) and row["finished"] is not None
    assert row_of(studio, tid)["state"] == "cancelled"


def test_stop_user_cancels_both_lanes(studio):
    uid, conn = gateway_conn(studio)
    held = hold_chat(studio)
    running = start(studio, "Hello.")
    wait_row(studio, running, ("working",))
    waiting = start(studio, "Hello again.")
    filed = file_job(studio, uid, conn)
    on_loop(studio, translate.stop_user, uid)
    assert [row_of(studio, t)["state"] for t in (running, waiting, filed)] == ["cancelled"] * 3
    assert translate.RUNNERS == {} and translate.JOBS == {}
    held[0] = False


# ---- API -------------------------------------------------------------------------------------------

def test_input_is_checked_before_a_job(studio):
    make_user(studio, "owner")
    signin(studio, "owner")

    def code(r, status):
        assert r.status_code == status, r.text
        return r.json()["detail"]

    assert code(post(studio, "Hello."), 409)["code"] == "no_model"
    add_gateway(studio)
    assert code(post(studio, "  \r\n "), 422) == {"code": "empty", "message": "введите текст"}
    assert code(post(studio, "a" * 300_001), 422) == {
        "code": "too_long", "message": "текст длиннее 300 000 знаков — разделите его"}
    hold_chat(studio)                                                   # длинный текст — не переводить на деле
    assert post(studio, "a\r\n" * 150_000).status_code == 202          # переносы Windows не считаются дважды
    assert code(post(studio, "Hello.", target="xx"), 422)["code"] == "lang"
    assert code(post(studio, "Hello.", target="auto"), 422)["code"] == "lang"
    assert code(post(studio, "Hello.", source="xx"), 422)["code"] == "lang"
    assert code(post(studio, "Hello.", model="gpt-5"), 422)["code"] == "model"
    assert studio.c.get("/api/translate/languages").json() == {
        "items": translate_text.languages(), "auto": "auto", "default_target": "ru"}


def test_foreign_translation_is_gone(studio):
    add_gateway(studio)
    tid = start(studio, "Hello.")
    wait_row(studio, tid)
    make_user(studio, "friend")
    signin(studio, "friend")
    assert studio.c.get("/api/translate").json() == {"items": []}
    for r in (studio.c.get(f"/api/translate/{tid}"), studio.c.post(f"/api/translate/{tid}/cancel"),
              studio.c.post(f"/api/translate/{tid}/continue"), studio.c.delete(f"/api/translate/{tid}")):
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    assert row_of(studio, tid)["state"] == "done"


def test_delete_running_is_refused(studio):
    add_gateway(studio)
    held = hold_chat(studio)
    tid = start(studio, "Hello.")
    wait_row(studio, tid, ("working",))
    r = studio.c.delete(f"/api/translate/{tid}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    studio.c.post(f"/api/translate/{tid}/cancel")
    held[0] = False
    wait_row(studio, tid)
    assert studio.c.delete(f"/api/translate/{tid}").status_code == 204
    assert row_of(studio, tid) is None and parts_of(studio, tid) == []
    assert studio.c.get(f"/api/translate/{tid}").status_code == 404


def test_delete_removes_a_source_file_nothing_else_uses(studio):
    uid, conn = gateway_conn(studio)
    mid = on_loop(studio, media.save_async, uid, b"%PDF-1.4 test", "application/pdf", "Договор.pdf")
    assert not on_loop(studio, media.referenced, uid, mid)
    tid = file_job(studio, uid, conn, kick=False, source_media_id=mid)
    assert on_loop(studio, media.referenced, uid, mid)                 # только запись перевода
    studio.c.post(f"/api/translate/{tid}/cancel")
    assert studio.c.get(f"/api/translate/{tid}").json()["media_id"] == mid
    assert studio.c.delete(f"/api/translate/{tid}").status_code == 204
    assert not on_loop(studio, media.referenced, uid, mid) and not on_loop(studio, media.alive, uid, mid)


def test_erasing_history_stops_and_removes_translations(studio):
    add_gateway(studio)
    held = hold_chat(studio)
    tid = start(studio, "Hello.")
    wait_row(studio, tid, ("working",))
    r = studio.c.delete("/api/history")
    assert r.status_code == 200, r.text
    assert row_of(studio, tid) is None and parts_of(studio, tid) == [] and translate.RUNNERS == {}
    held[0] = False


def test_live_text_is_visible_while_a_chunk_streams(studio):
    add_gateway(studio)
    studio.gw.chat_script = [["Пер", "вый."]]
    studio.gw.chat_hold = True
    tid = start(studio, "First.")
    wait_until(studio, lambda: tid in translate.JOBS and translate.JOBS[tid].live == "Пер")
    out = studio.c.get(f"/api/translate/{tid}").json()
    assert out["live"] == "Пер" and out["detail"] == "перевожу · 1 из 1" and out["parts"][0]["state"] == "waiting"
    studio.gw.chat_hold = False
    assert wait_row(studio, tid)["text"] == "Первый."
    assert studio.c.get(f"/api/translate/{tid}").json()["live"] == ""


def test_parts_are_incremental(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    add_gateway(studio)
    studio.gw.chat_script = [["Первый."], ["Второй."]]
    tid = start(studio, "First one.\n\nSecond one.")
    wait_row(studio, tid)
    full = studio.c.get(f"/api/translate/{tid}").json()
    assert full["source"] == "First one.\n\nSecond one." and [p["n"] for p in full["parts"]] == [1, 2]
    assert full["parts"][0] == {"n": 1, "source": "First one.", "sep": "\n\n", "box": None, "text": "Первый.",
                                "state": "done", "detail": None}
    rest = studio.c.get(f"/api/translate/{tid}?from=1").json()
    assert "source" not in rest and [p["n"] for p in rest["parts"]] == [2]
    assert rest["connection_id"] is not None and rest["media_id"] is None and rest["scan_id"] is None


def test_blocks_become_parts_with_boxes(studio):
    uid, conn = gateway_conn(studio)
    blocks = [{"source": "Exit", "box": [93, 146, 491, 214]}, {"source": "Staff only", "box": [193, 640, 379, 706]}]
    tid = on_loop(studio, lambda: translate.create(uid, conn, MODEL, kind="photo", name="Снимок.jpg", source_lang="en",
                                                   target_lang="ru", source="Exit\nStaff only", blocks=blocks))
    parts = studio.c.get(f"/api/translate/{tid}").json()["parts"]
    assert [(p["source"], p["sep"], p["box"]) for p in parts] == [
        ("Exit", "\n", [93, 146, 491, 214]), ("Staff only", "\n", [193, 640, 379, 706])]
    assert translate.lane_of("photo") == translate.lane_of("file") == "file" and translate.lane_of("text") == "text"
