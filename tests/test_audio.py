"""Звук студии (app/audio.py) настоящим ffmpeg: длительность, сжатие перед распознаванием,
образец голоса в WAV, склейка озвучки. Нужен ffmpeg на PATH — integration."""
import array
import asyncio
import math
import re
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio
import state

pytestmark = pytest.mark.integration


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "CFG", SimpleNamespace(tmp_dir=tmp_path / "tmp"))
    return tmp_path


def silence(path: Path, seconds: float, rate: int = 44100) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00\x00\x00" * int(rate * seconds))
    return path


def stream(path: Path) -> str:
    """Кодек, частота и каналы первой звуковой дорожки — из описания файла ffmpeg."""
    info = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    codec, rate, layout = re.search(r"Audio: (\w+)[^,]*, (\d+) Hz, (\w+)", info).groups()
    return f"{codec},{rate},{1 if layout == 'mono' else layout}"


def test_probe_and_compress_to_small_mono_mp3(tmp):
    src = silence(tmp / "call.wav", 2.0)
    assert asyncio.run(audio.probe(src)) == pytest.approx(2.0, abs=0.05)
    dst = tmp / "call.mp3"
    asyncio.run(audio.compress(src, dst))
    assert asyncio.run(audio.probe(dst)) == pytest.approx(2.0, abs=0.15)
    assert stream(dst) == "mp3,16000,1"
    assert dst.stat().st_size < src.stat().st_size / 10


def test_to_wav_cuts_to_the_limit(tmp):
    src = silence(tmp / "voice.wav", 3.0)
    dst = tmp / "voice-24k.wav"
    assert asyncio.run(audio.to_wav(src, dst, max_sec=1.0)) == pytest.approx(1.0, abs=0.05)
    assert stream(dst) == "pcm_s16le,24000,1"


def test_mp3_of_joins_parts(tmp):
    parts = [silence(tmp / "a.wav", 1.0, 24000), silence(tmp / "b.wav", 1.5, 24000)]
    dst = tmp / "speech.mp3"
    asyncio.run(audio.mp3_of(parts, dst))
    assert asyncio.run(audio.probe(dst)) == pytest.approx(2.5, abs=0.15)


def test_browser_webm_without_duration_is_measured(tmp):
    """Запись MediaRecorder (webm) длительности в заголовке не несёт — мерится декодированием."""
    src = silence(tmp / "a.wav", 1.5)
    webm = tmp / "rec.webm"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src), "-c:a", "libopus", str(webm)], check=True)
    assert asyncio.run(audio.probe(webm)) == pytest.approx(1.5, abs=0.1)


def tone_after_silence(path: Path, silent: float, loud: float, rate: int = 16000) -> Path:
    """Моно WAV: `silent` с тишины, затем `loud` с тона 1 кГц."""
    samples = [0] * int(rate * silent) + [int(16000 * math.sin(2 * math.pi * 1000 * i / rate)) for i in range(int(rate * loud))]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array("h", samples).tobytes())
    return path


def peaks(path: Path, window: float = 0.05) -> list[int]:
    """Наибольшая громкость каждого окна `window` с записи (PCM 16 кГц моно)."""
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000",
                          "pipe:1"], capture_output=True, check=True).stdout
    data, size = array.array("h", pcm), int(16000 * window)
    return [max(abs(x) for x in data[i:i + size]) for i in range(0, len(data), size)]


def test_parts_of_a_cueless_webm_are_cut_exactly_from_the_compressed_mp3(tmp):
    """Длинная «Расшифровка» (ревью C-M12): webm MediaRecorder — без индекса (здесь — вывод в поток, как у браузера);
    запись один раз сжимается в CBR mp3, части режутся из него — граница части на своём месте с точностью до 30 мс."""
    src = tone_after_silence(tmp / "a.wav", 30.0, 10.0)
    webm = tmp / "rec.webm"
    with webm.open("wb") as out:                       # в поток: muxer не пишет ни индекса, ни длительности
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src), "-c:a", "libopus", "-f", "webm", "pipe:1"],
                       stdout=out, check=True)
    whole = tmp / "whole.mp3"
    asyncio.run(audio.compress(webm, whole))
    assert stream(whole) == "mp3,16000,1"
    quiet, edge = tmp / "quiet.mp3", tmp / "edge.mp3"
    asyncio.run(audio.cut(whole, 28.0, 1.9, quiet))
    asyncio.run(audio.cut(whole, 29.0, 2.0, edge))
    assert max(peaks(quiet)) < 2000                   # до тона — тишина целиком
    assert asyncio.run(audio.probe(edge)) == pytest.approx(2.0, abs=0.1)
    onset = next(i for i, peak in enumerate(peaks(edge, 0.01)) if peak > 8000) * 0.01
    assert onset == pytest.approx(1.0, abs=0.03)      # тон начинается на 30-й секунде записи — через 1 с от начала части


def test_garbage_is_a_plain_error_without_paths(tmp):
    bad = tmp / "secret-name.m4a"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(audio.AudioError) as e:
        asyncio.run(audio.compress(bad, tmp / "out.mp3"))
    assert "secret-name" not in str(e.value) and str(tmp) not in str(e.value)
