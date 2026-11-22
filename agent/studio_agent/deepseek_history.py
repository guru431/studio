"""История чата DeepSeek Harness из файлов его сессий: у ACP DSH нет ни `session/load`, ни повтора событий.

Файл — `<DSH_HOME>/sessions/--<ключ каталога>--/<id>/session.v4.jsonl[.zstd]`: строка заголовка и события
`{"type", "seq", "time", "data"}`. Мост только читает: ничего не пишет и не создаёт. Читаются реплики человека
(`user/message` с `source.kind == "user"`), ответы (`assistant/message`: текст, мысли, вызовы инструментов) и итоги
вызовов (`tool/result`); подмешанный контекст и служебные записи пропускаются. Сообщения — той же формы, что у
`acp.normalize`; картинки — ссылками (`history_images.reference`), блоки с вложением — в `image_blocks`.
Журнала нет: причина отказа — только код `Unsupported.code`, без путей и текстов.
"""

import json
import os
import re
from pathlib import Path

from . import acp, events, history_images, timeline

# Поддерживаемые имена файлов сессии: версия формата 4, со сжатием (по умолчанию) и без.
FORMATS = ("session.v4.jsonl.zstd", "session.v4.jsonl")
VERSION = 4
READ_MAX = 64 * 1024 * 1024
CHUNK = 64 * 1024

