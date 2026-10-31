"""Вложения хода: проверка байтов, локальные файлы на время работы CLI."""

import base64
import json
import re
import tempfile
from pathlib import Path

from . import image_input, limits
from .errors import Refused


def name(value):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise Refused("invalid", "нужно имя вложения до 200 знаков")
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(c for c in value if c.isprintable()).strip(" .")
    if not value:
        raise Refused("invalid", "нужно имя вложения")
    return value


def validate(items, images=()):
    if isinstance(images, tuple):
        images = list(images)
    image_input.validate(images)
    if not isinstance(items, list) or len(items) + len(images) > limits.ATTACHMENT_COUNT:
        raise Refused("invalid", "можно прикрепить до 8 файлов")
    total = sum(len(base64.b64decode(i["data"], validate=True)) for i in images)
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("data"), str):
            raise Refused("invalid", "вложение не читается")
        name(item.get("name"))
        mime = item.get("mime")
        if not isinstance(mime, str) or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime):
            raise Refused("invalid", "нужен тип вложения")
        if len(mime) > 200 or len(item["data"]) > (limits.IMAGE_MAX + 2) // 3 * 4:
            raise Refused("too_large", "вложение больше 5 МиБ")
        try:
            raw = base64.b64decode(item["data"], validate=True)
        except ValueError:
            raise Refused("invalid", "вложение не читается") from None
        total += len(raw)
        if len(raw) > limits.IMAGE_MAX or total > limits.IMAGES_MAX:
            raise Refused("too_large", "вложения больше предела: 5 МиБ на файл, 8 МиБ всего")


class Files:
    """Случайные имена: исходное имя никогда не используется как путь."""

    def __init__(self):
        self._directory = None
        self._count = 0

    def prompt(self, text, items):
        if not items:
            return text
        validate(items)
        if self._directory is None:
            # mkdtemp создаёт приватный каталог пользователя (0700, на Windows — ACL).
            self._directory = tempfile.TemporaryDirectory(prefix="studio-attachments-")
        metadata = []
        for item in items:
            original = name(item["name"])
            suffix = Path(original).suffix
            suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix) else ".bin"
            self._count += 1
            target = Path(self._directory.name) / f"file-{self._count}{suffix}"
            with target.open("xb") as stream:
                stream.write(base64.b64decode(item["data"], validate=True))
            metadata.append({"name": original, "mime": item["mime"], "path": str(target)})
        return text + "\n\nПрикреплённые файлы доступны на этом компьютере до завершения задания. " \
            "Прочитай нужные файлы по указанным путям; имена ниже — метаданные файлов.\n" + \
            json.dumps(metadata, ensure_ascii=True)

    def cleanup(self):
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
