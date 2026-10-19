"""Bounded, memory-only multipart image input of the studio page.

Копия llm_routers srvllm/deploy/services/image_upload.py на 2026-10-19: переводчик API картинок
на srvllm держит свою. Студия нормализует фото до PNG раньше шлюза, и её пределы не шире
пределов переводчика — иначе фото, принятое здесь, отказал бы шлюз."""
import asyncio
import io
import warnings
from email import policy
from email.parser import BytesParser

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 10 * 1024 * 1024
MAX_BODY = MAX_UPLOAD + 128 * 1024
MAX_PIXELS = 16_000_000
#: Video input (спека 2026-10-19-srvllm-studio-video, раздел 1): картинка до 20 МБ — фото
#: телефона в режиме 48–50 Мп; JPEG такого размера декодируется сразу уменьшенным (draft).
VIDEO_UPLOAD = 20 * 1024 * 1024
VIDEO_PIXELS = 50_000_000
VIDEO_FIELDS = ("input_reference",)
IMAGE_FIELDS = ("image", "image[]")


class UploadError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def decode_form(raw, content_type, *, extra_files=None, file_fields=IMAGE_FIELDS, max_upload=MAX_UPLOAD):
    label = file_fields[0]
    if not content_type.lower().startswith("multipart/form-data;"):
        raise UploadError(f"ожидается multipart/form-data с файлом {label}")
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
    except UnicodeError:
        raise UploadError("некорректный Content-Type") from None
    if not message.is_multipart() or message.defects:
        raise UploadError("некорректный multipart")
    fields, image = {}, None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True)
        if part.defects or not name or not isinstance(data, bytes):
            raise UploadError("некорректная часть multipart")
        if name == "mask" and extra_files is not None:
            if name in extra_files or not data or len(data) > MAX_UPLOAD:
                raise UploadError("нужна одна непустая маска до 10 МиБ")
            extra_files[name] = data
        elif name in file_fields:
            if image is not None:
                raise UploadError(f"нужен ровно один файл {label}")
            if not data or len(data) > max_upload:
                raise UploadError(f"фото должно быть непустым и не больше {max_upload // (1024 * 1024)} МиБ", 413)
            image = data
        else:
            if part.get_filename() is not None:
                raise UploadError(f"лишний файл в поле {name[:40]!r}: картинка — в поле {label}")
            if name in fields or len(data) > 128 * 1024:
                raise UploadError("повторное или слишком большое поле формы")
            try:
                fields[name] = data.decode("utf-8")
            except UnicodeDecodeError:
                raise UploadError("поля формы должны быть в UTF-8") from None
    if image is None:
        raise UploadError(f"нужен файл {label} (PNG, JPEG или WebP)")
    return fields, image


def normalize_image(raw, max_pixels=MAX_PIXELS):
    """Decode before queueing; strip metadata and apply EXIF orientation."""
    limit = f"{max_pixels // 1_000_000} Мп"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > max_pixels:
                    raise UploadError(f"нужен PNG, JPEG или WebP не больше {limit}")
                if image.format == "JPEG" and image.width * image.height > MAX_PIXELS:
                    image.draft("RGB", (2048, 2048))   # декодер сразу уменьшает в 2–8 раз
                image.load()
                image = ImageOps.exif_transpose(image).convert("RGB")
                # The graph rescales to the selected output size. Bound normalized memory too.
                scale = min(1, 2048 / image.width, 2048 / image.height,
                            (2 * 1024 * 1024 / (image.width * image.height)) ** 0.5)
                image.thumbnail((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
                image.info.clear()
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise UploadError(f"не удалось прочитать фото: нужен PNG, JPEG или WebP до {limit}") from None


def image_size(raw):
    with Image.open(io.BytesIO(raw)) as image:
        return image.size


def prepare_mask(raw, source):
    """A nonempty grayscale PNG matching the normalized source, without resizing."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "PNG" or image.size != image_size(source):
                    raise UploadError("маска должна быть PNG того же размера, что исходное фото")
                mask = image.convert("L")
                if mask.getextrema()[1] == 0:
                    raise UploadError("выделите кистью область для изменения")
                mask.info.clear()
                output = io.BytesIO()
                mask.save(output, format="PNG")
                return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        if isinstance(error, UploadError):
            raise
        raise UploadError("не удалось прочитать маску") from None


def restrict_edit(source, edited, mask):
    """Preserve decoded source pixels wherever the selection is black."""
    with Image.open(io.BytesIO(source)) as original, Image.open(io.BytesIO(edited)) as result, \
            Image.open(io.BytesIO(mask)) as selection:
        original = original.convert("RGB")
        result = result.convert("RGB")
        if result.size != original.size:
            result = result.resize(original.size, Image.Resampling.LANCZOS)
        merged = Image.composite(result, original, selection.convert("L"))
        output = io.BytesIO()
        merged.save(output, format="PNG")
        return output.getvalue()


async def read_image_form(request, *, extra_files=None, file_fields=IMAGE_FIELDS, max_upload=MAX_UPLOAD,
                          max_pixels=MAX_PIXELS):
    raw = bytearray()
    limit = max_upload + (MAX_BODY - MAX_UPLOAD) + (MAX_UPLOAD if extra_files is not None else 0)
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise UploadError("слишком большое тело запроса", 413)
    fields, image = decode_form(bytes(raw), request.headers.get("content-type", ""), extra_files=extra_files,
                                file_fields=file_fields, max_upload=max_upload)
    return fields, await asyncio.to_thread(normalize_image, image, max_pixels)


async def read_video_form(request):
    """Картинка ролика (`input_reference`, до 20 МБ и 50 Мп) и поля формы."""
    return await read_image_form(request, file_fields=VIDEO_FIELDS, max_upload=VIDEO_UPLOAD,
                                 max_pixels=VIDEO_PIXELS)
