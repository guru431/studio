"""Run MCP tools directly from the corresponding Studio sections.

Section tools closed to the account by access groups (access.py) are neither listed nor run. Service calls Studio
makes itself (`artifact_upload`, `job_status`, `job_result`) only need access to the server: tool rules never block
uploading a source or collecting the result of a permitted tool.
"""

import asyncio
import base64
import binascii
from contextlib import AsyncExitStack
import json
import math
import re
from types import SimpleNamespace
import uuid

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
import httpx
from pydantic import BaseModel, Field, ValidationError

import access
import cleaner_route
import clock
import forms
import images
import mcp_client as mcp
import mcp_sources
import media
import operations
import security
import speech
import state
import upstream
import voices

router = APIRouter(prefix="/api/mcp-tools")
TOOLS = {
    "chat": {"llm_ask"},
    "photo": {"image_generate", "image_edit", "image_describe", "generate_image", "edit_image", "describe_image", "upscale_image"},
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
MODEL_CHOICES_MAX = 256
MODEL_ID_MAX = 128
RESULT_MAX = 4 << 20  # MCP-сервисы дублируют structuredContent в текстовом блоке.
DOWNLOAD_MAX = 512 << 20
DISCOVERY_TIMEOUT = 8
CHAT_PATHS = ("/v1/chat/completions", "/v1/messages")
TICKET_AGE = 3 * 60 * 60
JOB_ID = re.compile(r"job_[0-9a-f]{32}")
ARTIFACT_ID = re.compile(r"art_[0-9a-f]{32}")
ACTIVE = {"queued", "running", "waiting_gpu"}
INLINE_MIME = {mime for mime in media.EXT if mime.startswith(("image/", "audio/", "video/"))}
TICKET_MIME = INLINE_MIME | {"text/plain", "text/markdown", "application/json", "application/octet-stream", media.DOCX}
SRT_MAX = 2 << 20                # файл .srt итога «Расшифровки», который Studio читает сама для вида расшифровки
SRT_MIME = "application/x-subrip"


def section_tools(section: str) -> set[str]:
    if section not in TOOLS:
        raise mcp.refused("Такого раздела MCP нет", "not_found", 404)
    return TOOLS[section]


def clean_text(value, limit=60_000):
    return value[:limit].encode("utf-8", "replace").decode() if isinstance(value, str) else ""


def supported(section, tools, *, uid=None, sid=None, item=None):
    """Section tools of one server; with `sid` — only those the account may call (`access.tool_allowed`). With the
    server row `item` and the admin flag "web only through Cleaner" (`cleaner_route.web_only`) — no web tools (the
    "internet" mark with the admin policy, `mcp_sources.effective`): sections have no path through Cleaner."""
    by_name = {t["name"]: t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}
    has_upload = "artifact_upload" in by_name
    closed = item is not None and cleaner_route.web_only()
    rules = mcp_sources.policy(item["id"]) if closed and item["user_id"] is None else {}
    for name, tool in by_name.items():
        schema = tool.get("inputSchema")
        if name not in TOOLS[section] or not isinstance(schema, dict) or schema.get("type") != "object":
            continue
        if sid is not None and not access.tool_allowed(uid, sid, name):
            continue
        if closed and mcp_sources.effective(tool, rules.get(name))["open_world"]:
            continue
        properties, required = schema.get("properties", {}), schema.get("required", [])
        if (not isinstance(properties, dict) or len(properties) > 128
                or any(not isinstance(value, dict) for value in properties.values()) or not isinstance(required, list)
                or any(not isinstance(key, str) or key not in properties for key in required)):
            continue
        file_fields = FILE_FIELDS.get(section, {})
        if not has_upload and file_fields.keys() & set(required):
            continue
        if name == "upscale_image" and (not has_upload or "artifact_id" not in properties):
            continue
        try:
            encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8", "replace")
            schema = json.loads(encoded)
        except (ValueError, RecursionError):
            continue
        yield {"name": name, "title": clean_text(tool.get("title"), 160),
               "description": clean_text(tool.get("description"), 2000),
               "files": {key: kind for key, kind in file_fields.items() if has_upload and key in properties},
               "schema": schema, "jobs": {"job_status", "job_result"} <= by_name.keys(),
               "limits": {"max_seconds": found.max_seconds} if (found := speech.tool_limits(tool)) else None}


def model_catalog(payload):
    """Models from the OpenAI-style listing returned by an MCP catalog tool, by valid ID."""
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}
    return {model: item for item in items[:MODEL_CHOICES_MAX] if isinstance(item, dict)
            and isinstance(model := item.get("id"), str) and 0 < len(model) <= MODEL_ID_MAX
            and model.isprintable() and model == model.strip()}


