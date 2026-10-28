"""Перевод снимка (спека 2026-10-28-studio-translator-design, 6.5, 6.6): разбор ответа HunyuanOCR с координатами
(`translate_spot`), строки в блоки, распознавание тем же PNG, что хранит запись, нумерованный перевод блоков и
учёт карты слота ocr (`ocr.borrowed`, `ocr.EXTERNAL`): free_card до, release после, чужой идущий не задет."""
import base64
import io
import json
import logging
import time

from PIL import Image

import images
import ocr
import state
import translate
import translate_spot
from conftest import add_gateway, make_user, signin
from scans import photo
from test_translate import (count, on_loop, owner_of_tr, parts_of, paths, row_of, send_file, start_file, wait_row,
                            wait_until)
from translate_spot import Line

SPOTTED = "Exit(93,146),(491,214)Staff only(193,640),(379,706)"


def spot_reply(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def ocr_releases(s) -> int:
    return sum(1 for model, _ in s.gw.releases if model is None)


def sent_png(request) -> bytes:
    image = json.loads(request.content)["messages"][1]["content"][0]["image_url"]["url"]
    return base64.b64decode(image.removeprefix("data:image/png;base64,"))


def sent_prompt(request) -> str:
    return json.loads(request.content)["messages"][1]["content"][1]["text"]


# ---- разбор ответа ----------------------------------------------------------------------------------

def test_parse_reads_lines_with_brackets_in_text():
    lines = translate_spot.parse("Exit (north)(93,146),(491,214)Staff only(193,640),(379,706)tail")
    assert lines == [Line("Exit (north)", (93, 146, 491, 214)), Line("Staff only", (193, 640, 379, 706))]


def test_parse_clamps_and_orders_boxes():
    assert translate_spot.parse("A(1200,50),(10,20)")[0].box == (10, 20, 1000, 50)


def test_parse_survives_odd_answers():
    assert translate_spot.parse("") == []
    assert translate_spot.parse("Текст без координат (и со скобками)") == []
    # обрезанный ответ: последняя строка без второй точки — мимо, прежние целы
    assert translate_spot.parse("Exit(93,146),(491,214)\nStaff on(193,640),(3") == [Line("Exit", (93, 146, 491, 214))]
    # рамка без текста и строка из пробелов — мимо; переносы между строками ответа — не текст
    assert translate_spot.parse("(1,2),(3,4)\n  (5,6),(7,8)\nExit(9,10),(11,12)") == [Line("Exit", (9, 10, 11, 12))]
    # наклонная строка четырьмя точками — рамка-объединение, а не «,» строкой
    assert translate_spot.parse("Tilted(10,20),(110,25),(108,60),(8,55)Next(1,2),(3,4)") == [
        Line("Tilted", (8, 20, 110, 60)), Line("Next", (1, 2, 3, 4))]


def test_parse_is_linear_on_long_answers_without_coordinates():
    """Ревью I1: ленивое `(.*?)` до пары точек на 50 000 знаков без координат — 11 с цикла событий."""
    for content in ("слово (и скобка) " * 3000, "Exit(93,146),(491,214)" + "x(1,2)" * 9000):
        start = time.perf_counter()
        lines = translate_spot.parse(content)
        assert time.perf_counter() - start < 0.1 and len(lines) <= 1


def test_group_joins_close_aligned_lines_and_splits_far_ones():
    lines = [Line("Ticket office on", (90, 260, 400, 300)), Line("the right", (90, 305, 300, 345)),
             Line("Staff only", (190, 640, 380, 700))]
    assert translate_spot.group(lines) == [{"source": "Ticket office on the right", "box": [90, 260, 400, 345]},
                                           {"source": "Staff only", "box": [190, 640, 380, 700]}]


def test_group_keeps_columns_headings_and_hyphens_apart():
    lines = [Line("Left column", (50, 100, 300, 130)), Line("Right column", (600, 100, 900, 130)),
             Line("conti-", (50, 135, 300, 165)), Line("nues here", (50, 170, 300, 200)),
             Line("Second half", (600, 135, 900, 165)),
             Line("BIG HEADING", (50, 205, 600, 300)),                  # втрое выше — заголовок, свой блок
             Line("Well-", (50, 400, 300, 430)), Line("Known", (50, 435, 300, 465))]
    assert translate_spot.group(lines) == [
        {"source": "Left column continues here", "box": [50, 100, 300, 200]},
        {"source": "Right column Second half", "box": [600, 100, 900, 165]},
        {"source": "BIG HEADING", "box": [50, 205, 600, 300]},
        {"source": "Well- Known", "box": [50, 400, 300, 465]}]          # заглавная после дефиса — не перенос
    assert translate_spot.group([]) == []


# ---- снимок в переводе -----------------------------------------------------------------------------

def test_photo_is_rotated_once_and_the_same_png_is_recognized_and_shown(studio):
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply(SPOTTED)
    tid = start_file(studio, "Снимок.jpg", photo("JPEG", (40, 20), orientation=6))
    row = wait_row(studio, tid)
    assert (row["state"], row["kind"], row["name"]) == ("done", "photo", "Снимок.jpg"), row["detail"]
    shown = studio.c.get(f"/api/media/{row['source_media_id']}")
    assert shown.status_code == 200 and shown.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(shown.content)).size == (20, 40)       # EXIF 6 — повёрнут один раз
    assert [sent_png(r) for r in studio.gw.ocr_requests] == [shown.content]
    assert count(studio, "media") == 1                                  # исходный JPEG не хранится
    assert on_loop(studio, lambda: state.DB.execute("SELECT download_name, mime FROM media").fetchone()[:]) == (
        "Снимок.png", "image/png")


