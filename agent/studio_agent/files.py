"""Пути внутри проекта и файлы (протокол §4, §6): список папки, чтение, raw, правка."""

import base64
import ctypes
import hashlib
import logging
import os
import re
import stat
from pathlib import Path

from . import limits, store
from .errors import Refused

log = logging.getLogger(__name__)

# Имена устройств Windows — по части пути, с любым расширением. Сверх брифа: CONIN$, CONOUT$, COM¹²³ и
# LPT¹²³ и пробелы перед точкой («NUL .txt») — Win32 считает их устройствами так же.
DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]) *(?:\..*)?", re.IGNORECASE)
# Знаки, которых не бывает в имени Windows: разделитель, поток (ADS), подстановки, кавычка, труба.
BAD_CHARS = set('\\:*?"<>|')

TOO_BIG_TEXT = "файл больше 2 МиБ"
TOO_BIG_RAW = "файл больше 10 МиБ"
NO_FILE = "нет такого файла"
NO_DIR = "нет такой папки"
READ_ONLY = "файл только для чтения"
# Атрибуты, что `SetFileAttributesW` умеет ставить (без READONLY: такой файл не правится).
KEEP_ATTRIBUTES = (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM | stat.FILE_ATTRIBUTE_ARCHIVE
                   | stat.FILE_ATTRIBUTE_NOT_CONTENT_INDEXED | stat.FILE_ATTRIBUTE_OFFLINE
                   | stat.FILE_ATTRIBUTE_TEMPORARY)


def _parts(path: str) -> list[str]:
    """Части пути через `/`; вид неверный — `invalid`. К диску не обращается."""
    if not isinstance(path, str) or len(path) > limits.PATH_MAX:
        raise Refused("invalid")
    if path == "":
        return []
    for c in path:
        n = ord(c)
        if n < 32 or n == 127 or 0xD800 <= n <= 0xDFFF or c in BAD_CHARS:
            raise Refused("invalid")
    parts = path.split("/")
    for p in parts:
        if p in ("", ".", "..") or p[-1] in ". " or DEVICE.fullmatch(p):
            raise Refused("invalid")
    return parts


def inside(root: str, real: str) -> bool:
    """Настоящий путь `real` — сам `root` или внутри него (оба — после realpath)."""
    r, p = os.path.normcase(root), os.path.normcase(real)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def resolve(project: Path, path: str) -> Path:
    """Настоящий путь `path` в проекте: вид неверный — `invalid` (до диска), вне проекта — `outside`."""
    parts = _parts(path)
    root = os.path.realpath(project)
    real = os.path.realpath(os.path.join(root, *parts))
    if not inside(root, real):
        raise Refused("outside")
    return Path(real)


def version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_dir(project: Path, path: str) -> dict:
    """Папки, затем файлы, по имени; записи, ведущие наружу или без stat, пропускаются."""
    target = resolve(project, path)
    if not target.is_dir():
        raise Refused("not_found", NO_DIR)
    root = os.path.realpath(project)
    items = []
    with os.scandir(target) as it:
        for e in it:
            try:
                real = os.path.realpath(e.path)
                if not inside(root, real):
                    continue
                st = os.stat(real)
            except OSError:
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            items.append({"name": e.name, "dir": is_dir, "size": 0 if is_dir else st.st_size,
                          "modified": int(st.st_mtime)})
    items.sort(key=lambda i: (not i["dir"], i["name"].casefold(), i["name"]))
    if len(items) > limits.FILES_MAX:
        return {"items": items[:limits.FILES_MAX], "truncated": True}
    return {"items": items}


def _read(project: Path, path: str, limit: int, words: str) -> tuple[Path, bytes]:
    """Байты файла: нет файла — `not_found`; больше `limit` — `too_large` по stat до чтения."""
    p = resolve(project, path)
    try:
        st = os.stat(p)
    except OSError:
        raise Refused("not_found", NO_FILE) from None
    if not stat.S_ISREG(st.st_mode):
        raise Refused("not_found", NO_FILE)
    if st.st_size > limit:
        raise Refused("too_large", words)
    with open(p, "rb") as f:
        data = f.read(limit + 1)  # файл мог вырасти после stat
    if len(data) > limit:
        raise Refused("too_large", words)
    return p, data


def _text(data: bytes) -> str:
    """UTF-8 без NUL, BOM остаётся в тексте; иначе `binary`."""
    if b"\0" in data:
        raise Refused("binary")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("binary") from None


def read_text(project: Path, path: str) -> dict:
    _, data = _read(project, path, limits.TEXT_MAX, TOO_BIG_TEXT)
    return {"text": _text(data), "version": version(data)}


def read_raw(project: Path, path: str) -> dict:
    _, data = _read(project, path, limits.RAW_MAX, TOO_BIG_RAW)
    return {"data": base64.b64encode(data).decode("ascii"), "size": len(data), "version": version(data)}


def save_text(project: Path, path: str, text: str, ver: str) -> dict:
    """Только существующий текстовый файл и только при том же `version`; байты — UTF-8 текста как есть.

    «Только чтение» — отказ до записи; скрытый, системный и прочие атрибуты прежнего файла — новому (запись идёт
    временным файлом и `os.replace`, новый файл получил бы атрибуты по умолчанию)."""
    p, data = _read(project, path, limits.TEXT_MAX, TOO_BIG_TEXT)
    _text(data)
    now = version(data)
    if ver != now:
        raise Refused("conflict", version=now)
    try:
        new = text.encode("utf-8")
    except UnicodeEncodeError:  # одинокий суррогат из JSON
        raise Refused("invalid") from None
    if len(new) > limits.TEXT_MAX:
        raise Refused("too_large", TOO_BIG_TEXT)
    attrs = os.stat(p).st_file_attributes
    if attrs & stat.FILE_ATTRIBUTE_READONLY:
        raise Refused("read_only", READ_ONLY)
    store.write_atomic(p, new)
    keep = attrs & KEEP_ATTRIBUTES
    if not ctypes.windll.kernel32.SetFileAttributesW(str(p), keep or stat.FILE_ATTRIBUTE_NORMAL):
        log.warning("правка: атрибуты файла не перенесены")
    return {"version": version(new)}