async def image_models(session, tools, listed, uid):
    """The shared MCP platform publishes the gateway's image catalog in `services_list`."""
    candidates = [tool for tool in tools if tool["name"] in {"image_generate", "image_edit"}
                  and (prop := tool["schema"].get("properties", {}).get("model", {})).get("type") == "string"
                  and "enum" not in prop]
    if not candidates or not any(isinstance(tool, dict) and tool.get("name") == "services_list" for tool in listed):
        return
    if not access.tool_allowed(uid, session.sid, "services_list"):
        return
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            await session.initialize()
            result = await call(session, "services_list", {}, timeout=DISCOVERY_TIMEOUT)
    except (HTTPException, TimeoutError):
        return
    data = result.get("structuredContent")
    declarations = data.get("declarations") if isinstance(data, dict) else None
    images_catalog = declarations.get("images") if isinstance(declarations, dict) else None
    services = data.get("services") if isinstance(data, dict) else None
    video = services.get("video") if isinstance(services, dict) else None
    video_models = video.get("model_limits") if isinstance(video, dict) else None
    choices = [name for name in model_catalog(images_catalog) if not isinstance(video_models, dict) or name not in video_models]
    if choices:
        for tool in candidates:
            tool["models"] = choices


def require_chat_policy():
    if cleaner_route.forced():
        raise mcp.refused("Ответы через MCP недоступны при обязательном Cleaner", "cleaner_forced")


def model_mode(item):
    info = item.get("model_info")
    mode = item.get("mode") or (info.get("mode") if isinstance(info, dict) else None)
    return mode.strip().lower() if isinstance(mode, str) and mode.strip() else None


def chat_service_models(data):
    """The shared platform publishes service defaults and, on mixed gateways, a grouped `/limits` roster."""
    chat_names, other_names = set(), set()
    if not isinstance(data, dict):
        return chat_names, other_names, False
    services = data.get("services")
    if isinstance(services, dict):
        for name, service in services.items():
            if isinstance(service, dict) and isinstance(model := service.get("model"), str):
                (chat_names if name == "llm" else other_names).add(model)
    declarations = data.get("declarations")
    limits = declarations.get("llm") if isinstance(declarations, dict) else None
    groups = limits.get("services") if isinstance(limits, dict) and limits.get("object") == "limits" else None
    split_catalog = False
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict) or not isinstance(names := group.get("names"), list):
                continue
            is_chat = group.get("gateway_path") in CHAT_PATHS
            split_catalog |= is_chat
            (chat_names if is_chat else other_names).update(name for name in names if isinstance(name, str))
    return chat_names, other_names, split_catalog


