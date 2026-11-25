"""Owner-only Markdown/PDF downloads; PDF uses the shared md2pdf CLI, without network images.

The chat archive (`POST /api/chats/{chat_id}/archive` → token, `GET /api/chats/archive/{token}`) is one ZIP: chat.md
(the same `markdown`), `files/` — attachment sources, dialog media and MCP result files (through
`mcp_sections.relay`), the unavailable ones listed in missing.txt. Trash (`deleted`) stays out. A chat where a
protected MCP source was used (`chats.protected_used`) is never exported: Markdown, PDF, selection and archive — 409
`protected` (`chat.unprotected`). The log gets ids and numbers only, never file names or paths."""
import asyncio
import contextlib
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Literal
import weakref
import zlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field, model_validator

import chat
import chat_shares
import clock
from chat_export_selection import CSS, TAGS, selection_html
import claude_code
import exports
from exports import server_word, word
import mcp_client
import mcp_sections
import mcp_sources
import media
import state
import takeout

log = logging.getLogger("studio")
router = APIRouter()
RUNNING: set[int] = set()
PDF_SECONDS = 45
PDF_MAX_BYTES = 32 * 1024 * 1024
OFFLINE_BROWSER = Path(__file__).resolve().parent.parent / "deploy" / "chromium-offline.sh"


class ExportIn(BaseModel):
    format: Literal["md", "pdf"]
    title: str = Field(default="Чат", max_length=200)
    selection: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    lang: exports.Lang = "ru"              # слова Studio в файле (exports.word); переписка и имена — как есть

    @model_validator(mode="after")
    def selection_format(self):
        if self.selection is not None and self.format != "pdf":
            raise ValueError("selection requires PDF")
        return self


def _plain(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def _fenced(value) -> str:
    text = _plain(value)
    size = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0) + 1
    fence = "`" * max(3, size)
    return f"{fence}\n{text}\n{fence}"


def _lines(text: str) -> str:
    return re.sub(r"\r\n?", "\n", text)


# What would make a line of a user's message a heading, quote, list, rule, table, fence or HTML block.
_LINE_START = re.compile(r"^( {0,3})(\d{1,9}(?=[.)])|[#>+\-*=_~`|<])", re.M)


def _literal(text: str) -> str:
    """A user's message for the .md file: shown line by line, as the chat shows it, never as Markdown.
    A line `## Помощник` inside it cannot pass for the start of the model's answer."""
    text = _LINE_START.sub(lambda m: m[1] + (m[2] + "\\" if m[2][0].isdigit() else "\\" + m[2]), _lines(text))
    lines = text.split("\n")
    return "\n".join(line + "  " if line.strip() and n + 1 < len(lines) and lines[n + 1].strip() else line
                     for n, line in enumerate(lines))


def _inline(value) -> str:
    """One line of words from a web page or a tool (title, query, error) as literal Markdown text."""
    return re.sub(r"([\\`*_\[\]<>#|~!])", r"\\\1", " ".join(_plain(value).split()))


def _link(url, title) -> str:
    words = _inline(title or url or "")
    if isinstance(url, str) and re.match(r"https?://[^\s<>]+\Z", url):
        return f"[{words}](<{url}>)"
    return words


def _tool_line(step: dict, lang: str) -> list[str]:
    """A step of «Интернет» or MCP the way the chat lists it under «Источники» (web.public: no text for the model)."""
    kind, error = step.get("kind"), step.get("error")
    if kind == "search":
        query = step.get("query")
        lines = ["- " + (word("Поиск: «{}»", lang, _inline(query)) if query else word("Поиск", lang))]
        for result in step.get("results") or []:
            if isinstance(result, dict):
                lines.append("  - " + _link(result.get("url"), result.get("title"))
                             + (" · " + _inline(result["published"]) if result.get("published") else ""))
    elif kind in ("page", "youtube"):
        target = _inline(step.get("title") or step.get("url") or "") if error else _link(step.get("url"), step.get("title"))
        about = [] if kind == "page" else [_inline(step[key]) for key in ("channel", "lang") if step.get(key)]
        lines = ["- " + word("Страница: {}" if kind == "page" else "Ролик: {}", lang, target)
                 + "".join(" · " + a for a in about)]
    elif kind == "mcp":
        lines = ["- " + " · ".join(["MCP"] + [_inline(step[key]) for key in ("server", "tool") if step.get(key)])]
        # файлы результата — только имена (web.public): сами файлы остаются на сервере MCP
        lines += ["  - " + _inline(f["name"]) for f in step.get("files") or [] if isinstance(f, dict) and f.get("name")]
    else:
        lines = ["- " + word("Инструмент", lang)]
    if error:
        lines[0] += " — " + _inline(server_word(error, lang) if isinstance(error, str) else error)
    elif isinstance(step.get("note"), str):              # заметка хода (mcp_client.TRIMMED) — не ошибка
        lines[0] += " — " + _inline(server_word(step["note"], lang))
    return lines


