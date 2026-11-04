"""Неизменяемый снимок версии: обмен каталогов при выкладке не меняет старые ответы."""
import hashlib
import mimetypes
import re
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


def module_preloads(directory: Path) -> dict[str, list[str]]:
    """Static dependencies only; lazy tools stay lazy. Paths use the versioned import-map base."""
    root = directory.resolve()
    imports = re.compile(r"^\s*import\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]", re.MULTILINE)

    def graph(entries):
        found = set()

        def visit(path):
            path = path.resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('Invalid local module dependency')
            relative = path.relative_to(root).as_posix()
            if relative in found:
                return
            found.add(relative)
            for target in imports.findall(path.read_text(encoding='utf-8')):
                if target.startswith('.'):
                    visit(path.parent / target)
                elif target.startswith('/static/'):
                    visit(root / target.removeprefix('/static/'))

        for entry in entries:
            visit(root / 'js' / (entry + '.js'))
        return found

    shell = graph(['app', 'lock-start'])
    result = {'shell': sorted(shell)}
    for section in ('chat', 'photo', 'video', 'speech', 'transcribe', 'ocr', 'translate',
                    'code', 'code-chat', 'search', 'settings', 'admin'):
        result[section] = sorted(graph([section]) - shell)
    result['markdown'] = ['vendor/marked.min.js', 'vendor/purify.min.js', 'vendor/highlight.min.js']
    return result


class Snapshot(StaticFiles):
    def __init__(self, directory: Path):
        super().__init__(directory=directory)
        self.assets = {}
        root = directory.resolve()
        for path in directory.rglob('*'):
            if path.is_file() and path.resolve().is_relative_to(root):
                raw = path.read_bytes()
                mime = 'text/javascript' if path.suffix in ('.js', '.mjs') else mimetypes.guess_type(path)[0]
                self.assets[path.relative_to(directory).as_posix()] = (raw, mime or 'application/octet-stream',
                    '"' + hashlib.sha256(raw).hexdigest() + '"')

    async def check_config(self):
        # Каталог уже прочитан при создании; после обмена директорий нужны прежние байты снимка.
        return

    async def get_response(self, path, scope):
        if scope['method'] not in ('GET', 'HEAD'):
            raise HTTPException(status_code=405)
        asset = self.assets.get(path.replace('\\', '/'))
        if asset is None:
            raise HTTPException(status_code=404)
        raw, mime, etag = asset
        headers = {'ETag': etag, 'Cache-Control': 'public, max-age=31536000, immutable'}
        if dict(scope.get('headers', [])).get(b'if-none-match') == etag.encode('ascii'):
            return Response(status_code=304, headers=headers)
        headers['Content-Length'] = str(len(raw))
        return Response(raw if scope['method'] == 'GET' else b'', media_type=mime, headers=headers)
