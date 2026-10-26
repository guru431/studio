"""Распознавание и озвучка (app/speech.py): шлюз srvllm (WhisperX, GigaAM, Qwen3-TTS) и внешний
сервис OpenAI-стиля; освобождение карты перед речью."""
import asyncio
import json

import httpx
import pytest

import speech
import state
import upstream
from conftest import add_connection, add_gateway
from test_connections import conn_of


def cloud(studio, monkeypatch) -> int:
    """Внешний сервис речи по имени в списке: адрес имени подменён публичным."""
    state.DB.execute("UPDATE settings SET value = '[\"10.77.1.15:4000\", \"voice.cloud.test:443\"]' WHERE key = 'allowlist'")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    return add_connection(studio, base_url="https://voice.cloud.test/v1", token="cloud-key-1",
                          sections=["transcribe", "speech", "dictation"])


def test_transcribe_asks_whisperx_with_diarization(studio, tmp_path):
    conn = conn_of(add_gateway(studio))
    studio.gw.asr_reply = {"language": "ru", "text": [], "segments": [
        {"start": 0.9, "end": 3.1, "speaker": "SPEAKER_01", "text": " Бюро переводов."},
        {"start": 3.2, "end": 4.0, "speaker": "SPEAKER_01", "text": "Здравствуйте."},
        {"start": 4.1, "end": 6.0, "speaker": "SPEAKER_00", "text": "У меня вопрос."}]}
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    segs = speech.speakers(asyncio.run(speech.transcribe(conn, "whisperx", f, "audio/mpeg")))
    q = studio.gw.asr_requests[0]
    assert q.url.path == "/speech/asr" and dict(q.url.params) == {"language": "ru", "diarize": "true",
        "word_timestamps": "true", "output_format": "json", "model": "large-v3"}
    assert b'name="audio_file"' in q.content
    assert [(s.speaker, s.text, s.start, s.end) for s in segs] == [
        ("Собеседник 1", "Бюро переводов. Здравствуйте.", 0.9, 4.0), ("Собеседник 2", "У меня вопрос.", 4.1, 6.0)]


def test_turns_keep_the_recognized_segments_with_their_turn():
    """Сегменты до склейки (для SRT): метка той же нумерации и номер реплики, в которую склеен сегмент;
    по времени не сопоставлять — у WhisperX конец реплики и начало следующей совпадают."""
    Seg = speech.Segment
    replicas, raw = speech.turns([Seg(0.9, 3.1, "SPEAKER_01", "Бюро переводов."), Seg(3.1, 4.0, "SPEAKER_01", "Здравствуйте."),
                                  Seg(4.0, 6.0, "SPEAKER_00", "У меня вопрос."), Seg(6.0, 7.0, None, "Шум.")])
    assert [r.text for r in replicas] == ["Бюро переводов. Здравствуйте.", "У меня вопрос.", "Шум."]
    assert raw == [{"start": 0.9, "end": 3.1, "speaker": "Собеседник 1", "text": "Бюро переводов.", "turn": 0},
                   {"start": 3.1, "end": 4.0, "speaker": "Собеседник 1", "text": "Здравствуйте.", "turn": 0},
                   {"start": 4.0, "end": 6.0, "speaker": "Собеседник 2", "text": "У меня вопрос.", "turn": 1},
                   {"start": 6.0, "end": 7.0, "speaker": None, "text": "Шум.", "turn": 2}]
    assert speech.speakers([Seg(0, 1, "A", "Раз.")]) == [Seg(0, 1, "Собеседник 1", "Раз.")]


def test_transcribe_without_speakers_keeps_the_segments(studio, tmp_path):
    conn = conn_of(add_gateway(studio))
    studio.gw.asr_reply = {"text": [], "segments": [{"start": 0, "end": 1, "text": "Раз."}, {"start": 1, "end": 2, "text": "Два."}]}
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    segs = speech.speakers(asyncio.run(speech.transcribe(conn, "whisperx", f, "audio/mpeg")))
    assert [(s.speaker, s.text) for s in segs] == [(None, "Раз."), (None, "Два.")]


