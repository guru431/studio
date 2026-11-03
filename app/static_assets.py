"""Неизменяемый снимок версии: обмен каталогов при выкладке не меняет старые ответы."""
import hashlib
import mimetypes
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


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
