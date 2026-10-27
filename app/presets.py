"""Пресеты раздела «Фото» (спека 2026-10-21-studio-photo-presets-compare-series, раздел 5): модель,
формат, сила, seed и промпт под именем — в учётке (таблица `presets`), одни на всех её устройствах.

Пресеты — настройки, а не история: «Удалить всю историю» их не трогает, удаление учётки уносит
каскадом. То же имя — замена прежнего пресета (тот же id). Только свои: чужой id — 404. Роутер
подключается раньше `images.router`: иначе `/api/images/presets` взял бы `GET /api/images/{id}`.
Журнал — только id: имена и промпты туда не пишутся.
"""
import json
import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

import clock
import forms
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/images/presets")

SECTION = "photo"
PRESETS_MAX = 50
# промпт — до 24000 знаков, а знак — до 4 байт UTF-8 и до 12 в JSON с `\uXXXX` (суррогатная пара)
BODY_MAX = 512 * 1024
INPUT = {"code": "input", "message": "имя пресета — от 1 до 60 знаков; проверьте модель, формат, seed, силу и промпт"}


class ParamsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: "str | None" = Field(default=None, max_length=200)
    connection_id: "int | None" = None
    size: "Literal['portrait', 'square', 'landscape'] | None" = None
    seed: "int | None" = Field(default=None, ge=0, le=2**32 - 1)
    strength: "float | None" = Field(default=None, ge=0.05, le=1)
    prompt: "str | None" = Field(default=None, max_length=24000)


class PresetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    params: ParamsIn


def _out(row) -> dict:
    return {"id": row["id"], "name": row["name"], "params": json.loads(row["params"]), "created": row["created"]}


@router.get("")
async def list_presets(request: Request):
    rows = state.DB.execute("SELECT * FROM presets WHERE user_id = ? AND section = ? ORDER BY created DESC, id DESC",
                            (request.state.user.id, SECTION))
    return {"items": [_out(r) for r in rows]}


@router.post("", status_code=201)
async def save_preset(request: Request):
    uid = request.state.user.id
    try:
        body = PresetIn.model_validate_json(await forms.read_body(request, BODY_MAX))
    except ValidationError:
        raise HTTPException(422, INPUT) from None
    params, now = json.dumps(body.params.model_dump(), ensure_ascii=False), clock.NOW()
    # без await от проверки до записи: предел и замена по имени не гоняются с другой вкладкой
    row = state.DB.execute("SELECT id FROM presets WHERE user_id = ? AND section = ? AND name = ?",
                           (uid, SECTION, body.name)).fetchone()
    if row is not None:
        preset_id = row["id"]
        state.DB.execute("UPDATE presets SET params = ?, created = ? WHERE id = ?", (params, now, preset_id))
    else:
        count = state.DB.execute("SELECT COUNT(*) FROM presets WHERE user_id = ? AND section = ?",
                                 (uid, SECTION)).fetchone()[0]
        if count >= PRESETS_MAX:
            raise HTTPException(409, {"code": "presets",
                                      "message": f"не больше {PRESETS_MAX} пресетов — удалите ненужные"})
        preset_id = uuid.uuid4().hex
        state.DB.execute("INSERT INTO presets(id, user_id, section, name, params, created) VALUES (?, ?, ?, ?, ?, ?)",
                         (preset_id, uid, SECTION, body.name, params, now))
    log.info("пресет %s сохранён", preset_id[:8])
    return _out(state.DB.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)).fetchone())


@router.delete("/{preset_id}", status_code=204)
async def delete_preset(preset_id: str, request: Request):
    count = state.DB.execute("DELETE FROM presets WHERE id = ? AND user_id = ?",
                             (preset_id, request.state.user.id)).rowcount
    if not count:
        raise HTTPException(404, {"code": "gone", "message": "такого пресета нет"})
    return Response(status_code=204)
