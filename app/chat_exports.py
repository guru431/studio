"""Owner-only Markdown/PDF downloads; PDF uses the shared md2pdf CLI, without network images."""
import asyncio
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

import chat
import chat_shares
import claude_code
import media
import state

router = APIRouter()
RUNNING: set[int] = set()
PDF_SECONDS = 45
PDF_MAX_BYTES = 32 * 1024 * 1024


class ExportIn(BaseModel):
    format: Literal["md", "pdf"]
    title: str = Field(default="Чат", max_length=200)


def _plain(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def _fenced(value) -> str:
    text = _plain(value)
    size = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0) + 1
    fence = "`" * max(3, size)
    return f"{fence}\n{text}\n{fence}"


def markdown(snapshot: dict) -> str:
    """The whole conversation, without tokens, project metadata or private attachment URLs."""
    title = " ".join(snapshot["title"].split()) or "Чат"
    parts = ["# " + html.escape(title)]
    for message in snapshot["messages"]:
        parts.append("## " + ("Пользователь" if message["role"] == "user" else "Помощник"))
        parts.append(message["text"])
        for step in message.get("steps", []):
            parts.extend(["### " + html.escape(_plain(step.get("name") or step.get("tool") or "Команда")),
                          _fenced(step.get("input", "")), _fenced(step.get("text", ""))])
        if message.get("reasoning"):
            parts.extend(["### Рассуждение", _fenced(message["reasoning"])])
        for item in message.get("attachments", []) + message.get("images", []):
            parts.append("Вложение: " + html.escape(_plain(item.get("name", "Файл"))))
    return "\n\n".join(parts) + "\n"


def _safe_html(markdown_text: str) -> str:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    # Images in model Markdown must never read local files or contact an external host.
    parser.renderer.rules["image"] = lambda tokens, idx, options, env: html.escape(tokens[idx].content)
    body = parser.render(markdown_text)
    body = body.replace('<pre>', '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">')
    return '<div>\n' + body + '\n</div>\n'


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
            shutil.rmtree(path)


def render_pdf(text: str) -> bytes:
    tool = converter()
    if not tool.is_file():
        raise HTTPException(503, {"code": "pdf_unavailable", "message": "экспорт PDF ещё не настроен"})
    with tempfile.TemporaryDirectory(prefix="chat-export-", dir=state.CFG.tmp_dir) as folder:
        root = Path(folder)
        source, target = root / "chat.md", root / "chat.pdf"
        source.write_text(_safe_html(text), encoding="utf-8")
        source.chmod(0o600)
        env = os.environ.copy()
        env.pop("STUDIO_DATA_KEY", None)
        env["MD2PDF_TIMEOUT"] = str(PDF_SECONDS)
        # Temporary HTML and the private browser profile stay in this local work directory.
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(root)
        try:
            result = subprocess.run([sys.executable, str(tool), str(source), str(target)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=env, timeout=PDF_SECONDS + 30)
            if result.returncode or not target.is_file() or not 1024 <= target.stat().st_size <= PDF_MAX_BYTES:
                raise ValueError("pdf")
            raw = target.read_bytes()
            if not raw.startswith(b"%PDF-"):
                raise ValueError("pdf")
            return raw
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise HTTPException(503, {"code": "pdf_failed", "message": "не удалось подготовить PDF — повторите позже"}) from None


async def _file(snapshot: dict, body: ExportIn, request: Request, section: str, check):
    uid = request.state.user.id
    text = markdown(snapshot)
    if body.format == "pdf":
        if uid in RUNNING or len(RUNNING) >= 2:
            raise HTTPException(429, {"code": "export_busy", "message": "PDF уже готовится — подождите"})
        RUNNING.add(uid)
        task = asyncio.create_task(asyncio.to_thread(render_pdf, text))
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
        raw = text.encode("utf-8")
    await check()
    chat_shares._owner_open(request, section)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", snapshot["title"]).strip(' .')[:100] or "Чат"
    return Response(raw, media_type="application/pdf" if body.format == "pdf" else "text/markdown; charset=utf-8",
                    headers={"Content-Disposition": media.disposition(stem + "." + body.format),
                             "Cache-Control": "no-store"})


@router.post("/api/chats/{chat_id}/export")
async def export_chat(chat_id: str, body: ExportIn, request: Request):
    snapshot, _ = chat_shares.regular_snapshot(request.state.user.id, chat_id)

    async def check():
        chat._chat_row(request.state.user.id, chat_id)

    return await _file(snapshot, body, request, "chat", check)


@router.post("/api/code/agents/{aid}/projects/{pid}/chats/{session_id}/export")
async def export_agent_chat(aid: int, pid: str, session_id: str, body: ExportIn, request: Request):
    snapshot = await chat_shares.agent_snapshot(request, aid, pid, session_id, body.title)

    async def check():
        claude_code.own_agent(request, aid)
        await claude_code.ask(request, aid, "history", project=pid, chat=session_id, before=None, limit=1)

    return await _file(snapshot, body, request, "code", check)
