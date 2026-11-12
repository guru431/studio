"""HTML и растровые визуализации: только проект и отдельные каталоги артефактов Codex владельца."""
import os
import stat
import base64
from pathlib import Path

from . import files, image_input, limits
from .errors import Refused


IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.gif')


def valid_source(source) -> bool:
    return (isinstance(source, str) and 0 < len(source) <= limits.PATH_MAX
            and source.lower().endswith(('.html', *IMAGE_SUFFIXES))
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in source))


def _preview(root, relative):
    if relative.lower().endswith('.html'):
        return files.read_text(root, relative)
    _, raw = files._read(root, relative, limits.IMAGE_MAX, 'Изображение больше 5 МиБ')
    mime = image_input.mime_of(raw)
    if mime is None:
        raise Refused('binary', 'Формат изображения не поддерживается')
    return {'image': {'data': base64.b64encode(raw).decode('ascii'), 'mime': mime}}


def read(project: Path, source: str, *, codex_home: bool = True) -> dict:
    """`codex_home=False` — только проект: общий каталог визуализаций Codex хранит HTML тредов всех проектов
    владельца, участнику совместного проекта (протокол §17) он не открывается."""
    if not valid_source(source):
        raise Refused('invalid')
    target = Path(source.replace('\\', '/'))
    if not target.is_absolute():
        return _preview(project, source.replace('\\', '/'))
    # Check lexical containment first: realpath alone would admit traversal and junctions.
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    extra = home / ('visualizations' if source.lower().endswith('.html') else 'generated_images')
    roots = (project, extra) if codex_home else (project,)
    for root in roots:
        try:
            relative = target.relative_to(root.absolute())
        except ValueError:
            continue
        parts = files._parts(relative.as_posix())
        current = root
        try:
            for part in ('', *parts):
                if part:
                    current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise Refused('outside')
        except OSError:
            raise Refused('not_found', 'Нет такой визуализации') from None
        return _preview(root, relative.as_posix())
    raise Refused('outside')
