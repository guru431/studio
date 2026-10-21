"""Распознавание и озвучка: шлюз srvllm и внешний сервис OpenAI-стиля (спека 2026-10-20-studio-
sections-voice-design, 6.1 и 6.3–6.5; контракт шлюза сверен с llm_routers 2026-10-20).

- srvllm: расшифровка — `/speech/asr` WhisperX с собеседниками (запрос — ASR_QUERY, поле файла
  `audio_file`; текст собирается из сегментов, `speaker` может не быть); голосовой ввод — GigaAM
  `/speech/lite/transcriptions` или WhisperX `/speech/transcriptions` (`file`); озвучка — Qwen3-TTS
  `/speech/tts-qwen` (`text`, `language`, `ref_text`, `ref_audio` — WAV образца; ответ — WAV).
- Внешний сервис: `/audio/transcriptions` (расшифровка — `verbose_json`, без собеседников; ввод —
  `json`) и `/audio/speech` (JSON с голосом по имени; ответ — mp3).
- 503 (карта занята, `busy` Qwen) — Busy: ждать и повторять решает вызывающий; прочие отказы —
  SpeechError словами. Освобождения слотов `asr` и `tts-qwen` у шлюза нет: перед речью студия
  освобождает модели картинок и gemma-abl (`free_card`), чтобы карта досталась речи.
- Звук, текст и имена в журнал не пишутся.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

import images
import upstream

GATEWAY = {"whisperx": "large-v3", "gigaam": "gigaam-v3"}
ASR_QUERY = {"language": "ru", "diarize": "true", "word_timestamps": "true", "output_format": "json",
             "model": GATEWAY["whisperx"]}
ASR_TIMEOUT_SEC = 2460          # таймаут LiteLLM маршрута /speech/asr и /speech/transcriptions
LITE_TIMEOUT_SEC = 180
TTS_TIMEOUT_SEC = 720
EXTERNAL_TIMEOUT_SEC = 600
TTS_LANGUAGE = "Russian"
LABEL = "Собеседник {}"          # метка собеседника по порядку первого появления (с 1)


class Busy(Exception):
    """Карта занята (503 шлюза) или Qwen озвучивает другое: повторить позже."""


class SpeechError(Exception):
    """Отказ службы речи — словами для человека."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speaker: "str | None"
    text: str


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
        raise SpeechError(upstream.gateway_error(r, conn.name))
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


def _text(r: httpx.Response) -> str:
    text = upstream.json_body(r).get("text")
    return text.strip() if isinstance(text, str) else ""


async def transcribe(conn, model: str, audio: Path, mime: str) -> list[Segment]:
    """Расшифровка сжатой записи (audio.compress) — сегменты по порядку."""
    data = await asyncio.to_thread(audio.read_bytes)
    if conn.kind == "srvllm":
        r = await _post(conn, f"{conn.base_url}/speech/asr?{urlencode(ASR_QUERY)}", ASR_TIMEOUT_SEC,
                        files={"audio_file": ("audio.mp3", data, mime)})
    else:
        r = await _post(conn, f"{conn.base_url}/audio/transcriptions", EXTERNAL_TIMEOUT_SEC,
                        data={"model": model, "response_format": "verbose_json"},
                        files={"file": ("audio.mp3", data, mime)})
    return _segments(upstream.json_body(r))


async def dictate(conn, model: str, data: bytes, mime: str, name: str) -> str:
    """Голосовой ввод: запись как есть (m4a iPhone, webm Chrome) — текст."""
    if conn.kind != "srvllm":
        r = await _post(conn, f"{conn.base_url}/audio/transcriptions", EXTERNAL_TIMEOUT_SEC,
                        data={"model": model, "response_format": "json"}, files={"file": (name, data, mime)})
    elif model == "gigaam":
        r = await _post(conn, f"{conn.base_url}/speech/lite/transcriptions", LITE_TIMEOUT_SEC,
                        data={"model": GATEWAY["gigaam"], "response_format": "json"}, files={"file": (name, data, mime)})
    else:
        r = await _post(conn, f"{conn.base_url}/speech/transcriptions", ASR_TIMEOUT_SEC,
                        data={"model": GATEWAY["whisperx"], "language": "ru", "response_format": "json"},
                        files={"file": (name, data, mime)})
    return _text(r)


async def say(conn, model: str, text: str, voice) -> tuple[bytes, str]:
    """Одна фраза: srvllm — `voice` = (WAV образца, его текст), ответ WAV; внешний — имя голоса, mp3."""
    if conn.kind == "srvllm":
        wav, ref_text = voice
        r = await _post(conn, f"{conn.base_url}/speech/tts-qwen", TTS_TIMEOUT_SEC,
                        data={"text": text, "language": TTS_LANGUAGE, "ref_text": ref_text},
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
    if conn.kind != "srvllm":
        return
    try:
        for model in images.MODELS.values():
            await images.release(conn, model)
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
