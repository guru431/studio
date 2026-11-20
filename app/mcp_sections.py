"""Run MCP tools directly from the corresponding Studio sections."""

import asyncio
import base64
from contextlib import AsyncExitStack
import json
import re
from types import SimpleNamespace

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
import httpx
from pydantic import BaseModel, Field, ValidationError

import clock
import forms
import images
import mcp_client as mcp
import media
import security
import upstream
import voices

router = APIRouter(prefix="/api/mcp-tools")
TOOLS = {
    "photo": {"image_generate", "image_edit", "image_describe", "generate_image", "edit_image", "describe_image"},
    "video": {"video_create", "video_estimate", "generate_video"},
    "speech": {"speech_synthesize", "synthesize_speech", "text_to_speech"},
    "transcribe": {"audio_transcribe", "transcribe_audio"},
    "ocr": {"document_read", "read_document", "ocr_image"},
    "translate": {"text_translate", "translate_text"},
}
FILE_FIELDS = {"photo": {"artifact_id": "image"}, "video": {"artifact_id": "image"},
               "ocr": {"artifact_id": "image"}, "speech": {"reference_artifact_id": "audio"},
               "transcribe": {"artifact_id": "audio"}}
UPLOAD_MAX = 12 << 20
PARAMS_MAX = 128 << 10
RESULT_MAX = 4 << 20  # MCP-сервисы дублируют structuredContent в текстовом блоке.
DOWNLOAD_MAX = 512 << 20
DISCOVERY_TIMEOUT = 8
TICKET_AGE = 3 * 60 * 60
JOB_ID = re.compile(r"job_[0-9a-f]{32}")
ARTIFACT_ID = re.compile(r"art_[0-9a-f]{32}")
ACTIVE = {"queued", "running", "waiting_gpu"}
INLINE_MIME = {mime for mime in media.EXT if mime.startswith(("image/", "audio/", "video/"))}
TICKET_MIME = INLINE_MIME | {"text/plain", "text/markdown", "application/json", "application/octet-stream"}


def section_tools(section: str) -> set[str]:
    if section not in TOOLS:
        raise mcp.refused("Такого раздела MCP нет", "not_found", 404)
    return TOOLS[section]


def clean_text(value, limit=60_000):
    return value[:limit].encode("utf-8", "replace").decode() if isinstance(value, str) else ""


def supported(section, tools):
    by_name = {t["name"]: t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}
    has_upload = "artifact_upload" in by_name
    for name, tool in by_name.items():
        schema = tool.get("inputSchema")
        if name not in TOOLS[section] or not isinstance(schema, dict) or schema.get("type") != "object":
            continue
        properties, required = schema.get("properties", {}), schema.get("required", [])
        if (not isinstance(properties, dict) or len(properties) > 128
                or any(not isinstance(value, dict) for value in properties.values()) or not isinstance(required, list)
                or any(not isinstance(key, str) or key not in properties for key in required)):
            continue
        file_fields = FILE_FIELDS.get(section, {})
        if not has_upload and file_fields.keys() & set(required):
            continue
        yield {"name": name, "title": clean_text(tool.get("title"), 160),
               "description": clean_text(tool.get("description"), 2000),
               "files": {key: kind for key, kind in file_fields.items() if has_upload and key in properties},
               "schema": schema, "jobs": {"job_status", "job_result"} <= by_name.keys()}


@router.get("/{section}")
async def catalog(section: str, request: Request):
    section_tools(section)
    uid = request.state.user.id
    rows = mcp.servers(uid, enabled_only=True)

    async def discover(item):
        session = mcp.Session(item, uid=uid)
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                tools = list(supported(section, await session.tools()))
            # Keep broad or malformed schemas from breaking discovery for other servers.
            encoded = json.dumps(tools, ensure_ascii=False, allow_nan=False).encode("utf-8", "replace")
            return {"id": session.sid, "name": item["name"], "tools": json.loads(encoded)}
        except (HTTPException, TimeoutError, ValueError, RecursionError):
            return {"id": session.sid, "name": item["name"], "unavailable": True}
        finally:
            await session.close()

    servers = await asyncio.gather(*(discover(item) for item in rows))
    return {"items": [server for server in servers if server.get("tools") or server.get("unavailable")],
            "max_upload_bytes": UPLOAD_MAX}


