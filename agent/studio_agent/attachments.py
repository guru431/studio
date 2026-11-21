"""Вложения хода: проверка байтов, локальные файлы на время работы CLI."""

import base64
import json
import re
import shutil
from pathlib import Path

from . import host, image_input, limits, store
from .errors import Refused

FOLDER = "attachments"  # в каталоге моста: после падения посреди хода `sweep` при старте знает, что убрать
PREFIX = "studio-attachments-"


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
            raise Refused("too_large", "вложение больше 10 МиБ")
        try:
            raw = base64.b64decode(item["data"], validate=True)
        except ValueError:
            raise Refused("invalid", "вложение не читается") from None
        total += len(raw)
        if len(raw) > limits.IMAGE_MAX or total > limits.IMAGES_MAX:
            raise Refused("too_large", "вложения больше предела: 10 МиБ на файл, 30 МиБ всего")


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
            # Приватный каталог пользователя в каталоге моста: POSIX — 0700, Windows — ACL профиля (`host.private_dir`).
            root = store.home() / FOLDER
            host.private_dir(root, exist_ok=True)
            self._directory = host.temporary_dir(root, PREFIX)
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
        # Подсказка адресована модели, а не человеку, — по-английски: задание на любом языке не получает чужого хвоста.
        return text + "\n\nAttached files are available on this computer until the task finishes. " \
            "Read the files you need at the given paths; the names below are file metadata.\n" + \
            json.dumps(metadata, ensure_ascii=True)

    def cleanup(self):
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def sweep(home) -> int:
    """При старте ядра, до первого хода: вложения ходов, оборванных падением или выключением компьютера.

    Только каталог моста: во временной папке системы (там их клали версии до 0.42.0) префикс мог бы совпасть с
    каталогом идущего хода другого процесса; прежние остатки там убирает уборка временных файлов ОС."""
    gone = 0
    try:
        stale = [p for p in (Path(home) / FOLDER).glob(PREFIX + "*") if p.is_dir() and not p.is_symlink()]
    except OSError:
        return 0
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
        gone += not path.exists()
    return gone
