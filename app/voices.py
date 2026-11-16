"""Голоса озвучки по образцу (спека 2026-10-20-studio-sections-voice-design, 6.4): запись 5–60 с,
её текст и имя. Образец — WAV моно 24 кГц в media (audio.to_wav; длиннее SAMPLE_MAX — обрезается),
принадлежит пользователю, удаляется им и вместе с историей. Его WAV и текст берёт озвучка srvllm
(Qwen3-TTS `/clone`). Текст образца в списке не отдаётся — только в карточке голоса. Образец
обрабатывается в запросе, поэтому ffmpeg ограничен BUDGET_SEC (а не audio.TIMEOUT_SEC фоновой
расшифровки) — дольше: 504 словами; таймаут маршрута Apache больше (сверяет tests/test_deploy.py).
Журнал — только id: имя голоса, текст и звук туда не пишутся."""
import asyncio
import logging
import uuid
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import audio
import clock
import dictation
import documents
import forms
import media
import operations
import sections
import speech
import state
from image_upload import UploadError

AUDIO_MAX = 32 * 1024 * 1024
SAMPLE_MIN, SAMPLE_MAX = 4.5, 60.0
SAMPLES_MAX = 10
NAME_MAX = 64
TEXT_MAX = 2000
BUDGET_SEC = 60                 # WAV и длительность образца — ffmpeg за секунды
TRANSCRIBE_BUDGET_SEC = 120      # вся подготовка образца и ASR — раньше таймаута Apache
PROBE_MAX = 200                 # проба голоса — одна короткая фраза (speeches.PHRASE_MAX — 220)
#: пользователи, чья проба голоса идёт сейчас (одна за раз)
PROBING: set = set()

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/voices")


def _bad(code: str, message: str) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message})