class FileIn(BaseModel):
    data: str | None = None
    media_id: str | None = Field(default=None, pattern=forms.MEDIA_ID_RE.pattern, max_length=32)
    mime_type: str = Field(default="", max_length=100)
    name: str = Field(default="", max_length=255)


class RunIn(BaseModel):
    tool: str = Field(max_length=128)
    arguments: dict = Field(default_factory=dict, max_length=128)
    files: dict[str, FileIn] = Field(default_factory=dict, max_length=1)
    voice_id: str | None = Field(default=None, pattern=forms.MEDIA_ID_RE.pattern, max_length=32)


def ticket(session: mcp.Session, section: str, kind: str, *payload) -> str:
    return security.encrypt(json.dumps([session.uid, session.sid, security.digest(session.stamp.decode()),
                                       section, kind, clock.NOW(), *payload])).decode()


def redeem(request, section, encoded, kind):
    uid = request.state.user.id
    try:
        if len(encoded) > 1024:
            raise ValueError
        owner, sid, stamp, scope, purpose, issued, *payload = json.loads(security.decrypt(encoded.encode()))
        if owner != uid or scope != section or purpose != kind or not 0 <= clock.NOW() - issued <= TICKET_AGE:
            raise ValueError
        item = mcp.row(uid, sid)
        if not item["enabled"] or stamp != security.digest(item["secret_enc"].decode()):
            raise ValueError
    except (InvalidToken, TypeError, ValueError):
        raise mcp.refused("Результат MCP недоступен", "not_found", 404) from None
    return mcp.Session(item, uid=uid), payload


async def call(session, name, args):
    result = await session.rpc("tools/call", {"name": name, "arguments": args}, max_bytes=RESULT_MAX)
    if result.get("isError"):
        data = result.get("structuredContent")
        error = data.get("error") if isinstance(data, dict) else None
        message = clean_text(error.get("message"), 600) if isinstance(error, dict) else ""
        raise mcp.refused(message or first_text(result, 600) or "Инструмент MCP вернул ошибку")
    return result


def blocks(result):
    content = result.get("content")
    return (item for item in content if isinstance(item, dict)) if isinstance(content, list) else ()


def first_text(result, limit=60_000):
    return clean_text("\n".join(item["text"] for item in blocks(result)
                                if item.get("type") == "text" and isinstance(item.get("text"), str)), limit)


def result_text(data):
    segments = data.get("segments")
    if isinstance(segments, list):
        lines = []
        for segment in segments:
            if isinstance(segment, dict) and isinstance(segment.get("text"), str):
                speaker = clean_text(segment.get("speaker"), 80)
                lines.append(f"{speaker}: {segment['text']}" if speaker else segment["text"])
        if lines:
            return "\n".join(lines)
    for key in ("text", "markdown", "translation"):
        if isinstance(data.get(key), str):
            return data[key]
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return clean_text(message.get("content"))
    return ""


def present(session, section, result):
    data = result.get("structuredContent")
    files = []
    for item in blocks(result):
        mime, encoded = item.get("mimeType"), item.get("data")
        if (isinstance(mime, str) and mime in INLINE_MIME and isinstance(encoded, str) and encoded.isascii()
                and item.get("type") in ("image", "audio")):
            files.append({"mime": mime, "base64": encoded})
    if not isinstance(data, dict):
        return {"text": first_text(result), "files": files}
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts[:128]:
            if not isinstance(artifact, dict):
                continue
            aid, mime = artifact.get("id"), artifact.get("mime_type")
            if (not isinstance(aid, str) or not ARTIFACT_ID.fullmatch(aid)
                    or not isinstance(mime, str) or artifact.get("download_path") != f"/artifacts/{aid}/content"):
                continue
            mime = mime if mime in TICKET_MIME else "application/octet-stream"
            files.append({"mime": mime, "name": clean_text(artifact.get("name"), 120),
                          "url": f"/api/mcp-tools/{section}/files/{ticket(session, section, 'file', aid, mime)}"})
    text = result_text(data)
    if not text and not files:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    return {"text": clean_text(text), "files": files}