SESSION_ID = re.compile(r"(?:session-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FILE_NAME = re.compile(r"session\.v(\d+)\.jsonl(\.zstd)?")
MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
CALL_ID = re.compile(r".{1,200}", re.S)


class Unsupported(Exception):
    """Историю прочитать нельзя: `code` — `format` (незнакомая версия или нет распаковщика), `corrupt`
    (файл повреждён) или `too_large` (больше `READ_MAX`)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def revision(path):
    """Признак изменения файла для `metadata.HistoryIndex`."""
    info = Path(path).stat()
    return (info.st_mtime_ns, info.st_size)


def _zstd():
    """`(открыть(поток), класс ошибки)`: `compression.zstd` на Python 3.14+, иначе пакет `zstandard`."""
    try:
        import compression.zstd as zstd
        return zstd.ZstdFile, zstd.ZstdError
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        raise Unsupported("format") from None

    def open_stream(raw):
        return zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)
    return open_stream, zstandard.ZstdError


def _lines(path):
    """Строки файла как `(байты, целая)`; чтение потоковое, больше `READ_MAX` распакованных байт — `too_large`.
    Оборванный хвост (запись DSH прервана) даёт последнюю строку с `целая == False`."""
    path = Path(path)
    if path.name not in FORMATS:
        raise Unsupported("format")
    if path.stat().st_size > READ_MAX:
        raise Unsupported("too_large")
    with path.open("rb") as raw:
        if path.name.endswith(".zstd"):
            open_stream, zstd_error = _zstd()
            stream = open_stream(raw)
        else:
            stream, zstd_error = raw, ()
        total, pending = 0, []
        try:
            while chunk := stream.read(CHUNK):
                total += len(chunk)
                if total > READ_MAX:
                    raise Unsupported("too_large")
                pieces = chunk.split(b"\n")
                if len(pieces) == 1:
                    pending.append(chunk)
                    continue
                pieces[0] = b"".join(pending) + pieces[0]
                for line in pieces[:-1]:
                    yield line, True
                pending = [pieces[-1]]
        except EOFError:
            pass                                   # оборванный последний кадр zstd: берём всё до него
        except zstd_error:
            raise Unsupported("corrupt") from None
        tail = b"".join(pending)
        if tail:
            yield tail, False


def _records(path):
    """Записи файла: заголовок и события. Оборванная последняя строка отбрасывается, мусор внутри — `corrupt`."""
    for line, whole in _lines(path):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            if whole:
                raise Unsupported("corrupt") from None
            return
        if not isinstance(record, dict):
            raise Unsupported("corrupt")
        yield record


def _header(records):
    first = next(records, None)
    if first is None or first.get("type") != "session":
        raise Unsupported("corrupt")
    version = first.get("version")
    if type(version) is not int:
        raise Unsupported("corrupt")
    if version != VERSION:
        raise Unsupported("format")
    return first


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def locate(home, project, session_id):
    """Файл истории сессии или None. Ищет только внутри `<home>/sessions`, id — по шаблону DSH (UUID, у сессий Web
    с `session-`); каталог ключа проекта не вычисляется, а проверяется `cwd` заголовка. Из файлов сессии берётся
    наибольшая версия формата — незнакомую `read` отклонит как `Unsupported("format")`."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    try:
        root = (Path(home) / "sessions").resolve()
        keys = sorted(root.iterdir())
    except OSError:
        return None
    for key in keys:
        folder = key / session_id
        try:
            if key.is_symlink() or folder.is_symlink() or not folder.is_dir() or not folder.resolve().is_relative_to(root):
                continue
            found = [(int(m.group(1)), bool(m.group(2)), item) for item in folder.iterdir()
                     if (m := FILE_NAME.fullmatch(item.name)) and item.is_file() and not item.is_symlink()]
        except OSError:
            continue
        if not found:
            continue
        path = max(found, key=lambda row: row[:2])[2]
        records = _records(path)
        try:
            cwd = _header(records).get("cwd")
        except (Unsupported, OSError):
            cwd = None                             # проверить нельзя — `read` скажет, что не так
        finally:
            records.close()
        if isinstance(cwd, str) and not _same(cwd, str(project)):
            continue
        return path
    return None


def _texts(content):
    return [block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]


def _identifier(value, fallback):
    return value if isinstance(value, str) and MESSAGE_ID.fullmatch(value) else fallback


def _user(data, out, seen, image_blocks):
    content = data.get("content")
    source = data.get("source")
    if not isinstance(content, list) or not isinstance(source, dict) or source.get("kind") != "user":
        return None
    mid = _identifier(data.get("id"), str(len(out)))
    if mid in seen:
        return None
    seen.add(mid)
    message = {"id": mid, "role": "user", "text": "\n".join(_texts(content)), "steps": [], "time": None}
    for block in history_images.blocks(content):
        images = message.setdefault("images", [])
        images.append(history_images.reference(len(images)))
        if image_blocks is not None:
            image_blocks.setdefault(mid, []).append(block)
    return message


def _arguments(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {"arguments": value}
    return value if isinstance(value, dict) else {"arguments": value}


def _assistant(current, data, calls):
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        kind, text = block.get("type"), block.get("text")
        if kind == "text" and isinstance(text, str) and text:
            timeline.append_text(current, ("\n\n" if current["text"] else "") + text)
        elif kind == "reasoning" and isinstance(text, str) and text:
            thought = calls.get("thought")
            if thought is None:
                thought = calls["thought"] = {"id": "thought", "tool": "Рассуждения", "input": {}, "text": "", "error": False}
                timeline.append_step(current, thought)
            thought["text"] = events.clip(thought["text"] + ("\n\n" if thought["text"] else "") + text)
        elif kind == "tool-call" and isinstance(block.get("id"), str) and CALL_ID.fullmatch(block["id"]) \
                and block["id"] not in calls:
            step = acp.step_of({"toolCallId": block["id"], "title": str(block.get("name") or "Tool")[:200],
                                "rawInput": _arguments(block.get("arguments"))})
            calls[block["id"]] = step
            timeline.append_step(current, step)


def _result(data, calls):
    message = data.get("message")
    if not isinstance(message, dict):
        return
    step = calls.get(message.get("toolCallId"))
    content = message.get("content")
    if step is None or not isinstance(content, list):
        return
    text = "\n".join(_texts(content))
    step.update(acp.step_of({
        "toolCallId": step["id"], "status": "failed" if message.get("isError") is True else "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": text}}]}, step))


def read(path, *, image_blocks=None):
    """Сообщения сессии (как `acp.normalize`); `image_blocks[id сообщения]` — блоки картинок со ссылкой на
    вложение DSH (сами байты не читаются). Ошибки формата, повреждения и размера — `Unsupported`."""
    records = _records(path)
    out, seen, current, calls = [], set(), None, {}
    try:
        _header(records)
        for record in records:
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            kind = record.get("type")
            if kind == "user/message":
                message = _user(data, out, seen, image_blocks)
                if message is not None:
                    out.append(message)
                    current, calls = None, {}
            elif kind == "assistant/message":
                if current is None:
                    inner = data.get("message")
                    current = {"id": _identifier(inner.get("id") if isinstance(inner, dict) else None, str(len(out))),
                               "role": "assistant", "text": "", "steps": [], "time": None}
                    out.append(current)
                _assistant(current, data, calls)
            elif kind == "tool/result" and current is not None:
                _result(data, calls)
    except Unsupported:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        raise Unsupported("corrupt") from None
    finally:
        records.close()
    return [timeline.compact(m) for m in out if m["text"] or m["steps"] or m.get("images")]
