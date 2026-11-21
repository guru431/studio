"""Распознавание и озвучка: шлюз srvllm и внешний сервис OpenAI-стиля (спека 2026-10-20-studio-
sections-voice-design, 6.1 и 6.3–6.5; контракт шлюза сверен с llm_routers 2026-10-20).

- srvllm: расшифровка — `/speech/asr` WhisperX с собеседниками (запрос — ASR_QUERY, поле файла
  `audio_file`; текст собирается из сегментов, `speaker` может не быть); голосовой ввод — GigaAM
  `/speech/lite/transcriptions` или WhisperX `/speech/transcriptions` (`file`); озвучка — Qwen3-TTS
  `/speech/tts-qwen` и F5-TTS `/speech/tts-f5` (`text`, `language`, `ref_text`, `ref_audio`; ответ — WAV).
- Внешний сервис: `/audio/transcriptions` (расшифровка — `verbose_json` с временем сегментов, без собеседников;
  на 400 — повтор с `json`, текст одной репликой; модели `*-diarize*` — `diarized_json`, реплики по `speaker`;
  ввод — `json`) и `/audio/speech` (JSON с голосом по имени; ответ — mp3).
- Расшифровка, пришедшая текстом (результат MCP): `segments_from_labeled` — строки «Говорящий N:», «Speaker N:»,
  «Собеседник N:»; `segments_from_srt` — субтитры SRT.
- 503 (карта занята, `busy` Qwen) — Busy: ждать и повторять решает вызывающий; прочие отказы —
  SpeechError словами. Освобождения слотов `asr` и `tts-qwen` у шлюза нет: перед речью студия
  освобождает модели картинок и gemma-abl (`free_card`), чтобы карта досталась речи.
- Язык распознавания (`LANGUAGES`, None — «Авто»): srvllm — `language=` в запросе `/speech/asr` (поле формы у
  `/speech/transcriptions`), s06 и внешний сервис — поле `language`; «Авто» — поля нет. GigaAM — только русский, без поля.
- Пределы (`limits`): srvllm и s06 — `GET <адрес>/speech/limits` (`max_seconds`), кэш LIMITS_TTL_SEC; нет маршрута или
  внешний сервис — `transcripts.DURATION_MAX`. Инструмент MCP — пометка `studio/limits` (`tool_limits`).
- Звук, текст и имена в журнал не пишутся.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlencode

import httpx

import clock
import images
import upstream

GATEWAY = {"whisperx": "large-v3", "gigaam": "gigaam-v3"}
ASR_QUERY = {"diarize": "true", "word_timestamps": "true", "output_format": "json", "model": GATEWAY["whisperx"]}
LANGUAGES = ("ru", "en")         # язык распознавания; None — «Авто»
LIMITS_TTL_SEC = 600
LIMITS_TIMEOUT_SEC = 10
LIMITS_MIN_SEC = 60              # предел короче — неверный ответ: части по секунде не режем
#: (id подключения, адрес) → (когда спросили, Limits)
LIMITS: dict = {}
ASR_TIMEOUT_SEC = 2460          # таймаут LiteLLM маршрута /speech/asr и /speech/transcriptions
LITE_TIMEOUT_SEC = 180
TTS_TIMEOUT_SEC = 720
EXTERNAL_TIMEOUT_SEC = 600
TTS_LANGUAGE = "Russian"
#: язык озвучки шлюза: код Studio (`speeches.language`) → значение поля `language` Qwen3-TTS и F5-TTS
TTS_LANGUAGES = {"ru": "Russian", "en": "English"}
TTS_ROUTES = {"qwen3-tts": "/speech/tts-qwen", "f5-tts": "/speech/tts-f5"}
LABEL = "Собеседник {}"          # метка собеседника по порядку первого появления (с 1)


class Busy(Exception):
    """Карта занята (503 шлюза) или Qwen озвучивает другое: повторить позже."""


class SpeechError(Exception):
    """Отказ службы речи — словами для человека."""


class BadRequest(SpeechError):
    """400 службы: запрос не принят (у внешнего сервиса — в том числе формат ответа)."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speaker: "str | None"
    text: str