def test_photo_blocks_are_translated_in_one_numbered_request(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply(SPOTTED)
    studio.gw.chat_reply = ["en"]                                       # определение языка — без потока
    studio.gw.chat_script = [["[1] Выход\n", "[2] Только персонал"]]
    tid = start_file(studio, "Вокзал.png", photo("PNG", (60, 40)), source="auto")
    row = wait_row(studio, tid)
    assert (row["state"], row["detail"], row["parts"], row["done_parts"]) == ("done", translate.DONE, 2, 2)
    assert (row["source"], row["text"], row["detected_lang"]) == ("Exit\nStaff only", "Выход\nТолько персонал", "en")
    assert [sent_prompt(r) for r in studio.gw.ocr_requests] == [translate_spot.PROMPT]
    streams = [b for b in studio.gw.chat_requests if b.get("stream")]
    assert len(streams) == 1 and streams[0]["messages"][-1]["content"] == "[1] Exit\n[2] Staff only"
    assert translate_spot.PROMPT not in streams[0]["messages"][0]["content"]
    assert "[n] translation" in streams[0]["messages"][0]["content"]
    parts = studio.c.get(f"/api/translate/{tid}").json()["parts"]
    assert [(p["source"], p["box"], p["text"], p["state"]) for p in parts] == [
        ("Exit", [93, 146, 491, 214], "Выход", "done"), ("Staff only", [193, 640, 379, 706], "Только персонал", "done")]
    for secret in ("Exit", "Staff", "Выход", "Вокзал"):
        assert secret not in caplog.text
    assert "2 блоков" in caplog.text


def test_numbering_mismatch_falls_back_to_one_block_per_request(studio):
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply(SPOTTED)
    studio.gw.chat_script = [["[1] Выход"], ["Выход"], ["Только персонал"]]
    row = wait_row(studio, start_file(studio, "Вокзал.png", photo("PNG", (60, 40))))
    assert (row["state"], row["text"]) == ("done", "Выход\nТолько персонал")
    streams = [b["messages"][-1]["content"] for b in studio.gw.chat_requests if b.get("stream")]
    assert streams == ["[1] Exit\n[2] Staff only", "Exit", "Staff only"]
    singles = [b for b in studio.gw.chat_requests if b.get("stream")][1:]
    assert all("[n] translation" not in b["messages"][0]["content"] for b in singles)


def test_photo_without_text_is_done_with_words(studio):
    """Пустой ответ — «готово» без текста; текст без координат — ошибка (повтор может помочь), а не «не найден»."""
    add_gateway(studio)
    studio.gw.ocr_replies = [(200, spot_reply("")), (200, spot_reply("просто слова без рамок"))]
    row = wait_row(studio, start_file(studio, "Стена.png", photo("PNG", (60, 40))))
    assert (row["state"], row["detail"], row["parts"], row["text"]) == ("done", "текст на снимке не найден", 0, None)
    row = wait_row(studio, start_file(studio, "Стена.png", photo("PNG", (60, 40))))
    assert (row["state"], row["detail"], row["parts"], row["text"]) == (
        "error", "распознавание вернуло текст без координат — попробуйте ещё раз", 0, None)
    assert studio.gw.chat_requests == [] and len(studio.gw.ocr_requests) == 2
    assert ocr.EXTERNAL == {} and ocr_releases(studio) == 2


def test_photo_needs_the_ocr_section(studio):
    gw = add_gateway(studio)
    assert studio.c.delete(f"/api/sections/ocr/links/{gw}").status_code == 204
    r = send_file(studio, "Снимок.jpg", photo())
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "no_ocr", "message": "чтобы переводить снимки, подключите «Распознавание» в настройках"}
    assert (count(studio, "media"), count(studio, "translations")) == (0, 0)
    r = send_file(studio, "Снимок.jpg", b"\xff\xd8\xff")               # не снимок и не документ
    assert r.status_code == 415 and r.json()["detail"]["code"] == "format"


