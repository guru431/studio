"""Рабочий стол: только ретрансляция своего открытого компьютера, без хранения данных.

Разрешение меняется исключительно локальным окном агента. Сервер не имеет операции
изменения выключателя и не сохраняет изображения, ввод, буфер или токен управления.
"""
import base64
import re
from typing import Annotated, Literal, Union

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator

from claude_code import _BridgeJSON, _refuse, _session_open, ask, own_agent, require_capability

router = APIRouter(prefix="/api/code/agents/{aid}/desktop", default_response_class=_BridgeJSON)
IMAGE_MAX = 700 * 1024  # символов base64, как в native-модуле; всегда один кадр на запрос
CLIPBOARD_MAX = 65536
CONTROL = re.compile(r"[0-9a-f]{32}")


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Control(Empty):
    control: str = Field(pattern=r"^[0-9a-f]{32}$")


class Frame(Control):
    monitor: StrictInt = Field(ge=0, lt=32)
    max_edge: StrictInt = Field(default=1600, ge=640, le=2560)


class Move(Empty):
    type: Literal["move"]
    monitor: StrictInt = Field(ge=0, lt=32)
    x: Union[StrictInt, StrictFloat] = Field(ge=0, le=1, allow_inf_nan=False)
    y: Union[StrictInt, StrictFloat] = Field(ge=0, le=1, allow_inf_nan=False)


class Button(Empty):
    type: Literal["button"]
    button: Literal["left", "right", "middle"]
    down: StrictBool


class Wheel(Empty):
    type: Literal["wheel"]
    dx: StrictInt = Field(ge=-1200, le=1200)
    dy: StrictInt = Field(ge=-1200, le=1200)


class Key(Empty):
    type: Literal["key"]
    code: str = Field(pattern=r"^[A-Za-z0-9]{2,24}$")
    down: StrictBool


class Text(Empty):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00]*$")


Event = Annotated[Union[Move, Button, Wheel, Key, Text], Field(discriminator="type")]


class Input(Control):
    events: list[Event] = Field(min_length=1, max_length=32)


class Clipboard(Control):
    text: str | None = Field(default=None, max_length=CLIPBOARD_MAX)

    @field_validator("text")
    @classmethod
    def without_nul(cls, value):
        if value is not None and "\x00" in value:
            raise ValueError("text")
        return value


async def call(request, aid, op, **values):
    own_agent(request, aid)
    require_capability(aid, "desktop")
    data = await ask(request, aid, "desktop_" + op, **values)
    # Сессия могла запереться или закончиться, пока Windows снимала кадр.
    if not _session_open(request.state.digest):
        raise _refuse(409, "closed", "Сеанс Студии закрыт")
    return data


def bad():
    raise _refuse(502, "bridge", "Агент прислал неверные данные рабочего стола")


def clean_monitors(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        bad()
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            bad()
        ident, w, h = item.get("id"), item.get("width"), item.get("height")
        if (type(ident) is not int or not 0 <= ident < 32 or ident in seen
                or any(type(n) is not int or not 1 <= n <= 32768 for n in (w, h))
                or type(item.get("primary")) is not bool):
            bad()
        seen.add(ident)
        result.append({"id": ident, "width": w, "height": h, "primary": item["primary"]})
    return result


@router.get("")
async def status(aid: int, request: Request):
    data = await call(request, aid, "status")
    if any(type(data.get(key)) is not bool for key in ("enabled", "active")):
        bad()
    resolutions = data.get("resolutions", [1600])
    if (not isinstance(resolutions, list) or not 1 <= len(resolutions) <= 16
            or any(type(value) is not int or not 640 <= value <= 2560 for value in resolutions)
            or len(set(resolutions)) != len(resolutions)):
        bad()
    return {"enabled": data["enabled"], "active": data["active"], "resolutions": resolutions}


@router.post("/start")
async def start(aid: int, body: Empty, request: Request):
    data = await call(request, aid, "start")
    if not isinstance(data.get("control"), str) or not CONTROL.fullmatch(data["control"]):
        bad()
    monitors = clean_monitors(data.get("monitors"))
    if type(data.get("monitor")) is not int or data["monitor"] not in {item["id"] for item in monitors}:
        bad()
    return {"control": data["control"], "monitors": monitors, "monitor": data["monitor"]}


@router.post("/frame")
async def frame(aid: int, body: Frame, request: Request):
    # Старые мосты принимают только control/monitor и сохраняют прежний предел 1600 px.
    data = await call(request, aid, "frame", **body.model_dump(exclude_defaults=True))
    image = data.get("image")
    if not isinstance(image, str) or not 4 <= len(image) <= IMAGE_MAX:
        bad()
    try:
        raw = base64.b64decode(image, validate=True)
    except ValueError:
        bad()
    if not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
        bad()
    dimensions = {key: data.get(key) for key in ("width", "height", "screen_width", "screen_height")}
    if any(type(value) is not int or not 1 <= value <= (body.max_edge if key in ("width", "height") else 32768)
           for key, value in dimensions.items()) or data.get("monitor") != body.monitor:
        bad()
    return {"image": image, **dimensions, "monitor": body.monitor}


@router.post("/input", status_code=204)
async def input_events(aid: int, body: Input, request: Request):
    await call(request, aid, "input", **body.model_dump())
    return Response(status_code=204)


@router.post("/clipboard")
async def clipboard(aid: int, body: Clipboard, request: Request):
    data = await call(request, aid, "clipboard", **body.model_dump())
    if not isinstance(data.get("text"), str) or len(data["text"]) > CLIPBOARD_MAX or "\x00" in data["text"]:
        bad()
    return {"text": data["text"]}


@router.post("/stop", status_code=204)
async def stop(aid: int, body: Control, request: Request):
    await call(request, aid, "stop", **body.model_dump())
    return Response(status_code=204)