@dataclass(frozen=True)
class Limits:
    max_seconds: int


def _max_seconds(value) -> "int | None":
    return value if type(value) is int and value >= LIMITS_MIN_SEC else None


async def limits(conn, model: str) -> Limits:
    """Самая длинная запись, которую источник распознаёт за один запрос. srvllm и s06 — их маршрут пределов (кэш
    LIMITS_TTL_SEC и на «нет маршрута»); внешний сервис, нет маршрута, неверный ответ — `transcripts.DURATION_MAX`.
    Сбой сети не кэшируется."""
    import transcripts                       # transcripts импортирует speech
    fallback = Limits(transcripts.DURATION_MAX)
    if not (conn.s06 or conn.kind == "srvllm"):
        return fallback
    key = (conn.id, conn.base_url)
    cached = LIMITS.get(key)
    if cached is not None and clock.NOW() - cached[0] < LIMITS_TTL_SEC:
        return cached[1]
    try:
        r = await upstream.request(conn, "GET", f"{conn.base_url}/speech/limits", timeout=LIMITS_TIMEOUT_SEC)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return fallback
    if r.status_code not in (200, 404):
        return fallback
    seconds = _max_seconds(upstream.json_body(r).get("max_seconds")) if r.status_code == 200 else None
    found = Limits(seconds) if seconds else fallback
    LIMITS[key] = (clock.NOW(), found)
    return found


def tool_limits(tool: dict) -> "Limits | None":
    """Предел инструмента MCP из `_meta["studio/limits"].max_seconds`; нет или неверный — None."""
    meta = tool.get("_meta") if isinstance(tool, dict) else None
    raw = meta.get("studio/limits") if isinstance(meta, dict) else None
    seconds = raw.get("max_seconds") if isinstance(raw, dict) else None
    return Limits(seconds) if type(seconds) is int and seconds > 0 else None


def language_of(value) -> "str | None":
    """Язык запроса: «ru»/«en», пусто — «Авто» (None); другое — ValueError."""
    if value in (None, "", "auto"):
        return None
    if value not in LANGUAGES:
        raise ValueError(value)
    return value


def _lang(language: "str | None") -> dict:
    return {"language": language} if language else {}


async def _post(conn, url: str, timeout: float, **kw) -> httpx.Response:
    try:
        r = await upstream.request(conn, "POST", url, timeout=timeout, **kw)
    except httpx.TimeoutException:
        raise SpeechError("служба речи не ответила вовремя") from None
    except httpx.HTTPError as e:
        raise SpeechError(f"служба речи недоступна: {type(e).__name__}") from None
    except upstream.UpstreamRefused as e:
        raise SpeechError(upstream.refused_words(e, conn)) from None
    if r.status_code == 503:
        raise Busy(upstream.message(r) or "карта занята")
    if r.status_code != 200:
        raise (BadRequest if r.status_code == 400 else SpeechError)(upstream.gateway_error(r, conn))
    return r


def _seconds(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _segments(body: dict) -> list[Segment]:
    raw = body.get("segments")
    out = [Segment(_seconds(s.get("start")), _seconds(s.get("end")),
                   s.get("speaker") if isinstance(s.get("speaker"), str) else None, s["text"].strip())
           for s in (raw if isinstance(raw, list) else [])
           if isinstance(s, dict) and isinstance(s.get("text"), str) and s["text"].strip()]
    if not out and isinstance(body.get("text"), str) and body["text"].strip():
        out = [Segment(0.0, 0.0, None, body["text"].strip())]
    return out


#: метка собеседника в начале строки: «Говорящий 2:», «Speaker 2:», «Собеседник 2:» или «[SPEAKER_01]:» (с нуля)
_LABELED = re.compile(r"\s*(?:(?:Говорящий|Собеседник|Speaker)\s+(\d{1,3})|\[?SPEAKER_(\d{1,3})\]?)\s*:\s*(.*)", re.I)
_SRT_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})")


def _label(line: str) -> "tuple[str | None, str]":
    """Собеседник строки (LABEL с номером из метки) и текст после метки; без метки — (None, строка)."""
    found = _LABELED.fullmatch(line)
    if not found:
        return None, line.strip()
    number = int(found[1]) if found[1] else int(found[2]) + 1
    return LABEL.format(number), found[3].strip()


