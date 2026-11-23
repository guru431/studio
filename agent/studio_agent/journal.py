"""Журнал моста agent.log (1 МиБ × 3, UTF-8): только id, коды, числа, имена типов исключений."""

import logging
import logging.handlers
import re
import sys
from pathlib import Path

from .limits import LOG_LINE_MAX, LOG_TAIL, MIB

FORMAT = "%(asctime)s %(levelname)s %(message)s"
# Журнал путей не пишет, но хвост для Студии всё равно маскирует похожее на путь: диск Windows, UNC, `~/`,
# домашние и системные каталоги POSIX.
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\\s]|~[\\/]|/(?:home|Users|root|tmp|var|opt|mnt|media|srv|private|Volumes|etc|usr|run)/)\S*")
MASK = "<path>"


def tail(h: Path, count: int = LOG_TAIL) -> list[str]:
    """Последние `count` строк `agent.log` (с началом из `agent.log.1`, если текущий файл короче) для Студии:
    без управляющих знаков, пути — `MASK`, строка — не длиннее `LOG_LINE_MAX`."""
    lines: list[str] = []
    for path in (h / "agent.log", h / "agent.log.1"):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        lines = data.decode("utf-8", "replace").splitlines()[-(count - len(lines)):] + lines
        if len(lines) >= count:
            break
    return [PATH.sub(MASK, "".join(c for c in line if " " <= c != "\x7f" or c == "\t"))[:LOG_LINE_MAX] for line in lines[-count:]]


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
    ws = logging.getLogger("websockets")  # на DEBUG пишет заголовок Authorization с ключом устройства
    ws.setLevel(max(ws.level, logging.INFO))
    return log
