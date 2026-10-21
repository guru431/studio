"""Голосовой ввод (спека 2026-10-20-studio-sections-voice-design, 6.5): POST /api/dictation —
запись из браузера (iPhone — m4a, Chrome — webm) → текст модели раздела «Голосовой ввод» (по
умолчанию GigaAM шлюза). Текст вставляет в поле сама страница (mic.js), без отправки.

Звук — только в памяти запроса: ни на диск, ни в историю, ни в журнал; в журнал — событие с id
пользователя и код итога. Модель занята — 503 сразу, без ожидания: ввод — секунды, человек нажмёт
ещё раз. Ответ синхронный, поэтому распознавание целиком ограничено BUDGET_SEC (а не таймаутами
вызовов шлюза: у WhisperX там 2460 с) — дольше: 504 словами «повторите»; таймаут маршрута Apache
больше бюджета (сверяет tests/test_deploy.py), иначе он оборвал бы запрос раньше и текст пропал."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

import forms
import sections
import speech
from image_upload import UploadError

MAX_AUDIO = 8 * 1024 * 1024                      # две минуты записи браузера — с большим запасом
FORM_SLACK = 64 * 1024
BUDGET_SEC = 120                                 # запись до 2 минут (mic.js MAX_SEC): холодная модель и распознавание

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/dictation")


@router.post("")
async def dictate(request: Request):
    uid = request.state.user.id
    conn, model = sections.resolve(uid, "dictation", None, None)
    try:
        raw = await forms.read_body(request, MAX_AUDIO + FORM_SLACK)
        _fields, data, mime, name = await asyncio.to_thread(
            forms.audio_form, raw, request.headers.get("content-type", ""), max_bytes=MAX_AUDIO)
    except UploadError as e:
        raise HTTPException(e.status, {"code": "format" if e.status == 415 else "audio", "message": str(e)}) from None
    del raw
    try:
        text = await asyncio.wait_for(speech.dictate(conn, model, data, mime, name), BUDGET_SEC)
    except TimeoutError:
        log.info("пользователь %d: голосовой ввод — дольше %d с", uid, BUDGET_SEC)
        raise HTTPException(504, {"code": "timeout", "message": f"не успел распознать за {BUDGET_SEC} с — "
                                  "попробуйте ещё раз, запись покороче"}) from None
    except speech.Busy:
        log.info("пользователь %d: голосовой ввод — модель занята", uid)
        raise HTTPException(503, {"code": "busy", "message": "модель голосового ввода занята — попробуйте ещё раз"}) from None
    except speech.SpeechError as e:
        log.info("пользователь %d: голосовой ввод — отказ службы", uid)
        raise HTTPException(502, {"code": "speech", "message": str(e)}) from None
    log.info("пользователь %d: голосовой ввод", uid)
    return {"text": text}