def _sources(tools: list, lang: str) -> str:
    links = sum(len(step.get("results") or []) if step.get("kind") == "search" else int(bool(step.get("url")) and not step.get("error"))
                for step in tools)
    head = word("Источники ({})", lang, links) if links else word("Вызовы инструментов ({})", lang, len(tools))
    return "### " + head + "\n\n" + "\n".join(line for step in tools if isinstance(step, dict) for line in _tool_line(step, lang))


def _parts(snapshot: dict, lang: str) -> list[tuple[str, str]]:
    """The whole conversation as ("md", Markdown) and ("plain", text shown as is) parts: without tokens, project
    metadata or private attachment URLs. Studio's own words in `lang` (exports.word), the conversation as is."""
    title = " ".join(snapshot["title"].split()) or word("Чат", lang)
    parts = [("md", "# " + html.escape(title))]
    for message in snapshot["messages"]:
        user = message["role"] == "user"
        parts.append(("md", "## " + word("Пользователь" if user else "Помощник", lang)))
        if isinstance(message.get("skill"), str) and message["skill"]:    # имя навыка — без его текста
            parts.append(("plain", word("Навык: {}", lang, message["skill"])))
        if message["text"]:
            parts.append(("plain" if user else "md", message["text"]))
        if message.get("status") == "streaming":
            parts.append(("md", "*" + word("(ответ не дописан)", lang) + "*"))
        if message.get("error"):
            parts.append(("plain", word("Ошибка: {}", lang, server_word(message["error"], lang))))
        if message.get("tools"):
            parts.append(("md", _sources(message["tools"], lang)))
        for step in message.get("steps", []):
            tool = step.get("tool")
            tool = server_word(tool, lang) if isinstance(tool, str) else tool      # как страница: message(s.tool)
            parts.extend([("md", "### " + html.escape(_plain(step.get("name") or tool or word("Команда", lang)))),
                          ("md", _fenced(step.get("input", ""))), ("md", _fenced(step.get("text", "")))])
        if message.get("reasoning"):
            parts.extend([("md", "### " + word("Рассуждение", lang)), ("md", _fenced(message["reasoning"]))])
        for item in message.get("attachments", []) + message.get("images", []):
            parts.append(("md", word("Вложение: {}", lang, html.escape(_plain(item.get("name", word("Файл", lang)))))))
    return parts


def markdown(snapshot: dict, lang: exports.Lang = "ru") -> str:
    """The whole conversation as the .md file."""
    return "\n\n".join(text if kind == "md" else _literal(text) for kind, text in _parts(snapshot, lang)) + "\n"


def _render(markdown_text: str) -> str:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    # Images in model Markdown must never read local files or contact an external host.
    parser.renderer.rules["image"] = lambda tokens, idx, options, env: html.escape(tokens[idx].content)
    body = parser.render(markdown_text)
    return body.replace('<pre>', '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">')


def _safe_html(markdown_text: str) -> str:
    return _html_block(_render(markdown_text))


def pdf_html(snapshot: dict, lang: exports.Lang = "ru") -> str:
    """The whole conversation for md2pdf: model text as Markdown, a user's message line by line and never as
    Markdown — the same pre-wrap block as in a selected fragment (`chat-export-plain`)."""
    body = "".join(_render(text) if kind == "md" else
                   '<div class="chat-export-plain">' + html.escape(_lines(text), quote=False).replace("\n", "&#10;") + "</div>\n"
                   for kind, text in _parts(snapshot, lang))
    return _html_block("<style>" + CSS + "</style>" + body)


def _html_block(body: str) -> str:
    # md2pdf parses this file again as CommonMark with raw HTML: the <div> block ends at the first
    # blank line, and the rest would become Markdown again (images fetched, code re-formatted).
    # Newlines inside <pre> become character references; blank lines elsewhere are insignificant.
    # markdown-it ends a line at CR as well (`&#13;` in model text arrives here as a raw CR) and
    # turns NUL into U+FFFD: normalise the same way first, or the block check below refuses.
    body = re.sub(r'\r\n?', '\n', body).replace('\0', '�')
    body = re.sub(r'<pre\b.*?</pre>', lambda match: match[0].replace('\n', '&#10;'), body, flags=re.S)
    body = re.sub(r'\n\s*\n', '\n', body)
    return '<div>\n' + body + '</div>\n'


