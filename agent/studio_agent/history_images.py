"""Изображения истории: только ссылки в странице, байты — отдельной защищённой просьбой."""

import base64
import hashlib
import re
from collections import OrderedDict
from pathlib import Path

from . import files, image_input
from .errors import Refused

COUNT = 32
KINDS = ("image", "localImage", "input_image")


class LiveImages:
    """Байты уточнений до чтения штатной истории; только память моста, общий предел 32 МиБ."""

    def __init__(self, budget=32 * 1024 * 1024):
        self.budget = budget
        self._items = OrderedDict()
        self._size = 0

    def remember(self, *, project, chat, message, images):
        for index, image in enumerate(images):
            key = (str(project), chat, message, index)
            previous = self._items.pop(key, None)
            if previous is not None:
                self._size -= len(previous["data"])
            self._items[key] = {"mime": image["mime"], "data": image["data"]}
            self._size += len(image["data"])
            while self._size > self.budget and self._items:
                self._size -= len(self._items.popitem(last=False)[1]["data"])

    def get(self, project, chat, message, index):
        key = (str(project), chat, message, index)
        image = self._items.get(key)
        if image is not None:
            self._items.move_to_end(key)
            return dict(image)
        return None

    def clear(self):
        self._items.clear()
        self._size = 0


def blocks(content):
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") in KINDS][:COUNT]


def reference(index):
    return {"index": index, "name": f"Изображение {index + 1}"}


def references(content):
    return [reference(index) for index, _ in enumerate(blocks(content))]


def _deepseek(block, root):
    """Вложение DeepSeek Harness — по sha256 из хранилища `<DSH_HOME>/attachments/v1/objects`; только чтение."""
    attachment = block.get("attachment")
    digest = attachment.get("attachmentId") if isinstance(attachment, dict) else None
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", digest) if isinstance(digest, str) else None
    if match is None or root is None:
        raise Refused("unsupported", "В истории нет сохранённых байтов изображения")
    name = match.group(1)
    try:
        with (Path(root) / "attachments" / "v1" / "objects" / name[:2] / name).open("rb") as source:
            raw = source.read(image_input.FILE_MAX + 1)
    except OSError:
        raise Refused("unsupported", "В истории нет сохранённых байтов изображения") from None
    if len(raw) > image_input.FILE_MAX:
        raise Refused("too_large", "Изображение больше 10 МиБ")
    if hashlib.sha256(raw).hexdigest() != name:
        raise Refused("unsupported", "В истории нет сохранённых байтов изображения")
    return {"mime": image_input.mime_of(raw), "data": base64.b64encode(raw).decode("ascii")}


def payload(content, index, engine, project, *, root=None):
    """Индекс относится только к изображениям выбранного сообщения; URL никогда не загружаются.
    `root` — `DSH_HOME` для вложений DeepSeek Harness."""
    images = blocks(content)
    if type(index) is not int or not 0 <= index < len(images):
        raise Refused("not_found", "Изображение отсутствует в сообщении")
    block = images[index]
    if engine == "deepseek":
        result = _deepseek(block, root)
    elif engine == "claude":
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            raise Refused("unsupported", "В истории нет сохранённых байтов изображения")
        result = {"mime": source.get("media_type"), "data": source.get("data")}
    elif engine == "opencode":
        result = {"mime": block.get("mimeType"), "data": block.get("data")}
    elif block.get("type") == "localImage":
        path = block.get("path")
        if not isinstance(path, str):
            raise Refused("not_found")
        root = Path(project).resolve()
        source = (root / path).resolve()
        if not source.is_relative_to(root):
            raise Refused("outside", "Изображение находится вне проекта")
        _, raw = files._read(project, source.relative_to(root).as_posix(), image_input.FILE_MAX,
                            "Изображение больше 10 МиБ")
        result = {"mime": image_input.mime_of(raw), "data": base64.b64encode(raw).decode("ascii")}
    else:
        url = block.get("url") or block.get("image_url")
        if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
            raise Refused("unsupported", "В истории нет сохранённых байтов изображения")
        mime, data = url[5:].split(";base64,", 1)
        result = {"mime": mime, "data": data}
    image_input.validate([result])
    return result
