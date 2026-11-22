"""Голоса озвучки по образцу (спека 2026-10-20-studio-sections-voice-design, 6.4): запись 5–60 с,
её текст и имя. Образец — WAV моно 24 кГц в media (audio.to_wav; длиннее SAMPLE_MAX — обрезается),
принадлежит пользователю, удаляется им (сначала — в корзину, trash.py) и вместе с историей. Его WAV и текст берёт озвучка srvllm
(Qwen3-TTS `/clone`). Текст образца в списке не отдаётся — только в карточке голоса. Образец
обрабатывается в запросе, поэтому ffmpeg ограничен BUDGET_SEC (а не audio.TIMEOUT_SEC фоновой
расшифровки) — дольше: 504 словами; таймаут маршрута Apache больше (сверяет tests/test_deploy.py).
Журнал — только id: имя голоса, текст и звук туда не пишутся."""
import asyncio
import json
import logging
import math
import uuid
from contextlib import contextmanager

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Path, Request
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
import security
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
DRAFT_SAVE_SEC = 20             # WAV распознанного образца на шару: дольше — текст без upload (вместе — раньше Apache)
PROBE_MAX = 200                 # проба голоса — одна короткая фраза (speeches.PHRASE_MAX — 220)
PROBE_BUDGET_SEC = 120          # вся проба — раньше таймаута Apache (шлюз может держать запрос, пока карта занята)
DRAFT_SEC = 900                 # подготовленные WAV без ссылки убирает обычная чистка сирот
#: пользователи, чья проба голоса идёт сейчас (одна за раз)
PROBING: set = set()

#: условие на образец `s` (`voice_samples`): не в корзине (отметка `voice_sample_trash`, шаг схемы 61, trash.py).
#: Образец из корзины не виден в карточке, счёте и выгрузке, не выбирается для озвучки; файл держит до `purge_sample`
LIVE = "NOT EXISTS (SELECT 1 FROM voice_sample_trash t WHERE t.id = s.id)"
_COUNT = f"(SELECT count(*) FROM voice_samples s WHERE s.voice_id = v.id AND {LIVE}) AS sample_count"

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/voices")


def _bad(code: str, message: str) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message})


def _row(user_id: int, voice_id: str):
    """Свой голос не из корзины (`deleted`, trash.py), иначе 404: и озвучка голосом из корзины — «голос удалён»."""
    row = state.DB.execute(f"SELECT v.*, {_COUNT}"
                           " FROM voices v WHERE v.id = ? AND v.user_id = ? AND v.deleted IS NULL",
                           (voice_id, user_id)).fetchone()
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


def _sample_range(fields: dict) -> tuple[float, float]:
    try:
        start = float(fields.get("start") or 0)
        end = fields.get("end")
        length = float(end) - start if end else SAMPLE_MAX
    except (ValueError, OverflowError):
        start, length = -1, 0
    if not math.isfinite(start) or start < 0 or not SAMPLE_MIN <= length <= SAMPLE_MAX:
        raise _bad("range", "начало и конец должны задавать фрагмент длительностью 5–60 секунд")
    return start, length


async def _sample(uid: int, fields: dict, data: bytes, mime: str) -> tuple[bytes, float]:
    """Один и тот же WAV и предел длительности для ASR и сохранения образца."""
    start, length = _sample_range(fields)
    src, dst = audio.temp(media.EXT.get(mime, ".bin")), audio.temp(".wav")
    try:
        await audio.file_io(src.write_bytes, data)
        duration = await asyncio.wait_for(audio.to_wav(src, dst, max_sec=length, start=start), BUDGET_SEC)
        if duration < SAMPLE_MIN:
            raise _bad("short", "образец — 5–60 секунд речи")
        wav = await audio.file_io(dst.read_bytes)
    except audio.AudioError as e:
        raise _bad("audio", str(e)) from None
    except TimeoutError:
        log.info("пользователь %d: образец голоса — дольше %d с", uid, BUDGET_SEC)
        raise HTTPException(504, {"code": "timeout", "message": "образец обрабатывается слишком долго — "
                                  "попробуйте ещё раз"}) from None
    finally:
        await audio.remove_temp(src, dst)
    return wav, duration


