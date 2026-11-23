"""«PDF со слоем» скана (05:I9, app/ocr_pdf.py и ocr.find_lines): невидимый текст на местах строк находят поиск и
выделение PDFium (движок PDF Chrome) и pypdf, и кириллица, и знаки вне BMP; рамки строк — по требованию, тем же
раннером и правилами карты, что распознавание, с прежним итогом документа после; повторная выгрузка шлюз не зовёт."""
import io
import json

import pypdfium2
import pytest
from pypdf import PdfReader, PdfWriter

import ocr
import ocr_pdf
import ocr_render
import state
from conftest import add_gateway
from scans import photo, scan_pdf
from test_ocr import NAME, answer, on_loop, page_rows, post_files, scan_row, wait_ocr, wait_until


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 300)


def texts(pdf: bytes) -> list[str]:
    doc = pypdfium2.PdfDocument(pdf)
    return [doc[i].get_textpage().get_text_range().replace("\r\n", "\n") for i in range(len(doc))]


def in_box(pdf: bytes, index: int, box) -> str:
    """Текст PDFium в рамке `box` (доли 0…1000 видимой страницы, начало — слева сверху)."""
    page = pypdfium2.PdfDocument(pdf)[index]
    w, h = page.get_size()
    x1, y1, x2, y2 = box
    return page.get_textpage().get_text_bounded(x1 * w / 1000 - 1, h - y2 * h / 1000 - 1, x2 * w / 1000 + 1,
                                                h - y1 * h / 1000 + 1)


def test_invisible_lines_are_found_in_their_boxes_over_the_source_pages():
    lines = [("Договор аренды №5", (100, 100, 600, 130)), ("Hello 😀 world", (100, 500, 900, 540))]
    source = scan_pdf("ink", "Layer text of a page from the text layer", "ink")
    pdf = ocr_pdf.build(source, [ocr_pdf.Page(index=0, lines=lines), ocr_pdf.Page(index=1),
                                 ocr_pdf.Page(index=2, lines=ocr_pdf.flow("строка один\n\n  строка   два\n"))])
    assert texts(pdf) == ["Договор аренды №5\nHello 😀 world", "Layer text of a page from the text layer",
                          "строка один\nстрока два"]
    assert in_box(pdf, 0, (100, 100, 600, 130)) == "Договор аренды №5"
    assert in_box(pdf, 0, (100, 500, 900, 540)) == "Hello 😀 world"
    assert [p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages][0] == "Договор аренды №5\nHello 😀 world"
    original, layered = pypdfium2.PdfDocument(source), pypdfium2.PdfDocument(pdf)
    for i in range(3):                                # вид страниц — как у исходника: текст невидим
        assert layered[i].get_size() == original[i].get_size()
        assert layered[i].render(scale=0.2).to_pil().tobytes() == original[i].render(scale=0.2).to_pil().tobytes()


def test_more_than_256_distinct_characters_and_a_rotated_page():
    """Знаков больше 256 — несколько шрифтов Type3; повёрнутая страница — рамки по видимой странице."""
    many = "".join(chr(c) for c in range(0x410, 0x450)) + "".join(chr(c) for c in range(0x4E00, 0x4F00))
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(scan_pdf("ink"))))
    w.pages[0].rotate(90)
    out = io.BytesIO()
    w.write(out)
    pdf = ocr_pdf.build(out.getvalue(), [ocr_pdf.Page(index=0, lines=[(many, (0, 0, 1000, 40)),
                                                                      ("УГОЛ", (0, 900, 200, 1000))])])
    page = pypdfium2.PdfDocument(pdf)[0]
    assert page.get_size() == (792, 612)
    assert texts(pdf) == [f"{many}\nУГОЛ"]
    assert in_box(pdf, 0, (0, 900, 200, 1000)) == "УГОЛ"