async def chat_models(session, uid):
    if cleaner_route.forced():
        return []
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            found = await mcp_sources.catalog(session, DISCOVERY_TIMEOUT)
            names = {tool["name"] for tool in found.tools
                     if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
            if not {"models_list", "llm_ask"} <= names or not all(
                    access.tool_allowed(uid, session.sid, name) for name in ("models_list", "llm_ask")):
                return []
            tool = next(supported("chat", found.tools, uid=uid, sid=session.sid, item=mcp.row(uid, session.sid)), None)
            if tool is None:
                return []
            await session.initialize()
            result = await call(session, "models_list", {}, timeout=DISCOVERY_TIMEOUT)
    except (HTTPException, TimeoutError):
        return []

    models = {name: model_mode(item) for name, item in model_catalog(result.get("structuredContent")).items()}
    schema = tool["schema"].get("properties", {}).get("model", {})
    choices = schema.get("enum")
    if isinstance(schema.get("const"), str):
        choices = [schema["const"]]
    if isinstance(choices, list):
        return [name for name, mode in models.items() if name in choices and mode in (None, "chat")]

    data = None
    if None in models.values() and "services_list" in names and access.tool_allowed(uid, session.sid, "services_list"):
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                data = (await call(session, "services_list", {}, timeout=DISCOVERY_TIMEOUT)).get("structuredContent")
        except (HTTPException, TimeoutError):
            pass   # explicit chat modes and the tool default are still usable
    chat_names, other_names, split_catalog = chat_service_models(data)
    default = schema.get("default")
    # A grouped limits roster accounts for the non-chat services. Remaining unknown IDs include gateway chat aliases.
    return [name for name, mode in models.items() if mode == "chat" or (mode is None and (
        name in chat_names or (name not in other_names and (split_catalog or name == default))))]


async def check_chat_model(uid, sid, model):
    require_chat_policy()
    item = mcp.row(uid, sid)
    if not item["enabled"] or not model:
        raise mcp.refused("Выберите модель сервера MCP", "invalid", 422)
    session = mcp.Session(item, uid=uid)
    try:
        if model not in await chat_models(session, uid):
            raise mcp.refused("Эта модель MCP недоступна для чата. Выберите другую модель.")
    finally:
        await session.close()
    return item


async def ask_chat(uid, sid, model, messages):
    item = await check_chat_model(uid, sid, model)
    session = mcp.Session(item, uid=uid)
    try:
        await session.initialize()
        require_chat_policy()
        current = mcp.row(uid, sid)
        if not current["enabled"] or not access.tool_allowed(uid, sid, "llm_ask") or current["secret_enc"] != item["secret_enc"]:
            raise mcp.refused("Инструмент MCP недоступен")
        result = await call(session, "llm_ask", {"model": model, "messages": messages, "wait_seconds": 2})
        data = result.get("structuredContent")
        job_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            return result_text(data) if isinstance(data, dict) else first_text(result)
        args = {"job_id": job_id}
        while True:
            status = (await call(session, "job_status", args)).get("structuredContent")
            if isinstance(status, dict) and status.get("status") == "completed":
                reply = (await call(session, "job_result", args)).get("structuredContent")
                value = reply.get("result") if isinstance(reply, dict) else None
                return result_text(value) if isinstance(value, dict) else ""
            if not isinstance(status, dict) or status.get("status") not in ACTIVE:
                error = status.get("error") if isinstance(status, dict) else None
                words = clean_text(error.get("message"), 600) if isinstance(error, dict) else ""
                raise mcp.refused(words or "Не удалось получить ответ модели MCP")
            await asyncio.sleep(2)
    finally:
        await session.close()


@router.get("/{section}")
async def catalog(section: str, request: Request):
    section_tools(section)
    uid = request.state.user.id
    rows = mcp.servers(uid, enabled_only=True)

    async def discover(item):
        session = mcp.Session(item, uid=uid)
        try:
            found = await mcp_sources.catalog(session, DISCOVERY_TIMEOUT)     # общий кэш каталогов MCP
            tools = list(supported(section, found.tools, uid=uid, sid=session.sid, item=item))
            if section == "chat":
                choices = await chat_models(session, uid) if tools else []
                return {"id": session.sid, "name": item["name"], "tools": tools if choices else [], "models": choices}
            if section == "photo":
                await image_models(session, tools, found.tools, uid)
            return {"id": session.sid, "name": item["name"], "tools": tools}
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
    record: bool = False
    photo_job_id: str | None = Field(default=None, pattern=forms.MEDIA_ID_RE.pattern, max_length=32)


def ticket(session: mcp.Session, section: str, kind: str, *payload) -> str:
    return security.encrypt(json.dumps([session.uid, session.sid, security.digest(session.stamp.decode()),
                                       section, kind, clock.NOW(), *payload], ensure_ascii=False)).decode()


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


async def call(session, name, args, *, timeout=mcp.TIMEOUT):
    result = await session.rpc("tools/call", {"name": name, "arguments": args}, max_bytes=RESULT_MAX, timeout=timeout)
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


def artifacts(data):
    """Файлы итога `(id, mime, имя)` с путём скачивания по контракту; прочие записи `artifacts` пропускаются."""
    found = data.get("artifacts") if isinstance(data, dict) else None
    for artifact in found[:128] if isinstance(found, list) else ():
        if not isinstance(artifact, dict):
            continue
        aid, mime = artifact.get("id"), artifact.get("mime_type")
        if (isinstance(aid, str) and ARTIFACT_ID.fullmatch(aid) and isinstance(mime, str)
                and artifact.get("download_path") == f"/artifacts/{aid}/content"):
            yield aid, mime, clean_text(artifact.get("name"), 120)


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
    for aid, mime, name in artifacts(data):
        mime = mime if mime in TICKET_MIME else "application/octet-stream"
        # имя файла — для скачивания (`.srt`, `.docx`, `.md` под своим именем): только последняя часть пути
        plain = name.replace("\\", "/").rsplit("/", 1)[-1][:80]
        files.append({"mime": mime, "name": name,
                      "url": f"/api/mcp-tools/{section}/files/{ticket(session, section, 'file', aid, mime, plain)}"})
    text = result_text(data)
    if not text and not files:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    return {"text": clean_text(text), "files": files}


async def _srt_file(session, data) -> str:
    """Текст первого файла .srt итога (не больше SRT_MAX); нет, недоступен или больше — пусто."""
    for aid, mime, name in artifacts(data):
        if mime != SRT_MIME and not name.lower().endswith(".srt"):
            continue
        headers = httpx.Headers(session.headers(rpc=False))
        headers["Accept-Encoding"] = "identity"
        try:
            async with upstream.stream(SimpleNamespace(id=session.sid), "GET", endpoint(session, f"/artifacts/{aid}/content"),
                                       keyless=True, headers=dict(headers), timeout=mcp.TIMEOUT) as response:
                if response.status_code != 200:
                    return ""
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > SRT_MAX:
                        return ""
        except (httpx.HTTPError, upstream.UpstreamRefused, ValueError):
            return ""
        return bytes(body).decode("utf-8-sig", "replace")
    return ""


def _joined(segments) -> list[dict]:
    """Соседние сегменты одного собеседника — одна реплика (номера меток источника сохраняются)."""
    out = []
    for s in segments:
        if out and s.speaker is not None and out[-1]["speaker"] == s.speaker:
            out[-1].update(end=s.end, text=f"{out[-1]['text']} {s.text}")
        else:
            out.append({"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text})
    return out


async def turns(session, result, text) -> list[dict]:
    """Расшифровка MCP по собеседникам для вида «Расшифровки»: сегменты итога с `speaker` (метки — «Собеседник N» по
    первому появлению), SRT или строки с метками в тексте, файл .srt итога. Не по собеседникам — пусто."""
    data = result.get("structuredContent")
    raw = data.get("segments") if isinstance(data, dict) else None
    if isinstance(raw, list) and any(isinstance(s, dict) and isinstance(s.get("speaker"), str) for s in raw):
        return _joined(speech.speakers(speech._segments(data)))
    found = speech.segments_from_srt(text) or speech.segments_from_labeled(text)
    if not found and isinstance(data, dict):
        found = speech.segments_from_srt(await _srt_file(session, data))
    return _joined(found)


async def shown(session, section, result):
    """Итог инструмента для страницы (`present`); у «Расшифровки» — ещё реплики по собеседникам (`turns`)."""
    out = present(session, section, result)
    if section == "transcribe" and (found := await turns(session, result, out["text"])):
        out["turns"] = found
    return out


async def upload_artifact(session, data, mime, name=None):
    """Файл на сервер MCP (`artifact_upload`) → `{id, path}`; `path` — путь файла на сервере, если сервер его
    сообщил (иначе пусто), `name` — имя файла для сервера (файлы чата)."""
    args = {"data_base64": base64.b64encode(data).decode(), "mime_type": mime}
    if name:
        args["name"] = name
    # Keep the normal response budget and allow for sending the encoded body at 512 KiB/s.
    timeout = mcp.TIMEOUT + math.ceil(len(args["data_base64"]) / (512 << 10))
    reply = await call(session, "artifact_upload", args, timeout=timeout)
    item = reply.get("structuredContent")
    aid = item.get("id") if isinstance(item, dict) else None
    if not isinstance(aid, str) or not ARTIFACT_ID.fullmatch(aid):
        raise mcp.refused("MCP не вернул идентификатор файла")
    path = item.get("path")
    path = path if isinstance(path, str) and len(path) <= 1024 and path.isprintable() else ""
    return {"id": aid, "path": path}


def accepts_name(tools) -> bool:
    """Схема `artifact_upload` сервера принимает `name` — как у файлов чата (`mcp_files.sync`): имя файла уходит только
    такому серверу. По нему сервер называет итоги («Совещание — протокол.docx»), а не по номеру загрузки."""
    upload_tool = next((t for t in tools if isinstance(t, dict) and t.get("name") == "artifact_upload"), None)
    schema = upload_tool.get("inputSchema") if upload_tool else None
    return isinstance(schema, dict) and isinstance(schema.get("properties"), dict) and "name" in schema["properties"]


async def upload(session, kind, file, *, named=False, original_size=False):
    name = file.name.strip() if named and isinstance(file.name, str) and file.name.strip() else None
    if file.media_id is not None:
        if kind != "image" or file.data is not None:
            raise mcp.refused("Выберите одно исходное фото", "invalid", 422)
        try:
            if original_size:
                raw = await forms.history_file(session.uid, file.media_id)
                data = await asyncio.to_thread(forms.normalize_image, raw, forms.HISTORY_PIXELS, fit=False)
            else:
                data = await forms.photo(session.uid, None, file.media_id)
        except forms.UploadError as exc:
            raise mcp.refused(str(exc), "invalid", exc.status) from None
        mime, name = "image/png", None
    else:
        try:
            data = base64.b64decode(file.data or "", validate=True)
        except (ValueError, UnicodeError):
            raise mcp.refused("Неверные данные файла MCP", "invalid", 422) from None
        if not data or len(data) > UPLOAD_MAX:
            raise mcp.refused(f"Файл MCP должен быть непустым и не больше {UPLOAD_MAX >> 20} МиБ", "too_big", 413)
        if kind == "image":
            try:
                pixels = forms.HISTORY_PIXELS if original_size else forms.MAX_PIXELS
                data = await asyncio.to_thread(forms.normalize_image, data, pixels, fit=not original_size)
            except forms.UploadError as exc:
                raise mcp.refused(str(exc), "invalid", exc.status) from None
            mime = "image/png"
        else:
            mime = forms.audio_type(file.mime_type, file.name)
            if mime not in INLINE_MIME:
                raise mcp.refused("Выберите звуковой файл MCP", "invalid", 415)
    if original_size and len(data) > forms.MAX_UPLOAD:
        raise mcp.refused("После подготовки фото больше 10 МиБ", "too_big", 413)
    return (await upload_artifact(session, data, mime, name))["id"]


async def voice_arguments(session, section, tool, voice_id, files, args):
    props = tool["schema"].get("properties", {})
    engine = props.get("engine", {}).get("enum")
    if (section != "speech" or files or "reference_artifact_id" not in tool["files"] or "reference_text" not in props
            or not isinstance(engine, list) or "clone" not in engine):
        raise mcp.refused("Инструмент MCP не принимает образец голоса", "invalid", 422)
    audio, text = await voices.reference(session.uid, voice_id)
    if not audio or len(audio) > UPLOAD_MAX:
        raise mcp.refused("Образец голоса превышает предел MCP", "too_big", 413)
    values = {"engine": "clone", "reference_text": text}
    check_bounds(tool, {**args, **values})
    aid = (await upload_artifact(session, audio, "audio/wav"))["id"]
    return {**values, "reference_artifact_id": aid}


LIMIT_MAX = 10**6
ENUM_MAX = 64
ENUM_TEXT = 80


def _number(value):
    """Число предела: конечное и не больше LIMIT_MAX по модулю (bool — не число)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (isinstance(value, int) or math.isfinite(value)) and abs(value) <= LIMIT_MAX


def field_limits(prop) -> dict | None:
    """Пределы поля схемы по тем же правилам, что `limits(key)` страницы (mcp-sections.js): {min, max, enum} или None —
    поле с бессмысленными пределами (не числа, min > max, больше LIMIT_MAX, enum длиннее ENUM_MAX или со значением
    длиннее ENUM_TEXT) не проверяет ни страница, ни сервер: решает сам инструмент."""
    if not isinstance(prop, dict):
        return None
    low, high = prop.get("minimum"), prop.get("maximum")
    if (low is not None and not _number(low)) or (high is not None and not _number(high)) or (
            low is not None and high is not None and low > high):
        return None
    choices = prop.get("enum")
    if choices is not None and (not isinstance(choices, list) or not 0 < len(choices) <= ENUM_MAX or not all(
            (isinstance(item, str) and len(item) <= ENUM_TEXT) or _number(item) for item in choices)):
        return None
    return {"min": low, "max": high, "enum": choices}


def check_bounds(tool: dict, args: dict) -> None:
    """Refuse values outside the limits the tool schema declares (`minimum`/`maximum`, `enum`) — the same limits the
    page form enforces (`field_limits`); a field with nonsense limits is left to the tool. Also the field types the page
    renders as extra fields (mcp-sections.js `extraFields`): `boolean` — only true/false, `integer` — a whole number,
    `string` with `maxLength` — a string that fits."""
    properties = tool["schema"].get("properties", {})
    for key, value in args.items():
        prop = properties.get(key)
        kind = prop.get("type") if isinstance(prop, dict) else None
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        name = clean_text(key, 80)
        if kind == "boolean" and not isinstance(value, bool):
            raise mcp.refused(f"Значение «{name}» — да или нет", "invalid", 422)
        if kind == "integer" and isinstance(value, float) and not value.is_integer():
            raise mcp.refused(f"Значение «{name}» — целое число", "invalid", 422)
        longest = prop.get("maxLength") if kind == "string" else None
        if (isinstance(longest, int) and not isinstance(longest, bool) and 0 < longest <= LIMIT_MAX
                and (not isinstance(value, str) or len(value) > longest)):
            raise mcp.refused(f"Значение «{name}» — строка до {longest} знаков", "invalid", 422)
        limits = field_limits(prop)
        if limits is None:
            continue
        low, high = limits["min"], limits["max"]
        if (low is not None or high is not None) and (
                not number or (low is not None and value < low) or (high is not None and value > high)):
            lo, hi = (int(bound) if isinstance(bound, float) and bound.is_integer() else bound for bound in (low, high))
            words = (f"Значение «{name}» — от {lo} до {hi}" if lo is not None and hi is not None
                     else f"Значение «{name}» — не меньше {lo}" if hi is None else f"Значение «{name}» — не больше {hi}")
            raise mcp.refused(words, "invalid", 422)
        choices = limits["enum"]
        if choices is not None and (isinstance(value, bool) or value not in choices):
            raise mcp.refused(f"Значение «{clean_text(key, 80)}» — не из списка инструмента", "invalid", 422)


@router.post("/{section}/servers/{sid}/run")
async def run(section: str, sid: int, request: Request):
    if section == "chat":
        raise mcp.refused("Для модели MCP откройте диалог", "not_found", 404)
    names = section_tools(section)
    uid = request.state.user.id
    item = mcp.row(uid, sid)
    session = mcp.Session(item, uid=uid)
    try:
        body = RunIn.model_validate_json(await forms.read_body(request, UPLOAD_MAX * 4 // 3 + PARAMS_MAX))
        arguments = json.dumps(body.arguments, allow_nan=False)
    except (ValidationError, ValueError, RecursionError):
        raise mcp.refused("Неверные параметры инструмента MCP", "invalid", 422) from None
    if body.tool not in names or len(arguments) > PARAMS_MAX or not access.tool_allowed(uid, sid, body.tool):
        raise mcp.refused("Инструмент недоступен в этом разделе MCP", "invalid", 422)
    if body.photo_job_id:
        prior = history_row(uid, "photo", body.photo_job_id)
        if section != "photo" or (prior["server_id"], prior["tool"], prior["stamp"]) != (
                sid, body.tool, mcp_sources.stamp(session.stamp)):
            raise mcp.refused("Подключение или права MCP изменились")
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            listed = await session.tools()
        tools = list(supported(section, listed, uid=uid, sid=sid, item=item))
        named = accepts_name(listed)
        tool = next((item for item in tools if item["name"] == body.tool), None)
        if tool is None:
            raise mcp.refused("Инструмент недоступен в этом разделе MCP")
        args = {key: value for key, value in body.arguments.items() if key not in body.files}
        if not body.files.keys() <= tool["files"].keys():
            raise mcp.refused("Инструмент не принимает этот файл MCP", "invalid", 422)
        if body.tool in {"image_generate", "image_edit"} and isinstance(args.get("model"), str):
            model_schema = tool["schema"].get("properties", {}).get("model", {})
            if "enum" not in model_schema and any(isinstance(t, dict) and t.get("name") == "services_list" for t in listed):
                args["model"] = images.MODELS.get(args["model"], args["model"])
        if body.voice_id:
            for key in ("voice", "reference_artifact_id"):
                args.pop(key, None)
            args.update(await voice_arguments(session, section, tool, body.voice_id, body.files, args))
        else:
            check_bounds(tool, args)
            for key, file in body.files.items():
                args[key] = await upload(session, tool["files"][key], file, named=named,
                                         original_size=body.tool == "upscale_image")
        check_bounds(tool, args)
        if not access.tool_allowed(uid, sid, body.tool):   # правило могли сменить, пока шла загрузка файла
            raise mcp.refused("Инструмент недоступен в этом разделе MCP", "invalid", 422)
        if body.record:
            operations.check(uid)
        result = await call(session, body.tool, args)
        data = result.get("structuredContent")
        jid = data.get("id") if isinstance(data, dict) else None
        if body.record:
            remote_id = jid if tool["jobs"] and isinstance(jid, str) and JOB_ID.fullmatch(jid) else None
            return await record_result(session, section, item["name"], body.tool, args, result, remote_id, body.files)
        if tool["jobs"] and isinstance(jid, str) and JOB_ID.fullmatch(jid):
            return {"job": ticket(session, section, "job", jid), "status": "queued"}
        return await shown(session, section, result)
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
                return await shown(session, section, {"structuredContent": reply["result"]})
            raise mcp.refused("MCP не вернул результат задания")
        error = job.get("error")
        return {"status": clean_text(job["status"], 80), "active": job["status"] in ACTIVE,
                "error": clean_text(error.get("message"), 600) if isinstance(error, dict) else ""}
    finally:
        await session.close()


HISTORY_PAGE = 30
HISTORY_MAX_WAIT = 3 * 3600
HISTORY_BATCH = 12
HISTORY_ACTIVE: set[str] = set()


def history_view(row, *, details=True):
    output = {key: row[key] for key in ("id", "server", "tool", "model", "prompt", "created", "status", "error")} | {
        "active": row["remote_id"] is not None, "server_id": row["server_id"],
        "result": json.loads(row["result"]) if details and row["result"] else None,
    }
    if not details:
        output["has_photo"] = bool(row["has_photo"])
    return output


async def record_result(session, section, server, tool, args, result, remote_id, input_files):
    operations.check(session.uid)
    rid = uuid.uuid4().hex
    stamp = mcp_sources.stamp(session.stamp)
    prompt = clean_text(args.get("prompt") or args.get("text"), 24_000)
    model = clean_text(args.get("model"), MODEL_ID_MAX)
    metadata = {}
    if section == "photo":
        source = input_files.get("artifact_id")
        if source is not None and source.media_id is not None:
            metadata["source_media_id"] = source.media_id
        size = args.get("size")
        if isinstance(size, str) and re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", size):
            metadata["size"] = size
        seed, strength = args.get("seed"), args.get("strength")
        if type(seed) is int and 0 <= seed <= 2**53 - 1 and args.get("n", 1) == 1:
            metadata["seed"] = seed
        if type(strength) in (int, float) and 0 <= strength <= 1:
            metadata["strength"] = strength
    state.DB.execute("INSERT INTO mcp_section_jobs(id, user_id, section, server_id, server, tool, model, prompt, "
                     "created, stamp, remote_id, status, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (rid, session.uid, section, session.sid, server, tool, model, prompt, clock.NOW(), stamp,
                      remote_id, "queued" if remote_id else "saving", json.dumps({"photo": metadata}) if metadata else None))
    if remote_id is None:
        try:
            await save_history(session, section, rid, result)
        except BaseException:
            end_history(rid, "failed", "Не удалось сохранить результат MCP")
            raise
    return {"history": history_view(state.DB.execute("SELECT * FROM mcp_section_jobs WHERE id = ?", (rid,)).fetchone())}


def end_history(rid, status, error=""):
    state.DB.execute("UPDATE mcp_section_jobs SET status = ?, error = ?, remote_id = NULL WHERE id = ?",
                     (status, error, rid))


async def save_history(session, section, rid, result):
    output = await shown(session, section, result)
    data = result.get("structuredContent")
    sources = iter(artifacts(data) if isinstance(data, dict) else ())
    remaining = DOWNLOAD_MAX
    saved, media_ids = [], []
    missing = False
    for file in output["files"]:
        mime = file["mime"]
        name = clean_text(file.get("name"), 120).replace("\\", "/").rsplit("/", 1)[-1]
        name = name or "result" + media.EXT.get(mime, ".bin")
        try:
            if "url" in file:
                aid, _, _ = next(sources)
                reply = await relay(session, aid, mime, SimpleNamespace(headers={}), name=name)
                if not isinstance(reply, ArtifactStream):
                    raise ValueError("artifact response")
                try:
                    if reply.status_code != 200:
                        raise ValueError("incomplete artifact")
                    mid = await media.save_stream(session.uid, reply.body_iterator, mime, name, remaining)
                finally:
                    await reply.stack.aclose()
            else:
                raw = base64.b64decode(file["base64"], validate=True)
                if not raw or len(raw) > remaining:
                    raise ValueError("result size")
                mid = await media.save_async(session.uid, raw, mime, name, thumb=mime.startswith("image/"))
        except (HTTPException, media.StorageError, ConnectionError, ValueError, binascii.Error):
            missing = True
            continue
        remaining -= state.DB.execute("SELECT size FROM media WHERE id = ?", (mid,)).fetchone()[0]
        media_ids.append(mid)
        saved.append({"mime": mime, "name": name, "url": f"/api/media/{mid}"})
    output["files"] = saved
    operations.check(session.uid)
    if section == "photo":
        stored = state.DB.execute("SELECT result FROM mcp_section_jobs WHERE id = ?", (rid,)).fetchone()[0]
        output["photo"] = images.mcp_photo_metadata(output | (json.loads(stored) if stored else {}))
        if isinstance(data, dict) and len(saved) == 1:
            seed = data.get("seed")
            if type(seed) is int and 0 <= seed <= 2**53 - 1:
                output["photo"]["seed"] = seed
    state.DB.execute("BEGIN")
    try:
        state.DB.executemany("INSERT INTO mcp_section_files(job_id, user_id, media_id) VALUES (?, ?, ?)",
                             ((rid, session.uid, mid) for mid in media_ids))
        state.DB.execute("UPDATE mcp_section_jobs SET result = ? WHERE id = ?",
                         (json.dumps(output, ensure_ascii=False), rid))
        end_history(rid, "completed" if saved or output["text"] or not missing else "failed",
                    "Часть файлов MCP не удалось сохранить" if missing and saved else
                    "Файлы MCP не удалось сохранить" if missing else "")
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise


async def refresh_history(row):
    rid, uid, sid = row["id"], row["user_id"], row["server_id"]
    if rid in HISTORY_ACTIVE:
        return
    HISTORY_ACTIVE.add(rid)
    try:
        if clock.NOW() - row["created"] > HISTORY_MAX_WAIT:
            end_history(rid, "failed", "Истекло время ожидания задания MCP")
            return
        try:
            item = mcp.row(uid, sid)
            if (not item["enabled"] or row["stamp"] != mcp_sources.stamp(item["secret_enc"])
                    or not access.tool_allowed(uid, sid, row["tool"])):
                raise mcp.refused("Подключение или права MCP изменились")
        except HTTPException:
            end_history(rid, "failed", "Подключение или права MCP изменились")
            return
        session = mcp.Session(item, uid=uid)
        try:
            await session.initialize()
            args = {"job_id": row["remote_id"]}
            job = (await call(session, "job_status", args, timeout=DISCOVERY_TIMEOUT)).get("structuredContent")
            if not isinstance(job, dict) or not isinstance(job.get("status"), str):
                return
            status = job["status"]
            if status in ACTIVE:
                state.DB.execute("UPDATE mcp_section_jobs SET status = ? WHERE id = ?", (status, rid))
            elif status == "completed":
                response = (await call(session, "job_result", args, timeout=DISCOVERY_TIMEOUT)).get("structuredContent")
                if isinstance(response, dict) and isinstance(response.get("result"), dict):
                    with operations.bind(uid):
                        operations.check(uid)
                        if state.DB.execute("SELECT 1 FROM mcp_section_jobs WHERE id = ?", (rid,)).fetchone():
                            await save_history(session, row["section"], rid, {"structuredContent": response["result"]})
                else:
                    end_history(rid, "failed", "MCP не вернул результат задания")
            else:
                error = job.get("error")
                words = clean_text(error.get("message"), 600) if isinstance(error, dict) else ""
                end_history(rid, clean_text(status, 80), words or "Задание MCP не завершилось")
        except (HTTPException, TimeoutError):
            pass                            # Retry transient server failures on the next pass within the wait limit.
        finally:
            await session.close()
    finally:
        HISTORY_ACTIVE.discard(rid)


async def watch_history():
    # No remote id means an inline result was interrupted before Studio finished saving it.
    state.DB.execute("UPDATE mcp_section_jobs SET status = 'failed', error = ? WHERE status = 'saving'",
                     ("Сохранение результата прервано перезапуском Studio",))
    cursor = (0.0, "")
    while True:
        rows = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE remote_id IS NOT NULL AND (created, id) > (?, ?)"
                                " ORDER BY created, id LIMIT ?", (*cursor, HISTORY_BATCH)).fetchall()
        if rows:
            await asyncio.gather(*(refresh_history(row) for row in rows), return_exceptions=True)
            cursor = (rows[-1]["created"], rows[-1]["id"])
        if len(rows) < HISTORY_BATCH:
            cursor = (0.0, "")
            await asyncio.sleep(4)


@router.get("/{section}/history")
async def history(section: str, request: Request, before: str = ""):
    section_tools(section)
    uid = request.state.user.id
    if before and not forms.MEDIA_ID_RE.fullmatch(before):
        raise mcp.refused("Неверная страница истории MCP", "invalid", 422)
    cursor = state.DB.execute("SELECT created, rowid FROM mcp_section_jobs WHERE user_id = ? AND section = ? AND id = ?",
                              (uid, section, before)).fetchone() if before else None
    if before and cursor is None:
        raise mcp.refused("Запись истории MCP недоступна", "not_found", 404)
    extra = " AND (created, rowid) < (?, ?)" if cursor else ""
    rows = state.DB.execute("SELECT j.*, EXISTS (SELECT 1 FROM mcp_section_files f JOIN media m ON m.id = f.media_id"
                            " WHERE f.job_id = j.id AND m.mime LIKE 'image/%') AS has_photo"
                            " FROM mcp_section_jobs j WHERE user_id = ? AND section = ?" + extra
                            + " ORDER BY created DESC, rowid DESC LIMIT ?",
                            (uid, section, *(tuple(cursor) if cursor else ()), HISTORY_PAGE + 1)).fetchall()
    return {"items": [history_view(row, details=False) for row in rows[:HISTORY_PAGE]],
            "next": rows[HISTORY_PAGE - 1]["id"] if len(rows) > HISTORY_PAGE else None}


def history_row(uid, section, rid):
    section_tools(section)
    row = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE user_id = ? AND section = ? AND id = ?",
                           (uid, section, rid)).fetchone()
    if row is None:
        raise mcp.refused("Запись истории MCP недоступна", "not_found", 404)
    return row


@router.get("/{section}/history/{rid}")
async def history_item(section: str, rid: str, request: Request):
    row = history_row(request.state.user.id, section, rid)
    return history_view(row)


@router.delete("/{section}/history/{rid}", status_code=204)
async def delete_history(section: str, rid: str, request: Request):
    uid = request.state.user.id
    row = history_row(uid, section, rid)
    if row["remote_id"] or row["status"] == "saving":
        raise mcp.refused("Дождитесь завершения задания MCP")
    mids = state.DB.execute("SELECT media_id FROM mcp_section_files WHERE job_id = ?", (rid,)).fetchall()
    state.DB.execute("DELETE FROM mcp_section_jobs WHERE id = ?", (rid,))
    for (mid,) in mids:
        if not media.referenced(uid, mid):
            await media.delete_async(uid, mid)
    return Response(status_code=204)


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
    session, (aid, mime, *name) = redeem(request, section, encoded, "file")    # билеты до 0.41.0 — без имени
    return await relay(session, aid, mime, request, name=name[0] if name and isinstance(name[0], str) else "")


GONE_FILE = "Файла больше нет на сервере MCP"


def endpoint(session: mcp.Session, path: str) -> httpx.URL:
    """Адрес маршрута контракта на хосте сервера MCP: тот же хост и порт, путь — из кода Studio (`/artifacts/…`,
    `/status`, `/user-files/…`), никогда не из ответа сервера или инструмента."""
    if not path.startswith("/") or not path.isascii() or "?" in path or "#" in path:
        raise ValueError("contract path")
    return httpx.URL(session.url).copy_with(raw_path=path.encode())


async def relay(session, aid, mime, request, *, name=""):
    """Файл результата MCP `aid` потоком через Studio: диапазоны (`Range`), `nosniff`, предел DOWNLOAD_MAX;
    не картинка, звук или видео — вложением (под именем `name`, если оно есть). Сервер ответил 404 — 404 GONE_FILE."""
    # The transfer path is part of the service contract. Never fetch a URL from a tool result.
    url = endpoint(session, f"/artifacts/{aid}/content")
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
        if response.status_code == 404:
            raise mcp.refused(GONE_FILE, "not_found", 404)
        if (response.status_code not in (200, 206) or response.headers.get("content-encoding", "identity") != "identity"
                or (length and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > DOWNLOAD_MAX))):
            raise mcp.refused("Файл MCP недоступен или превышает предел загрузки")
        outgoing["X-Content-Type-Options"] = "nosniff"
        if mime not in INLINE_MIME:
            outgoing["Content-Disposition"] = (media.disposition(name) if name
                                               else f'attachment; filename="result{media.EXT.get(mime, ".bin")}"')

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
