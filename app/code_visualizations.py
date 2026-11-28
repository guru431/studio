"""Ephemeral, session-bound HTML previews. No file content or paths are written to disk.

The fragment may load libraries from the CDN hosts below. Studio serves its own lucide and image scripts when needed,
and includes project image data in the document itself."""
import json
import posixpath
import re
import secrets
from collections.abc import Sequence
from html import escape
from html.parser import HTMLParser
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

import clock

TTL = 300
MAX_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # decoded total of the images referenced by one HTML document
MAX_IMAGES = 24
MAX_IMAGE_REFERENCES = 128  # includes different URLs for the same file
MAX_IMAGE_TAGS = 2048
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.gif')
IMAGE_ATTRIBUTES = {'img': 'src', 'a': 'href'}
CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]')
# One preview per user, as with the project PDF cache; token is only a lookup key.
VIEWS: dict[int, tuple] = {}
CDNS = ('https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://esm.sh '
        'https://unpkg.com https://persistent.oaistatic.com/plugins/visualize/')
FONTS = 'https://fonts.googleapis.com https://fonts.gstatic.com https://fonts.bunny.net'
LUCIDE = '/static/vendor/lucide.min.js'
IMAGE_SCRIPT = '/static/js/code-visualization-images.js'
USES_LUCIDE = re.compile(r'lucide', re.IGNORECASE)
# 'self' allows the Studio static scripts in the sandboxed frame.
CSP = (f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' 'self' {CDNS}; "
       f"style-src 'unsafe-inline' {CDNS} {FONTS}; img-src data: blob: {CDNS}; "
       f"font-src data: {CDNS} {FONTS}; media-src data: blob:; connect-src data: blob:; "
       "worker-src blob:; frame-src 'none'; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts")


def forget(user_id: int, session: str) -> None:
    kept = VIEWS.get(user_id)
    if kept and kept[1] == session:
        del VIEWS[user_id]


def expire() -> None:
    now = clock.NOW()
    for user_id, kept in list(VIEWS.items()):
        if kept[-1] <= now:
            del VIEWS[user_id]


def keep(user_id: int, session: str, aid: int, pid: str, text: str, source: str = '') -> str:
    expire()
    token = secrets.token_hex(16)
    VIEWS[user_id] = (token, session, aid, pid, text, source, clock.NOW() + TTL)
    return token


class _ImageReferences(HTMLParser):
    # These elements contain text when scripts are enabled in the preview, including on older Python versions.
    CDATA_CONTENT_ELEMENTS = frozenset(HTMLParser.CDATA_CONTENT_ELEMENTS) | {
        'textarea', 'title', 'noscript', 'xmp', 'iframe', 'noembed', 'noframes',
    }

    def __init__(self, fragment: str, directory: str, path_max: int):
        super().__init__()
        self.fragment = fragment
        self.directory = directory
        self.path_max = path_max
        self.paths: dict[str, list[str]] = {}
        self.seen: set[str] = set()
        self.tags: list[tuple[int, int, str]] = []
        self.line = 1
        self.line_start = 0

    def handle_starttag(self, tag, attrs):
        attribute = IMAGE_ATTRIBUTES.get(tag)
        if not attribute or len(self.tags) >= MAX_IMAGE_TAGS:
            return
        value = next((v for name, v in attrs if name == attribute), None)
        if not value or not self._add_reference(value):
            return
        # Remove the original URL before the browser preloader can try it. Preserve the other attributes.
        marker = f'data-studio-visualization-{attribute}'
        kept = [(name, v) for name, v in attrs if name not in (attribute, marker)]
        kept.append((marker, value))
        if tag == 'a':
            kept.append(('href', '#'))
        rendered = ''.join(f' {escape(name)}' if v is None else f' {escape(name)}="{escape(v)}"' for name, v in kept)
        line, column = self.getpos()
        for _ in range(line - self.line):
            self.line_start = self.fragment.index('\n', self.line_start) + 1
        self.line = line
        start = self.line_start + column
        original = self.get_starttag_text()
        ending = '/>' if original.endswith('/>') else '>'
        self.tags.append((start, start + len(original), f'<{tag}{rendered}{ending}'))

    def _add_reference(self, value: str) -> bool:
        if value in self.seen:
            return True
        if len(self.seen) >= MAX_IMAGE_REFERENCES or len(value) > self.path_max or CONTROL.search(value):
            return False
        try:
            url = urlsplit(value)
            path = unquote(url.path, errors='strict')
        except ValueError:
            return False
        if (url.scheme or url.netloc or path.startswith('/') or '\\' in path
                or CONTROL.search(path) or not path.lower().endswith(IMAGE_SUFFIXES)):
            return False
        path = posixpath.normpath(posixpath.join(self.directory, path))
        if path.startswith('../') or len(path) > self.path_max:
            return False
        if path not in self.paths and len(self.paths) >= MAX_IMAGES:
            return False
        self.paths.setdefault(path, []).append(value)
        self.seen.add(value)
        return True

    def rewritten(self) -> str:
        chunks = []
        previous = 0
        for start, end, replacement in self.tags:
            chunks.extend((self.fragment[previous:start], replacement))
            previous = end
        chunks.append(self.fragment[previous:])
        return ''.join(chunks)