def test_photo_pages_are_jpeg_pages_with_the_layer():
    pdf = ocr_pdf.build(None, [ocr_pdf.Page(image=photo("PNG", (800, 600)), lines=[("Фото", (0, 0, 500, 100))]),
                               ocr_pdf.Page(image=photo("JPEG", (600, 900)))])
    doc = pypdfium2.PdfDocument(pdf)
    assert [doc[i].get_size() for i in range(2)] == [(842, 631.5), (561.3333129882812, 842)]
    assert texts(pdf) == ["Фото", ""]
    assert in_box(pdf, 0, (0, 0, 500, 100)) == "Фото"
    image = PdfReader(io.BytesIO(pdf)).pages[1].images[0]
    assert image.image.size == (600, 900)


SPOT = "Первая строка(100,100),(600,130)Second line(100,200),(500,230)"


def done_pdf(s, *pages):
    sid = post_files(s, [(NAME, scan_pdf(*pages))]).json()["id"]
    return sid, wait_ocr(s, sid, {"done"})


def test_lines_are_found_on_request_once_and_the_document_comes_back_done(studio):
    add_gateway(studio)
    sid, before = done_pdf(studio, "ink", "ink", "blank")
    asked = len(studio.gw.ocr_requests)
    studio.gw.ocr_replies = [(200, answer(SPOT)), (200, answer(""))]       # вторая страница — без строк
    studio.gw.ocr_hold = True
    r = studio.c.post(f"/api/ocr/{sid}/layer")
    assert r.status_code == 200 and r.json() == {"ready": False}
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == asked + 1)
    jobs = studio.c.get("/api/jobs").json()["running"]
    assert [(j["title"], j["detail"]) for j in jobs] == [(f"PDF со слоем «{NAME}»", "ищу строки для PDF · стр. 1 из 2")]
    assert studio.c.get(f"/api/ocr/{sid}/layer").json() == {"ready": False, "error": None}
    studio.gw.ocr_hold = False
    item = wait_ocr(studio, sid)
    assert (item["state"], item["detail"], item["finished"]) == ("done", "готово", before["finished"])
    assert scan_row(studio, sid)["layer_prior"] is None
    spot = [json.loads(q.content)["messages"][1]["content"][1]["text"] for q in studio.gw.ocr_requests[asked:]]
    assert spot == [ocr.translate_spot.PROMPT] * 2
    assert [p["boxes"] for p in page_rows(studio, sid)] == [
        json.dumps([["Первая строка", 100, 100, 600, 130], ["Second line", 100, 200, 500, 230]], ensure_ascii=False),
        "[]", None]
    assert studio.gw.requests[-1].url.path == "/pages/ocr-release"
    assert studio.c.get(f"/api/ocr/{sid}/layer").json() == {"ready": True, "error": None}

    r = studio.c.get(f"/api/ocr/{sid}/export?format=pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "filename*=UTF-8''%D0%94%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80%20%D0%B0%D1%80%D0%B5%D0%BD%D0%B4%D1%8B.pdf" \
        in r.headers["content-disposition"]
    assert texts(r.content) == ["Первая строка\nSecond line", "Договор\nТекст страницы.", ""]
    assert in_box(r.content, 0, (100, 100, 600, 130)) == "Первая строка"
    # повторная выгрузка — без карты
    asked = len(studio.gw.requests)
    assert studio.c.post(f"/api/ocr/{sid}/layer").json() == {"ready": True}
    assert studio.c.get(f"/api/ocr/{sid}/export?format=pdf").status_code == 200
    assert len(studio.gw.requests) == asked and ocr.JOBS == {}


def test_edited_pages_and_s06_need_no_card(studio):
    """Правленая страница — её текст без координат; подключение s06 координат не даёт — слой без них, сразу."""
    cid = add_gateway(studio)
    sid, _ = done_pdf(studio, "ink", "ink")
    assert studio.c.patch(f"/api/ocr/{sid}/pages/1", json={"text": "Правка\n\nвторая строка"}).status_code == 200
    on_loop(studio, lambda: state.DB.execute("UPDATE connections SET s06 = 1 WHERE id = ?", (cid,)))
    asked = len(studio.gw.requests)
    assert studio.c.post(f"/api/ocr/{sid}/layer").json() == {"ready": True}
    assert texts(studio.c.get(f"/api/ocr/{sid}/export?format=pdf").content) == [
        "Правка\nвторая строка", "Договор\nТекст страницы."]
    assert len(studio.gw.requests) == asked and scan_row(studio, sid)["state"] == "done"


def test_cancel_and_failure_bring_back_the_previous_outcome(studio):
    add_gateway(studio)
    studio.gw.ocr_replies = [(200, answer("# Готово")), (200, answer("Обрывок", "length"))]
    sid = post_files(studio, [(NAME, scan_pdf("ink", "ink"))]).json()["id"]
    before = wait_ocr(studio, sid)
    assert (before["state"], before["failed_pages"]) == ("done", 1)
    asked = len(studio.gw.ocr_requests)
    studio.gw.ocr_hold = True
    assert studio.c.post(f"/api/ocr/{sid}/layer").json() == {"ready": False}
    wait_until(studio, lambda: len(studio.gw.ocr_requests) == asked + 1)
    assert studio.c.post(f"/api/ocr/{sid}/cancel").json() == {"state": "cancelling"}
    studio.gw.ocr_hold = False
    item = wait_ocr(studio, sid)
    assert (item["state"], item["detail"], item["finished"]) == ("done", "готово", before["finished"])
    assert page_rows(studio, sid)[0]["boxes"] is None                  # ответ отменённого выброшен
    assert studio.c.get(f"/api/ocr/{sid}/layer").json() == {"ready": False, "error": None}

    studio.gw.ocr_replies = [(500, {"error": {"message": "boom"}})]
    assert studio.c.post(f"/api/ocr/{sid}/layer").json() == {"ready": False}
    item = wait_ocr(studio, sid)
    assert (item["state"], item["detail"]) == ("done", "готово")
    got = studio.c.get(f"/api/ocr/{sid}/layer").json()
    assert got["ready"] is False and got["error"]
    # подключения больше нет — слой без координат, без отказа
    on_loop(studio, lambda: state.DB.execute("DELETE FROM connections"))
    assert studio.c.post(f"/api/ocr/{sid}/layer").json() == {"ready": True}
    assert texts(studio.c.get(f"/api/ocr/{sid}/export?format=pdf").content) == ["Готово", ""]


def test_a_restart_mid_search_returns_the_previous_outcome(studio):
    add_gateway(studio)
    sid, before = done_pdf(studio, "ink")
    prior = json.dumps({"state": "done", "detail": "готово", "finished": before["finished"]})
    on_loop(studio, lambda: state.DB.execute("UPDATE scans SET state = 'working', finished = NULL, layer_prior = ?"
                                             " WHERE id = ?", (prior, sid)))
    on_loop(studio, ocr.recover)
    row = scan_row(studio, sid)
    assert (row["state"], row["detail"], row["finished"], row["layer_prior"]) == (
        "done", "готово", before["finished"], None)


def test_photo_document_and_refusals(studio):
    add_gateway(studio)
    sid = post_files(studio, [("a.jpg", photo()), ("b.png", photo("PNG"))]).json()["id"]
    wait_ocr(studio, sid, {"done"})
    studio.gw.ocr_reply = answer("Фото(0,0),(500,100)")
    assert studio.c.post(f"/api/ocr/{sid}/layer").json() == {"ready": False}
    wait_ocr(studio, sid)
    pdf = studio.c.get(f"/api/ocr/{sid}/export?format=pdf").content
    assert texts(pdf) == ["Фото", "Фото"] and pypdfium2.PdfDocument(pdf)[0].get_size() == (842, 631.5)
    assert in_box(pdf, 1, (0, 0, 500, 100)) == "Фото"
    studio.gw.ocr_hold = True
    other = post_files(studio, [("c.jpg", photo())]).json()["id"]
    r = studio.c.post(f"/api/ocr/{other}/layer")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    studio.gw.ocr_hold = False
    wait_ocr(studio, other)
