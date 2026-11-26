"""Настройки HTTPS. Закрытый ключ передаётся только в локальную службу Apache."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

import account_profile
import auth
import state


HOST_DIR = Path("/var/lib/studio-https")
LIMIT = 256 * 1024
MESSAGES = {
    "domain": "Введите доменное имя без схемы, порта и пути.",
    "certificate": "Проверьте формат файлов сертификата и цепочки.",
    "key": "Выберите закрытый ключ без пароля, соответствующий сертификату.",
    "hostname": "Сертификат не покрывает указанное доменное имя.",
    "expired": "Проверьте срок действия сертификата и его цепочки.",
    "chain": "Цепочка сертификатов не соответствует сертификату сайта.",
    "apply": "Не удалось применить сертификат. Проверьте конфигурацию HTTPS на сервере.",
    "busy": "HTTPS уже настраивается. Повторите через несколько секунд.",
    "unavailable": "Служба настройки HTTPS недоступна. Проверьте текущий адрес через несколько секунд.",
}


class Upload(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    certificate: str = Field(min_length=1)
    key: str = Field(min_length=1)
    chain: str = ""
    password: str = Field(max_length=account_profile.PASSWORD_MAX)


def status():
    if state.CFG.managed_https:
        try:
            value = json.loads((HOST_DIR / "status.json").read_bytes())
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
    return {}


def origin() -> str:
    return status().get("origin", "")


def settings():
    if not state.CFG.managed_https:
        raise HTTPException(404)
    return {"port": urlsplit(state.CFG.origin).port or 443, **status()}


async def _apply(payload):
    writer = None
    try:
        async with asyncio.timeout(40):
            reader, writer = await asyncio.open_unix_connection(HOST_DIR / "apply.sock", limit=4096)
            writer.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            return json.loads(await reader.readline())
    except (OSError, ValueError, TimeoutError, NotImplementedError):
        return {"error": "unavailable"}
    finally:
        if writer is not None:
            writer.close()


async def apply(request: Request):
    if not state.CFG.managed_https:
        raise HTTPException(404)
    if auth.foreign_origin(request):
        raise HTTPException(403)
    raw = await auth.small_body(request, LIMIT)
    if raw is None:
        raise HTTPException(413)
    admin = auth.require_admin(request)
    try:
        body = Upload.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, {"code": "https", "message": MESSAGES["certificate"]}) from None
    await auth.confirm_password(admin.id, body.password, "настройка HTTPS", digest=request.state.digest)
    auth.recheck(request)
    auth.require_admin(request)
    reply = await _apply(body.model_dump(exclude={"password"}))
    if reply.get("ok") is not True:
        error = reply.get("error", "unavailable")
        code = {"busy": 409, "unavailable": 503, "apply": 503}.get(error, 422)
        raise HTTPException(code, {"code": "https", "message": MESSAGES.get(error, MESSAGES["unavailable"])})
    return settings()
