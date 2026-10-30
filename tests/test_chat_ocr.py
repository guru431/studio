"""Скан в чате (спека 2026-10-27-studio-ocr-design, 6.5): PDF со страницами без текстового слоя становится
документом «Распознавания», ход через сервер ждёт его в StreamTask (событие SSE `ocr`, `message.ocr` для
перечитавшей вкладки), модель получает вложение только готовым; прямое подключение — 409 `not_ready`.
Без подключения «Распознавания» — всё как прежде."""
import asyncio

import pytest

import chat
import connections
import ocr
import ocr_render
import sections
import state
from conftest import add_connection, add_gateway
from scans import scan_pdf
from test_chat import attach, events_of, messages, new_chat, sse

TEXT = "# Договор\n\nТекст страницы."
LAYER = "This page has a text layer long enough to be read as it is."      # 46 знаков без пробелов
QUESTION = "о чём договор?"


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    """Рендер мелкий — быстрый; предел 2200 сверяет test_ocr_render."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 300)


def ask(s, cid, text=QUESTION, ids=(), **extra):
    return s.c.post(f"/api/chats/{cid}/turns", json={"text": text, "attachment_ids": list(ids)} | extra)


def upload(s, pdf, name="скан.pdf"):
    return attach(s, name, pdf, "application/pdf")


def on_loop(s, fn, *args):
    return s.c.portal.call(fn, *args)


def scan_of(s, sid):
    return on_loop(s, lambda: state.DB.execute("SELECT * FROM scans WHERE id = ?", (sid,)).fetchone())


def att_text(s, aid):
    return on_loop(s, lambda: state.DB.execute("SELECT text, chars FROM attachments WHERE id = ?", (aid,)).fetchone()[:])


def wait_until(s, cond, limit=3000):
    for _ in range(limit):
        if on_loop(s, cond):
            return
        s.c.portal.call(asyncio.sleep, 0.001)
    raise AssertionError("не дождались")


def held_until_the_turn_waits(s):
    """Страница скана держится шлюзом, пока ход не начал ждать: иначе скан кончился бы до хода."""
    s.gw.ocr_hold = lambda request: not chat.STREAMS


def paths(s) -> list:
    return [r.url.path for r in s.gw.requests]


def doc(name, text, question=QUESTION):
    return f'<document name="{name}">\n{text}\n</document>\n\n{question}'


def test_scan_pdf_attaches_and_the_turn_waits_for_it(studio):
    """…и модель зовётся, только когда раннер освободил карту: release слота ocr сначала 409 (slot-gate
    ещё считает чужую страницу) — ход ждёт повтора, а не идёт к gemma-abl под арендой ocr."""
    import httpx
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    busy = [True]

    async def release(request):
        if busy:
            busy.pop()
            studio.gw.releases.append((None, request.headers.get("authorization")))
            return httpx.Response(409, json={"error": {"message": "slot_busy"}})
        return await studio.gw.builtin(request)
    studio.gw.on["/pages/ocr-release"] = release
    att = upload(studio, scan_pdf("ink", "ink"))
    assert (att["kind"], att["chars"], att["ocr"]["pages"]) == ("document", 0, 2)
    assert scan_of(studio, att["ocr"]["id"])["attachment_id"] == att["id"]
    events = sse(ask(studio, cid, ids=[att["id"]]))
    progress = [d for e, d in events if e == "ocr"]
    assert progress[0] == {"page": 0, "pages": 2} and progress[-1] == {"page": 2, "pages": 2}
    assert events[-1] == ("done", {"status": "done"})
    expected = f"Страница 1\n\n{TEXT}\n\nСтраница 2\n\n{TEXT}"
    assert studio.gw.chat_requests[-1]["messages"][1:] == [{"role": "user", "content": doc("скан.pdf", expected)}]
    assert studio.gw.chat_requests[-1]["messages"][0]["role"] == "system"                 # дата — и здесь
    sent = paths(studio)
    model_at = sent.index("/v1/chat/completions")
    assert sent.count("/pages/ocr") == 2
    assert sent[model_at - 2:model_at] == ["/pages/ocr-release"] * 2 and ocr.RETRY_SEC in studio.sleeps
    msgs = messages(studio, cid)
    assert msgs[1]["status"] == "done" and msgs[1]["ocr"] is None and msgs[0]["attachments"][0]["chars"] == len(expected)


def test_mixed_pdf_recognizes_only_pages_without_a_layer(studio):
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    att = upload(studio, scan_pdf(LAYER, "ink"))
    assert att["ocr"]["pages"] == 1
    events = sse(ask(studio, cid, ids=[att["id"]]))
    assert [d for e, d in events if e == "ocr"][-1] == {"page": 1, "pages": 1}
    assert len(studio.gw.ocr_requests) == 1
    expected = f"Страница 1\n\n{LAYER}\n\nСтраница 2\n\n{TEXT}"
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == doc("скан.pdf", expected)


def test_short_text_page_is_taken_from_its_layer(studio):
    """Строка без картинки (подписи, одна фраза) — текст слоя сразу, без распознавания и ожидания карты."""
    add_gateway(studio); cid = new_chat(studio)
    att = upload(studio, scan_pdf("Signed: J. Smith", "Payment is due in 30 days"))
    assert "ocr" not in att and att["chars"] == len("Signed: J. Smith\n\nPayment is due in 30 days")
    assert sse(ask(studio, cid, ids=[att["id"]]))[-1] == ("done", {"status": "done"})
    assert paths(studio) == ["/v1/chat/completions"]
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM scans").fetchone()[0]) == 0


def test_camscanner_watermark_over_a_scan_is_recognized(studio):
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    att = upload(studio, scan_pdf("ink+Scanned with CamScanner", "Signed: J. Smith"))
    assert att["chars"] == 0 and att["ocr"]["pages"] == 1
    assert sse(ask(studio, cid, ids=[att["id"]]))[-1] == ("done", {"status": "done"})
    assert len(studio.gw.ocr_requests) == 1
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == doc(
        "скан.pdf", f"Страница 1\n\n{TEXT}\n\nСтраница 2\n\nSigned: J. Smith")


def test_only_blank_scanned_pages_do_not_touch_the_card(studio):
    """Текстовый PDF с пустой страницей: страница уходит в задание (пустую определяет рендер), но шлюз
    распознавания не зовётся, карта не освобождается — ни моделей картинок, ни слота ocr."""
    add_gateway(studio); cid = new_chat(studio)
    att = upload(studio, scan_pdf(LAYER, "blank"))
    assert att["ocr"]["pages"] == 1 and att["chars"] == 0
    assert sse(ask(studio, cid, ids=[att["id"]]))[-1] == ("done", {"status": "done"})
    assert paths(studio) == ["/v1/chat/completions"] and studio.gw.releases == []
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == doc("скан.pdf", f"Страница 1\n\n{LAYER}")
    assert [r["state"] for r in on_loop(studio, lambda: state.DB.execute(
        "SELECT state FROM scan_pages WHERE scan_id = ? ORDER BY n", (att["ocr"]["id"],)).fetchall())] == ["layer", "blank"]


def test_without_an_ocr_connection_the_chat_works_as_before(studio):
    gw = add_gateway(studio); cid = new_chat(studio)
    assert studio.c.delete(f"/api/sections/ocr/links/{gw}").status_code == 204
    att = upload(studio, scan_pdf(LAYER, "empty"))
    assert "ocr" not in att and att["chars"] == len(LAYER)
    sse(ask(studio, cid, ids=[att["id"]]))
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == doc("скан.pdf", LAYER)
    r = studio.c.post("/api/attachments", files={"file": ("скан.pdf", scan_pdf("ink"), "application/pdf")})
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "scan", "message": "это скан — подключите «Сканы» в настройках"}
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM scans").fetchone()[0]) == 0
    assert studio.gw.ocr_requests == []


def test_mixed_scan_pdf_without_ocr_is_refused(studio):
    """Сканы — только часть страниц, а «Распознавания» через сервер нет: отказ словами, как у переводчика, —
    иначе модель молча получила бы документ без страниц-сканов. Белая картинка без слоя — тоже скан: пустую
    страницу отличит лишь рендер."""
    gw = add_gateway(studio)
    owner = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (gw,)).fetchone()[0]
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")     # адрес убран из списка
    for pages in [(LAYER, "ink"), (LAYER, "blank", LAYER)]:
        r = studio.c.post("/api/attachments", files={"file": ("скан.pdf", scan_pdf(*pages), "application/pdf")})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "scan", pages
        assert r.json()["detail"]["message"] == (
            f"страниц-сканов в PDF — 1 из {len(pages)}, без распознавания модель получила бы документ без них; "
            + sections.unlisted(connections.get(owner, gw))), pages
    state.DB.execute("""UPDATE settings SET value = '["10.77.1.15:4000"]' WHERE key = 'allowlist'""")
    assert studio.c.delete(f"/api/sections/ocr/links/{gw}").status_code == 204
    r = studio.c.post("/api/attachments", files={"file": ("скан.pdf", scan_pdf(LAYER, "ink"), "application/pdf")})
    assert r.status_code == 422 and r.json()["detail"] == {"code": "scan", "message": (
        "страниц-сканов в PDF — 1 из 2, без распознавания модель получила бы документ без них"
        " — подключите «Сканы» в настройках")}
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM attachments").fetchone()[0]) == 0
    assert studio.gw.ocr_requests == []


def test_too_many_scanned_pages_are_refused_in_words(studio, monkeypatch):
    monkeypatch.setattr(ocr_render, "PAGES_MAX", 2)
    add_gateway(studio)
    r = studio.c.post("/api/attachments", files={"file": ("скан.pdf", scan_pdf(LAYER, "ink", "ink", "ink"),
                                                          "application/pdf")})
    assert r.status_code == 422 and r.json()["detail"]["message"] == (
        "больше 2 страниц-сканов — распознайте его частями в разделе «Распознавание»")
    assert on_loop(studio, lambda: state.DB.execute("SELECT count(*) FROM attachments").fetchone()[0]) == 0


def test_direct_connection_turn_is_not_ready_until_the_scan_is_done(studio):
    add_gateway(studio)
    cloud = add_connection(studio)
    cid = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    studio.gw.ocr_hold = True
    att = upload(studio, scan_pdf("ink"))
    r = ask(studio, cid, ids=[att["id"]])
    assert r.status_code == 409 and r.json()["detail"] == {"code": "not_ready", "message": "скан ещё распознаётся"}
    assert messages(studio, cid) == []
    studio.gw.ocr_hold = False
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans").fetchone()[0] == "done")
    r = ask(studio, cid, ids=[att["id"]])
    assert r.status_code == 200 and r.json()["messages"][-1]["content"] == doc("скан.pdf", f"Страница 1\n\n{TEXT}")


def test_failed_scan_is_an_error_answer_and_the_model_is_not_called(studio):
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    studio.gw.ocr_replies = [(500, {"error": {"message": "внутренняя ошибка"}})]
    att = upload(studio, scan_pdf("ink"))
    events = sse(ask(studio, cid, ids=[att["id"]]))
    words = events[-1][1]["message"]
    assert events[-1][0] == "error" and words.startswith("скан не распознан: ")
    assert studio.gw.chat_requests == []
    answer = messages(studio, cid)[1]
    assert answer["status"] == "error" and answer["ocr"] is None
    # вернувшись в диалог, человек видит причину, а не «ответ не получен» (ревью M1); модели ответ не уходит
    assert (answer["error"], answer["content"]) == (words, "")
    assert messages(studio, cid)[0]["error"] is None
    assert sse(ask(studio, cid, "ещё раз"))[-1] == ("done", {"status": "done"})
    assert all(m["role"] != "assistant" for m in studio.gw.chat_requests[-1]["messages"])


def test_sent_scan_is_found_by_the_chat_search(studio):
    """Текст отправленного скана — в поиске раздела «Чат», и когда раннер дописал его после хода; документ
    удалили в «Распознавании» — в диалоге текст остался, и в поиске тоже (ревью M2)."""
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    att = upload(studio, scan_pdf("ink"))
    assert sse(ask(studio, cid, ids=[att["id"]]))[-1] == ("done", {"status": "done"})
    found = studio.c.post("/api/search", json={"q": "страницы", "section": "chat"}).json()["items"]
    assert [(i["id"], i["part"]) for i in found] == [(cid, att["id"])]
    assert studio.c.delete(f"/api/ocr/{att['ocr']['id']}").status_code == 204
    found = studio.c.post("/api/search", json={"q": "страницы"}).json()["items"]
    assert [(i["section"], i["id"], i["part"]) for i in found] == [("chat", cid, att["id"])]


def test_direct_connection_scan_is_indexed_once(studio):
    add_gateway(studio)
    cloud = add_connection(studio)
    cid = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    att = upload(studio, scan_pdf("ink"))
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans").fetchone()[0] == "done")
    assert ask(studio, cid, ids=[att["id"]]).status_code == 200
    rows = on_loop(studio, lambda: state.DB.execute("SELECT section FROM search_docs WHERE part = ?",
                                                    (att["id"],)).fetchall())
    assert [r[0] for r in rows] == ["chat"]


def test_scan_with_the_ocr_address_off_the_allowlist_says_so(studio):
    """Подключение «Распознавания» есть, но адрес убран из списка: слова — как у sections.resolve (ревью M8)."""
    add_gateway(studio)
    on_loop(studio, lambda: state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'"))
    r = studio.c.post("/api/attachments", files={"file": ("скан.pdf", scan_pdf("ink"), "application/pdf")})
    assert r.status_code == 422
    assert r.json()["detail"]["message"] == ("адрес подключения «srvllm» убран из списка разрешённых — попросите"
                                             " администратора внести его")


def test_stop_while_waiting_cancels_the_scan(studio):
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.ocr_hold = True
    att = upload(studio, scan_pdf("ink", "ink"))
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": QUESTION, "attachment_ids": [att["id"]]}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn"
            assert next(d for e, d in events if e == "ocr") == {"page": 0, "pages": 2}
            wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
            assert studio.c.post(f"/api/chats/{cid}/stop").status_code == 204
            rest = [(e, d) for e, d in events if e not in ("ping", "ocr")]
        assert scan_of(studio, att["ocr"]["id"])["state"] == "cancelling"
    finally:
        studio.gw.ocr_hold = False
    assert rest == [("done", {"status": "stopped"})]
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans").fetchone()[0] == "cancelled")
    assert messages(studio, cid)[1]["status"] == "stopped" and studio.gw.chat_requests == []
    assert len(studio.gw.ocr_requests) == 1


def test_reload_shows_the_scan_progress(studio):
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.ocr_hold = True
    att = upload(studio, scan_pdf("ink", "ink"))
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": QUESTION, "attachment_ids": [att["id"]]}) as r:
            events = events_of(r.iter_lines())
            assert next(d for e, d in events if e == "ocr") == {"page": 0, "pages": 2}
            user, answer = messages(studio, cid)
            assert (user["ocr"], answer["ocr"], answer["status"]) == (None, {"page": 0, "pages": 2}, "streaming")
            # вложение идущего скана — с его id, а не «0 зн.»: страница прячет знаки и опрашивает документ
            assert user["attachments"][0]["ocr"] == {"id": att["ocr"]["id"]}
            studio.gw.ocr_hold = False
            assert [(e, d) for e, d in events if e not in ("ping", "ocr", "delta")][-1] == ("done", {"status": "done"})
    finally:
        studio.gw.ocr_hold = False
    user, answer = messages(studio, cid)
    assert answer["ocr"] is None and "ocr" not in user["attachments"][0] and user["attachments"][0]["chars"] > 0


def test_restart_while_a_turn_waits_leaves_a_marker(studio):
    """Остановка службы посреди ожидания (main: ocr.stop, затем chat.stop_all) и старт (recover): ответ —
    ошибка, во вложении — пометка, и следующий ход шлёт модели её, а не «None»."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.ocr_hold = True
    att = upload(studio, scan_pdf("ink"))
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": QUESTION, "attachment_ids": [att["id"]]}) as r:
            events = events_of(r.iter_lines())
            assert next(e for e, _ in events if e == "ocr")

        async def restart():
            await ocr.stop()
            await chat.stop_all()
            chat.recover()
            ocr.recover()
        on_loop(studio, restart)
    finally:
        studio.gw.ocr_hold = False
    marker = ocr.attachment_marker(ocr.INTERRUPTED)
    assert messages(studio, cid)[1]["status"] == "error" and att_text(studio, att["id"]) == (marker, len(marker))
    assert studio.gw.chat_requests == []
    assert sse(ask(studio, cid, "ещё раз"))[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[-1]["messages"][-1]["content"]
    assert sent == doc("скан.pdf", marker, f"{QUESTION}\n\nещё раз") and "None" not in sent


def test_wait_does_not_eat_the_answer_deadline(studio, monkeypatch):
    monkeypatch.setattr(chat, "STREAM_TOTAL_SEC", 3000)
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.ocr_hold = True
    att = upload(studio, scan_pdf("ink"))
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": QUESTION, "attachment_ids": [att["id"]]}) as r:
            events = events_of(r.iter_lines())
            assert next(e for e, _ in events if e == "ocr")
            studio.clock[0] += 10_000                                      # распознавание шло дольше срока ответа
            studio.gw.ocr_hold = False
            rest = [(e, d) for e, d in events if e in ("done", "error")]
    finally:
        studio.gw.ocr_hold = False
    assert rest == [("done", {"status": "done"})]
    assert messages(studio, cid)[1]["content"] == "Здравствуйте!"


def test_too_big_after_recognition_is_an_error_in_words(studio, monkeypatch):
    """Предел контекста — после распознавания, перед моделью, прежними словами отказа."""
    monkeypatch.setitem(chat.CONTEXT, "local-gemma-abl", chat.RESERVE_TOKENS + 10)
    add_gateway(studio); cid = new_chat(studio)
    held_until_the_turn_waits(studio)
    att = upload(studio, scan_pdf("ink"))
    events = sse(ask(studio, cid, ids=[att["id"]]))
    assert events[-1][0] == "error" and events[-1][1]["message"].startswith("Не помещается в контекст модели")
    assert studio.gw.chat_requests == []


def test_unsent_scan_attachment_cancels_its_scan(studio):
    add_gateway(studio)
    studio.gw.ocr_hold = True
    att = upload(studio, scan_pdf("ink"))
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    try:
        assert studio.c.delete(f"/api/attachments/{att['id']}").status_code == 204
        assert scan_of(studio, att["ocr"]["id"])["state"] == "cancelling"
    finally:
        studio.gw.ocr_hold = False
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans").fetchone()[0] == "cancelled")
