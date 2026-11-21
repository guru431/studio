"""Распознавание и озвучка (app/speech.py): шлюз srvllm (WhisperX, GigaAM, Qwen3-TTS) и внешний
сервис OpenAI-стиля; освобождение карты перед речью."""
import asyncio
import json
import threading

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
    segs = speech.speakers(asyncio.run(speech.transcribe(conn, "whisperx", f, "audio/mpeg", language="ru")))
    q = studio.gw.asr_requests[0]
    assert q.url.path == "/speech/asr" and dict(q.url.params) == {"language": "ru", "diarize": "true",
        "word_timestamps": "true", "output_format": "json", "model": "large-v3"}
    assert b'name="audio_file"' in q.content
    assert [(s.speaker, s.text, s.start, s.end) for s in segs] == [
        ("Собеседник 1", "Бюро переводов. Здравствуйте.", 0.9, 4.0), ("Собеседник 2", "У меня вопрос.", 4.1, 6.0)]


def test_transcribe_cancellation_waits_for_the_audio_reader(tmp_path):
    """Отмена не отдаёт временный MP3 уборке, пока поток держит файл открытым."""
    path = tmp_path / "a.mp3"
    path.write_bytes(b"ID3fake")
    reading, release = threading.Event(), threading.Event()

    class HeldAudio:
        def read_bytes(self):
            with path.open("rb") as source:
                reading.set()
                assert release.wait(5)
                return source.read()

    async def scenario():
        task = asyncio.create_task(speech.transcribe(None, "whisperx", HeldAudio(), "audio/mpeg"))
        try:
            assert await asyncio.to_thread(reading.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "отмена вернулась при открытом MP3"
        finally:
            release.set()
            [result] = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)
        path.unlink()

    asyncio.run(scenario())


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
    assert sorted(m for m, _ in studio.gw.releases) == ["flux-3-image-klein", "gemma4-abl", "snofs-krea2-v1.6"]


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


def test_external_transcription_falls_back_to_json_for_models_without_segments(studio, monkeypatch, tmp_path):
    """gpt-4o-transcribe и gpt-4o-mini-transcribe не принимают `verbose_json` (400): запись расшифровывается
    повтором с `json` — одной репликой без времени; другой отказ после повтора — словами сервиса."""
    add_gateway(studio)
    conn = conn_of(cloud(studio, monkeypatch))
    seen = []

    async def transcriptions(q):
        seen.append(q)
        if b"verbose_json" in q.content:
            return httpx.Response(400, json={"error": {"message": "response_format 'verbose_json' is not compatible",
                                                       "param": "response_format"}})
        return httpx.Response(200, json={"text": " целиком "})
    studio.gw.on["/v1/audio/transcriptions"] = transcriptions
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    segs = asyncio.run(speech.transcribe(conn, "gpt-4o-transcribe", f, "audio/mpeg"))
    assert [(s.speaker, s.text, s.start) for s in segs] == [(None, "целиком", 0.0)]
    assert [b"verbose_json" in q.content for q in seen] == [True, False]
    assert b"\r\n\r\njson\r\n" in seen[1].content

    async def refused(q):
        seen.append(q)
        return httpx.Response(400, json={"error": {"message": "плохой файл"}})
    studio.gw.on["/v1/audio/transcriptions"] = refused
    seen.clear()
    with pytest.raises(speech.SpeechError) as e:
        asyncio.run(speech.transcribe(conn, "whisper-1", f, "audio/mpeg"))
    assert "плохой файл" in str(e.value) and len(seen) == 2


def test_ffmpeg_gets_no_environment_of_the_service(monkeypatch):
    """ffmpeg разбирает загруженные записи: окружения службы (STUDIO_DATA_KEY) он не видит, как yt-dlp и md2pdf."""
    import audio
    monkeypatch.setenv("STUDIO_DATA_KEY", "секрет-службы")
    seen = {}

    class Done:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def spawn(*argv, **kwargs):
        seen.update(kwargs)
        return Done()

    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(audio._run([audio.FFMPEG, "-version"]))
    env = seen["env"]
    assert "STUDIO_DATA_KEY" not in env and set(env) <= {"PATH", "LANG", "HOME", "SYSTEMROOT"}
    assert env["HOME"] == "/nonexistent" and env["LANG"] == "C.UTF-8"
