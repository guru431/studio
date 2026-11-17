"""Атомарная запись без сторонних зависимостей, доступная также первичному установщику."""
import os
import tempfile
from pathlib import Path


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
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