@router.post("/transcribe")
async def transcribe(request: Request):
    """Текст будущего образца до сохранения и `upload` — его подготовленный WAV, как у prepare-sample: редактор
    сохраняет образец по нему, не загружая файл второй раз. Без текста — ни истории, ни media; хранилище
    недоступно — текст без `upload` (сохранение подготовит образец само)."""
    uid = request.state.user.id

    async def run():
        fields, data, mime = await _form(request)
        wav, duration = await _sample(uid, fields, data, mime)
        return await dictation.recognize(uid, wav, "audio/wav", "sample.wav"), wav, duration

    try:
        text, wav, duration = await asyncio.wait_for(run(), TRANSCRIBE_BUDGET_SEC)
    except TimeoutError:
        raise HTTPException(504, {"code": "timeout", "message": "не успел распознать образец — попробуйте ещё раз "
                                  "или введите текст вручную"}) from None
    if not text:
        raise _bad("text", "в образце не удалось распознать речь — запишите ещё раз или введите текст вручную")
    if len(text) > TEXT_MAX:
        raise _bad("text", f"распознанный текст длиннее {TEXT_MAX} знаков — выберите запись покороче")
    try:
        upload = await asyncio.wait_for(_draft_of(uid, wav, duration), DRAFT_SAVE_SEC)
    except (media.StorageError, TimeoutError):
        upload = None
    return {"text": text, "upload": upload}


@router.post("", status_code=201)
async def create(request: Request):
    uid = request.state.user.id
    fields, data, mime = await _form(request)
    title, text = fields.get("name", "").strip(), _text(fields.get("text", ""))
    if not 1 <= len(title) <= NAME_MAX:
        raise _bad("name", f"имя голоса — от 1 до {NAME_MAX} знаков")
    wav, duration = await _sample(uid, fields, data, mime)
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
    rows = state.DB.execute(f"SELECT v.*, {_COUNT}"
                           " FROM voices v WHERE user_id = ? AND deleted IS NULL ORDER BY created, id",
                           (request.state.user.id,))
    # SQLite NOCASE only folds ASCII. Keep Cyrillic names and Ё in alphabetical order too.
    ordered = sorted(rows, key=lambda r: documents.nfc(r["name"]).casefold().replace("ё", "е"))
    return {"items": [_out(r) for r in ordered]}


@router.get("/{voice_id}")
async def get_voice(voice_id: str, request: Request):
    uid = request.state.user.id
    row = _row(uid, voice_id)
    samples = _samples(uid, voice_id)
    return _out(row, with_text=True) | {"revision": _revision(row, samples), "samples": [
        {"id": s["id"], "media_id": s["media_id"], "text": s["ref_text"], "duration": s["duration"], "created": s["created"],
         "active": s["media_id"] == row["media_id"]}
        for s in samples]}


def _samples(uid, voice_id):
    """Живые образцы голоса (не из корзины) по порядку."""
    return state.DB.execute(f"SELECT * FROM voice_samples s WHERE voice_id = ? AND user_id = ? AND {LIVE}"
                            " ORDER BY created, id", (voice_id, uid)).fetchall()


