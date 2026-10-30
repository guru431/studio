"""Перевод (app/translate.py, спека 2026-10-28-studio-translator-design, 6.3): куски по очереди, две полосы
заданий на пользователя, ожидание карты, обрезанный кусок — половинками, ошибка куска — со своим номером,
«Остановить» и перезапуск с готовыми кусками, «Продолжить» только оставшегося; текст — не в журнал."""
import asyncio
import io
import json
import logging
import zipfile
from xml.etree import ElementTree

import httpx
import pytest

import clock
import documents
import images
import media
import ocr
import ocr_render
import sections
import state
import translate
import translate_text
from conftest import add_connection, add_gateway, make_user, signin
from fakes import FakeGateway
from scans import scan_pdf
from test_documents import docx, pdf

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
    assert item["text_head"] == "Первый." and item["source_head"] == "First one."       # начала — первого куска
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


# ---- файлы и сканы (спека 6.4, 6.5), повтор, выгрузка ----------------------------------------------

@pytest.fixture
def small_pages(monkeypatch):
    """Рендер скана мелкий — быстрый; предел 2200 сверяет test_ocr_render."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 300)


def send_file(s, name, data, source="en", target="ru", **headers):
    return s.c.post("/api/translate/files", files={"file": (name, data, "application/octet-stream")},
                    headers={"X-Studio-Source-Lang": source, "X-Studio-Target-Lang": target} | headers)


def start_file(s, name, data, **extra) -> str:
    r = send_file(s, name, data, **extra)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def count(s, table) -> int:
    return on_loop(s, lambda: state.DB.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def scan_row(s, sid):
    return on_loop(s, lambda: (r := state.DB.execute("SELECT * FROM scans WHERE id = ?", (sid,)).fetchone()) and dict(r))


def owner_of_tr(tid) -> int:
    return state.DB.execute("SELECT user_id FROM translations WHERE id = ?", (tid,)).fetchone()[0]


def paths(s) -> list:
    return [r.url.path for r in s.gw.requests]


def test_docx_and_pdf_with_a_layer_are_translated_from_their_text(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    tid = start_file(studio, "Записка.docx", docx(["Первый", " абзац"], ["Второй"]))
    row = wait_row(studio, tid)
    assert (row["state"], row["kind"], row["name"], row["source"]) == ("done", "file", "Записка.docx", "Первый абзац\nВторой")
    assert row["scan_id"] is None and row["text"] == "Здравствуйте!"
    uid = on_loop(studio, owner_of_tr, tid)
    assert on_loop(studio, lambda: media.path(uid, row["source_media_id"]).read_bytes()) == docx(
        ["Первый", " абзац"], ["Второй"])
    pid = start_file(studio, "Договор.pdf", pdf("Payment is due in 30 days", "Second page"))
    row = wait_row(studio, pid)
    assert (row["state"], row["name"], row["source"]) == ("done", "Договор.pdf", "Payment is due in 30 days\n\nSecond page")
    assert count(studio, "scans") == 0 and studio.gw.ocr_requests == []
    assert "Записка" not in caplog.text and "Договор" not in caplog.text and "Payment" not in caplog.text
    r = send_file(studio, "фото.jpg", b"\xff\xd8\xff")
    assert r.status_code == 415 and r.json()["detail"] == {
        "code": "format", "message": "нужен PDF, DOCX, TXT, MD или снимок PNG, JPEG, WebP"}
    r = send_file(studio, "Записка.txt", b"Hi", target="xx")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "lang"
    assert count(studio, "media") == 2


def slow_ocr_release(s):
    """Первый release слота ocr — 409 (slot-gate ещё считает чужую страницу): раннер «Сканов» повторит через
    RETRY_SEC, и всё это время в студии идёт освобождение карты."""
    busy = [True]

    async def release(request):
        if busy:
            busy.pop()
            s.gw.releases.append((None, request.headers.get("authorization")))
            return httpx.Response(409, json={"error": {"message": "slot_busy"}})
        return await s.gw.builtin(request)
    s.gw.on["/pages/ocr-release"] = release


def test_scanned_pdf_is_recognized_then_translated(studio, small_pages):
    """…и модель перевода зовётся только после ocr.idle: release слота ocr (первый — 409 и повтор) — раньше
    первого запроса к модели, иначе gemma-abl ждала бы карту под арендой ocr."""
    add_gateway(studio)
    slow_ocr_release(studio)
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink", "This page has a text layer long enough to be read as it is."))
    row = wait_row(studio, tid)
    assert row["state"] == "done", row["detail"]
    scan = scan_row(studio, row["scan_id"])
    assert (scan["origin"], scan["state"], scan["name"], scan["pages"]) == ("section", "done", "Скан.pdf", 2)
    assert scan["source_media_id"] == row["source_media_id"]
    assert row["source"] == on_loop(studio, ocr.attachment_text, row["scan_id"])
    assert row["source"].startswith("Страница 1\n\n# Договор") and row["parts"] >= 1 and row["text"]
    sent = paths(studio)
    model_at = sent.index("/v1/chat/completions")
    assert sent.count("/pages/ocr") == 1 and sent[:model_at].count("/pages/ocr-release") == 2
    assert ocr.RETRY_SEC in studio.sleeps


def test_scan_progress_is_the_stage_of_the_translation(studio, small_pages):
    add_gateway(studio)
    studio.gw.ocr_hold = True
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    wait_until(studio, lambda: state.DB.execute("SELECT detail FROM translations WHERE id = ?",
                                                (tid,)).fetchone()[0] == "распознаю · стр. 1 из 1")
    assert studio.c.get(f"/api/translate/{tid}").json()["detail"] == "распознаю · стр. 1 из 1"
    studio.gw.ocr_hold = False
    assert wait_row(studio, tid)["state"] == "done"


def test_scan_deleted_while_waiting_fails_the_translation(studio, small_pages):
    add_gateway(studio)
    studio.gw.ocr_hold = True
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    sid = row_of(studio, tid)["scan_id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    assert studio.c.post(f"/api/ocr/{sid}/cancel").json() == {"state": "cancelling"}
    studio.gw.ocr_hold = False
    row = wait_row(studio, tid)
    assert (row["state"], row["detail"], row["source"]) == ("error", "скан не распознан: отменено", None)
    assert studio.gw.chat_requests == []

    studio.gw.ocr_hold = True
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    sid = row_of(studio, tid)["scan_id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 2)
    on_loop(studio, lambda: state.DB.execute("DELETE FROM scans WHERE id = ?", (sid,)))
    row = wait_row(studio, tid)
    assert (row["state"], row["detail"]) == ("error", "скан не распознан: документ удалён")
    studio.gw.ocr_hold = False
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert studio.gw.chat_requests == []


def test_stopping_a_translation_cancels_its_scan(studio, small_pages):
    add_gateway(studio)
    studio.gw.ocr_hold = True
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    sid = row_of(studio, tid)["scan_id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    assert studio.c.post(f"/api/translate/{tid}/cancel").json() == {"state": "cancelling"}
    row = wait_row(studio, tid)
    assert (row["state"], row["detail"]) == ("cancelled", translate.CANCELLED)
    assert scan_row(studio, sid)["state"] == "cancelling"
    studio.gw.ocr_hold = False
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans WHERE id = ?", (sid,)).fetchone()[0]
               == "cancelled")
    assert studio.gw.chat_requests == []


def test_scanned_pdf_without_ocr_is_refused_with_words(studio):
    gw = add_gateway(studio)
    assert studio.c.delete(f"/api/sections/ocr/links/{gw}").status_code == 204
    r = send_file(studio, "Скан.pdf", scan_pdf("ink"))
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "scan", "message": "это скан — подключите «Сканы» в настройках"}
    assert (count(studio, "media"), count(studio, "translations"), count(studio, "scans")) == (0, 0, 0)


def test_partly_scanned_pdf_without_ocr_is_refused_too(studio):
    """Ревью I2: без «Распознавания» страницы-сканы пропали бы из перевода молча."""
    gw = add_gateway(studio)
    assert studio.c.delete(f"/api/sections/ocr/links/{gw}").status_code == 204
    r = send_file(studio, "Договор.pdf", scan_pdf("This page has a text layer long enough to be read as it is.", "ink"))
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "scan", "message": (
        "страниц-сканов в PDF — 1 из 2, без распознавания перевод вышел бы без них"
        " — подключите «Сканы» в настройках")}
    assert (count(studio, "media"), count(studio, "translations"), count(studio, "scans")) == (0, 0, 0)
    assert studio.gw.chat_requests == []
    r = send_file(studio, "Договор.pdf", scan_pdf("empty", "ink", "This page has a text layer long enough to be read."))
    assert r.status_code == 422 and r.json()["detail"]["message"].startswith("страниц-сканов в PDF — 1 из 3")


def test_text_pdf_with_an_empty_page_without_ocr_is_translated_from_its_layer(studio):
    """Пустая страница без картинки (выгрузка из Word) — не скан: без «Распознавания» её пропускают молча."""
    gw = add_gateway(studio)
    assert studio.c.delete(f"/api/sections/ocr/links/{gw}").status_code == 204
    row = wait_row(studio, start_file(studio, "Договор.pdf", scan_pdf("Payment is due in 30 days", "empty",
                                                                      "Second page")))
    assert (row["state"], row["source"], row["scan_id"]) == ("done", "Payment is due in 30 days\n\nSecond page", None)
    assert count(studio, "scans") == 0 and studio.gw.ocr_requests == []


def test_too_long_document_is_refused_before_a_job(studio, monkeypatch):
    monkeypatch.setattr(translate_text, "TEXT_MAX", 50)
    add_gateway(studio)
    r = send_file(studio, "Записка.txt", ("a" * 60).encode())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_long"
    monkeypatch.setattr(documents, "TEXT_MAX_CHARS", 10)               # предел извлечения — слова перевода
    for name, data in (("Договор.pdf", pdf("Payment is due in 30 days")), ("Записка.txt", b"a" * 20)):
        r = send_file(studio, name, data)
        assert r.status_code == 422 and r.json()["detail"] == {"code": "too_long", "message": translate.TOO_LONG}
    r = send_file(studio, "Пусто.txt", b" \r\n ")
    assert r.status_code == 422 and r.json()["detail"] == {"code": "empty", "message": "в файле нет текста"}
    assert (count(studio, "media"), count(studio, "translations")) == (0, 0)


def test_full_file_lane_drops_the_saved_file(studio):
    uid, conn = gateway_conn(studio)
    for _ in range(translate.QUEUE_MAX):
        file_job(studio, uid, conn, kick=False)
    r = send_file(studio, "Записка.txt", b"Hello.")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert count(studio, "media") == 0


def test_repeat_reuses_the_stored_file(studio):
    add_gateway(studio)
    tid = start_file(studio, "Записка.docx", docx(["First."], ["Second."]))
    first = wait_row(studio, tid)
    files = count(studio, "media")
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "de"})
    assert r.status_code == 202, r.text
    assert r.json()["id"] != tid and r.json()["state"] == "queued" and r.json()["name"] == "Записка.docx"
    row = wait_row(studio, r.json()["id"])
    assert (row["state"], row["kind"], row["result_lang"]) == ("done", "file", "de")
    assert (row["source_media_id"], row["source"]) == (first["source_media_id"], first["source"])
    assert count(studio, "media") == files and row_of(studio, tid) == first
    assert studio.gw.chat_requests[-1]["messages"][0]["content"] == translate_text.system_prompt("en", "de")

    text = wait_row(studio, start(studio, "Hello."))
    r = studio.c.post(f"/api/translate/{text['id']}/repeat", json={"source_lang": "en", "target_lang": "fr"})
    row = wait_row(studio, r.json()["id"])
    assert (row["kind"], row["source"], row["result_lang"], row["source_media_id"]) == ("text", "Hello.", "fr", None)
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "xx"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "lang"


def test_repeat_of_a_failed_scan_starts_a_new_scan(studio, small_pages):
    add_gateway(studio)
    studio.gw.ocr_replies = [(400, {"error": {"message": "плохая страница"}})]
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    first = wait_row(studio, tid)
    assert first["state"] == "error" and first["detail"].startswith("скан не распознан: ")
    assert studio.c.get(f"/api/translate/{tid}").json()["can_continue"] is False
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "ru"})
    assert r.status_code == 202, r.text
    row = wait_row(studio, r.json()["id"])
    assert row["state"] == "done" and row["scan_id"] not in (None, first["scan_id"])
    assert row["source_media_id"] == first["source_media_id"] == scan_row(studio, row["scan_id"])["source_media_id"]
    assert count(studio, "scans") == 2

    uid = on_loop(studio, owner_of_tr, tid)
    wait_until(studio, lambda: not ocr.RUNNERS)
    on_loop(studio, media.delete_async, uid, first["source_media_id"])
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "ru"})
    assert r.status_code == 409 and r.json()["detail"] == {"code": "gone", "message": "исходный файл удалён"}


def test_scan_without_a_recognized_page_fails_with_words(studio, small_pages):
    """Ревью M11: скан «готов», но ни одна страница не распознана — не перевод пометок, а ошибка словами."""
    add_gateway(studio)
    studio.gw.ocr_replies = [(200, {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]})]
    row = wait_row(studio, start_file(studio, "Скан.pdf", scan_pdf("ink")))
    assert scan_row(studio, row["scan_id"])["state"] == "done"
    assert (row["state"], row["detail"], row["source"]) == ("error", "скан не распознан: на странице не найден текст", None)
    assert studio.gw.chat_requests == []


def test_repeat_reuses_a_recognized_scan(studio, small_pages, monkeypatch):
    """Ревью M12: перевод не дошёл до кусков (здесь — длиннее предела), скан готов — повтор берёт его текст, без
    нового документа «Сканов» и без распознавания."""
    add_gateway(studio)
    limit = translate_text.TEXT_MAX
    monkeypatch.setattr(translate_text, "TEXT_MAX", 10)
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    first = wait_row(studio, tid)
    assert (first["state"], first["detail"], first["source"]) == ("error", translate.TOO_LONG, None)
    wait_until(studio, lambda: not ocr.RUNNERS)
    body = {"source_lang": "en", "target_lang": "ru"}
    r = studio.c.post(f"/api/translate/{tid}/repeat", json=body)
    assert r.status_code == 422 and r.json()["detail"] == {"code": "too_long", "message": translate.TOO_LONG}
    monkeypatch.setattr(translate_text, "TEXT_MAX", limit)
    r = studio.c.post(f"/api/translate/{tid}/repeat", json=body)
    assert r.status_code == 202, r.text
    row = wait_row(studio, r.json()["id"])
    assert row["state"] == "done" and row["scan_id"] == first["scan_id"]
    assert row["source"] == translate_text.normalize(on_loop(studio, ocr.attachment_text, first["scan_id"]))
    assert count(studio, "scans") == 1 and len(studio.gw.ocr_requests) == 1


def test_continue_with_a_deleted_connection_says_so(studio, monkeypatch):
    """Ревью M14: не 404 gone — страница приняла бы его за удалённую запись."""
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    cid = add_gateway(studio)
    failing_second(studio)
    tid = start(studio, THREE)
    before = wait_row(studio, tid)
    assert studio.c.delete(f"/api/connections/{cid}").status_code == 204
    r = studio.c.post(f"/api/translate/{tid}/continue")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "no_connection", "message": translate.NO_CONNECTION}
    assert row_of(studio, tid) == before and tid not in translate.JOBS


def test_stop_before_detection_costs_no_model_call(studio):
    """Ревью M7: и у облачной модели остановка до определения языка запроса не делает."""
    add_gateway(studio)
    cloud = add_connection(studio, kind="openai", base_url="http://10.77.1.15:4000/v1", token=FakeGateway.TOKEN,
                           sections=["translate"], name="cloud")
    uid = owner_of(cloud)
    sections.set_models(uid, "translate", cloud, ["gpt-5"])
    conn = on_loop(studio, lambda: sections.resolve(uid, "translate", cloud, "gpt-5")[0])

    async def go():
        tid = translate.create(uid, conn, "gpt-5", kind="text", name="Hello.", source_lang="auto", target_lang="ru",
                               source="Hello.")
        translate.JOBS[tid].cancel = True
        translate.kick(uid, "text")
        return tid
    row = wait_row(studio, on_loop(studio, go))
    assert (row["state"], row["detected_lang"]) == ("cancelled", None) and studio.gw.chat_requests == []


def test_docx_export_keeps_plain_text_and_strips_markdown_only_of_scans(studio):
    """Ревью M10: `md_plain` убрал бы `<…>` и обратные кавычки у перевода, который не Markdown."""
    uid, conn = gateway_conn(studio)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def docx_lines(tid):
        r = studio.c.get(f"/api/translate/{tid}/export?format=docx")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
        return ["".join(t.text or "" for t in p.iter(f"{w}t")) for p in root.iter(f"{w}p")][1:]

    studio.gw.chat_script = [["Тип `List<int>`, a < b > c, **жирно**"], ["Тип `List<int>`, **жирно**"]]
    plain = wait_row(studio, file_job(studio, uid, conn, text="Type `List<int>`, a < b > c, **bold**"))["id"]
    assert docx_lines(plain) == ["Тип `List<int>`, a < b > c, **жирно**"]
    scanned = wait_row(studio, file_job(studio, uid, conn, text="Type `List<int>`, **bold**", scan_id="0" * 32))["id"]
    assert docx_lines(scanned) == ["Тип List, жирно"]


def test_export_gives_txt_and_docx_named_by_language(studio):
    add_gateway(studio)
    studio.gw.chat_script = [["Первая строка.\nВторая строка."]]
    tid = start_file(studio, "Договор.txt", b"First line.\nSecond line.")
    wait_row(studio, tid)
    r = studio.c.get(f"/api/translate/{tid}/export?format=txt")
    assert r.status_code == 200 and r.text == "Первая строка.\nВторая строка."
    assert r.headers["content-disposition"] == media.disposition("Договор.ru.txt")
    r = studio.c.get(f"/api/translate/{tid}/export?format=docx")
    assert r.headers["content-disposition"] == media.disposition("Договор.ru.docx")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    assert ["".join(t.text or "" for t in p.iter(f"{w}t")) for p in root.iter(f"{w}p")] == [
        "Договор", "Первая строка.", "Вторая строка."]
    assert studio.c.get(f"/api/translate/{tid}/export?format=pdf").status_code == 422

    text = wait_row(studio, start(studio, "Hello."))["id"]
    r = studio.c.get(f"/api/translate/{text}/export?format=txt")
    assert r.headers["content-disposition"] == media.disposition("Перевод.ru.txt")
    held = hold_chat(studio)
    running = start(studio, "Hello again.")
    r = studio.c.get(f"/api/translate/{running}/export?format=txt")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "running", "message": "перевода ещё нет"}
    held[0] = False
    wait_row(studio, running)


def test_delete_translation_removes_its_scan(studio, small_pages):
    """Находка «удаление записи оставляет её скан»: документ «Сканов», что завёл перевод PDF, удаляется с записью —
    строка, страницы, исходный PDF и картинки страниц; скан ещё останавливается — 409, запись на месте."""
    add_gateway(studio)
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    row = wait_row(studio, tid)
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert studio.c.get("/api/translate").json()["items"][0]["scan_id"] == row["scan_id"]   # окну подтверждения
    assert count(studio, "media") == 2                                  # PDF и картинка страницы
    assert studio.c.delete(f"/api/translate/{tid}").status_code == 204
    assert scan_row(studio, row["scan_id"]) is None and count(studio, "scan_pages") == 0
    assert count(studio, "media") == 0 and studio.c.get(f"/api/ocr/{row['scan_id']}").status_code == 404

    studio.gw.ocr_hold = True
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    sid = row_of(studio, tid)["scan_id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 2)
    assert studio.c.post(f"/api/translate/{tid}/cancel").json() == {"state": "cancelling"}
    assert wait_row(studio, tid)["state"] == "cancelled" and scan_row(studio, sid)["state"] == "cancelling"
    r = studio.c.delete(f"/api/translate/{tid}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running" and row_of(studio, tid) is not None
    studio.gw.ocr_hold = False
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert studio.c.delete(f"/api/translate/{tid}").status_code == 204
    assert scan_row(studio, sid) is None and count(studio, "media") == 0


def test_delete_translation_keeps_scan_shared_by_repeat(studio, small_pages):
    """Повтор ссылается на тот же скан: удаление одной записи его не трогает, последней — уносит."""
    add_gateway(studio)
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    first = wait_row(studio, tid)
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "de"})
    again = wait_row(studio, r.json()["id"])
    assert again["scan_id"] == first["scan_id"]
    wait_until(studio, lambda: not ocr.RUNNERS)
    uid, mid = on_loop(studio, owner_of_tr, tid), first["source_media_id"]
    assert studio.c.delete(f"/api/translate/{tid}").status_code == 204
    assert scan_row(studio, first["scan_id"]) is not None and on_loop(studio, media.alive, uid, mid)
    assert studio.c.delete(f"/api/translate/{again['id']}").status_code == 204
    assert scan_row(studio, first["scan_id"]) is None and not on_loop(studio, media.alive, uid, mid)


def test_poll_from_k_omits_source_of_waiting_parts(studio, monkeypatch):
    """Опрос (`from` > 0) не несёт исходник ждущих кусков — он у страницы с первого ответа; готовые — с ним; у
    снимка — всегда: рамки и исходник блоков нужны слою."""
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    uid, conn = gateway_conn(studio)
    held = hold_chat(studio, when=lambda text: text == "Second one is here.")
    tid = start(studio, THREE)
    wait_until(studio, lambda: state.DB.execute("SELECT done_parts FROM translations WHERE id = ?",
                                                (tid,)).fetchone()[0] == 1)
    full = studio.c.get(f"/api/translate/{tid}").json()
    assert [p["source"] for p in full["parts"]] == ["First one is here.", "Second one is here.", "Third one is here."]
    polled = studio.c.get(f"/api/translate/{tid}?from=1").json()
    assert [(p["n"], p["state"], "source" in p) for p in polled["parts"]] == [(2, "waiting", False), (3, "waiting", False)]
    assert polled["parts"][0]["sep"] == "\n\n" and "source" not in polled
    held[0] = False
    wait_row(studio, tid)
    done = studio.c.get(f"/api/translate/{tid}?from=1").json()["parts"]
    assert [p["source"] for p in done] == ["Second one is here.", "Third one is here."]

    blocks = [{"source": "Exit", "box": [93, 146, 491, 214]}, {"source": "Staff only", "box": [193, 640, 379, 706]}]
    pid = on_loop(studio, lambda: translate.create(uid, conn, MODEL, kind="photo", name="Снимок.png", source_lang="en",
                                                   target_lang="ru", source="Exit\nStaff only", blocks=blocks))
    parts = studio.c.get(f"/api/translate/{pid}?from=1").json()["parts"]
    assert [(p["source"], p["box"], p["state"]) for p in parts] == [("Staff only", [193, 640, 379, 706], "waiting")]


def test_feed_reads_heads_from_first_part(studio):
    """Начала исходника и перевода в ленте и ответах — из первого куска (до HEAD_CHARS), а не `substr` записи:
    SQLite читал бы её текст целиком."""
    uid, conn = gateway_conn(studio)
    tid = file_job(studio, uid, conn, text="x" * 300, kick=False)

    def rewrite():
        state.DB.execute("UPDATE translations SET source = 'запись', text = 'перевод записи' WHERE id = ?", (tid,))
        state.DB.execute("UPDATE translation_parts SET text = 'кусок' WHERE translation_id = ? AND n = 1", (tid,))
    on_loop(studio, rewrite)
    item = studio.c.get("/api/translate").json()["items"][0]
    assert (item["source_head"], item["text_head"]) == ("x" * translate.HEAD_CHARS, "кусок")
    out = studio.c.get(f"/api/translate/{tid}?from=1").json()
    assert (out["source_head"], out["text_head"]) == ("x" * translate.HEAD_CHARS, "кусок")
    on_loop(studio, lambda: state.DB.execute("DELETE FROM translation_parts WHERE translation_id = ?", (tid,)))
    item = studio.c.get("/api/translate").json()["items"][0]
    assert (item["source_head"], item["text_head"]) == (None, None)    # скан ещё не распознан — кусков нет


def test_feed_heads_of_a_photo_are_all_its_blocks(studio):
    """Ревью: кусок снимка — один блок вывески, и начало первого куска показывало лишь «EXIT». Начала снимка —
    блоки по порядку (до HEAD_CHARS); перевод — готовые до первого неготового."""
    uid, conn = gateway_conn(studio)
    words = ["EXIT", "Выход", "Открыто 9–18"]
    blocks = [{"source": w, "box": [10, 10 + 100 * i, 500, 80 + 100 * i]} for i, w in enumerate(words)]
    pid = on_loop(studio, lambda: translate.create(uid, conn, MODEL, kind="photo", name="Снимок.png", source_lang="en",
                                                   target_lang="ru", source="\n".join(words), blocks=blocks))

    def translated(*ns):
        for n in ns:
            state.DB.execute("UPDATE translation_parts SET state = 'done', text = ? WHERE translation_id = ? AND n = ?",
                             (f"п{n}", pid, n))
    on_loop(studio, translated, 1, 3)
    item = studio.c.get("/api/translate").json()["items"][0]
    assert (item["source_head"], item["text_head"]) == ("EXIT\nВыход\nОткрыто 9–18", "п1")
    on_loop(studio, translated, 2)
    out = studio.c.get(f"/api/translate/{pid}").json()
    assert (out["source_head"], out["text_head"]) == ("EXIT\nВыход\nОткрыто 9–18", "п1\nп2\nп3")


def test_scan_shared_tells_the_delete_window_whether_the_scan_stays(studio, small_pages):
    """Ревью: окно удаления обещало унести скан, который остаётся у повтора. `scan_shared` — есть ли другая запись
    с тем же сканом: после повтора — у обеих, после удаления одной — у другой уже нет."""
    add_gateway(studio)
    tid = start_file(studio, "Скан.pdf", scan_pdf("ink"))
    first = wait_row(studio, tid)
    assert studio.c.get(f"/api/translate/{tid}").json()["scan_shared"] is False
    again = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "de"}).json()
    wait_row(studio, again["id"])
    wait_until(studio, lambda: not ocr.RUNNERS)
    feed = {i["id"]: i for i in studio.c.get("/api/translate").json()["items"]}
    assert feed[tid]["scan_shared"] is True and feed[again["id"]]["scan_shared"] is True
    assert studio.c.get(f"/api/translate/{again['id']}").json()["scan_shared"] is True
    assert studio.c.delete(f"/api/translate/{tid}").status_code == 204
    assert studio.c.get(f"/api/translate/{again['id']}").json()["scan_shared"] is False
    assert studio.c.get("/api/translate").json()["items"][0]["scan_shared"] is False
    assert first["scan_id"] is not None


def test_translate_scan_waits_without_deadline_while_own_ocr_holds_card(studio, small_pages, monkeypatch):
    """Ревью M1: карту держит следующий скан того же пользователя — перевод ждёт её без срока WAIT_MAX_SEC (её
    освободит конец скана), а не падает «карта занята»."""
    monkeypatch.setattr(translate, "WAIT_MAX_SEC", 60)
    add_gateway(studio)
    studio.gw.ocr_hold = True
    r = studio.c.post("/api/ocr", files=[("file", ("Скан.pdf", scan_pdf("ink"), "application/pdf"))])
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)

    async def chat(request):
        if state.DB.execute("SELECT state FROM scans WHERE id = ?", (sid,)).fetchone()[0] in ocr.RUNNING:
            return httpx.Response(503, json={"error": {"message": "карта занята"}})
        return await studio.gw.builtin(request)
    studio.gw.on["/v1/chat/completions"] = chat
    tid = start(studio, "Hello.")
    wait_until(studio, lambda: studio.sleeps.count(translate.RETRY_SEC) >= 8       # 120 с — вдвое дольше срока
               or state.DB.execute("SELECT state FROM translations WHERE id = ?", (tid,)).fetchone()[0] == "error")
    assert row_of(studio, tid)["state"] == "working"
    studio.gw.ocr_hold = False
    row = wait_row(studio, tid)
    assert (row["state"], row["text"]) == ("done", "Здравствуйте!")
