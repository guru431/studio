"""Переменные окружения службы → Config."""
import os
import re
from base64 import b64decode
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

REQUIRED = ("STUDIO_DATA_KEY", "STUDIO_DB", "STUDIO_MEDIA_DIR", "STUDIO_ORIGIN")
OIDC_ENV = ("STUDIO_CLEANER_OIDC_REDIRECT_URI", "STUDIO_CLEANER_OIDC_CLIENT_SECRET",
            "STUDIO_CLEANER_OIDC_SIGNING_KEY")


@dataclass(frozen=True)
class CleanerOidc:
    issuer: str
    redirect_uri: str
    secret: str = field(repr=False)
    signing_key: RSAPrivateKey = field(repr=False)


def _https_url(value: str, path: str) -> bool:
    if not value.isascii() or not re.fullmatch(r"https://[a-zA-Z0-9.\-:\[\]]+" + re.escape(path), value):
        return False
    try:
        parts = urlsplit(value)
        return parts.hostname is not None and (parts.port is None or parts.port > 0)
    except ValueError:
        return False


def _cleaner_oidc(origin: str) -> CleanerOidc | None:
    redirect, secret, pem = (os.environ.get(name, "") for name in OIDC_ENV)
    if not any((redirect, secret, pem)):
        return None
    issuer = origin.rstrip("/")
    if (not _https_url(issuer, "") or not _https_url(redirect, "/login/studio/callback")
            or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", secret) or len(pem) > 8192):
        raise RuntimeError("Неполная или неверная конфигурация STUDIO_CLEANER_OIDC_*")
    try:
        key = load_pem_private_key(b64decode(pem, validate=True), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise RuntimeError("STUDIO_CLEANER_OIDC_SIGNING_KEY — нужен PEM RSA в Base64") from None
    if not isinstance(key, RSAPrivateKey) or key.key_size not in (2048, 3072, 4096):
        raise RuntimeError("STUDIO_CLEANER_OIDC_SIGNING_KEY — нужен ключ RSA 2048, 3072 или 4096 бит")
    return CleanerOidc(issuer, redirect, secret, key)


@dataclass(frozen=True)
class Config:
    data_key: str
    db_path: Path
    media_dir: Path
    origin: str
    hairpin_ip: str = "10.77.1.1"
    cleaner_ca: Path | None = None
    alternate_origin: str = ""
    spark_chat_url: str = ""
    managed_https: bool = False
    cleaner_oidc: CleanerOidc | None = None

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
                  cleaner_ca=Path(os.environ["STUDIO_CLEANER_CA"]) if os.environ.get("STUDIO_CLEANER_CA") else None,
                  alternate_origin=os.environ.get("STUDIO_ALTERNATE_ORIGIN", "").rstrip("/"),
                  spark_chat_url=os.environ.get("STUDIO_SPARK_CHAT_URL", "").strip().rstrip("/"),
                  managed_https=os.environ.get("STUDIO_MANAGED_HTTPS") == "1",
                  cleaner_oidc=_cleaner_oidc(os.environ["STUDIO_ORIGIN"]))
