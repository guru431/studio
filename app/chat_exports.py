"""Owner-only Markdown/PDF downloads; PDF uses the shared md2pdf CLI, without network images."""
import asyncio
import contextlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field, model_validator

import chat
import chat_shares
import clock
from chat_export_selection import CSS, TAGS, selection_html
import claude_code
import media
import state

router = APIRouter()
RUNNING: set[int] = set()
PDF_SECONDS = 45
PDF_MAX_BYTES = 32 * 1024 * 1024
OFFLINE_BROWSER = Path(__file__).resolve().parent.parent / "deploy" / "chromium-offline.sh"


class ExportIn(BaseModel):
    format: Literal["md", "pdf"]
    title: str = Field(default="Чат", max_length=200)
    selection: str | None = Field(default=None, min_length=1, max_length=1_000_000)

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


def _tool_line(step: dict) -> list[str]:
    """A step of «Интернет» or MCP the way the chat lists it under «Источники» (web.public: no text for the model)."""
    kind, error = step.get("kind"), step.get("error")
    if kind == "search":
        query = step.get("query")
        lines = ["- " + (f"Поиск: «{_inline(query)}»" if query else "Поиск")]
        for result in step.get("results") or []:
            if isinstance(result, dict):
                lines.append("  - " + _link(result.get("url"), result.get("title"))
                             + (" · " + _inline(result["published"]) if result.get("published") else ""))
    elif kind in ("page", "youtube"):
        target = _inline(step.get("title") or step.get("url") or "") if error else _link(step.get("url"), step.get("title"))
        about = [] if kind == "page" else [_inline(step[key]) for key in ("channel", "lang") if step.get(key)]
        lines = ["- " + ("Страница: " if kind == "page" else "Ролик: ") + target + "".join(" · " + a for a in about)]
    elif kind == "mcp":
        lines = ["- " + " · ".join(["MCP"] + [_inline(step[key]) for key in ("server", "tool") if step.get(key)])]
    else:
        lines = ["- Инструмент"]
    if error:
        lines[0] += " — " + _inline(error)
    return lines


def _sources(tools: list) -> str:
    links = sum(len(step.get("results") or []) if step.get("kind") == "search" else int(bool(step.get("url")) and not step.get("error"))
                for step in tools)
    head = f"### Источники ({links})" if links else f"### Вызовы инструментов ({len(tools)})"
    return head + "\n\n" + "\n".join(line for step in tools if isinstance(step, dict) for line in _tool_line(step))


def _parts(snapshot: dict) -> list[tuple[str, str]]:
    """The whole conversation as ("md", Markdown) and ("plain", text shown as is) parts: without tokens, project
    metadata or private attachment URLs."""
    title = " ".join(snapshot["title"].split()) or "Чат"
    parts = [("md", "# " + html.escape(title))]
    for message in snapshot["messages"]:
        user = message["role"] == "user"
        parts.append(("md", "## " + ("Пользователь" if user else "Помощник")))
        if message["text"]:
            parts.append(("plain" if user else "md", message["text"]))
        if message.get("status") == "streaming":
            parts.append(("md", "*(ответ не дописан)*"))
        if message.get("error"):
            parts.append(("plain", "Ошибка: " + message["error"]))
        if message.get("tools"):
            parts.append(("md", _sources(message["tools"])))
        for step in message.get("steps", []):
            parts.extend([("md", "### " + html.escape(_plain(step.get("name") or step.get("tool") or "Команда"))),
                          ("md", _fenced(step.get("input", ""))), ("md", _fenced(step.get("text", "")))])
        if message.get("reasoning"):
            parts.extend([("md", "### Рассуждение"), ("md", _fenced(message["reasoning"]))])
        for item in message.get("attachments", []) + message.get("images", []):
            parts.append(("md", "Вложение: " + html.escape(_plain(item.get("name", "Файл")))))
    return parts


def markdown(snapshot: dict) -> str:
    """The whole conversation as the .md file."""
    return "\n\n".join(text if kind == "md" else _literal(text) for kind, text in _parts(snapshot)) + "\n"


def _render(markdown_text: str) -> str:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    # Images in model Markdown must never read local files or contact an external host.
    parser.renderer.rules["image"] = lambda tokens, idx, options, env: html.escape(tokens[idx].content)
    body = parser.render(markdown_text)
    return body.replace('<pre>', '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">')


def _safe_html(markdown_text: str) -> str:
    return _html_block(_render(markdown_text))


def pdf_html(snapshot: dict) -> str:
    """The whole conversation for md2pdf: model text as Markdown, a user's message line by line and never as
    Markdown — the same pre-wrap block as in a selected fragment (`chat-export-plain`)."""
    body = "".join(_render(text) if kind == "md" else
                   '<div class="chat-export-plain">' + html.escape(_lines(text), quote=False).replace("\n", "&#10;") + "</div>\n"
                   for kind, text in _parts(snapshot))
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


async def _file(snapshot: dict, body: ExportIn, request: Request, section: str, check):
    uid = request.state.user.id
    if body.format == "pdf":
        if uid in RUNNING:
            raise HTTPException(429, {"code": "export_busy", "message": "PDF уже готовится — подождите"})
        if len(RUNNING) >= 2:
            raise HTTPException(429, {"code": "export_busy", "message": "сервер готовит другие PDF — повторите через минуту"})
        RUNNING.add(uid)
        try:
            text = body.selection if body.selection is not None else await asyncio.to_thread(pdf_html, snapshot)
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
        raw = markdown(snapshot).encode("utf-8")
    await check()
    chat_shares._owner_open(request, section)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", snapshot["title"]).strip(' .')[:100] or "Чат"
    return Response(raw, media_type="application/pdf" if body.format == "pdf" else "text/markdown; charset=utf-8",
                    headers={"Content-Disposition": media.disposition(stem + "." + body.format),
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
        row = chat._chat_row(request.state.user.id, chat_id)
        snapshot = {"title": row["title"] or "Чат", "messages": []}

    async def check():
        chat._chat_row(request.state.user.id, chat_id)

    return await _file(snapshot, body, request, "chat", check)


@router.post("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/export")
async def export_agent_chat(aid: int, pid: str, session_id: str, body: ExportIn, request: Request):
    async def check():
        claude_code.own_agent(request, aid)
        await claude_code.ask(request, aid, "history", project=pid, chat=session_id, before=None, limit=1)

    if body.selection is None:
        with _export_size():
            snapshot = await chat_shares.agent_snapshot(request, aid, pid, session_id, body.title)
    else:
        await check()
        snapshot = {"title": body.title, "messages": []}

    return await _file(snapshot, body, request, "code", check)