def segments_from_labeled(text: str) -> list[Segment]:
    """Текст с метками собеседников по строкам — реплики без времени; строка без метки продолжает реплику (до первой
    метки — реплика без собеседника). Ни одной метки — пусто: это не расшифровка по собеседникам."""
    out: list[Segment] = []
    labeled = False
    for line in text.splitlines():
        speaker, rest = _label(line)
        if speaker is not None:
            labeled = True
            out.append(Segment(0.0, 0.0, speaker, rest))
        elif rest and out:
            out[-1] = Segment(0.0, 0.0, out[-1].speaker, f"{out[-1].text} {rest}".strip())
        elif rest:
            out.append(Segment(0.0, 0.0, None, rest))
    return [s for s in out if s.text] if labeled else []


def segments_from_srt(text: str) -> list[Segment]:
    """Субтитры SRT — сегменты со временем; метка собеседника — у первого субтитра реплики, субтитр без метки — тот же
    собеседник. Не SRT (ни одной строки времени) — пусто."""
    out: list[Segment] = []
    speaker = None
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        at = next((n for n, line in enumerate(lines[:2]) if _SRT_TIME.fullmatch(line.strip())), None)
        if at is None:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, _SRT_TIME.fullmatch(lines[at].strip()).groups())
        own, words = _label(" ".join(line.strip() for line in lines[at + 1:]))
        speaker = own or speaker
        if words:
            out.append(Segment(h1 * 3600 + m1 * 60 + s1 + ms1 / 1000, h2 * 3600 + m2 * 60 + s2 + ms2 / 1000, speaker, words))
    return out


def _text(r: httpx.Response) -> str:
    text = upstream.json_body(r).get("text")
    return text.strip() if isinstance(text, str) else ""


async def transcribe(conn, model: str, audio: Path, mime: str, language: "str | None" = None) -> list[Segment]:
    """Расшифровка сжатой записи (audio.compress) — сегменты по порядку; `language` — из LANGUAGES, None — «Авто»."""
    reading = asyncio.create_task(asyncio.to_thread(audio.read_bytes))
    try:
        data = await asyncio.shield(reading)
    finally:
        # Отмена ожидания не закрывает файл в потоке. Уборка MP3 должна дождаться close,
        # иначе Windows откажет в unlink; повторная отмена тоже не обрывает ожидание.
        while not reading.done():
            try:
                await asyncio.shield(reading)
            except asyncio.CancelledError:
                pass
    if conn.s06:
        r = await _post(conn, f"{conn.base_url}/speech/transcriptions", ASR_TIMEOUT_SEC,
                        data={"model": model, **_lang(language), "diarize": "true"},
                        files={"file": ("audio.mp3", data, mime)})
    elif conn.kind == "srvllm":
        r = await _post(conn, f"{conn.base_url}/speech/asr?{urlencode({**_lang(language), **ASR_QUERY})}",
                        ASR_TIMEOUT_SEC, files={"audio_file": ("audio.mp3", data, mime)})
    else:
        url = f"{conn.base_url}/audio/transcriptions"
        # модели *-diarize* (gpt-4o-transcribe-diarize) отдают реплики по `speaker` в diarized_json; запись длиннее 30 с
        # они принимают только с chunking_strategy
        shape = ({"response_format": "diarized_json", "chunking_strategy": "auto"} if "-diarize" in model
                 else {"response_format": "verbose_json"})
        try:
            r = await _post(conn, url, EXTERNAL_TIMEOUT_SEC, data={"model": model, **shape, **_lang(language)},
                            files={"file": ("audio.mp3", data, mime)})
        except BadRequest:
            # OpenAI gpt-4o-transcribe и gpt-4o-mini-transcribe понимают только json/text: текст одной репликой
            r = await _post(conn, url, EXTERNAL_TIMEOUT_SEC, data={"model": model, "response_format": "json",
                                                                   **_lang(language)},
                            files={"file": ("audio.mp3", data, mime)})
    return _segments(upstream.json_body(r))