async def upload_artifact(session, data, mime):
    reply = await call(session, "artifact_upload", {"data_base64": base64.b64encode(data).decode(), "mime_type": mime})
    item = reply.get("structuredContent")
    aid = item.get("id") if isinstance(item, dict) else None
    if not isinstance(aid, str) or not ARTIFACT_ID.fullmatch(aid):
        raise mcp.refused("MCP не вернул идентификатор файла")
    return aid


async def upload(session, kind, file):
    if file.media_id is not None:
        if kind != "image" or file.data is not None:
            raise mcp.refused("Выберите одно исходное фото", "invalid", 422)
        try:
            data = await forms.photo(session.uid, None, file.media_id)
        except forms.UploadError as exc:
            raise mcp.refused(str(exc), "invalid", exc.status) from None
        return await upload_artifact(session, data, "image/png")
    encoded = file.data
    try:
        data = base64.b64decode(encoded or "", validate=True)
    except (ValueError, UnicodeError):
        raise mcp.refused("Неверные данные файла MCP", "invalid", 422) from None
    if not data or len(data) > UPLOAD_MAX:
        raise mcp.refused(f"Файл MCP должен быть непустым и не больше {UPLOAD_MAX >> 20} МиБ", "too_big", 413)
    if kind == "image":
        try:
            data = await asyncio.to_thread(forms.normalize_image, data)
        except forms.UploadError as exc:
            raise mcp.refused(str(exc), "invalid", exc.status) from None
        mime = "image/png"
    else:
        mime = forms.audio_type(file.mime_type, file.name)
        if mime not in INLINE_MIME:
            raise mcp.refused("Выберите звуковой файл MCP", "invalid", 415)
    return await upload_artifact(session, data, mime)


async def voice_arguments(session, section, tool, voice_id, files):
    props = tool["schema"].get("properties", {})
    engine = props.get("engine", {}).get("enum")
    if (section != "speech" or files or "reference_artifact_id" not in tool["files"] or "reference_text" not in props
            or not isinstance(engine, list) or "clone" not in engine):
        raise mcp.refused("Инструмент MCP не принимает образец голоса", "invalid", 422)
    audio, text = await voices.reference(session.uid, voice_id)
    if not audio or len(audio) > UPLOAD_MAX:
        raise mcp.refused("Образец голоса превышает предел MCP", "too_big", 413)
    aid = await upload_artifact(session, audio, "audio/wav")
    return {"engine": "clone", "reference_artifact_id": aid, "reference_text": text}