def test_photo_frees_and_releases_the_ocr_slot_around_spotting(studio):
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply("Exit(93,146),(491,214)")
    row = wait_row(studio, start_file(studio, "Снимок.png", photo("PNG", (60, 40))))
    assert row["state"] == "done", row["detail"]
    frees = ["/slot/images/release"] * len(images.MODELS) + ["/slot/images/descriptions/release"]
    # модели картинок отпустил free_card того же шлюза — перед переводом их не отпускают второй раз
    assert paths(studio) == [*frees, "/pages/ocr", "/pages/ocr-release", "/v1/chat/completions"]
    assert ocr.EXTERNAL == {} and ocr.LEASED == {}


def test_chat_waits_while_a_photo_is_spotted(studio):
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply("Exit(93,146),(491,214)")
    studio.gw.ocr_hold = True
    tid = start_file(studio, "Снимок.png", photo("PNG", (60, 40)))
    uid = on_loop(studio, owner_of_tr, tid)
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    assert ocr.EXTERNAL == {tid: uid} and not on_loop(studio, ocr.idle, uid)
    assert on_loop(studio, ocr._scanning)                               # release «Сканов» слот не отнимет
    assert row_of(studio, tid)["detail"] == "распознаю снимок"
    studio.gw.ocr_hold = False
    assert wait_row(studio, tid)["state"] == "done"
    assert ocr.EXTERNAL == {} and on_loop(studio, ocr.idle, uid)


def test_scan_release_waits_for_a_photo_being_spotted(studio):
    """Анти-цель 5: документ «Сканов» кончился, пока снимок перевода распознаётся, — release его не обрывает;
    слот отпускает конец снимка, один раз."""
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply("Exit(93,146),(491,214)")
    studio.gw.ocr_hold = lambda request: sent_prompt(request) == translate_spot.PROMPT
    tid = start_file(studio, "Снимок.png", photo("PNG", (60, 40)))
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    r = studio.c.post("/api/ocr", files=[("file", ("Страница.png", photo("PNG", (60, 40)), "image/png"))])
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    wait_until(studio, lambda: state.DB.execute("SELECT state FROM scans WHERE id = ?", (sid,)).fetchone()[0] == "done")
    wait_until(studio, lambda: not ocr.RUNNERS)
    assert ocr_releases(studio) == 0 and ocr.LEASED != {}
    studio.gw.ocr_hold = False
    assert wait_row(studio, tid)["state"] == "done"
    assert ocr_releases(studio) == 1 and ocr.LEASED == {} and ocr.EXTERNAL == {}