MD2PDF = MarkdownIt("commonmark", {"html": True, "typographer": False}).enable("table").enable("strikethrough")
PRINTED_ATTRS = {"href", "title", "alt", "class", "align", "start", "value", "colspan", "rowspan", "style"}
PRINTED_STYLES = {"white-space:pre-wrap;overflow-wrap:anywhere", "text-align:left", "text-align:center",
                  "text-align:right"}


class _Printed(HTMLParser):
    """Only the tags and attributes Studio's two sanitizers write; none of them loads a resource."""

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS | {"style"}:
            raise ValueError("pdf")
        for key, value in attrs:
            if key not in PRINTED_ATTRS or (key == "style" and value not in PRINTED_STYLES):
                raise ValueError("pdf")

    def handle_data(self, data):
        if self.cdata_elem == "style" and data != CSS:
            raise ValueError("pdf")


def _printable(document: str) -> None:
    """Refuse chat.md unless md2pdf's second pass (raw HTML on) keeps it as the one block Studio wrote."""
    tokens = MD2PDF.parse(document)
    if len(tokens) != 1 or tokens[0].type != "html_block" or tokens[0].content != document:
        raise ValueError("pdf")
    checker = _Printed(convert_charrefs=True)
    checker.feed(document)
    checker.close()


def converter() -> Path:
    configured = os.environ.get("STUDIO_MD2PDF")
    if configured:
        return Path(configured)
    deployed = Path(__file__).resolve().parent.parent / "deploy" / "md2pdf.py"
    return deployed if deployed.is_file() else Path.home() / ".claude" / "bin" / "md2pdf.py"


