"""Распознавание сканов (app/ocr.py): страницы по порядку указанием cleaner, отказ страницы — не
отказ документа, ожидание карты, отмена после ответа страницы и освобождение слота ocr последним
документом студии, очередь пользователя, перезапуск; имя файла и текст — не в журнал."""
import asyncio
import json
import logging

import pytest
from fastapi import HTTPException

import clock
import connections
import history
import media
import ocr
import ocr_render
import state
from conftest import add_gateway, make_user, signin
from scans import photo, scan_pdf

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