def relative_images(fragment: str, source: str, project_dir: str, path_max: int) -> tuple[str, dict[str, list[str]]]:
    """Only paths below the bridge's project root, using the HTML's directory as the base."""
    source = source.replace('\\', '/')
    if source.startswith('/') or re.match(r'^[A-Za-z]:', source):
        root = project_dir.replace('\\', '/')
        flavor = PureWindowsPath if root.startswith('//') or re.match(r'^[A-Za-z]:', root) else PurePosixPath
        if not flavor(root).is_absolute():
            return fragment, {}
        try:
            source = flavor(source).relative_to(flavor(root)).as_posix()
        except ValueError:
            return fragment, {}
    if any(part in ('', '.', '..') for part in source.split('/')):
        return fragment, {}
    parser = _ImageReferences(fragment, posixpath.dirname(source), path_max)
    parser.feed(fragment)
    parser.close()
    return parser.rewritten(), parser.paths


def document(fragment: str, images: Sequence[tuple[list[str], str]] = ()) -> str:
    # The same font/color variables used by Codex fragments; all styles stay in the frame.
    icons = (f'\n<script src="{LUCIDE}"></script>\n'
             '<script>globalThis.lucide?.createIcons({attrs:{width:16,height:16}});</script>'
             if USES_LUCIDE.search(fragment) else '')
    # The map keys are original HTML attribute values, so prevent an embedded closing script tag.
    assets = ('<script type="application/json">' + json.dumps(images).replace('<', r'\u003c') + '</script>'
              + f'<script src="{IMAGE_SCRIPT}"></script>' if images else '')
    return ('''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Визуализация</title>
<style>:root{color-scheme:light dark;--color-background-primary:light-dark(#fff,#202020);
--color-background-secondary:light-dark(#f4f4f4,#292929);--color-background-tertiary:light-dark(#eee,#333);
--color-text-primary:light-dark(#202020,#eee);--color-text-secondary:light-dark(#666,#aaa);
--color-text-tertiary:light-dark(#888,#888);--color-border-primary:light-dark(#ccc,#555);
--color-border-secondary:light-dark(#ddd,#444);--color-border-tertiary:light-dark(#eee,#333);
--font-sans:system-ui,sans-serif;--font-mono:ui-monospace,monospace;
--border-radius-md:8px;--border-radius-lg:12px;--border-radius-xl:16px}
*{box-sizing:border-box}body{margin:0;padding:12px;font:14px var(--font-sans);
color:var(--color-text-primary);background:var(--color-background-primary)}
button,input,select,textarea{font:inherit}svg{max-width:100%}</style>
</head><body>''' + fragment + icons + assets + '''
</body></html>''')