def _revision(voice, samples) -> str:
    return security.digest(json.dumps([voice["name"], voice["media_id"], [tuple(s) for s in samples]]))


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
    sample = state.DB.execute(f"SELECT * FROM voice_samples s WHERE id = ? AND voice_id = ? AND user_id = ? AND {LIVE}",
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


class BatchSampleIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    text: str = Field(max_length=TEXT_MAX)
    upload: str | None = Field(default=None, max_length=512)


class BatchIn(VoiceIn):
    revision: str | None = Field(default=None, max_length=64)
    samples: list[BatchSampleIn] = Field(min_length=1, max_length=SAMPLES_MAX)
    selected: int = Field(ge=0, lt=SAMPLES_MAX)


class DraftsIn(BaseModel):
    uploads: list[str] = Field(max_length=SAMPLES_MAX)


@router.post("/prepare-sample")
async def prepare_sample(request: Request):
    """Проверить один новый WAV. Пока редактор не подтвердил весь голос, ссылки на него нет."""
    uid = request.state.user.id
    fields, data, mime = await _form(request)
    wav, duration = await _sample(uid, fields, data, mime)
    try:
        return {"upload": await _draft_of(uid, wav, duration)}
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None


async def _draft_of(uid: int, wav: bytes, duration: float) -> str:
    """WAV образца — в media без ссылки; токен на DRAFT_SEC для batch. media.StorageError — как есть."""
    mid = await media.save_async(uid, wav, "audio/wav", "voice-sample.wav")
    return security.encrypt(json.dumps(["voice-draft", uid, mid, duration, clock.NOW() + DRAFT_SEC])).decode("ascii")


def _draft(uid: int, token: str):
    if len(token) > 512:
        return None
    try:
        purpose, owner, mid, duration, expires = json.loads(security.decrypt(token.encode("ascii")))
    except (InvalidToken, TypeError, ValueError):
        return None
    if purpose == "voice-draft" and owner == uid and clock.NOW() <= expires:
        if media.alive(uid, mid) and not media.referenced(uid, mid):
            return mid, duration
    return None


def _retire_samples(uid: int, media_ids) -> None:
    """Снять строки невостребованных образцов и передать удаление файлов очереди media."""
    rows = []
    for mid in media_ids:
        if not media.referenced(uid, mid):
            row = state.DB.execute("SELECT id, rel_path FROM media WHERE id = ? AND user_id = ?", (mid, uid)).fetchone()
            if row is not None:
                rows.append(tuple(row))
    media.queue(uid, rows)
    state.DB.executemany("DELETE FROM media WHERE id = ?", [(mid,) for mid, _ in rows])


def _archive_recording(sample, now: float) -> None:
    """Сохранить заменяемую запись отдельным образцом в корзине, оставив id живого образца на месте."""
    archived = uuid.uuid4().hex
    state.DB.execute("INSERT INTO voice_samples VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (archived, sample["voice_id"], sample["user_id"], sample["ref_text"], sample["media_id"],
                      sample["duration"], now))
    state.DB.execute("INSERT INTO voice_sample_trash(id, deleted) VALUES (?, ?)", (archived, now))


@router.post("/discard-samples", status_code=204)
async def discard_samples(body: DraftsIn, request: Request):
    uid = request.state.user.id
    with _transaction():
        _retire_samples(uid, {draft[0] for token in body.uploads if (draft := _draft(uid, token))})
    return Response(status_code=204)


def _batch_plan(uid: int, body: BatchIn, old: dict, first_id: str | None):
    result, ids, mids = [], set(), set()
    for index, item in enumerate(body.samples):
        previous = old.get(item.id)
        if item.id is not None and (previous is None or item.id in ids):
            raise _bad("samples", "неверный список образцов голоса")
        sample_id = item.id or (first_id if index == 0 else None) or uuid.uuid4().hex
        if item.upload:
            draft = _draft(uid, item.upload)
            if not draft or draft[0] in mids:
                raise _bad("samples", "подготовленный образец недоступен — загрузите запись ещё раз")
            mid, duration = draft
        elif previous is not None:
            mid, duration = previous["media_id"], previous["duration"]
        else:
            raise _bad("samples", "нужна запись образца: файл или с микрофона")
        ids.add(sample_id)
        mids.add(mid)
        result.append((sample_id, _text(item.text), mid, duration, previous["created"] if previous else clock.NOW()))
    return result


