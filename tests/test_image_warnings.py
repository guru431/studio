"""Фильтр `error::DecompressionBombWarning` и потоки (находка 2026-11-09).

`warnings.catch_warnings()` не потокобезопасен: два перекрывшихся вызова оставляют фильтр «ошибка» на
весь процесс. `image_upload.py` — копия модуля переводчика и сохраняет его; свои модули его не ставят
(`ocr_render`), а разбор картинок ответа и моста не падает 500, если фильтр всё же остался."""
import base64
import io
import threading
import warnings

import pytest
from fastapi import HTTPException
from PIL import Image

import claude_code
import images
import ocr_render
from test_images import png_header


@pytest.fixture
def filters():
    saved = warnings.filters[:]
    yield
    warnings.filters[:] = saved


def photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(out, "PNG")
    return out.getvalue()


def test_parallel_photo_checks_leave_the_process_warning_filters_as_they_were(filters, monkeypatch):
    """Порядок, при котором `catch_warnings` теряет фильтр: A вошёл, B вошёл, A вышел, B вышел."""
    before = warnings.filters[:]
    original = Image.open
    a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()

    def gated(*args, **kwargs):
        if threading.current_thread().name == "A":
            a_in.set()
            b_in.wait(5)
        else:
            a_in.wait(5)
            b_in.set()
            a_out.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", gated)

    def run_a():
        ocr_render.check_photo(photo())
        a_out.set()

    threads = [threading.Thread(target=run_a, name="A"),
               threading.Thread(target=ocr_render.check_photo, args=(photo(),), name="B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert warnings.filters == before


BOMB = png_header(10_000, 9_000)     # 90 Мп: больше MAX_IMAGE_PIXELS, меньше двух — предупреждение, не ошибка


def test_reply_picture_is_too_big_even_when_the_warning_filter_leaked(filters):
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with pytest.raises(images.JobError) as e:
        images._picture(BOMB)
    assert e.value.code == "too_big"


def test_bridge_image_is_413_even_when_the_warning_filter_leaked(filters):
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with pytest.raises(HTTPException) as e:
        claude_code._image_bytes({"data": base64.b64encode(BOMB).decode()})
    assert e.value.status_code == 413 and e.value.detail["code"] == "too_large"


def test_ocr_photo_check_still_refuses_a_bomb_without_the_filter(filters):
    with pytest.raises(ocr_render.ScanError) as e:
        ocr_render.check_photo(BOMB)
    assert e.value.status == 413
