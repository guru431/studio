"""Каталог моста: настройки (config.json), ключ устройства (key.bin под DPAPI), выбор чатов (choices.json)."""

import dataclasses
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from . import dpapi

log = logging.getLogger(__name__)

CONFIG = "config.json"
KEY = "key.bin"
CHOICES = "choices.json"
CHOICES_MAX = 500


@dataclasses.dataclass
class Config:
    url: str = "https://studio.example.com"
    agent_id: int | None = None
    name: str = ""
    projects_root: str = ""
    autostart: bool = True


def home() -> Path:
    """STUDIO_AGENT_HOME, иначе %LOCALAPPDATA%\\studio-agent; каталог создаётся."""
    h = Path(os.environ.get("STUDIO_AGENT_HOME") or Path(os.environ["LOCALAPPDATA"]) / "studio-agent")
    h.mkdir(parents=True, exist_ok=True)
    return h


def write_atomic(path: Path, data: bytes) -> None:
    """Временный файл рядом, затем os.replace: читатель видит прежний файл или новый целиком."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_config(h: Path) -> Config:
    """Настройки; нет файла — по умолчанию. Битый файл (не JSON, не объект, поле не того вида) — тоже по умолчанию
    («не настроен», окно настройки) со строкой в журнале; файл не перезаписывается, пока владелец не сохранит."""
    try:
        raw = json.loads((h / CONFIG).read_text("utf-8"))
        if not (isinstance(raw, dict) and all(_KINDS[k](v) for k, v in raw.items() if k in _KINDS)):
            raise ValueError("поля")
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError) as e:  # и без прав на чтение: окно настройки, а не молчаливый незапуск
        log.warning("config.json не читается: %s", type(e).__name__)
        return Config()
    return Config(**{k: v for k, v in raw.items() if k in _KINDS})


_KINDS = {"url": lambda v: isinstance(v, str), "name": lambda v: isinstance(v, str),
          "projects_root": lambda v: isinstance(v, str), "autostart": lambda v: isinstance(v, bool),
          "agent_id": lambda v: v is None or (type(v) is int)}


def save_config(h: Path, cfg: Config) -> None:
    text = json.dumps(dataclasses.asdict(cfg), ensure_ascii=False, indent=2)
    write_atomic(h / CONFIG, text.encode("utf-8"))


def save_key(h: Path, key: str) -> None:
    write_atomic(h / KEY, dpapi.protect(key.encode("utf-8")))


class Choices:
    """Выбор модели и усилия по чатам (`choices.json`): не больше `CHOICES_MAX` чатов, старые прочь.

    Блокирующее — звать через `asyncio.to_thread`; замок — два хода пишут файл из разных потоков."""

    def __init__(self, h: Path):
        self.path = h / CHOICES
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, chat: str) -> tuple[str | None, str | None]:
        with self._lock:
            item = self._load().get(chat)
        if not isinstance(item, dict):
            return None, None
        model, effort = item.get("model"), item.get("effort")
        return (model if isinstance(model, str) else None), (effort if isinstance(effort, str) else None)

    def put(self, chat: str, model: str | None, effort: str | None) -> None:
        with self._lock:
            data = self._load()
            data.pop(chat, None)  # свежий — в конец
            data[chat] = {"model": model, "effort": effort}
            for old in list(data)[:max(0, len(data) - CHOICES_MAX)]:
                del data[old]
            write_atomic(self.path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def load_key(h: Path) -> str | None:
    """Ключ устройства; нет файла или блоб не читается (чужая учётка, порча) — None."""
    try:
        return dpapi.unprotect((h / KEY).read_bytes()).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
