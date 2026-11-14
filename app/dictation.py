"""Голосовой ввод (спека 2026-10-20-studio-sections-voice-design, 6.5): POST /api/dictation —
запись из браузера (iPhone — m4a, Chrome — webm) → текст модели раздела «Голосовой ввод» (по
умолчанию GigaAM шлюза). Текст вставляет в поле сама страница (mic.js), без отправки.

Звук — только в памяти запроса: ни на диск, ни в историю, ни в журнал; в журнал — событие с id
пользователя и код итога. WhisperX шлюза делит карту с картинками и gemma-abl (аренда 600 с):
перед ним — их release (speech.free_card), на 503 — повтор раз в RETRY_SEC, пока не кончился
бюджет, затем 504 «карта занята». GigaAM и внешний сервис занятую модель не ждут — 503 сразу: ввод
— секунды, человек нажмёт ещё раз. Ответ синхронный, поэтому распознавание целиком ограничено
BUDGET_SEC (а не таймаутами вызовов шлюза: у WhisperX там 2460 с) — дольше: 504 словами
«повторите»; таймаут маршрута Apache больше бюджета (сверяет tests/test_deploy.py), иначе он
оборвал бы запрос раньше и текст пропал."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

import clock
import forms
import sections
import speech
from image_upload import UploadError

MAX_AUDIO = 8 * 1024 * 1024                      # две минуты записи браузера — с большим запасом
FORM_SLACK = 64 * 1024
BUDGET_SEC = 120                                 # запись до 2 минут (mic.js MAX_SEC): холодная модель и распознавание
RETRY_SEC = 5                                    # WhisperX: карта занята — повтор

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/dictation")


class CardBusy(Exception):
    """WhisperX не получил карту за бюджет."""


async def _recognize(conn, model: str, data: bytes, mime: str, name: str) -> str:
    if conn.kind != "srvllm" or model == "gigaam":            # карту делит только WhisperX (как speech.dictate)
        return await speech.dictate(conn, model, data, mime, name)
    deadline = clock.NOW() + BUDGET_SEC
    await speech.free_card(conn)
    while True:
        try:
            return await speech.dictate(conn, model, data, mime, name)
        except speech.Busy:
            if clock.NOW() + RETRY_SEC >= deadline:
                raise CardBusy from None
            await clock.SLEEP(RETRY_SEC)


@router.post("")
async def dictate(request: Request):
    uid = request.state.user.id
    try:
        raw = await forms.read_body(request, MAX_AUDIO + FORM_SLACK)
        _fields, data, mime, name = await asyncio.to_thread(
            forms.audio_form, raw, request.headers.get("content-type", ""), max_bytes=MAX_AUDIO)
    except UploadError as e:
        raise HTTPException(e.status, {"code": "format" if e.status == 415 else "audio", "message": str(e)}) from None
    del raw
    return {"text": await recognize(uid, data, mime, name)}


async def recognize(uid: int, data: bytes, mime: str, name: str) -> str:
    """Распознать запись моделью голосового ввода, с теми же сроком и отказами для образца голоса."""
    conn, model = sections.resolve(uid, "dictation", None, None)
    try:
        text = await asyncio.wait_for(_recognize(conn, model, data, mime, name), BUDGET_SEC)
    except TimeoutError:
        log.info("пользователь %d: голосовой ввод — дольше %d с", uid, BUDGET_SEC)
        raise HTTPException(504, {"code": "timeout", "message": f"не успел распознать за {BUDGET_SEC} с — "
                                  "попробуйте ещё раз, запись покороче"}) from None
    except CardBusy:
        log.info("пользователь %d: голосовой ввод — карта занята дольше %d с", uid, BUDGET_SEC)
        raise HTTPException(504, {"code": "busy", "message": f"карта занята дольше {BUDGET_SEC} с — "
                                  "попробуйте ещё раз позже"}) from None
    except speech.Busy:
        log.info("пользователь %d: голосовой ввод — модель занята", uid)
        raise HTTPException(503, {"code": "busy", "message": "модель голосового ввода занята — попробуйте ещё раз"}) from None
    except speech.SpeechError as e:
        log.info("пользователь %d: голосовой ввод — отказ службы", uid)
        raise HTTPException(502, {"code": "speech", "message": str(e)}) from None
    log.info("пользователь %d: голосовой ввод", uid)
    return text
