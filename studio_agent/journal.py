"""Журнал моста agent.log (1 МиБ × 3, UTF-8): только id, коды, числа, имена типов исключений."""

import logging
import logging.handlers
import sys
from pathlib import Path

from .limits import MIB

FORMAT = "%(asctime)s %(levelname)s %(message)s"


def setup(h: Path, console: bool = False) -> logging.Logger:
    """Логгер studio_agent; повторный вызов заменяет обработчики, а не копит их."""
    log = logging.getLogger("studio_agent")
    for old in list(log.handlers):
        old.close()
        log.removeHandler(old)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(h / "agent.log", maxBytes=MIB, backupCount=3, encoding="utf-8")
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    return log