@router.post("/{section}/servers/{sid}/run")
async def run(section: str, sid: int, request: Request):
    names = section_tools(section)
    uid = request.state.user.id
    session = mcp.Session(mcp.row(uid, sid), uid=uid)
    try:
        body = RunIn.model_validate_json(await forms.read_body(request, UPLOAD_MAX * 4 // 3 + PARAMS_MAX))
        arguments = json.dumps(body.arguments, allow_nan=False)
    except (ValidationError, ValueError, RecursionError):
        raise mcp.refused("Неверные параметры инструмента MCP", "invalid", 422) from None
    if body.tool not in names or len(arguments) > PARAMS_MAX:
        raise mcp.refused("Инструмент недоступен в этом разделе MCP", "invalid", 422)
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            tools = list(supported(section, await session.tools()))
        tool = next((item for item in tools if item["name"] == body.tool), None)
        if tool is None:
            raise mcp.refused("Инструмент недоступен в этом разделе MCP")
        args = body.arguments
        if not body.files.keys() <= tool["files"].keys():
            raise mcp.refused("Инструмент не принимает этот файл MCP", "invalid", 422)
        if body.voice_id:
            args.update(await voice_arguments(session, section, tool, body.voice_id, body.files))
            args.pop("voice", None)
        else:
            for key, file in body.files.items():
                args[key] = await upload(session, tool["files"][key], file)
        if body.tool in {"image_generate", "image_edit"} and isinstance(args.get("model"), str):
            args["model"] = images.MODELS.get(args["model"], args["model"])
        result = await call(session, body.tool, args)
        data = result.get("structuredContent")
        jid = data.get("id") if isinstance(data, dict) else None
        if tool["jobs"] and isinstance(jid, str) and JOB_ID.fullmatch(jid):
            return {"job": ticket(session, section, "job", jid), "status": "queued"}
        return present(session, section, result)
    except TimeoutError:
        raise mcp.refused("MCP не успел вернуть список инструментов") from None
    finally:
        await session.close()


class JobIn(BaseModel):
    ticket: str = Field(max_length=1024)


@router.post("/{section}/status")
async def status(section: str, body: JobIn, request: Request):
    section_tools(section)
    session, (jid,) = redeem(request, section, body.ticket, "job")
    try:
        await session.initialize()
        job = (await call(session, "job_status", {"job_id": jid})).get("structuredContent")
        if not isinstance(job, dict) or not isinstance(job.get("status"), str):
            raise mcp.refused("MCP вернул неверное состояние задания")
        if job["status"] == "completed":
            reply = (await call(session, "job_result", {"job_id": jid})).get("structuredContent")
            if isinstance(reply, dict) and isinstance(reply.get("result"), dict):
                return present(session, section, {"structuredContent": reply["result"]})
            raise mcp.refused("MCP не вернул результат задания")
        error = job.get("error")
        return {"status": clean_text(job["status"], 80), "active": job["status"] in ACTIVE,
                "error": clean_text(error.get("message"), 600) if isinstance(error, dict) else ""}
    finally:
        await session.close()


class ArtifactStream(StreamingResponse):
    def __init__(self, stack, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stack = stack

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.stack.aclose()


@router.get("/{section}/files/{encoded}")
async def download(section: str, encoded: str, request: Request):
    section_tools(section)
    session, (aid, mime) = redeem(request, section, encoded, "file")
    # The transfer path is part of the service contract. Never fetch a URL from a tool result.
    url = httpx.URL(session.url).copy_with(raw_path=f"/artifacts/{aid}/content".encode())
    headers = httpx.Headers(session.headers(rpc=False))
    headers["Accept-Encoding"] = "identity"
    headers.pop("Range", None)
    value = request.headers.get("range", "")
    if value:
        if len(value) > 64 or not media.RANGE_RE.fullmatch(value):
            return Response(status_code=416)
        headers["Range"] = value
    async with AsyncExitStack() as stack:
        try:
            response = await stack.enter_async_context(upstream.stream(
                SimpleNamespace(id=session.sid), "GET", url, keyless=True, headers=dict(headers), timeout=mcp.TIMEOUT))
        except (httpx.HTTPError, upstream.UpstreamRefused, ValueError):
            raise mcp.refused("Файл MCP недоступен") from None
        outgoing = {key: response.headers[key] for key in ("content-length", "content-range", "accept-ranges")
                    if key in response.headers}
        if response.status_code == 416:
            return Response(status_code=416, headers={k: v for k, v in outgoing.items() if k != "content-length"})
        length = outgoing.get("content-length", "")
        if (response.status_code not in (200, 206) or response.headers.get("content-encoding", "identity") != "identity"
                or (length and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > DOWNLOAD_MAX))):
            raise mcp.refused("Файл MCP недоступен или превышает предел загрузки")
        outgoing["X-Content-Type-Options"] = "nosniff"
        if mime not in INLINE_MIME:
            outgoing["Content-Disposition"] = f'attachment; filename="result{media.EXT.get(mime, ".bin")}"'

        async def chunks():
            size = 0
            try:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > DOWNLOAD_MAX:
                        raise ConnectionError("MCP artifact exceeds the transfer limit")
                    yield chunk
            except httpx.HTTPError:
                raise ConnectionError("MCP artifact transfer was interrupted") from None

        return ArtifactStream(stack.pop_all(), chunks(), media_type=mime, status_code=response.status_code, headers=outgoing)
