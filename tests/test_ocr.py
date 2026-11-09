"""Распознавание сканов (app/ocr.py): страницы по порядку указанием cleaner, отказ страницы — не
отказ документа, ожидание карты, отмена после ответа страницы и освобождение слота ocr последним
документом студии, очередь пользователя, перезапуск; имя файла и текст — не в журнал."""
import asyncio
import io
import json
import logging
import time
import zipfile
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
import pytest
from fastapi import HTTPException

import clock
import connections
import documents
import history
import media
import ocr
import ocr_render
import state
from conftest import add_gateway, make_user, signin
from scans import photo, scan_pdf

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

NAME = "Договор аренды.pdf"
TEXT = "# Договор\n\nТекст страницы."


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    """Рендер мелкий — быстрый; предел 2200 сверяет test_ocr_render."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 300)


def answer(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def gateway_user(s):
    cid = add_gateway(s)
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    return uid, connections.get(uid, cid)


def pdf_scan(s, uid, conn, *pages, name=NAME, kick=True, origin="section", attachment_id=None, layer=None):
    """Документ из PDF: страницы `layer` (номер → текст) — из текстового слоя, остальные ждут."""
    layer = layer or {}

    async def go():
        source = await media.save_async(uid, scan_pdf(*pages), "application/pdf", name)
        rows = [{"n": n, "state": "layer", "media_id": None, "text": layer[n]} if n in layer else
                {"n": n, "state": "waiting", "media_id": None, "text": None} for n in range(1, len(pages) + 1)]
        sid = ocr.create(uid, conn, "hunyuan-ocr", name, origin=origin, source_media_id=source, pages=rows,
                         attachment_id=attachment_id)
        if kick:
            ocr.kick(uid)
        return sid
    return s.c.portal.call(go)


def on_loop(s, fn, *args):
    return s.c.portal.call(fn, *args)


def scan_row(s, sid):
    return on_loop(s, lambda: state.DB.execute("SELECT * FROM scans WHERE id = ?", (sid,)).fetchone())


def page_rows(s, sid):
    return on_loop(s, lambda: [dict(r) for r in state.DB.execute(
        "SELECT * FROM scan_pages WHERE scan_id = ? ORDER BY n", (sid,))])


def wait_until(s, cond, limit=3000):
    for _ in range(limit):
        if on_loop(s, cond):
            return
        s.c.portal.call(asyncio.sleep, 0.001)
    raise AssertionError("не дождались")


def wait_scan(s, sid, states=ocr.TERMINAL):
    wait_until(s, lambda: (r := state.DB.execute("SELECT state FROM scans WHERE id = ?", (sid,)).fetchone())
               is not None and r["state"] in states)
    return scan_row(s, sid)


def paths(s) -> list:
    return [r.url.path for r in s.gw.requests]


def ocr_releases(s) -> int:
    return sum(1 for model, _ in s.gw.releases if model is None)


def test_pages_are_recognized_in_order_with_the_cleaner_prompt(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid, conn = gateway_user(studio)
    sid = pdf_scan(studio, uid, conn, "ink", "ink")
    row = wait_scan(studio, sid)
    assert (row["state"], row["pages"], row["done_pages"]) == ("done", 2, 2)
    assert row["text"] == f"{TEXT}\n\n{TEXT}"
    pages = page_rows(studio, sid)
    assert [(p["n"], p["state"], p["text"]) for p in pages] == [(1, "done", TEXT), (2, "done", TEXT)]
    names = on_loop(studio, lambda: [state.DB.execute("SELECT mime, download_name FROM media WHERE id = ?",
                                                      (p["media_id"],)).fetchone()[:] for p in pages])
    assert names == [("image/png", "Договор аренды-1.png"), ("image/png", "Договор аренды-2.png")]

    body = json.loads(studio.gw.ocr_requests[0].content)
    assert body["model"] == "tencent/HunyuanOCR" and body["max_tokens"] == 16384 and body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": ""}
    image, prompt = body["messages"][1]["content"]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/png;base64,")
    assert prompt == {"type": "text", "text": ocr.PROMPT}
    assert ocr.PROMPT.startswith("Extract all information from the document image and represent it as Markdown.\n")
    assert studio.gw.ocr_requests[0].extensions["timeout"]["read"] == ocr.PAGE_TIMEOUT_SEC

    sent = paths(studio)
    first = sent.index("/pages/ocr")
    assert {"/slot/images/release", "/slot/images/descriptions/release"} <= set(sent[:first])
    assert sent[first:] == ["/pages/ocr", "/pages/ocr", "/pages/ocr-release"]
    assert json.loads(studio.gw.requests[-1].content) == {}
    assert NAME not in caplog.text and "Договор" not in caplog.text and "Текст страницы" not in caplog.text
    assert ocr.JOBS == {}


def test_truncated_empty_and_layout_only_pages_fail_alone(studio):
    uid, conn = gateway_user(studio)
    studio.gw.ocr_replies = [(200, answer("Обрывок", "length")), (200, answer("  ")),
                             (200, answer('[{"layout_type":"text","bbox":[1,2,3,4]}]'))]
    row = wait_scan(studio, pdf_scan(studio, uid, conn, "ink", "ink", "ink", "ink"))
    assert row["state"] == "done" and row["done_pages"] == 4 and row["text"] == TEXT
    assert [(p["state"], p["detail"]) for p in page_rows(studio, row["id"])] == [
        ("failed", "ответ модели обрезан — страница слишком плотная"),
        ("failed", "на странице не найден текст"),
        ("failed", "модель вернула разметку без текста"),
        ("done", None)]


def test_unreadable_page_fails_alone(studio, monkeypatch):
    """Страница, которую PDFium не нарисовал, и обрезанное фото — отказ своей страницы словами, а не
    документа (ревью M4)."""
    uid, conn = gateway_user(studio)
    real = ocr_render.render_page

    def render(data, index):
        if index == 1:
            raise ocr_render.ScanError(ocr_render.BROKEN)
        return real(data, index)
    monkeypatch.setattr(ocr_render, "render_page", render)
    row = wait_scan(studio, pdf_scan(studio, uid, conn, "ink", "ink"))
    assert row["state"] == "done" and row["done_pages"] == 2 and row["text"] == TEXT
    assert [(p["state"], p["detail"]) for p in page_rows(studio, row["id"])] == [
        ("done", None), ("failed", ocr.UNREADABLE)]

    async def go():
        good = await media.save_async(uid, photo(), "image/jpeg", "a.jpg")
        cut = await media.save_async(uid, photo()[:-200], "image/jpeg", "b.jpg")       # заголовок цел
        sid = ocr.create(uid, conn, "hunyuan-ocr", "a.jpg", origin="section", source_media_id=None,
                         pages=[{"n": 1, "state": "waiting", "media_id": cut, "text": None},
                                {"n": 2, "state": "waiting", "media_id": good, "text": None}])
        ocr.kick(uid)
        return sid
    row = wait_scan(studio, on_loop(studio, go))
    assert row["state"] == "done" and row["text"] == TEXT
    assert [(p["state"], p["detail"]) for p in page_rows(studio, row["id"])] == [
        ("failed", ocr.UNREADABLE), ("done", None)]


def test_cancel_while_the_card_is_freed_skips_the_page(studio, monkeypatch):
    """Отмена, пока освобождается карта (speech.free_card): страница в шлюз не уходит (ревью M5)."""
    uid, conn = gateway_user(studio)

    async def free(conn):
        for job in ocr.JOBS.values():
            job.cancel = True
    monkeypatch.setattr(ocr.speech, "free_card", free)
    row = wait_scan(studio, pdf_scan(studio, uid, conn, "ink"))
    assert row["state"] == "cancelled" and studio.gw.ocr_requests == []
    assert ocr_releases(studio) == 0 and ocr.LEASED == {}


def test_card_busy_waits_and_says_so(studio, monkeypatch):
    """Фото — страница как есть (PNG не сохраняется), карта занята — «жду карту · м:сс»."""
    uid, conn = gateway_user(studio)
    studio.gw.ocr_replies = [(503, {"error": "слот ocr не взят: no_capacity"})] * 3
    details, real = [], clock.SLEEP

    async def spy(seconds):
        details.extend(ocr.detail(j) for j in ocr.JOBS.values())
        await real(seconds)
    monkeypatch.setattr(clock, "SLEEP", spy)

    async def go():
        shot = await media.save_async(uid, photo(), "image/jpeg", "Скан.jpg")
        sid = ocr.create(uid, conn, "hunyuan-ocr", "Скан.jpg", origin="section", source_media_id=None,
                         pages=[{"n": 1, "state": "waiting", "media_id": shot, "text": None}])
        ocr.kick(uid)
        return sid, shot
    sid, shot = on_loop(studio, go)
    row = wait_scan(studio, sid)
    assert row["state"] == "done" and len(studio.gw.ocr_requests) == 4
    assert details and all(d.startswith("жду карту · ") for d in details) and "жду карту · 0:15" in details
    assert studio.sleeps.count(ocr.RETRY_SEC) == 3
    assert page_rows(studio, sid)[0]["media_id"] == shot
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM media").fetchone()[0]) == 1


def test_card_busy_too_long_is_an_error_in_words(studio, monkeypatch):
    uid, conn = gateway_user(studio)
    monkeypatch.setattr(ocr, "WAIT_MAX_SEC", 30)
    studio.gw.ocr_replies = [(503, {"error": "слот ocr не взят"})] * 10
    row = wait_scan(studio, pdf_scan(studio, uid, conn, "ink"))
    assert row["state"] == "error" and "карта занята" in row["detail"] and row["text"] is None


def test_cancel_waits_for_the_page_and_releases(studio, caplog):
    """Отмена запрос страницы не обрывает: ответ дожидается и выбрасывается, вторая страница не
    запрашивается, слот ocr освобождается после ответа — 200, а не 409 занятого слота."""
    caplog.set_level(logging.INFO)
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    sid = pdf_scan(studio, uid, conn, "ink", "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    with pytest.raises(HTTPException) as e:
        on_loop(studio, ocr.request_cancel, uid + 1, sid)        # чужой документ
    assert e.value.status_code == 404
    assert on_loop(studio, ocr.request_cancel, uid, sid) == "cancelling"
    assert scan_row(studio, sid)["state"] == "cancelling"
    for _ in range(20):
        studio.c.portal.call(asyncio.sleep, 0.001)
    assert "/pages/ocr-release" not in paths(studio)
    studio.gw.ocr_hold = False
    row = wait_scan(studio, sid)
    assert (row["state"], row["detail"], row["text"]) == ("cancelled", "отменено", None)
    assert len(studio.gw.ocr_requests) == 1
    assert paths(studio)[-2:] == ["/pages/ocr", "/pages/ocr-release"] and ocr_releases(studio) == 1
    assert ocr.RETRY_SEC not in studio.sleeps and "release ответил 409" not in caplog.text
    assert [p["state"] for p in page_rows(studio, sid)] == ["waiting", "waiting"]   # ответ выброшен
    assert ocr.JOBS == {}


def test_release_waits_for_the_last_scan_of_the_studio(studio):
    uid_a, conn_a = gateway_user(studio)
    make_user(studio, "bob")
    signin(studio, "bob")
    uid_b, conn_b = gateway_user(studio)
    go = []
    studio.gw.ocr_hold = lambda request: request is studio.gw.ocr_requests[0] and not go
    sid_b = pdf_scan(studio, uid_b, conn_b, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    sid_a = pdf_scan(studio, uid_a, conn_a, "ink")
    assert wait_scan(studio, sid_a)["state"] == "done"
    assert scan_row(studio, sid_b)["state"] == "working" and ocr_releases(studio) == 0
    go.append(True)
    assert wait_scan(studio, sid_b)["state"] == "done"
    assert ocr_releases(studio) == 1 and paths(studio)[-1] == "/pages/ocr-release"


def released(s):
    """release слота ocr дошёл до шлюза (он — после итога документа, отдельным запросом)."""
    wait_until(s, lambda: ocr_releases(s) >= 1)
    return ocr_releases(s)


def test_release_after_a_next_document_that_never_calls_the_gateway(studio):
    """Первый распознаётся, второй (только пустые страницы) ждёт: первый слот не освобождает (в очереди
    второй), второй шлюз не звал — release всё равно делает конец второго (ревью I1)."""
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    first = pdf_scan(studio, uid, conn, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    second = pdf_scan(studio, uid, conn, "blank")
    studio.gw.ocr_hold = False
    assert wait_scan(studio, first)["state"] == "done"
    assert wait_scan(studio, second)["state"] == "done"
    assert released(studio) == 1 and ocr.LEASED == {}


def test_release_after_a_next_document_cancelled_before_its_first_page(studio, monkeypatch):
    """«Отменить» второго документа, пока рисуется его первая страница (до запроса к шлюзу)."""
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    first = pdf_scan(studio, uid, conn, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    second = pdf_scan(studio, uid, conn, "ink")
    real = ocr_render.render_page

    def render(data, index):
        job = ocr.JOBS.get(second)
        if job is not None and job.stage != ocr.QUEUED:
            job.cancel = True                                     # нажали «Отменить» во время рендера
        return real(data, index)
    monkeypatch.setattr(ocr_render, "render_page", render)
    studio.gw.ocr_hold = False
    assert wait_scan(studio, first)["state"] == "done"
    assert wait_scan(studio, second)["state"] == "cancelled"
    assert released(studio) == 1 and ocr.LEASED == {}
    assert len(studio.gw.ocr_requests) == 1


def hold_releases(s, go: list):
    """release слота ocr ждёт, пока в `go` не появится True (не дольше 5 с), и отвечает 200; в `releases` —
    когда ответил."""
    async def release(request):
        deadline = time.monotonic() + 5
        while not go and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        s.gw.releases.append((None, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})
    s.gw.on["/pages/ocr-release"] = release


def leased_with_a_queued_document(s):
    """Документ Алисы распознан, ждёт документ Боба (его раннер не запущен): слот держим. → (uid, uid_b, ждущий)."""
    uid, conn = gateway_user(s)
    make_user(s, "bob")
    signin(s, "bob")
    uid_b, conn_b = gateway_user(s)
    s.gw.ocr_hold = True
    first = pdf_scan(s, uid, conn, "ink")
    wait_until(s, lambda: len(s.gw.ocr_requests) == 1)
    second = pdf_scan(s, uid_b, conn_b, "ink", kick=False)        # раннер Боба ещё не запущен
    s.gw.ocr_hold = False
    assert wait_scan(s, first)["state"] == "done"       # итог и решение о release — без await между ними
    assert ocr_releases(s) == 0 and ocr.LEASED != {}  # в студии ждёт документ — слот держим
    return uid, uid_b, second


def test_release_after_cancelling_the_last_queued_document(studio):
    """Чужой ждущий документ отменён, пока его раннер не взял его: release — от отмены (ревью I1), фоном —
    ответ отмены шлюза не ждёт; ход чата ждёт и его (idle), запись LEASED снимает прошедший release."""
    uid, uid_b, second = leased_with_a_queued_document(studio)
    go = []
    hold_releases(studio, go)
    assert on_loop(studio, ocr.request_cancel, uid_b, second) == "cancelled"
    assert ocr_releases(studio) == 0 and ocr.LEASED != {} and ocr.SETTLING
    assert not on_loop(studio, ocr.idle, uid) and not on_loop(studio, ocr.idle, uid_b)
    go.append(True)
    wait_until(studio, lambda: ocr.LEASED == {} and not ocr.SETTLING)
    assert ocr_releases(studio) == 1 and on_loop(studio, ocr.idle, uid_b)


def test_service_stop_ends_the_background_release(studio):
    uid, uid_b, second = leased_with_a_queued_document(studio)
    hold_releases(studio, [])
    assert on_loop(studio, ocr.request_cancel, uid_b, second) == "cancelled"
    tasks = set(ocr.SETTLING)
    assert tasks
    on_loop(studio, ocr.stop)
    assert all(t.cancelled() for t in tasks) and ocr_releases(studio) == 0


def test_failed_release_keeps_the_lease_for_the_next_document(studio):
    """release не прошёл — запись LEASED остаётся: её освобождает конец следующего документа, хоть он шлюз и
    не звал."""
    uid, conn = gateway_user(studio)
    codes = [500, 200]

    async def release(request):
        studio.gw.releases.append((None, request.headers.get("authorization")))
        return httpx.Response(codes.pop(0), json={})
    studio.gw.on["/pages/ocr-release"] = release
    assert wait_scan(studio, pdf_scan(studio, uid, conn, "ink"))["state"] == "done"
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert ocr_releases(studio) == 1 and ocr.LEASED != {}
    assert wait_scan(studio, pdf_scan(studio, uid, conn, "blank"))["state"] == "done"
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert ocr_releases(studio) == 2 and ocr.LEASED == {}


def test_release_keeps_the_lease_of_a_document_started_meanwhile(studio):
    """Пока шёл release конца документа Алисы, начался документ Боба и взял слот: прошедший release его
    запись не снимает — слот освободит конец документа Боба."""
    uid, conn = gateway_user(studio)
    make_user(studio, "bob")
    signin(studio, "bob")
    uid_b, conn_b = gateway_user(studio)
    go = []
    hold_releases(studio, go)
    first = pdf_scan(studio, uid, conn, "ink")
    assert wait_scan(studio, first)["state"] == "done"
    studio.gw.ocr_hold = True
    second = pdf_scan(studio, uid_b, conn_b, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 2)
    go.append(True)
    wait_until(studio, lambda: ocr_releases(studio) == 1 and not ocr.RUNNERS.get(uid))
    assert list(ocr.LEASED) == [conn_b.base_url] and ocr.LEASED[conn_b.base_url] is conn_b
    studio.gw.ocr_hold = False
    assert wait_scan(studio, second)["state"] == "done"
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert ocr_releases(studio) == 2 and ocr.LEASED == {}


def test_release_retry_stops_when_a_document_starts_meanwhile(studio, monkeypatch):
    """409 release конца документа Алисы, пауза перед повтором — за неё в студии появился документ Боба:
    повтора нет (он освободил бы слот посреди нового документа), запись LEASED — концу документа Боба."""
    uid, conn = gateway_user(studio)
    make_user(studio, "bob")
    signin(studio, "bob")
    uid_b, conn_b = gateway_user(studio)
    codes = [409, 200]

    async def release(request):
        studio.gw.releases.append((None, request.headers.get("authorization")))
        return httpx.Response(codes.pop(0), json={})
    studio.gw.on["/pages/ocr-release"] = release
    real, started = clock.SLEEP, []

    async def pause(seconds):
        if seconds == ocr.RETRY_SEC and not started:
            source = await media.save_async(uid_b, scan_pdf("ink"), "application/pdf", NAME)
            started.append(ocr.create(uid_b, conn_b, "hunyuan-ocr", NAME, origin="section", source_media_id=source,
                                      pages=[{"n": 1, "state": "waiting", "media_id": None, "text": None}]))
        await real(seconds)
    monkeypatch.setattr(clock, "SLEEP", pause)
    assert wait_scan(studio, pdf_scan(studio, uid, conn, "ink"))["state"] == "done"
    wait_until(studio, lambda: not ocr.RUNNERS and started)
    assert ocr_releases(studio) == 1 and ocr.LEASED != {}
    on_loop(studio, ocr.kick, uid_b)
    assert wait_scan(studio, started[0])["state"] == "done"
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert ocr_releases(studio) == 2 and ocr.LEASED == {}
    assert [p for p in paths(studio) if p.startswith("/pages/")] == [
        "/pages/ocr", "/pages/ocr-release", "/pages/ocr", "/pages/ocr-release"]


def test_retry_page_recognizes_only_that_page_again(studio):
    """«Повторить страницу»: только эта `failed` — снова в очередь, PNG страницы — прежний (не рисуется и не
    сохраняется заново), итог документа — со всеми страницами."""
    uid, conn = gateway_user(studio)
    studio.gw.ocr_replies = [(200, answer("Обрывок", "length"))]
    sid = pdf_scan(studio, uid, conn, "ink", "ink")
    row = wait_scan(studio, sid)
    assert row["state"] == "done" and row["text"] == TEXT
    media_before = on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM media").fetchone()[0])
    assert studio.c.post(f"/api/ocr/{sid}/retry", json={"page": 2}).status_code == 409      # не failed
    r = studio.c.post(f"/api/ocr/{sid}/retry", json={"page": 1})
    assert r.status_code == 202 and r.json()["state"] in ocr.RUNNING
    row = wait_scan(studio, sid)
    assert (row["state"], row["done_pages"], row["text"]) == ("done", 2, f"{TEXT}\n\n{TEXT}")
    assert [(p["state"], p["detail"]) for p in page_rows(studio, sid)] == [("done", None), ("done", None)]
    assert len(studio.gw.ocr_requests) == 3
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM media").fetchone()[0]) == media_before
    assert studio.c.post(f"/api/ocr/{sid}/retry", json={}).json()["detail"]["code"] == "nothing"


def test_retry_refuses_a_running_or_foreign_document(studio):
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    sid = pdf_scan(studio, uid, conn, "ink")
    try:
        wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
        r = studio.c.post(f"/api/ocr/{sid}/retry", json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    finally:
        studio.gw.ocr_hold = False
    wait_scan(studio, sid)
    make_user(studio, "bob")
    signin(studio, "bob")
    assert studio.c.post(f"/api/ocr/{sid}/retry", json={}).status_code == 404


def test_recognize_again_after_an_error_continues_the_waiting_pages(studio, monkeypatch):
    """Отказ документа (карта занята дольше срока) — «Распознать заново»: нераспознанные страницы снова в
    очереди, у документа из чата текст вложения пересобирается его итогом."""
    uid, conn = gateway_user(studio)
    monkeypatch.setattr(ocr, "WAIT_MAX_SEC", 30)
    studio.gw.ocr_replies = [(503, {"error": "занято"})] * 3
    att = on_loop(studio, lambda: state.DB.execute(
        "INSERT INTO attachments(id, user_id, kind, name, media_id, created) VALUES ('a1', ?, 'document', 'с.pdf', 'm', 0)"
        " RETURNING id", (uid,)).fetchone()[0])
    sid = pdf_scan(studio, uid, conn, "ink", origin="chat", attachment_id=att)
    assert wait_scan(studio, sid)["state"] == "error"
    assert on_loop(studio, lambda: state.DB.execute("SELECT text FROM attachments").fetchone()[0]).startswith(
        "[скан не распознан: карта занята")
    assert studio.c.post(f"/api/ocr/{sid}/retry", json={}).status_code == 202
    assert on_loop(studio, lambda: state.DB.execute("SELECT text FROM attachments").fetchone()[0]) is None
    row = wait_scan(studio, sid)
    assert (row["state"], row["text"]) == ("done", TEXT)
    assert on_loop(studio, lambda: state.DB.execute("SELECT text FROM attachments").fetchone()[0]) == (
        f"Страница 1\n\n{TEXT}")


def test_queue_runs_one_at_a_time_and_caps_at_five(studio):
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    first = pdf_scan(studio, uid, conn, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    queued = [pdf_scan(studio, uid, conn, "ink") for _ in range(ocr.QUEUE_MAX)]
    assert [scan_row(studio, q)["detail"] for q in queued] == ["в очереди"] * 5
    with pytest.raises(HTTPException) as e:
        pdf_scan(studio, uid, conn, "ink")
    assert e.value.status_code == 409 and e.value.detail["code"] == "busy"
    assert on_loop(studio, ocr.request_cancel, uid, queued[0]) == "cancelled"
    assert scan_row(studio, queued[0])["state"] == "cancelled"
    assert len(studio.gw.ocr_requests) == 1
    studio.gw.ocr_hold = False
    for sid in [first, *queued[1:]]:
        assert wait_scan(studio, sid)["state"] == "done"
    assert len(studio.gw.ocr_requests) == 5 and ocr_releases(studio) == 1
    assert ocr.JOBS == {} and ocr.RUNNERS == {}


def test_blank_pages_skip_the_gateway(studio):
    uid, conn = gateway_user(studio)
    row = wait_scan(studio, pdf_scan(studio, uid, conn, "blank", "blank"))
    assert row["state"] == "done" and row["done_pages"] == 2
    assert [p["state"] for p in page_rows(studio, row["id"])] == ["blank", "blank"]
    assert studio.gw.requests == [] and studio.gw.releases == []


def add_attachment(s, uid) -> str:
    """Неотправленный документ чата без текста — как его оставит загрузка скана (Task 6)."""
    def put():
        aid = f"{state.DB.execute('SELECT count(*) FROM attachments').fetchone()[0]:032x}"
        state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
                         " VALUES (?, ?, NULL, 'document', 'скан.pdf', ?, NULL, 0, ?)", (aid, uid, "f" * 32, clock.NOW()))
        return aid
    return on_loop(s, put)


def attachment(s, aid):
    return on_loop(s, lambda: state.DB.execute("SELECT text, chars FROM attachments WHERE id = ?", (aid,)).fetchone()[:])


def test_chat_scan_fills_the_attachment_text(studio):
    """Скан из чата: готовое — текст вложения по страницам («Страница N»), пустые пропущены,
    нераспознанные — пометкой; страница слоя — как есть."""
    uid, conn = gateway_user(studio)
    aid = add_attachment(studio, uid)
    studio.gw.ocr_replies = [(200, answer(TEXT)), (200, answer(""))]
    sid = pdf_scan(studio, uid, conn, "Layer text of the first page, long enough", "ink", "blank", "ink",
                   origin="chat", attachment_id=aid, layer={1: "Слой первой страницы"})
    row = wait_scan(studio, sid)
    assert row["state"] == "done" and row["done_pages"] == 4 and row["text"] == f"Слой первой страницы\n\n{TEXT}"
    expected = (f"Страница 1\n\nСлой первой страницы\n\nСтраница 2\n\n{TEXT}\n\n"
                "[страница 4 не распознана: на странице не найден текст]")
    assert attachment(studio, aid) == (expected, len(expected)) and on_loop(studio, ocr.attachment_text, sid) == expected
    assert [p["state"] for p in page_rows(studio, sid)] == ["layer", "done", "blank", "failed"]
    assert len(studio.gw.ocr_requests) == 2


def test_chat_scan_text_is_cut_at_the_document_limit(studio, monkeypatch):
    """Текст вложения — не длиннее documents.TEXT_MAX_CHARS, с пометкой обрезки (спека 6.5, ревью M3)."""
    uid, conn = gateway_user(studio)
    monkeypatch.setattr(documents, "TEXT_MAX_CHARS", 100)
    aid = add_attachment(studio, uid)
    sid = pdf_scan(studio, uid, conn, "ink", "ink", "ink", origin="chat", attachment_id=aid)
    assert wait_scan(studio, sid)["state"] == "done"
    text, chars = attachment(studio, aid)
    mark = "\n\n[текст скана обрезан: длиннее 100 знаков]"
    assert len(text) == chars == 100 and text.endswith(mark) and text.startswith(f"Страница 1\n\n{TEXT}")


def test_recover_marks_running_as_interrupted_and_fills_the_attachment(studio):
    uid, conn = gateway_user(studio)
    atts = [add_attachment(studio, uid) for _ in range(4)]
    sids = [pdf_scan(studio, uid, conn, "ink", kick=False, origin="chat", attachment_id=a) for a in atts]
    on_loop(studio, lambda: [state.DB.execute("UPDATE scans SET state = ? WHERE id = ?", (st, sid))
                             for st, sid in zip(("working", "cancelling", "done"), sids[1:])])
    on_loop(studio, lambda: state.DB.execute("UPDATE attachments SET text = 'готово', chars = 6 WHERE id = ?",
                                             (atts[3],)))
    on_loop(studio, ocr.recover)
    marker = ocr.attachment_marker(ocr.INTERRUPTED)
    assert marker == "[скан не распознан: прервано перезапуском студии]"
    for sid, aid in zip(sids[:3], atts[:3]):
        row = scan_row(studio, sid)
        assert (row["state"], row["detail"], row["finished"]) == ("error", ocr.INTERRUPTED, clock.NOW())
        assert attachment(studio, aid) == (marker, len(marker))
    assert scan_row(studio, sids[3])["state"] == "done" and attachment(studio, atts[3]) == ("готово", 6)


def test_stop_user_stops_the_runner(studio):
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    pdf_scan(studio, uid, conn, "ink", "ink")
    pdf_scan(studio, uid, conn, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    on_loop(studio, history.delete_all, uid)
    assert ocr.JOBS == {} and ocr.RUNNERS == {}
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM scans").fetchone()[0]) == 0
    studio.gw.ocr_hold = False
    for _ in range(20):
        studio.c.portal.call(asyncio.sleep, 0.001)
    assert len(studio.gw.ocr_requests) == 1


def test_stop_user_cancels_the_running_and_the_queued(studio):
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    running = pdf_scan(studio, uid, conn, "ink")
    queued = pdf_scan(studio, uid, conn, "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    on_loop(studio, ocr.stop_user, uid)
    assert [scan_row(studio, s)["state"] for s in (running, queued)] == ["cancelled", "cancelled"]
    assert ocr.JOBS == {} and ocr.RUNNERS == {}


def test_stop_user_releases_the_cut_page_in_the_background(studio):
    """Стирание истории посреди страницы: раннер оборван, а слот ocr освобождает release фоном — шлюз ещё
    считает оборванную страницу (409), release повторяет и проходит."""
    uid, conn = gateway_user(studio)
    studio.gw.ocr_hold = True
    pdf_scan(studio, uid, conn, "ink", "ink")
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    codes = [409, 200]

    async def release(request):
        studio.gw.releases.append((None, request.headers.get("authorization")))
        return httpx.Response(codes.pop(0), json={})
    studio.gw.on["/pages/ocr-release"] = release
    try:
        on_loop(studio, history.delete_all, uid)
    finally:
        studio.gw.ocr_hold = False
    wait_until(studio, lambda: ocr.LEASED == {} and not ocr.SETTLING)
    assert ocr_releases(studio) == 2 and ocr.RETRY_SEC in studio.sleeps


# ---- API раздела ---------------------------------------------------------------

TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def post_files(s, files):
    """POST /api/ocr: части `file` в порядке списка (имя, байты)."""
    parts = [("file", (name, data, TYPES.get(name[name.rfind("."):], "application/octet-stream")))
             for name, data in files]
    return s.c.post("/api/ocr", files=parts)


def wait_ocr(s, sid, states=ocr.TERMINAL, limit=3000):
    for _ in range(limit):
        r = s.c.get(f"/api/ocr/{sid}")
        assert r.status_code == 200, r.text
        if r.json()["state"] in states:
            return r.json()
        s.c.portal.call(asyncio.sleep, 0.001)
    raise AssertionError("не дождались")


def count(s, table):
    return on_loop(s, lambda: state.DB.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_upload_photos_make_one_document_in_order(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    r = post_files(studio, [("a.jpg", photo()), ("b.png", photo("PNG"))])
    assert r.status_code == 202 and r.json()["pages"] == 2 and r.json()["name"] == "a.jpg"
    assert set(r.json()) == {"id", "name", "state", "detail", "model", "pages", "done_pages", "origin", "created",
                             "finished"}
    assert (r.json()["origin"], r.json()["model"]) == ("section", "hunyuan-ocr")
    item = wait_ocr(studio, r.json()["id"], {"done"})
    assert [p["n"] for p in item["pages"]] == [1, 2] and all(p["media_id"] for p in item["pages"])
    assert [(p["state"], p["text"], p["detail"]) for p in item["pages"]] == [("done", TEXT, None)] * 2
    assert item["done_pages"] == 2 and item["detail"] == "готово"
    mimes = [on_loop(studio, lambda m=p["media_id"]: state.DB.execute(
        "SELECT mime, download_name FROM media WHERE id = ?", (m,)).fetchone()[:]) for p in item["pages"]]
    assert mimes == [("image/jpeg", "a.jpg"), ("image/png", "b.png")]                 # фото — как есть
    assert studio.c.get(f"/api/media/{item['pages'][0]['media_id']}").content == photo()
    assert len(studio.gw.ocr_requests) == 2 and count(studio, "media") == 2
    listed = studio.c.get("/api/ocr").json()["items"]
    assert [i["id"] for i in listed] == [item["id"]] and "text" not in listed[0] and listed[0]["pages"] == 2
    # опрос чата — только состояние: страницы числом, как в ленте, без текстов
    assert studio.c.get(f"/api/ocr/{item['id']}?brief=1").json() == listed[0]
    assert f"ocr {item['id'][:8]}: принят, 2 стр." in caplog.text
    assert "a.jpg" not in caplog.text and "Текст страницы" not in caplog.text


def test_pdf_in_the_section_recognizes_every_page(studio):
    """Текстовый слой в разделе не читается: страница со слоем тоже уходит в /pages/ocr."""
    add_gateway(studio)
    r = post_files(studio, [(NAME, scan_pdf("Layer text of the first page, long enough to count", "ink"))])
    assert r.status_code == 202 and r.json()["name"] == NAME and r.json()["pages"] == 2
    item = wait_ocr(studio, r.json()["id"], {"done"})
    assert [(p["n"], p["state"], p["text"]) for p in item["pages"]] == [(1, "done", TEXT), (2, "done", TEXT)]
    assert all(p["media_id"] for p in item["pages"]) and len(studio.gw.ocr_requests) == 2
    source = scan_row(studio, item["id"])["source_media_id"]
    assert on_loop(studio, lambda: state.DB.execute("SELECT mime FROM media WHERE id = ?", (source,)).fetchone()[0]) \
        == "application/pdf"


def test_refusals_are_words(studio, monkeypatch):
    add_gateway(studio)
    pdf = scan_pdf("ink")

    def refused(files, status, message):
        r = post_files(studio, files)
        assert r.status_code == status, r.text
        assert message in r.json()["detail"]["message"], r.json()
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    refused([("IMG_1.heic", heic)], 415, ocr.FORMAT)
    refused([(NAME, pdf), ("a.jpg", photo())], 415, ocr.FORMAT)
    refused([("a.jpg", photo()), (NAME, pdf)], 415, ocr.FORMAT)
    refused([(NAME, pdf), ("b.pdf", pdf)], 415, ocr.FORMAT)
    refused([(NAME, scan_pdf(*["page"] * 51))], 422, "больше 50 страниц — разделите документ")
    refused([(f"{n}.png", photo("PNG", (8, 8))) for n in range(51)], 422, "50")
    refused([(NAME, b"%PDF-1.4 broken")], 422, "повреждён")
    refused([(NAME, b"\x00" * (21 * 1024 * 1024))], 413, "20 МиБ")
    monkeypatch.setattr(ocr_render.image_upload, "VIDEO_PIXELS", 100)
    refused([("a.jpg", photo())], 413, "Мп")
    assert count(studio, "media") == 0 and count(studio, "scans") == 0
    make_user(studio, "bob")
    signin(studio, "bob")                                         # без подключения раздела
    r = post_files(studio, [("a.jpg", photo())])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"
    assert "Сканы" in r.json()["detail"]["message"]


def test_someone_else_cannot_see_the_scan_or_its_pages(studio):
    add_gateway(studio)
    sid = post_files(studio, [(NAME, scan_pdf("ink"))]).json()["id"]
    page = wait_ocr(studio, sid, {"done"})["pages"][0]["media_id"]
    make_user(studio, "bob")
    signin(studio, "bob")
    for r in (studio.c.get(f"/api/ocr/{sid}"), studio.c.get(f"/api/ocr/{sid}/export?format=txt"),
              studio.c.post(f"/api/ocr/{sid}/attachments"), studio.c.post(f"/api/ocr/{sid}/cancel"),
              studio.c.delete(f"/api/ocr/{sid}"), studio.c.get(f"/api/media/{page}")):
        assert r.status_code == 404, r.request.url
    assert studio.c.get("/api/ocr").json() == {"items": []}
    assert count(studio, "scans") == 1


TABLE = ("# Итоги\n\n**Важно:** `код`\n\n"
         "<table><tr><td>Товар</td><td>Цена</td></tr><tr><td>Хлеб &amp; соль</td><td>50</td></tr></table>")


def test_export_txt_md_docx(studio):
    add_gateway(studio)
    studio.gw.ocr_replies = [(200, answer(TABLE)), (200, answer("Обрывок", "length"))]
    studio.gw.ocr_hold = True
    sid = post_files(studio, [("Отчёт.pdf", scan_pdf("ink", "ink", "blank"))]).json()["id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    r = studio.c.get(f"/api/ocr/{sid}/export?format=txt")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    studio.gw.ocr_hold = False
    wait_ocr(studio, sid, {"done"})

    r = studio.c.get(f"/api/ocr/{sid}/export?format=txt")
    assert r.status_code == 200 and r.headers["content-type"] == "text/plain; charset=utf-8"
    assert quote("Отчёт.txt", safe="") in r.headers["content-disposition"]
    assert r.text == ("Страница 1\n\nИтоги\n\nВажно: код\n\nТовар\tЦена\nХлеб & соль\t50\n\n"
                      "Страница 2\n\n[не распознана: ответ модели обрезан — страница слишком плотная]\n\n"
                      "Страница 3\n\n[пустая страница]\n")
    r = studio.c.get(f"/api/ocr/{sid}/export?format=md")
    assert r.status_code == 200 and r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert quote("Отчёт.md", safe="") in r.headers["content-disposition"]
    assert r.text.startswith(f"## Страница 1\n\n{TABLE}\n\n## Страница 2\n\n[не распознана: ")
    r = studio.c.get(f"/api/ocr/{sid}/export?format=docx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert quote("Отчёт.docx", safe="") in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    texts = [t.text for t in root.iter(f"{W}t")]
    assert texts[:2] == ["Отчёт", "Страница 1"] and "Хлеб & соль" in texts and "[пустая страница]" in texts
    assert len(list(root.iter(f"{W}tab"))) == 2                        # ячейки таблицы — табуляцией
    assert studio.c.get(f"/api/ocr/{sid}/export?format=pdf").status_code == 422


def test_to_chat_attaches_markdown(studio, monkeypatch):
    add_gateway(studio)
    sid = post_files(studio, [("Отчёт.pdf", scan_pdf("ink"))]).json()["id"]
    assert studio.c.post(f"/api/ocr/{sid}/attachments").status_code == 409
    wait_ocr(studio, sid, {"done"})
    r = studio.c.post(f"/api/ocr/{sid}/attachments")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "kind", "name", "chars", "media_id", "text", "tokens"}
    assert body["kind"] == "document" and body["name"] == "Отчёт.md" and body["text"].startswith("## Страница 1")
    assert body["text"] == f"## Страница 1\n\n{TEXT}\n" and body["chars"] == len(body["text"])
    row = on_loop(studio, lambda: state.DB.execute("SELECT * FROM attachments WHERE id = ?", (body["id"],)).fetchone())
    assert row["message_id"] is None and row["source_transcript_id"] is None and row["text"] == body["text"]
    assert studio.c.get(f"/api/media/{body['media_id']}").text == body["text"]
    monkeypatch.setattr(documents, "TEXT_MAX_CHARS", 10)
    r = studio.c.post(f"/api/ocr/{sid}/attachments")
    assert r.status_code == 413 and "в чат его не передать" in r.json()["detail"]["message"]


def test_delete_running_is_409_and_done_removes_files(studio):
    add_gateway(studio)
    studio.gw.ocr_hold = True
    sid = post_files(studio, [(NAME, scan_pdf("ink", "ink"))]).json()["id"]
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    r = studio.c.delete(f"/api/ocr/{sid}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    assert studio.c.post(f"/api/ocr/{sid}/cancel").json() == {"state": "cancelling"}
    studio.gw.ocr_hold = False
    assert wait_ocr(studio, sid)["state"] == "cancelled"
    sid2 = post_files(studio, [(NAME, scan_pdf("ink", "ink"))]).json()["id"]
    wait_ocr(studio, sid2, {"done"})
    assert count(studio, "media") == 2 + 3                               # PDF и страница отменённого, PDF и две готового
    assert studio.c.delete(f"/api/ocr/{sid2}").status_code == 204
    assert studio.c.delete(f"/api/ocr/{sid}").status_code == 204
    assert count(studio, "media") == 0 and count(studio, "scans") == 0 and count(studio, "scan_pages") == 0
    assert studio.c.get(f"/api/ocr/{sid}").status_code == 404
