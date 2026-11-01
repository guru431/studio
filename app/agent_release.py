"""Версия и контрольная сумма именно того ZIP, который публикует выкладка."""
import functools
import hashlib
import re
import zipfile
from pathlib import Path

VERSION = re.compile(r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}')
MAX_PACKAGE = 16 * 1024 * 1024


@functools.lru_cache(maxsize=2)
def _read(path: Path, stamp: int, size: int) -> dict:
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            prefix = 'studio-agent/' if 'studio-agent/studio_agent/__init__.py' in archive.namelist() else ''
            info = archive.getinfo(prefix + 'studio_agent/__init__.py')
            if info.file_size > 4096:
                raise ValueError('version')
            match = re.search(rb'^VERSION = "([0-9.]+)"\r?$', archive.read(info), re.MULTILINE)
    if match is None or not VERSION.fullmatch(version := match[1].decode('ascii')):
        raise ValueError('version')
    return {'version': version, 'sha256': digest, 'size': size}


def release(path: Path) -> dict | None:
    try:
        stat = path.stat()
        if not 0 < stat.st_size <= MAX_PACKAGE:
            return None
        return _read(path, stat.st_mtime_ns, stat.st_size).copy()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