def test_dictation_goes_to_gigaam_and_keeps_the_phone_format(studio):
    conn = conn_of(add_gateway(studio))
    studio.gw.asr_reply = {"text": " кот на окне "}
    text = asyncio.run(speech.dictate(conn, "gigaam", b"\x00" * 64, "audio/mp4", "voice.m4a"))
    q = studio.gw.asr_requests[0]
    assert text == "кот на окне" and q.url.path == "/speech/lite/transcriptions"
    assert b'filename="voice.m4a"' in q.content and b"audio/mp4" in q.content and b'name="file"' in q.content


def test_dictation_by_whisperx_names_the_model(studio):
    conn = conn_of(add_gateway(studio))
    studio.gw.asr_reply = {"text": "привет"}
    assert asyncio.run(speech.dictate(conn, "whisperx", b"\x00" * 64, "audio/webm", "voice.webm")) == "привет"
    q = studio.gw.asr_requests[0]
    assert q.url.path == "/speech/transcriptions" and b"large-v3" in q.content


def test_busy_card_is_busy_and_other_refusals_are_words(studio, tmp_path):
    conn = conn_of(add_gateway(studio))
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    studio.gw.asr_replies = [(503, {"error": "слот asr не взят: no_capacity"}), (400, {"detail": "плохой файл"})]
    with pytest.raises(speech.Busy):
        asyncio.run(speech.transcribe(conn, "whisperx", f, "audio/mpeg"))
    with pytest.raises(speech.SpeechError) as e:
        asyncio.run(speech.transcribe(conn, "whisperx", f, "audio/mpeg"))
    assert "плохой файл" in str(e.value)


def test_qwen_busy_is_busy_and_wav_comes_back(studio):
    conn = conn_of(add_gateway(studio))
    studio.gw.tts_replies = [(503, {"detail": "busy: идёт другая генерация"}), (200, b"RIFFwav")]
    with pytest.raises(speech.Busy):
        asyncio.run(speech.say(conn, "qwen3-tts", "Привет.", (b"RIFFref", "образец")))
    data, mime = asyncio.run(speech.say(conn, "qwen3-tts", "Привет.", (b"RIFFref", "образец")))
    q = studio.gw.tts_requests[-1]
    assert (data, mime) == (b"RIFFwav", "audio/wav") and q.url.path == "/speech/tts-qwen"
    for field in (b'name="text"', b'name="language"', b"Russian", b'name="ref_text"', b'name="ref_audio"'):
        assert field in q.content


def test_free_card_releases_images_and_gemma(studio):
    conn = conn_of(add_gateway(studio))
    asyncio.run(speech.free_card(conn))
    assert sorted(m for m, _ in studio.gw.releases) == ["flux-3-image-klein", "gemma4-abl", "lustify-v10-krea2-turbo"]


def test_external_service_speaks_openai(studio, monkeypatch, tmp_path):
    add_gateway(studio)
    conn = conn_of(cloud(studio, monkeypatch))
    seen = []
    async def transcriptions(q):
        seen.append(q)
        return httpx.Response(200, json={"text": "целиком", "segments": [{"start": 0, "end": 1.5, "text": "целиком"}]})
    async def speak(q):
        seen.append(q)
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})
    studio.gw.on["/v1/audio/transcriptions"] = transcriptions
    studio.gw.on["/v1/audio/speech"] = speak
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    segs = asyncio.run(speech.transcribe(conn, "whisper-1", f, "audio/mpeg"))
    assert [(s.speaker, s.text) for s in segs] == [(None, "целиком")]
    assert b"verbose_json" in seen[0].content and seen[0].headers["host"] == "voice.cloud.test"
    assert asyncio.run(speech.say(conn, "tts-1", "Привет.", "alloy")) == (b"ID3mp3", "audio/mpeg")
    assert json.loads(seen[1].content) == {"model": "tts-1", "input": "Привет.", "voice": "alloy", "response_format": "mp3"}
    asyncio.run(speech.free_card(conn))
    assert studio.gw.releases == []                         # карты студии у внешнего сервиса нет
