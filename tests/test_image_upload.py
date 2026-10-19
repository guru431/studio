"""Input image contract: bounded multipart, valid pixels, no original metadata."""
import asyncio
import io

import httpx
import pytest
from PIL import Image

import conftest  # noqa: F401 — кладёт app/ в sys.path
from image_upload import (MAX_BODY, VIDEO_PIXELS, VIDEO_UPLOAD, UploadError, decode_form, normalize_image,
                          prepare_mask, read_image_form, read_video_form, restrict_edit)


def multipart(fields=None, files=None):
    request = httpx.Request("POST", "http://test", data=fields or {"prompt": "test"},
                            files=files or {"image": ("source.jpg", b"image", "image/jpeg")})
    return request.read(), request.headers["content-type"]


def test_image_array_and_utf8_prompt_are_accepted():
    raw, ct = multipart({"prompt": "новый фон"}, {"image[]": ("s.jpg", b"image")})
    fields, image = decode_form(raw, ct)
    assert fields == {"prompt": "новый фон"} and image == b"image"


def test_duplicate_images_are_rejected():
    raw, ct = multipart(files=[("image", ("1.png", b"one")), ("image[]", ("2.png", b"two"))])
    with pytest.raises(UploadError, match="ровно один"):
        decode_form(raw, ct)


def test_truncated_multipart_is_rejected():
    raw, ct = multipart()
    with pytest.raises(UploadError):
        decode_form(raw[:-40], ct)


def test_orientation_is_applied_and_metadata_removed():
    raw = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private description"
    Image.new("RGB", (40, 20), "blue").save(raw, "JPEG", exif=exif)
    with Image.open(io.BytesIO(normalize_image(raw.getvalue()))) as image:
        assert image.size == (20, 40) and image.format == "PNG"
        assert not image.getexif()


def test_stream_limit_does_not_trust_content_length():
    class Request:
        headers = {"content-length": "0"}

        async def stream(self):
            yield b"x" * (MAX_BODY + 1)
            raise AssertionError("must stop reading at the limit")

    with pytest.raises(UploadError) as exc:
        asyncio.run(read_image_form(Request()))
    assert exc.value.status == 413


def encoded(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_mask_compositing_keeps_every_unselected_pixel_identical():
    original = Image.new("RGB", (32, 24))
    original.putdata([(x * 7, y * 9, (x + y) * 4) for y in range(24) for x in range(32)])
    source = encoded(original)
    mask = Image.new("L", (32, 24))
    for x in range(10, 20):
        for y in range(8, 16):
            mask.putpixel((x, y), 255)
    selected = prepare_mask(encoded(mask), source)
    # Simulate an upstream changing the whole frame and even returning a different size.
    edited = encoded(Image.new("RGB", (64, 48), "red"))
    with Image.open(io.BytesIO(restrict_edit(source, edited, selected))) as result:
        assert result.size == original.size
        for y in range(24):
            for x in range(32):
                expected = (255, 0, 0) if mask.getpixel((x, y)) else original.getpixel((x, y))
                assert result.getpixel((x, y)) == expected


class StreamRequest:
    def __init__(self, raw, content_type):
        self.headers = {"content-type": content_type}
        self._raw = raw

    async def stream(self):
        yield self._raw


def test_video_form_takes_input_reference_and_ignores_image_field():
    raw, ct = multipart({"prompt": "wave"}, {"input_reference": ("p.png", encoded(Image.new("RGB", (40, 20))))})
    fields, image = asyncio.run(read_video_form(StreamRequest(raw, ct)))
    assert fields == {"prompt": "wave"} and image.startswith(b"\x89PNG")
    raw, ct = multipart({"prompt": "wave"}, {"image": ("p.png", b"x")})
    with pytest.raises(UploadError, match="input_reference"):
        asyncio.run(read_video_form(StreamRequest(raw, ct)))


def test_video_form_allows_twenty_megabytes_but_images_keep_ten(monkeypatch):
    """Предел файла — параметр: у видео 20 МБ, у картинок прежние 10 (большие тела не гоняются —
    проверяется граница на малом пределе и то, какой предел берёт read_video_form)."""
    raw, ct = multipart(files={"input_reference": ("p.jpg", b"\xff" * 100)})
    assert decode_form(raw, ct, file_fields=("input_reference",), max_upload=100)[1] == b"\xff" * 100
    raw, ct = multipart(files={"input_reference": ("p.jpg", b"\xff" * 101)})
    with pytest.raises(UploadError) as exc:
        decode_form(raw, ct, file_fields=("input_reference",), max_upload=100)
    assert exc.value.status == 413
    seen = {}
    import image_upload

    def spy(raw, content_type, **kw):
        seen.update(kw)
        raise UploadError("стоп")

    monkeypatch.setattr(image_upload, "decode_form", spy)
    for reader in (read_video_form, read_image_form):
        with pytest.raises(UploadError):
            asyncio.run(reader(StreamRequest(b"x", "multipart/form-data; boundary=x")))
        assert seen["max_upload"] == (VIDEO_UPLOAD if reader is read_video_form else 10 * 1024 * 1024)
    assert VIDEO_UPLOAD == 20 * 1024 * 1024


def test_large_phone_jpeg_is_decoded_reduced_for_video_only():
    raw = io.BytesIO()
    Image.new("RGB", (6000, 4000), "green").save(raw, "JPEG", quality=50)
    with pytest.raises(UploadError, match="16 Мп"):
        normalize_image(raw.getvalue())
    with Image.open(io.BytesIO(normalize_image(raw.getvalue(), VIDEO_PIXELS))) as image:
        assert image.format == "PNG" and image.width * image.height <= 2 * 1024 * 1024
        assert abs(image.width / image.height - 1.5) < 0.01


@pytest.mark.parametrize("size,color", [((32, 24), 0), ((16, 12), 255)])
def test_empty_or_wrong_size_mask_is_rejected(size, color):
    with pytest.raises(UploadError):
        prepare_mask(encoded(Image.new("L", size, color)), encoded(Image.new("RGB", (32, 24))))
