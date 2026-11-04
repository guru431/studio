"""Переменные окружения службы → Config."""
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

REQUIRED = ("STUDIO_DATA_KEY", "STUDIO_DB", "STUDIO_MEDIA_DIR", "STUDIO_ORIGIN")


@dataclass(frozen=True)
class Config:
    data_key: str
    db_path: Path
    media_dir: Path
    origin: str
    hairpin_ip: str = "10.77.1.1"
    cleaner_ca: Path | None = None

    @property
    def tmp_dir(self) -> Path:
        """Временные файлы звука (ffmpeg) — на локальном диске рядом с базой, не на шаре."""
        return self.db_path.parent / "tmp"


def load() -> Config:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:   # только имена: значения секретов в текст ошибки не попадают
        raise RuntimeError("не заданы переменные окружения: " + ", ".join(missing))
    try:          # неверный ключ — отказ при старте, а не 500 на первом токене
        Fernet(os.environ["STUDIO_DATA_KEY"])
    except (ValueError, TypeError):
        raise RuntimeError("STUDIO_DATA_KEY — не ключ Fernet (32 байта в base64url)") from None
    return Config(data_key=os.environ["STUDIO_DATA_KEY"], db_path=Path(os.environ["STUDIO_DB"]),
                  media_dir=Path(os.environ["STUDIO_MEDIA_DIR"]), origin=os.environ["STUDIO_ORIGIN"],
                  hairpin_ip=os.environ.get("STUDIO_HAIRPIN_IP", Config.hairpin_ip),
                  cleaner_ca=Path(os.environ["STUDIO_CLEANER_CA"]) if os.environ.get("STUDIO_CLEANER_CA") else None)