def test_spotting_error_and_stop_release_the_slot(studio):
    add_gateway(studio)
    studio.gw.ocr_replies = [(200, spot_reply("Exit(93,146),(491,214)Sta", "length"))]
    row = wait_row(studio, start_file(studio, "Снимок.png", photo("PNG", (60, 40))))
    assert (row["state"], row["detail"]) == ("error", "снимок не распознан: ответ модели обрезан — страница слишком плотная")
    assert ocr.EXTERNAL == {} and ocr_releases(studio) == 1 and studio.gw.chat_requests == []

    studio.gw.ocr_hold = True
    tid = start_file(studio, "Снимок.png", photo("PNG", (60, 40)))
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 2)
    assert studio.c.post(f"/api/translate/{tid}/cancel").json() == {"state": "cancelling"}
    studio.gw.ocr_hold = False                                          # запрос не обрывается — ответ выброшен
    row = wait_row(studio, tid)
    assert (row["state"], row["detail"], row["parts"]) == ("cancelled", translate.CANCELLED, 0)
    assert ocr.EXTERNAL == {} and ocr_releases(studio) == 2 and studio.gw.chat_requests == []


def test_busy_card_is_waited_for_while_spotting(studio):
    add_gateway(studio)
    studio.gw.ocr_replies = [(503, {"error": {"message": "slot busy"}})] * 2 + [(200, spot_reply("Exit(93,146),(491,214)"))]
    row = wait_row(studio, start_file(studio, "Снимок.png", photo("PNG", (60, 40))))
    assert (row["state"], row["source"]) == ("done", "Exit") and len(studio.gw.ocr_requests) == 3
    assert studio.sleeps.count(translate.RETRY_SEC) == 2 and ocr.EXTERNAL == {} and ocr_releases(studio) == 1


def test_stop_user_ends_spotting_and_releases_in_the_background(studio):
    add_gateway(studio)
    studio.gw.ocr_hold = True
    tid = start_file(studio, "Снимок.png", photo("PNG", (60, 40)))
    uid = on_loop(studio, owner_of_tr, tid)
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == 1)
    on_loop(studio, translate.stop_user, uid)
    assert row_of(studio, tid)["state"] == "cancelled" and ocr.EXTERNAL == {}
    studio.gw.ocr_hold = False
    wait_until(studio, lambda: ocr_releases(studio) >= 1 and ocr.LEASED == {})


def test_repeat_of_a_photo_copies_blocks_without_ocr(studio):
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply(SPOTTED)
    studio.gw.chat_script = [["[1] Выход\n[2] Только персонал"], ["[1] Ausgang\n[2] Nur Personal"]]
    tid = start_file(studio, "Вокзал.png", photo("PNG", (60, 40)))
    first = wait_row(studio, tid)
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "de"})
    assert r.status_code == 202, r.text
    again = wait_row(studio, r.json()["id"])
    assert (again["state"], again["kind"], again["text"]) == ("done", "photo", "Ausgang\nNur Personal")
    assert again["source_media_id"] == first["source_media_id"] and len(studio.gw.ocr_requests) == 1
    assert [p["box"] for p in parts_of(studio, again["id"])] == [p["box"] for p in parts_of(studio, tid)]


def test_repeat_of_a_photo_without_text_recognizes_again(studio):
    add_gateway(studio)
    studio.gw.ocr_replies = [(200, spot_reply(""))]
    tid = start_file(studio, "Снимок.png", photo("PNG", (60, 40)))
    assert wait_row(studio, tid)["detail"] == "текст на снимке не найден"
    studio.gw.ocr_reply = spot_reply("Exit(93,146),(491,214)")
    r = studio.c.post(f"/api/translate/{tid}/repeat", json={"source_lang": "en", "target_lang": "ru"})
    assert r.status_code == 202, r.text
    again = wait_row(studio, r.json()["id"])
    assert (again["state"], again["source"]) == ("done", "Exit") and len(studio.gw.ocr_requests) == 2
    assert [sent_png(q) for q in studio.gw.ocr_requests][0] == [sent_png(q) for q in studio.gw.ocr_requests][1]


def test_foreign_photo_is_gone(studio):
    add_gateway(studio)
    studio.gw.ocr_reply = spot_reply("Exit(93,146),(491,214)")
    row = wait_row(studio, start_file(studio, "Снимок.png", photo("PNG", (60, 40))))
    make_user(studio, "friend")
    signin(studio, "friend")
    assert studio.c.get(f"/api/media/{row['source_media_id']}").status_code == 404
    assert studio.c.get(f"/api/translate/{row['id']}").status_code == 404