async def dictate(conn, model: str, data: bytes, mime: str, name: str, language: "str | None" = None) -> str:
    """Голосовой ввод: запись как есть (m4a iPhone, webm Chrome) — текст; `language` — как у `transcribe`."""
    if conn.s06:
        r = await _post(conn, f"{conn.base_url}/speech/transcriptions", ASR_TIMEOUT_SEC,
                        data={"model": model, **_lang(language), "diarize": "false"}, files={"file": (name, data, mime)})
    elif conn.kind != "srvllm":
        r = await _post(conn, f"{conn.base_url}/audio/transcriptions", EXTERNAL_TIMEOUT_SEC,
                        data={"model": model, "response_format": "json", **_lang(language)},
                        files={"file": (name, data, mime)})
    elif model == "gigaam":
        r = await _post(conn, f"{conn.base_url}/speech/lite/transcriptions", LITE_TIMEOUT_SEC,
                        data={"model": GATEWAY["gigaam"], "response_format": "json"}, files={"file": (name, data, mime)})
    else:
        r = await _post(conn, f"{conn.base_url}/speech/transcriptions", ASR_TIMEOUT_SEC,
                        data={"model": GATEWAY["whisperx"], **_lang(language), "response_format": "json"},
                        files={"file": (name, data, mime)})
    return _text(r)


async def say(conn, model: str, text: str, voice, language: "str | None" = None) -> tuple[bytes, str]:
    """Одна фраза: srvllm — `voice` = (WAV образца, его текст), ответ WAV; внешний — имя голоса, mp3.
    `language` — код из TTS_LANGUAGES (только шлюз; нет — TTS_LANGUAGE); внешний сервис язык не принимает."""
    if conn.kind == "srvllm":
        route = TTS_ROUTES.get(model)
        if route is None:
            raise SpeechError("неизвестная модель озвучки шлюза")
        wav, ref_text = voice
        r = await _post(conn, f"{conn.base_url}{route}", TTS_TIMEOUT_SEC,
                        data={"text": text, "language": TTS_LANGUAGES.get(language, TTS_LANGUAGE), "ref_text": ref_text},
                        files={"ref_audio": ("voice.wav", wav, "audio/wav")})
        mime = "audio/wav"
    else:
        r = await _post(conn, f"{conn.base_url}/audio/speech", EXTERNAL_TIMEOUT_SEC,
                        json={"model": model, "input": text, "voice": voice, "response_format": "mp3"})
        mime = "audio/mpeg"
    if not r.content or r.headers.get("content-type", "").startswith("application/json"):
        raise SpeechError("служба озвучки не вернула звук")
    return r.content, mime


async def free_card(conn) -> None:
    """Release моделей картинок и gemma-abl перед речью srvllm; итог не важен (занятую дождётся шлюз)."""
    if conn.kind != "srvllm" or not await images.free_models(conn):
        return
    try:
        await images.release_description(conn)
    except (httpx.HTTPError, upstream.UpstreamRefused):
        return


def turns(segments: list[Segment]) -> tuple[list[Segment], list[dict]]:
    """`SPEAKER_xx` → «Собеседник N» по первому появлению; соседние реплики одного собеседника —
    одна. Без собеседников сегменты остаются как есть. Второе — сегменты до склейки
    `{start, end, speaker, text, turn}` (`turn` — номер реплики): по ним SRT."""
    names: dict[str, str] = {}
    out: list[Segment] = []
    raw: list[dict] = []
    for s in segments:
        name = names.setdefault(s.speaker, LABEL.format(len(names) + 1)) if s.speaker is not None else None
        if out and name is not None and out[-1].speaker == name:
            last = out[-1]
            out[-1] = Segment(last.start, s.end, name, f"{last.text} {s.text}")
        else:
            out.append(Segment(s.start, s.end, name, s.text))
        raw.append({"start": s.start, "end": s.end, "speaker": name, "text": s.text, "turn": len(out) - 1})
    return out, raw


def speakers(segments: list[Segment]) -> list[Segment]:
    """Реплики по собеседникам (turns без сегментов до склейки)."""
    return turns(segments)[0]