def _row(user_id: int, voice_id: str):
    row = state.DB.execute("SELECT v.*, (SELECT count(*) FROM voice_samples s WHERE s.voice_id = v.id) AS sample_count"
                           " FROM voices v WHERE v.id = ? AND v.user_id = ?", (voice_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого голоса нет"})
    return row


def _out(row, with_text: bool = False) -> dict:
    out = {"id": row["id"], "name": row["name"], "duration": row["duration"], "created": row["created"],
           "sample_count": row["sample_count"]}
    if with_text:
        out["text"] = row["ref_text"]
    return out


async def reference(user_id: int, voice_id: str) -> tuple[bytes, str]:
    """WAV образца (с шары — в потоке) и его текст — для озвучки; чужой или удалённый — 404 gone."""
    row = _row(user_id, voice_id)
    try:
        return await asyncio.to_thread(media.path(user_id, row["media_id"]).read_bytes), row["ref_text"]
    except FileNotFoundError:
        raise HTTPException(404, media.GONE) from None


async def _form(request: Request):
    try:
        raw = await forms.read_body(request, AUDIO_MAX + 64 * 1024)
        fields, data, mime, _name = await asyncio.to_thread(
            forms.audio_form, raw, request.headers.get("content-type", ""), max_bytes=AUDIO_MAX)
    except UploadError as e:
        raise HTTPException(e.status, {"code": "format" if e.status == 415 else "audio", "message": str(e)}) from None
    return fields, data, mime


async def _sample(uid: int, data: bytes, mime: str) -> tuple[bytes, float]:
    """Один и тот же WAV и предел длительности для ASR и сохранения образца."""
    async def file_io(fn, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        finally:
            # Отмена ожидания не останавливает поток; файлы можно убирать только после close.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass

    src, dst = audio.temp(media.EXT.get(mime, ".bin")), audio.temp(".wav")

    def cleanup():
        for f in (src, dst):
            f.unlink(missing_ok=True)

    try:
        await file_io(src.write_bytes, data)
        duration = await asyncio.wait_for(audio.to_wav(src, dst, max_sec=SAMPLE_MAX), BUDGET_SEC)
        if duration < SAMPLE_MIN:
            raise _bad("short", "образец — 5–60 секунд речи")
        wav = await file_io(dst.read_bytes)
    except audio.AudioError as e:
        raise _bad("audio", str(e)) from None
    except TimeoutError:
        log.info("пользователь %d: образец голоса — дольше %d с", uid, BUDGET_SEC)
        raise HTTPException(504, {"code": "timeout", "message": "образец обрабатывается слишком долго — "
                                  "попробуйте ещё раз"}) from None
    finally:
        await file_io(cleanup)                 # оба файла одной операцией: отмена не пропустит второй
    return wav, duration


@router.post("/transcribe")
async def transcribe(request: Request):
    """Текст будущего образца до сохранения: только временный WAV, без истории и media."""
    uid = request.state.user.id

    async def run():
        _fields, data, mime = await _form(request)
        wav, _duration = await _sample(uid, data, mime)
        return await dictation.recognize(uid, wav, "audio/wav", "sample.wav")

    try:
        text = await asyncio.wait_for(run(), TRANSCRIBE_BUDGET_SEC)
    except TimeoutError:
        raise HTTPException(504, {"code": "timeout", "message": "не успел распознать образец — попробуйте ещё раз "
                                  "или введите текст вручную"}) from None
    if not text:
        raise _bad("text", "в образце не удалось распознать речь — запишите ещё раз или введите текст вручную")
    if len(text) > TEXT_MAX:
        raise _bad("text", f"распознанный текст длиннее {TEXT_MAX} знаков — выберите запись покороче")
    return {"text": text}


@router.post("", status_code=201)
async def create(request: Request):
    uid = request.state.user.id
    fields, data, mime = await _form(request)
    title, text = fields.get("name", "").strip(), _text(fields.get("text", ""))
    if not 1 <= len(title) <= NAME_MAX:
        raise _bad("name", f"имя голоса — от 1 до {NAME_MAX} знаков")
    wav, duration = await _sample(uid, data, mime)
    vid = uuid.uuid4().hex
    operations.check(uid)
    try:
        media_id = await media.save_async(uid, wav, "audio/wav", f"voice-{vid[:8]}.wav")
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    state.DB.execute("INSERT INTO voices(id, user_id, name, ref_text, media_id, duration, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (vid, uid, title, text, media_id, duration, clock.NOW()))
    log.info("пользователь %d: голос %s добавлен", uid, vid[:8])
    return _out(_row(uid, vid))


@router.get("")
async def list_voices(request: Request):
    rows = state.DB.execute("SELECT v.*, (SELECT count(*) FROM voice_samples s WHERE s.voice_id = v.id) AS sample_count"
                           " FROM voices v WHERE user_id = ? ORDER BY created, id", (request.state.user.id,))
    return {"items": [_out(r) for r in rows]}


@router.get("/{voice_id}")
async def get_voice(voice_id: str, request: Request):
    row = _row(request.state.user.id, voice_id)
    return _out(row, with_text=True) | {"samples": [
        {"id": s["id"], "text": s["ref_text"], "duration": s["duration"], "created": s["created"],
         "active": s["media_id"] == row["media_id"]}
        for s in state.DB.execute("SELECT * FROM voice_samples WHERE voice_id = ? AND user_id = ? ORDER BY created, id",
                                  (voice_id, request.state.user.id))]}


class VoiceIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)


class SampleIn(BaseModel):
    text: str | None = Field(default=None, max_length=TEXT_MAX)
    active: bool = False


def _text(text: str) -> str:
    text = text.strip()
    if not 1 <= len(text) <= TEXT_MAX:
        raise _bad("text", f"текст образца — от 1 до {TEXT_MAX} знаков: что именно сказано в записи")
    return text


def _sample_row(uid: int, voice_id: str, sample_id: str):
    _row(uid, voice_id)
    sample = state.DB.execute("SELECT * FROM voice_samples WHERE id = ? AND voice_id = ? AND user_id = ?",
                              (sample_id, voice_id, uid)).fetchone()
    if sample is None:
        raise HTTPException(404, {"code": "gone", "message": "такого образца нет"})
    return sample


def _select(uid: int, voice_id: str, sample_id: str):
    sample = _sample_row(uid, voice_id, sample_id)
    state.DB.execute("UPDATE voices SET media_id = ?, ref_text = ?, duration = ? WHERE id = ? AND user_id = ?",
                     (sample["media_id"], sample["ref_text"], sample["duration"], voice_id, uid))


@contextmanager
def _transaction():
    """Keep the sample and the compatible selected reference in sync; no awaits inside."""
    state.DB.execute("BEGIN")
    try:
        yield
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise


@router.patch("/{voice_id}")
async def rename_voice(voice_id: str, body: VoiceIn, request: Request):
    uid = request.state.user.id
    _row(uid, voice_id)
    name = body.name.strip()
    if not name:
        raise _bad("name", f"имя голоса — от 1 до {NAME_MAX} знаков")
    state.DB.execute("UPDATE voices SET name = ? WHERE id = ? AND user_id = ?", (name, voice_id, uid))
    return _out(_row(uid, voice_id))


@router.post("/{voice_id}/samples", status_code=201)
async def add_sample(voice_id: str, request: Request):
    return await _save_sample(voice_id, request)


@router.put("/{voice_id}/samples/{sample_id}")
async def replace_sample(voice_id: str, sample_id: str, request: Request):
    return await _save_sample(voice_id, request, sample_id)


async def _save_sample(voice_id: str, request: Request, sample_id: str | None = None):
    uid = request.state.user.id
    voice = _row(uid, voice_id)
    previous = _sample_row(uid, voice_id, sample_id) if sample_id else None
    if previous is None and voice["sample_count"] >= SAMPLES_MAX:
        raise _bad("samples", f"у голоса не больше {SAMPLES_MAX} образцов")
    fields, data, mime = await _form(request)
    text = _text(fields.get("text", ""))
    wav, duration = await _sample(uid, data, mime)
    operations.check(uid)
    _row(uid, voice_id)
    try:
        mid = await media.save_async(uid, wav, "audio/wav", "voice-sample.wav")
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    try:
        with _transaction():
            operations.check(uid)
            current = _row(uid, voice_id)
            if previous is not None:
                sample = _sample_row(uid, voice_id, sample_id)
                if sample["media_id"] != previous["media_id"]:
                    raise HTTPException(409, {"code": "changed", "message": "образец изменён — откройте голос ещё раз"})
                state.DB.execute("UPDATE voice_samples SET media_id = ?, ref_text = ?, duration = ? WHERE id = ?",
                                 (mid, text, duration, sample_id))
                if current["media_id"] == previous["media_id"]:
                    _select(uid, voice_id, sample_id)
            else:
                if current["sample_count"] >= SAMPLES_MAX:
                    raise _bad("samples", f"у голоса не больше {SAMPLES_MAX} образцов")
                sample_id = uuid.uuid4().hex
                state.DB.execute("INSERT INTO voice_samples VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (sample_id, voice_id, uid, text, mid, duration, clock.NOW()))
    except BaseException:
        await media.delete_async(uid, mid)
        raise
    if previous is not None and not media.referenced(uid, previous["media_id"]):
        await media.delete_async(uid, previous["media_id"])
    log.info("пользователь %d: образец голоса %s сохранён", uid, voice_id[:8])
    return {"id": sample_id, "text": text, "duration": duration}


@router.patch("/{voice_id}/samples/{sample_id}")
async def edit_sample(voice_id: str, sample_id: str, body: SampleIn, request: Request):
    uid = request.state.user.id
    sample = _sample_row(uid, voice_id, sample_id)
    text = _text(body.text) if body.text is not None else sample["ref_text"]
    with _transaction():
        state.DB.execute("UPDATE voice_samples SET ref_text = ? WHERE id = ?", (text, sample_id))
        if body.active or _row(uid, voice_id)["media_id"] == sample["media_id"]:
            _select(uid, voice_id, sample_id)
    return {"id": sample_id, "text": text, "duration": sample["duration"]}


@router.delete("/{voice_id}/samples/{sample_id}", status_code=204)
async def delete_sample(voice_id: str, sample_id: str, request: Request):
    uid = request.state.user.id
    sample = _sample_row(uid, voice_id, sample_id)
    voice = _row(uid, voice_id)
    if voice["sample_count"] <= 1:
        raise _bad("samples", "нужен хотя бы один образец голоса")
    with _transaction():
        state.DB.execute("DELETE FROM voice_samples WHERE id = ?", (sample_id,))
        if voice["media_id"] == sample["media_id"]:
            other = state.DB.execute("SELECT id FROM voice_samples WHERE voice_id = ? ORDER BY created, id LIMIT 1",
                                     (voice_id,)).fetchone()
            _select(uid, voice_id, other["id"])
    if not media.referenced(uid, sample["media_id"]):
        await media.delete_async(uid, sample["media_id"])
    return Response(status_code=204)


class ProbeIn(BaseModel):
    text: str = Field(max_length=PROBE_MAX * 2)


@router.post("/{voice_id}/probe")
async def probe(voice_id: str, body: ProbeIn, request: Request):
    """Проба голоса короткой фразой пользователя (до PROBE_MAX знаков, одна фраза Qwen3-TTS `/clone`
    подключения раздела «Озвучка»). Не задание очереди и не история: звук — в ответе (`no-store`), ничего не
    сохраняется. Карта занята — сразу 503 словами, без ожидания (очередь озвучек её дождётся сама); одна проба
    пользователя за раз — иначе 409."""
    uid = request.state.user.id
    text = " ".join(documents.nfc(body.text).split())
    if not 1 <= len(text) <= PROBE_MAX:
        raise _bad("text", f"фраза пробы — от 1 до {PROBE_MAX} знаков")
    conn, model = sections.resolve(uid, "speech", None, None)
    if conn.kind != "srvllm":
        raise HTTPException(409, {"code": "voice", "message": "голос по образцу — у озвучки шлюза srvllm"})
    reference_ = await reference(uid, voice_id)
    if uid in PROBING:
        raise HTTPException(409, {"code": "busy", "message": "проба уже идёт — дождитесь её"})
    PROBING.add(uid)
    try:
        await speech.free_card(conn)
        data, mime = await speech.say(conn, model, text, reference_)
    except speech.Busy:
        raise HTTPException(503, {"code": "busy", "message": "карта занята — попробуйте пробу позже"}) from None
    except speech.SpeechError as e:
        raise HTTPException(502, {"code": "gateway", "message": str(e)}) from None
    finally:
        PROBING.discard(uid)
    log.info("пользователь %d: проба голоса %s", uid, voice_id[:8])
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


@router.delete("/{voice_id}", status_code=204)
async def delete_voice(voice_id: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, voice_id)
    mids = {s[0] for s in state.DB.execute("SELECT media_id FROM voice_samples WHERE voice_id = ? AND user_id = ?",
                                          (voice_id, uid))} | {row["media_id"]}
    state.DB.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
    for mid in mids:
        if not media.referenced(uid, mid):
            await media.delete_async(uid, mid)
    log.info("пользователь %d: голос %s удалён", uid, voice_id[:8])
    return Response(status_code=204)
