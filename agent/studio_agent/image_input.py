"""Картинки хода: только байты из просьбы, без URL, путей и временных файлов."""

import base64

from .errors import Refused
from .limits import IMAGE_COUNT as COUNT, IMAGE_MAX as FILE_MAX, IMAGES_MAX as TOTAL_MAX

MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def validate(images):
    """Проверка на машине обязательна: сервер не может подставить URL или локальный путь."""
    if not isinstance(images, list) or len(images) > COUNT:
        raise Refused("invalid", "можно прикрепить до 4 изображений")
    total = 0
    for image in images:
        if not isinstance(image, dict) or image.get("mime") not in MIMES or not isinstance(image.get("data"), str):
            raise Refused("invalid", "нужен PNG, JPEG, WebP или GIF")
        data = image["data"]
        if len(data) > (FILE_MAX + 2) // 3 * 4:
            raise Refused("too_large", "изображение больше 5 МиБ")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:
            raise Refused("invalid", "изображение не читается") from None
        total += len(raw)
        if len(raw) > FILE_MAX or total > TOTAL_MAX:
            raise Refused("too_large", "изображения больше предела: 5 МиБ на файл, 8 МиБ всего")
        matches = {"image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
                   "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
                   "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
                   "image/gif": raw.startswith((b"GIF87a", b"GIF89a"))}
        if not matches[image["mime"]]:
            raise Refused("invalid", "формат изображения не совпадает с содержимым")


async def prompt(text, images):
    """Agent SDK принимает блоки картинки в потоке сообщений пользователя."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": i["mime"], "data": i["data"]}}
               for i in images]
    if text.strip():
        content.append({"type": "text", "text": text})
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
