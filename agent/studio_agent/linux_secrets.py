"""Headless Linux: Fernet с отдельным ключом, доступным только пользователю моста."""
import os
import secrets
import stat

from . import host

_NAME = "secrets.key"


def _private(fd, kind):
    info = os.fstat(fd)
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("unsafe secret storage permissions")


def _read(directory):
    from cryptography.fernet import Fernet

    fd = os.open(_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        _private(fd, stat.S_ISREG)
        key = stream.read(45)
        if len(key) != 44:
            raise OSError("invalid secret storage key")
        try:
            return Fernet(key)
        except ValueError as error:
            raise OSError("invalid secret storage key") from error


def _cipher(*, create):
    from cryptography.fernet import Fernet

    folder = host.xdg_home("XDG_CONFIG_HOME", ".config") / "studio-agent"
    if create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(folder, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    try:
        _private(directory, stat.S_ISDIR)
        try:
            return _read(directory)
        except FileNotFoundError:
            if not create:
                raise
        # Опубликовать целый ключ один раз даже при одновременной первой записи из меню и службы.
        temporary = ".key-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(fd)
            try:
                os.link(temporary, _NAME, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary, dir_fd=directory)
        return _read(directory)
    finally:
        os.close(directory)


def protect(data: bytes) -> bytes:
    return _cipher(create=True).encrypt(data)


def unprotect(blob: bytes) -> bytes:
    from cryptography.fernet import InvalidToken

    try:
        return _cipher(create=False).decrypt(blob)
    except InvalidToken as error:
        raise OSError("invalid protected data") from error
