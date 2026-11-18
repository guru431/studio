"""Один мост на пользователя: Windows mutex или POSIX flock, освобождаемый ОС при выходе."""
from .host import LINUX, MACOS

if not (MACOS or LINUX):
    from .single_windows import NAME, Mutex, acquire as _acquire
else:
    import fcntl
    import hashlib
    import os
    from . import store

    NAME = "Local\\StudioAgent"

    class Mutex:
        def __init__(self, handle):
            self.handle = handle

        def close(self):
            if self.handle is not None:
                os.close(self.handle)
                self.handle = None

    def _acquire(name=NAME):
        path = store.home() / (".instance-" + hashlib.sha256(name.encode()).hexdigest()[:16])
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        return Mutex(fd)


def acquire(name=NAME):
    """Захватить системный замок этой учётки; занято — None."""
    return _acquire(name)
