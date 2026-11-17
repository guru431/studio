"""Атомарная запись без сторонних зависимостей, доступная также первичному установщику."""
import os
import tempfile
from pathlib import Path
from time import sleep as _sleep

_WINDOWS = os.name == 'nt'


def write_atomic(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Временный файл рядом, затем os.replace: прежний файл или новый целиком."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            if mode is not None:
                os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as error:
                # Читатель без FILE_SHARE_DELETE даёт также ERROR_ACCESS_DENIED.
                # Разрешения не меняем: постоянный отказ после короткого ожидания остаётся ошибкой.
                if not _WINDOWS or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 4:
                    raise
                _sleep(0.01 * 2 ** attempt)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