def clean_tmp() -> None:
    """Only export work directories, before audio.clean_tmp handles the remaining flat files."""
    for path in state.CFG.tmp_dir.glob("chat-export-*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


# A browser process can outlive md2pdf for a moment and hold its profile lockfile (Windows: WinError 32);
# measured up to ~2 s when an elevated Chromium relaunches itself de-elevated. The work directory is retried
# for a bounded time; what is still locked stays for clean_tmp at the next start and never fails the export.
CLEANUP_PAUSES = (0.1, 0.2, 0.4, 0.8, 1.5)
PAUSE = time.sleep


def _remove(root: Path) -> None:
    for pause in CLEANUP_PAUSES:
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except OSError:
            PAUSE(pause)
    shutil.rmtree(root, ignore_errors=True)


def render_pdf(text: str, *, selection: bool = False) -> bytes:
    """`text` — the owner's selected HTML fragment (`selection`) or the whole chat already built by `pdf_html`."""
    tool = converter()
    if not tool.is_file():
        raise HTTPException(503, {"code": "pdf_unavailable", "message": "экспорт PDF ещё не настроен"})
    root = Path(tempfile.mkdtemp(prefix="chat-export-", dir=state.CFG.tmp_dir))
    try:
        source, target = root / "chat.md", root / "chat.pdf"
        document = _html_block(selection_html(text)) if selection else text
        env = os.environ.copy()
        env.pop("STUDIO_DATA_KEY", None)
        env["MD2PDF_TIMEOUT"] = str(PDF_SECONDS)
        # Temporary HTML and the private browser profile stay in this local work directory.
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(root)
        # Service accounts can have /nonexistent as HOME. Chromium's crash handler
        # still needs writable config/cache paths even with a private user-data-dir.
        env["HOME"] = str(root)
        env["XDG_CONFIG_HOME"] = str(root / "config")
        env["XDG_CACHE_HOME"] = str(root / "cache")
        if sys.platform.startswith("linux"):
            # The server's Chromium: no network, whatever reaches the page (deploy/chromium-offline.sh).
            env["MD2PDF_BROWSER"] = str(OFFLINE_BROWSER)
        started = clock.NOW()
        try:
            _printable(document)
            source.write_text(document, encoding="utf-8")
            source.chmod(0o600)
            result = subprocess.run([sys.executable, str(tool), str(source), str(target)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=env, timeout=PDF_SECONDS + 30)
            if result.returncode or not target.is_file() or not 1024 <= target.stat().st_size <= PDF_MAX_BYTES:
                raise ValueError("pdf")
            raw = target.read_bytes()
            if not raw.startswith(b"%PDF-"):
                raise ValueError("pdf")
            return raw
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            # md2pdf spends its whole budget (MD2PDF_TIMEOUT) on a chat too long to print: a retry gets the same.
            if isinstance(exc, subprocess.TimeoutExpired) or clock.NOW() - started >= PDF_SECONDS:
                raise HTTPException(503, {"code": "pdf_timeout", "message": "переписка слишком длинная для PDF —"
                                          " скачайте Markdown или PDF выделенного фрагмента"}) from None
            raise HTTPException(503, {"code": "pdf_failed", "message": "не удалось подготовить PDF — повторите позже"}) from None
    finally:
        _remove(root)


def _stem(title: str, lang: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(' .')[:100] or word("Чат", lang)


async def _file(snapshot: dict, body: ExportIn, request: Request, section: str, check):
    uid = request.state.user.id
    if body.format == "pdf":
        if uid in RUNNING:
            raise HTTPException(429, {"code": "export_busy", "message": "PDF уже готовится — подождите"})
        if len(RUNNING) >= 2:
            raise HTTPException(429, {"code": "export_busy", "message": "сервер готовит другие PDF — повторите через минуту"})
        RUNNING.add(uid)
        try:
            text = body.selection if body.selection is not None else await asyncio.to_thread(pdf_html, snapshot, body.lang)
        except BaseException:
            RUNNING.discard(uid)
            raise
        task = asyncio.create_task(asyncio.to_thread(render_pdf, text, **(
            {"selection": True} if body.selection is not None else {})))
        try:
            try:
                raw = await asyncio.shield(task)
            except asyncio.CancelledError:
                # The thread owns its temporary directory; let its bounded child finish cleanup.
                await asyncio.shield(task)
                raise
        finally:
            RUNNING.discard(uid)
    else:
        raw = markdown(snapshot, body.lang).encode("utf-8")
    await check()
    chat_shares._owner_open(request, section)
    return Response(raw, media_type="application/pdf" if body.format == "pdf" else "text/markdown; charset=utf-8",
                    headers={"Content-Disposition": media.disposition(_stem(snapshot["title"], body.lang) + "." + body.format),
                             "Cache-Control": "no-store"})


@contextlib.contextmanager
def _export_size():
    """The snapshot's size limit is the share's; an export says so in its own words."""
    try:
        yield
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "share_size":
            raise HTTPException(413, {"code": "export_size", "message": "переписка слишком большая для экспорта —"
                                      " выделите часть и скачайте её PDF"}) from None
        raise


@router.post("/api/chats/{chat_id}/export")
async def export_chat(chat_id: str, body: ExportIn, request: Request):
    if body.selection is None:
        with _export_size():
            snapshot, _ = chat_shares.regular_snapshot(request.state.user.id, chat_id)
    else:
        row = chat.unprotected(chat._chat_row(request.state.user.id, chat_id))
        snapshot = {"title": row["title"] or word("Чат", body.lang), "messages": []}

    async def check():                  # и после сборки PDF: защищённый источник мог сработать за это время
        chat.unprotected(chat._chat_row(request.state.user.id, chat_id))

    return await _file(snapshot, body, request, "chat", check)


@router.post("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/export")
async def export_agent_chat(aid: int, pid: str, session_id: str, body: ExportIn, request: Request):
    claude_code.own_agent(request, aid)
    claude_code.check_pid(pid)
    claude_code.check_chat(session_id)

    async def check():
        claude_code.own_agent(request, aid)
        chat_shares.unreadable(await claude_code.ask(request, aid, "history", project=pid, chat=session_id,
                                                     before=None, limit=1))

    if body.selection is None:
        with _export_size():
            snapshot = await chat_shares.agent_snapshot(request, aid, pid, session_id, body.title)
    else:
        await check()
        snapshot = {"title": body.title, "messages": []}

    return await _file(snapshot, body, request, "code", check)


# ---- chat archive: one ZIP with chat.md and the files ---------------------------------------------------------

ARCHIVE_MAX = 512 * 1024 * 1024
#: token lifetime and per-user count — as the history export (takeout); one ZIP at a time with it (takeout.RUNNING)
ARCHIVE_TTL_SEC = takeout.EXPORT_TTL_SEC
#: token → (user_id, chat_id, lang, created)
ARCHIVES: dict[str, tuple[int, str, str, float]] = {}
ARCHIVE_BIG = {"code": "export_size", "message": "чат с файлами больше 512 МиБ — скачайте Markdown или PDF"}
_NO_RANGE = SimpleNamespace(headers={})             # the archive always takes a result file whole
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


class ArchiveIn(BaseModel):
    lang: exports.Lang = "ru"


def _clean(name, fallback: str) -> str:
    """A file name for the archive: the last part of a path, without characters a file system refuses."""
    plain = name.replace("\\", "/").rsplit("/", 1)[-1] if isinstance(name, str) else ""
    return _UNSAFE.sub("_", plain).strip(" .")[:120] or fallback


def _unique(plain: str, used: set) -> str:
    stem, dot, ext = plain.rpartition(".")
    if not stem:
        stem, dot, ext = plain, "", ""
    candidate, n = plain, 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{stem} ({n}){dot}{ext}"
    used.add(candidate.lower())
    return candidate


def _plan(uid: int, chat_id: str, lang: str) -> tuple[str, bytes, list, list]:
    """(title, chat.md, files on the share [(name, rel_path or None, size, created)], MCP result files
    [(name, sid, stamp, aid, mime)]): 404 for a foreign or trashed chat, 413 above ARCHIVE_MAX of known bytes."""
    with _export_size():
        snapshot, _ = chat_shares.regular_snapshot(uid, chat_id)
    text = markdown(snapshot, lang).encode("utf-8")
    local = [tuple(row) for row in state.DB.execute(
        "SELECT a.name, m.rel_path, m.size, a.created FROM attachments a JOIN messages g ON g.id = a.message_id"
        " LEFT JOIN media m ON m.id = a.media_id AND m.user_id = a.user_id"
        " WHERE g.chat_id = ? AND a.user_id = ? ORDER BY a.created, a.rowid", (chat_id, uid))]
    local += [tuple(row) for row in state.DB.execute(
        "SELECT c.name, m.rel_path, m.size, c.created FROM chat_media c"
        " LEFT JOIN media m ON m.id = c.media_id AND m.user_id = c.user_id"
        " WHERE c.chat_id = ? AND c.user_id = ? AND c.deleted IS NULL ORDER BY c.created, c.rowid", (chat_id, uid))]
    results = []
    for row in state.DB.execute("SELECT tools FROM messages WHERE chat_id = ? AND unchosen = 0 ORDER BY created, rowid", (chat_id,)):
        try:
            steps = json.loads(row["tools"] or "[]")
        except ValueError:
            continue
        for step in steps if isinstance(steps, list) else ():
            files = step.get("files") if isinstance(step, dict) else None
            if isinstance(files, list):
                results += [(f.get("name"), step.get("sid"), step.get("stamp"), f.get("aid"), f.get("mime"))
                            for f in files if isinstance(f, dict)]
    if len(text) + sum(item[2] or 0 for item in local) > ARCHIVE_MAX:
        raise HTTPException(413, ARCHIVE_BIG)
    return snapshot["title"], text, local, results


async def _result(uid: int, sid, stamp, aid, mime, room: int, live):
    """An MCP result file spooled to a local temporary file — (file, crc, size) — with the checks of
    `mcp_files.result_file` (server open to the account and on, address and key unchanged); unavailable, gone on
    the server or larger than `room` — None."""
    if type(sid) is not int or not isinstance(aid, str) or not mcp_sections.ARTIFACT_ID.fullmatch(aid):
        return None
    try:
        server = mcp_client.row(uid, sid)
    except HTTPException:
        return None
    if not server["enabled"] or mcp_sources.stamp(server["secret_enc"]) != stamp:
        return None
    mime = mime if mime in mcp_sections.TICKET_MIME else "application/octet-stream"
    spool = tempfile.TemporaryFile(dir=state.CFG.tmp_dir)
    crc = size = 0
    try:
        response = await mcp_sections.relay(mcp_client.Session(server, uid=uid), aid, mime, _NO_RANGE)
        if not isinstance(response, mcp_sections.ArtifactStream):
            raise ConnectionError("unexpected answer")
        try:
            async for chunk in response.body_iterator:
                size += len(chunk)
                if size > room or not live():
                    raise ConnectionError("archive transfer stopped")
                crc = zlib.crc32(chunk, crc)
                spool.write(chunk)
        finally:
            await response.stack.aclose()
    except (HTTPException, ConnectionError):
        spool.close()
        return None
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool, crc, size


async def _archive(uid: int, chat_id: str, text: bytes, local: list, results: list, mark: object, digest: str):
    out, now = media.ZipWriter(media.user_zone(uid)), clock.NOW()
    used, missing, total = set(), [], len(text) + sum(item[2] or 0 for item in local)

    def live() -> bool:        # the session closed or a protected source was used meanwhile — stop (the ZIP stays cut)
        return state.DB.execute("SELECT 1 FROM sessions WHERE digest = ? AND user_id = ? AND NOT EXISTS"
                                " (SELECT 1 FROM chats WHERE id = ? AND protected_used = 1)",
                                (digest, uid, chat_id)).fetchone() is not None

    async def put(name: str, created: float, f, crc: int, size: int):
        with f:
            if not live():
                return
            yield out.entry("files/" + _unique(name, used), created, crc, size)
            left = size
            while left > 0 and live():
                block = await asyncio.to_thread(f.read, min(takeout.CHUNK, left))
                if not live():
                    return
                if not block:
                    raise OSError("file got shorter")
                left -= len(block)
                yield block

    try:
        if not live():
            return
        yield out.entry("chat.md", now, zlib.crc32(text), len(text))
        if not live():
            return
        yield text
        for name, rel, _, created in local:
            plain = _clean(name, "file")
            if rel is None:
                missing.append(plain)
                continue
            if not live():
                return
            try:
                f, crc, size, _ = await asyncio.to_thread(media.open_summed, state.CFG.media_dir / rel, takeout.CHUNK)
            except FileNotFoundError:
                await asyncio.to_thread(state.CFG.media_dir.stat)     # the share is gone — break off, not "missing"
                missing.append(plain)
                continue
            async with contextlib.aclosing(put(plain, created, f, crc, size)) as chunks:
                async for piece in chunks:
                    yield piece
        for name, sid, stamp, aid, mime in results:
            plain = _clean(name, "result" + media.EXT.get(mime, ".bin"))
            if not live():
                return
            spooled = await _result(uid, sid, stamp, aid, mime, ARCHIVE_MAX - total, live)
            if spooled is None:
                missing.append(plain)
                continue
            f, crc, size = spooled
            total += size
            async with contextlib.aclosing(put(plain, now, f, crc, size)) as chunks:
                async for piece in chunks:
                    yield piece
        if not live():
            return
        if missing:
            listed = ("\n".join(missing) + "\n").encode("utf-8")
            yield out.entry("missing.txt", now, zlib.crc32(listed), len(listed))
            if not live():
                return
            yield listed
        if not live():
            return
        yield out.end()
        log.info("chat archive %s: %d files, %d missing", chat_id[:8], out.count - 1 - bool(missing), len(missing))
    except OSError:
        log.warning("chat archive %s: storage broke off reading", chat_id[:8])
        raise media.StorageError("хранилище оборвало чтение") from None
    finally:
        takeout._release(uid, mark)


@router.post("/api/chats/{chat_id}/archive")
async def create_archive(chat_id: str, body: ArchiveIn, request: Request):
    uid, now = request.state.user.id, clock.NOW()
    _, _, local, _ = _plan(uid, chat_id, body.lang)
    if any(item[1] for item in local):
        await takeout._share_ready()
    for token in [t for t, held in ARCHIVES.items() if now - held[3] > ARCHIVE_TTL_SEC]:
        del ARCHIVES[token]
    token = secrets.token_urlsafe(18)
    ARCHIVES[token] = (uid, chat_id, body.lang, now)
    for old in [t for t, held in ARCHIVES.items() if held[0] == uid][:-takeout.EXPORT_KEEP]:
        del ARCHIVES[old]
    return {"token": token}


@router.get("/api/chats/archive/{token}")
async def get_archive(token: str, request: Request):
    uid = request.state.user.id
    held = ARCHIVES.get(token)
    if held is None or held[0] != uid or clock.NOW() - held[3] > ARCHIVE_TTL_SEC:
        raise HTTPException(404, media.GONE)
    title, text, local, results = _plan(uid, held[1], held[2])
    if any(item[1] for item in local):
        await takeout._share_ready()
    # check and mark without an await between them: two downloads at once do not both pass
    if uid in takeout.RUNNING:
        raise HTTPException(409, takeout.BUSY)
    mark = object()
    takeout.RUNNING[uid] = mark
    body = _archive(uid, held[1], text, local, results, mark, request.state.digest)
    weakref.finalize(body, takeout._release, uid, mark)    # the answer never started — leaving memory drops the mark
    return StreamingResponse(body, media_type="application/zip",
                             headers={"Content-Disposition": media.disposition(_stem(title, held[2]) + ".zip"),
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
