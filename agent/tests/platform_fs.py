"""Ссылка на каталог для контрактов границ: Windows junction или POSIX symlink."""
import os
from pathlib import Path


def create_directory_link(target, link):
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        Path(link).symlink_to(target, target_is_directory=True)
