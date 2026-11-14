"""Хеш пароля (scrypt), шифрование токенов подключений (Fernet), случайные токены."""
import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

import state

PASSWORD_MIN = 8
_N, _R, _P, _DKLEN = 16384, 8, 1, 32


def _scrypt(pw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(_scrypt(pw, salt, _N, _R, _P)).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        got = _scrypt(pw, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(want))
    except ValueError:
        return False


# Хеш неизвестного пароля: вход с несуществующим логином тратит то же время на проверку.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def encrypt(text: str) -> bytes:
    return Fernet(state.CFG.data_key).encrypt(text.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    return Fernet(state.CFG.data_key).decrypt(blob).decode("utf-8")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