@router.put("/{voice_id}/batch")
async def save_batch(body: BatchIn, request: Request, voice_id: str = Path(pattern=r"^[0-9a-f]{32}$")):
    uid = request.state.user.id
    name = body.name.strip()
    if not name:
        raise _bad("name", f"имя голоса — от 1 до {NAME_MAX} знаков")
    if body.selected >= len(body.samples) or len(body.samples) > SAMPLES_MAX:
        raise _bad("samples", "неверный список образцов голоса")
    changed = HTTPException(409, {"code": "changed", "message": "голос изменён — откройте его ещё раз"})
    creating = body.revision is None
    with _transaction():
        operations.check(uid)
        samples = [] if creating else _samples(uid, voice_id)
        if not creating and body.revision != _revision(_row(uid, voice_id), samples):
            raise changed
        old = {s["id"]: s for s in samples}
        plan = _batch_plan(uid, body, old, voice_id if creating else None)
        _, active_text, active_media, active_duration, _ = plan[body.selected]
        if creating:
            # Триггер voices_sample_ai добавляет первый образец с id голоса.
            _, text, mid, duration, created = plan[0]
            if not state.DB.execute("INSERT OR IGNORE INTO voices(id, user_id, name, ref_text, media_id, duration, created)"
                                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (voice_id, uid, name, text, mid, duration, created)).rowcount:
                raise changed
        for sid, text, mid, duration, created in plan:
            if sid in old:
                if old[sid]["media_id"] != mid:
                    _archive_recording(old[sid], clock.NOW())
                state.DB.execute("UPDATE voice_samples SET ref_text = ?, media_id = ?, duration = ? WHERE id = ?",
                                 (text, mid, duration, sid))
            elif not creating or sid != voice_id:
                state.DB.execute("INSERT INTO voice_samples VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (sid, voice_id, uid, text, mid, duration, created))
        if not creating or body.selected != 0:
            state.DB.execute("UPDATE voices SET name = ?, ref_text = ?, media_id = ?, duration = ? WHERE id = ?",
                             (name, active_text, active_media, active_duration, voice_id))
        if not creating:                             # убранные из списка — в корзину, их файлы остаются
            now = clock.NOW()
            state.DB.executemany("INSERT INTO voice_sample_trash(id, deleted) VALUES (?, ?)",
                                 [(sid, now) for sid in old.keys() - {s[0] for s in plan}])
    log.info("пользователь %d: голос %s сохранён", uid, voice_id[:8])
    return _out(_row(uid, voice_id))


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
    wav, duration = await _sample(uid, fields, data, mime)
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
                _archive_recording(sample, clock.NOW())
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
    """Образец — в корзину (`voice_sample_trash`, trash.py); строка и файл — до `purge_sample`. Последний живой — 422;
    выбранный для озвучки — выбор переходит к первому живому."""
    uid = request.state.user.id
    sample = _sample_row(uid, voice_id, sample_id)
    voice = _row(uid, voice_id)
    if voice["sample_count"] <= 1:
        raise _bad("samples", "нужен хотя бы один образец голоса")
    with _transaction():
        state.DB.execute("INSERT INTO voice_sample_trash(id, deleted) VALUES (?, ?)", (sample_id, clock.NOW()))
        if voice["media_id"] == sample["media_id"]:
            _select(uid, voice_id, _samples(uid, voice_id)[0]["id"])
    log.info("пользователь %d: образец голоса %s в корзине", uid, voice_id[:8])
    return Response(status_code=204)


def sample_title(row) -> str:
    """Название образца в корзине (строка с `id` и `voice_id`): «имя голоса · номер образца» — по порядку всех его
    образцов, и из корзины тоже."""
    voice = state.DB.execute("SELECT name FROM voices WHERE id = ?", (row["voice_id"],)).fetchone()
    n = state.DB.execute("SELECT count(*) FROM voice_samples o, voice_samples s WHERE s.id = ? AND o.voice_id = s.voice_id"
                         " AND (o.created, o.id) <= (s.created, s.id)", (row["id"],)).fetchone()[0]
    return f"{voice['name'] if voice else ''} · {n}"


def restoring(user_id: int, ids: "list[str]") -> None:
    """Перед «Вернуть» образца (trash.py): у голоса стало бы больше SAMPLES_MAX живых — 409 `samples`."""
    for voice_id, count in state.DB.execute(
            "SELECT voice_id, count(*) FROM voice_samples WHERE user_id = ?"
            " AND id IN (SELECT value FROM json_each(?)) GROUP BY voice_id", (user_id, json.dumps(ids))).fetchall():
        if _row(user_id, voice_id)["sample_count"] + count > SAMPLES_MAX:
            raise HTTPException(409, {"code": "samples", "message": (
                f"у голоса уже {SAMPLES_MAX} образцов — удалите лишний, чтобы вернуть этот")})


async def purge_sample(user_id: int, item_id: str) -> None:
    """Окончательно: образец (отметка корзины — каскадом) и его файл, если на него больше никто не ссылается.
    Образца уже нет — ничего."""
    row = state.DB.execute("SELECT media_id FROM voice_samples WHERE id = ? AND user_id = ?",
                           (item_id, user_id)).fetchone()
    if row is None:
        return
    state.DB.execute("DELETE FROM voice_samples WHERE id = ?", (item_id,))
    if not media.referenced(user_id, row["media_id"]):
        await media.delete_async(user_id, row["media_id"])
    log.info("пользователь %d: образец голоса %s удалён", user_id, item_id[:8])


class ProbeIn(BaseModel):
    text: str = Field(max_length=PROBE_MAX * 2)
    connection_id: int | None = None
    model: str | None = Field(default=None, max_length=sections.MODEL_MAX)


def _probe_model(uid: int, body: ProbeIn):
    """Выбранные подключение и модель «Озвучки»; без выбора — умолчание раздела, а если оно не шлюз srvllm —
    первая отмеченная модель шлюза (голос по образцу есть только у него)."""
    if body.connection_id is not None or body.model:
        return sections.resolve(uid, "speech", body.connection_id, body.model)
    chosen = sections.default(uid, "speech")
    if chosen is None or chosen[0].kind != "srvllm":
        gateway = next((l for l in sections.links(uid, "speech") if l.connection.kind == "srvllm" and l.models), None)
        if gateway is not None:
            return sections.resolve(uid, "speech", gateway.connection.id, gateway.models[0])
    return sections.resolve(uid, "speech", None, None)


@router.post("/{voice_id}/probe")
async def probe(voice_id: str, body: ProbeIn, request: Request):
    """Проба голоса короткой фразой пользователя (до PROBE_MAX знаков, одна фраза модели шлюза srvllm
    из «Озвучки»: выбранной или `_probe_model`). Не задание очереди и не история: звук — в ответе (`no-store`),
    ничего не сохраняется. Карта занята — сразу 503 словами, без ожидания (очередь озвучек её дождётся сама);
    шлюз держит запрос дольше PROBE_BUDGET_SEC — 504 словами раньше таймаута Apache; одна проба
    пользователя за раз — иначе 409."""
    uid = request.state.user.id
    text = " ".join(documents.nfc(body.text).split())
    if not 1 <= len(text) <= PROBE_MAX:
        raise _bad("text", f"фраза пробы — от 1 до {PROBE_MAX} знаков")
    conn, model = _probe_model(uid, body)
    if conn.kind != "srvllm":
        raise HTTPException(409, {"code": "voice", "message": "голос по образцу — у озвучки шлюза srvllm"})
    reference_ = await reference(uid, voice_id)
    if uid in PROBING:
        raise HTTPException(409, {"code": "busy", "message": "проба уже идёт — дождитесь её"})
    PROBING.add(uid)

    async def run():
        if model == "qwen3-tts":
            await speech.free_card(conn)
        return await speech.say(conn, model, text, reference_)

    try:
        data, mime = await asyncio.wait_for(run(), PROBE_BUDGET_SEC)
    except TimeoutError:
        log.info("пользователь %d: проба голоса — дольше %d с", uid, PROBE_BUDGET_SEC)
        raise HTTPException(504, {"code": "timeout", "message": f"служба озвучки не ответила за {PROBE_BUDGET_SEC} с — "
                                  "возможно, карта занята; попробуйте пробу позже"}) from None
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
    """Голос — в корзину (`deleted`, trash.py); образцы и их файлы — до `purge`."""
    uid = request.state.user.id
    _row(uid, voice_id)
    state.DB.execute("UPDATE voices SET deleted = ? WHERE id = ?", (clock.NOW(), voice_id))
    log.info("пользователь %d: голос %s в корзине", uid, voice_id[:8])
    return Response(status_code=204)


async def purge(user_id: int, item_id: str) -> None:
    """Окончательно: голос, его образцы (каскадом) и их файлы, если на них больше никто не ссылается. Голоса уже
    нет — ничего."""
    row = state.DB.execute("SELECT media_id FROM voices WHERE id = ? AND user_id = ?", (item_id, user_id)).fetchone()
    if row is None:
        return
    mids = {s[0] for s in state.DB.execute("SELECT media_id FROM voice_samples WHERE voice_id = ? AND user_id = ?",
                                          (item_id, user_id))} | {row["media_id"]}
    state.DB.execute("DELETE FROM voices WHERE id = ?", (item_id,))
    for mid in mids:
        if not media.referenced(user_id, mid):
            await media.delete_async(user_id, mid)
    log.info("пользователь %d: голос %s удалён", user_id, item_id[:8])
