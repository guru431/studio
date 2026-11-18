"""Подсчёт исходного текста без встроенных ресурсов. Содержимое остаётся на компьютере."""

import codecs
import re
from html.parser import HTMLParser


MARKUP = {".html", ".htm", ".svelte", ".vue", ".astro"}
# Заголовок ограничен 210 байтами. Хвост блока удерживает даже разрезанное «data:».
DATA_URI = re.compile(rb"data:[\w.+/-]{0,80}(?:;[\w.+=/-]{1,38}){0,3};base64,", re.I)
PAYLOAD = re.compile(rb"[^a-zA-Z0-9+/=]")
BLOCK = 128 * 1024
PARSER_BUFFER_MAX = 8 * 1024 * 1024


def _source_chunks(stream, deadline, monotonic, decoder):
    tail = b""
    in_payload = False
    while True:
        if monotonic() >= deadline:
            raise TimeoutError
        raw = stream.read(BLOCK)
        chunk = decoder.decode(raw, final=not raw).encode("utf-8") if decoder else raw
        data = tail + chunk
        limit = max(0, len(data) - 256) if raw else len(data)
        start = 0
        while True:
            if in_payload:
                end = PAYLOAD.search(data, start)
                if end is None:
                    start = len(data)
                    break
                start = end.start()
                in_payload = False
            header = DATA_URI.search(data, start)
            if header is None or header.start() >= limit:
                break
            yield data[start:header.start()]
            start = header.end()
            in_payload = True
        if start < limit:
            yield data[start:limit]
        if not raw:
            break
        tail = data[max(limit, start):]


class _EmbeddedCode(HTMLParser):
    """HTMLParser пропускает разметку и комментарии, различает текст и содержимое script/style."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.counting = False
        self.size = 0

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (name == "style" or name.startswith("on") and name[2:].isalpha()):
                self.size += len(value)
        if tag not in ("script", "style"):
            return
        attrs = dict(attrs)
        kind = (attrs.get("type") or "").partition(";")[0].strip().lower()
        self.counting = (kind in ("", "text/css") if tag == "style" else
                         "src" not in attrs and kind in ("", "module", "text/javascript", "application/javascript",
                                                         "text/ecmascript", "application/ecmascript", "text/typescript"))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.counting = False

    def handle_data(self, data):
        if self.counting:
            self.size += len(data)


def count(stream, suffix, deadline, monotonic):
    prefix = stream.read(4096)
    stream.seek(0)
    decoder = None
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        decoder = codecs.getincrementaldecoder("utf-16")()
    elif b"\0" in prefix:
        return 0
    chunks = _source_chunks(stream, deadline, monotonic, decoder)
    if suffix not in MARKUP:
        return sum(map(len, chunks))
    parser = _EmbeddedCode()
    # В начале компонента Astro между строками «---» находится серверный JavaScript.
    frontmatter = suffix == ".astro" and prefix.removeprefix(codecs.BOM_UTF8).startswith((b"---\n", b"---\r\n"))
    opening_bytes = prefix.index(b"\n") + 1 if frontmatter else 0
    tail = b""
    for chunk in chunks:
        if frontmatter:
            chunk = tail + chunk
            end = re.search(rb"\n---\r?\n", chunk)
            if end is None:
                tail = chunk[-5:]
                parser.size += len(chunk) - len(tail)
                continue
            parser.size += end.start() + 1
            chunk, frontmatter = chunk[end.end():], False
        # Latin-1 даёт ровно один символ на байт, в том числе для UTF-8 исходника.
        parser.feed(chunk.decode("latin-1"))
        if len(parser.rawdata) > PARSER_BUFFER_MAX:
            raise ValueError("превышен предел разбора разметки")
    parser.close()
    return parser.size + (len(tail) if frontmatter else 0) - opening_bytes
