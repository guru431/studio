"""Длинные записи «Расшифровки» частями по живым пределам источника (`speech.limits`, `audio.cut`) и язык
распознавания (`transcripts.language`, поле `language` по виду подключения)."""
import asyncio
import json

import httpx
import pytest

import speech
import state
import transcripts
from conftest import add_gateway
from test_connections import conn_of
from test_s06 import create as create_s06
from test_speech import cloud
from test_transcripts import upload, wait_tr

MARKER_2 = "— Часть 2 (с 00:10:00): собеседники пронумерованы заново —"


def limits_route(studio, seconds=600):
    seen = []

    async def limits(q):
        seen.append(q)
        return httpx.Response(200, json={"max_seconds": seconds, "max_bytes": 25 * 1024 * 1024})
    studio.gw.on["/speech/limits"] = limits
    return seen


def parts(*replies):
    return [(200, {"text": "", "segments": [{"start": a, "end": b, "speaker": who, "text": text}
                                             for a, b, who, text in reply]}) for reply in replies]


def test_long_recording_split_by_gateway_limits_and_times_shifted(studio, fake_audio):
    add_gateway(studio)
    asked = limits_route(studio)
    assert studio.c.get("/api/transcripts/limits").json() == {"max_seconds": 600, "total_max": transcripts.TOTAL_MAX,
                                                              "total_slack": transcripts.TOTAL_SLACK}
    fake_audio.duration = 1500.0
    studio.gw.asr_replies = parts([(0.0, 2.0, "SPEAKER_00", "Один.")], [(1.0, 3.0, "SPEAKER_01", "Два.")],
                                  [(0.5, 1.0, "SPEAKER_00", "Три.")])
    details = []

    async def asr(q):
        details.append(transcripts.detail(next(iter(transcripts.JOBS.values()))))
        return await studio.gw.builtin(q)
    studio.gw.on["/speech/asr"] = asr
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "done", item["detail"]
    assert [c for c in fake_audio.calls if c[0] in ("cut", "compress")] == [
        ("compress",), ("cut", 0.0, 500.0), ("cut", 500.0, 500.0), ("cut", 1000.0, 500.0)]   # равные, не длиннее предела
    assert [d.split(" · ")[:2] for d in details] == [["распознаю", "часть 1 из 3"], ["распознаю", "часть 2 из 3"],
                                                    ["распознаю", "часть 3 из 3"]]
    assert [(s["start"], s["end"], s["speaker"], s["text"]) for s in item["segments"]] == [
        (0.0, 2.0, "Собеседник 1", "Один."),
        (500.0, 500.0, None, "— Часть 2 (с 00:08:20): собеседники пронумерованы заново —"),
        (501.0, 503.0, "Собеседник 1", "Два."),
        (1000.0, 1000.0, None, "— Часть 3 (с 00:16:40): собеседники пронумерованы заново —"),
        (1000.5, 1001.0, "Собеседник 1", "Три.")]
    assert len(asked) == 1                            # предел — из кэша на 600 с


def test_no_limits_route_falls_back_to_duration_max(studio, fake_audio, monkeypatch):
    conn = conn_of(add_gateway(studio))
    assert asyncio.run(speech.limits(conn, "whisperx")) == speech.Limits(transcripts.DURATION_MAX)
    assert asyncio.run(speech.limits(conn, "whisperx")) == speech.Limits(transcripts.DURATION_MAX)
    assert [q.url.path for q in studio.gw.requests].count("/speech/limits") == 1
    external = conn_of(cloud(studio, monkeypatch))
    before = len(studio.gw.requests)
    assert asyncio.run(speech.limits(external, "whisper-1")) == speech.Limits(transcripts.DURATION_MAX)
    assert len(studio.gw.requests) == before          # у внешнего сервиса маршрута пределов нет — не спрашиваем
    fake_audio.duration = 4000.0
    studio.gw.asr_replies = parts([(0.0, 1.0, None, "Начало.")], [(0.0, 1.0, None, "Конец.")])
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "done", item["detail"]
    assert [c for c in fake_audio.calls if c[0] in ("cut", "compress")] == [
        ("compress",), ("cut", 0.0, 2000.0), ("cut", 2000.0, 2000.0)]


def test_recording_at_the_limit_is_accepted_within_the_slack(studio, fake_audio):
    """Запись встречи встаёт на паузу у предела, а ffmpeg меряет её чуть длиннее времени страницы: в пределах
    `TOTAL_SLACK` сверх 4 ч она расшифровывается, а не застревает на устройстве (ревью C-I1)."""
    add_gateway(studio)
    limits_route(studio)
    assert studio.c.get("/api/transcripts/limits").json()["total_slack"] == transcripts.TOTAL_SLACK
    fake_audio.duration = transcripts.TOTAL_MAX + 0.25
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "done", item["detail"]


def test_over_total_max_refused_before_work(studio, fake_audio):
    add_gateway(studio)
    limits_route(studio)
    fake_audio.duration = transcripts.TOTAL_MAX + transcripts.TOTAL_SLACK + 1.0
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "error" and item["detail"] == "запись длиннее 4 ч — разделите её"
    paths = [q.url.path for q in studio.gw.requests]
    assert "/speech/asr" not in paths and "/speech/limits" not in paths
    assert not [c for c in fake_audio.calls if c[0] in ("cut", "compress")]


def test_tiny_tail_does_not_become_its_own_part(studio, fake_audio):
    """Запись чуть длиннее кратного пределу: части равные, без 50-мс хвоста — внешний сервис отказал бы ему
    (у OpenAI минимум 0,1 с), и вместе с ним пропали бы уже распознанные часы (ревью C-M3)."""
    add_gateway(studio)
    limits_route(studio)
    fake_audio.duration = 1200.05
    studio.gw.asr_replies = parts(*[[(0.0, 1.0, None, f"Часть {n}.")] for n in range(3)])
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "done", item["detail"]
    cuts = [c for c in fake_audio.calls if c[0] == "cut"]
    assert len(cuts) == 3 and all(c[2] == pytest.approx(1200.05 / 3) for c in cuts), cuts
    assert [c[1] for c in cuts] == pytest.approx([0.0, 1200.05 / 3, 2 * 1200.05 / 3])
    assert all(c[2] <= 600 for c in cuts)


def test_long_recording_compressed_once_and_parts_cut_from_the_mp3(studio, fake_audio, monkeypatch):
    """webm браузера без индекса и VBR mp3 ищутся неточно: источник один раз сжимается в CBR mp3 16 кГц, части
    режутся из него (точный и дешёвый поиск), исходник после сжатия не трогается (ревью C-M12)."""
    import audio
    add_gateway(studio)
    limits_route(studio)
    fake_audio.duration = 1500.0
    seen = []
    compress, cut = audio.compress, audio.cut

    async def compress_spy(src, dst):
        seen.append(("compress", src, dst))
        await compress(src, dst)

    async def cut_spy(src, start, length, dst):
        seen.append(("cut", src, dst))
        await cut(src, start, length, dst)
    monkeypatch.setattr(audio, "compress", compress_spy)
    monkeypatch.setattr(audio, "cut", cut_spy)
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "done", item["detail"]
    assert [s[0] for s in seen] == ["compress", "cut", "cut", "cut"], seen
    _, source, whole = seen[0]
    assert str(whole).endswith(".mp3") and whole != source
    assert all(src == whole and dst != whole for _, src, dst in seen[1:]), seen
    assert not whole.exists()                         # сжатая запись целиком — временная, убрана


def test_part_marker_between_parts(studio, fake_audio):
    add_gateway(studio)
    limits_route(studio)
    fake_audio.duration = 1200.0
    studio.gw.asr_replies = parts([(0.0, 1.0, "SPEAKER_00", "Первая.")], [(2.0, 4.0, "SPEAKER_00", "Вторая.")])
    tid = upload(studio).json()["id"]
    item = wait_tr(studio, tid, {"done", "error"})
    assert [s["text"] for s in item["segments"]] == ["Первая.", MARKER_2, "Вторая."]
    assert [s.get("system", False) for s in item["segments"]] == [False, True, False]
    assert item["speakers"] == 1
    txt = studio.c.get(f"/api/transcripts/{tid}/export?format=txt").text
    assert txt.index("Первая.") < txt.index(MARKER_2) < txt.index("Вторая.")
    srt = studio.c.get(f"/api/transcripts/{tid}/export?format=srt").text
    assert "Собеседник 1: Вторая." in srt and "00:10:02,000" in srt
    for fmt in ("txt", "srt"):
        translated = studio.c.get(f"/api/transcripts/{tid}/export?format={fmt}&lang=en").text
        assert "Part 2 (from 00:10:00): speaker numbering restarts" in translated
        assert "Первая." in translated and MARKER_2 not in translated


def test_language_sent_per_connection_kind_and_omitted_for_auto(studio, fake_audio, tmp_path, monkeypatch):
    gateway = conn_of(add_gateway(studio))
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    for language in ("en", None):
        asyncio.run(speech.transcribe(gateway, "whisperx", f, "audio/mpeg", language=language))
        assert studio.gw.asr_requests[-1].url.params.get("language") == language
        asyncio.run(speech.dictate(gateway, "whisperx", b"a", "audio/webm", "a.webm", language=language))
        assert (b'name="language"\r\n\r\nen' in studio.gw.asr_requests[-1].content) == (language == "en")
        assert (b'name="language"' in studio.gw.asr_requests[-1].content) == bool(language)
    s06 = conn_of(create_s06(studio).json()["id"])
    for language in ("ru", None):
        asyncio.run(speech.transcribe(s06, "whisperx", f, "audio/mpeg", language=language))
        body = studio.gw.asr_requests[-1].content
        assert (b'name="language"\r\n\r\nru' in body) == (language == "ru") and (b'name="language"' in body) == bool(language)
        asyncio.run(speech.dictate(s06, "whisperx", b"a", "audio/webm", "a.webm", language=language))
        assert (b'name="language"' in studio.gw.asr_requests[-1].content) == bool(language)
    external = conn_of(cloud(studio, monkeypatch))
    seen = []

    async def transcriptions(q):
        seen.append(q)
        return httpx.Response(200, json={"text": "целиком", "segments": [{"start": 0, "end": 1, "text": "целиком"}]})
    studio.gw.on["/v1/audio/transcriptions"] = transcriptions
    for language in ("en", None):
        asyncio.run(speech.transcribe(external, "whisper-1", f, "audio/mpeg", language=language))
        asyncio.run(speech.dictate(external, "whisper-1", b"a", "audio/webm", "a.webm", language=language))
        for q in seen[-2:]:
            assert (b'name="language"\r\n\r\nen' in q.content) == (language == "en")
            assert (b'name="language"' in q.content) == bool(language)
    # загрузка и «Расшифровать ещё раз» помнят язык записи; «Авто» — без поля
    r = studio.c.post("/api/transcripts", content=b"\x00" * 64,
                      headers={"content-type": "audio/mp4", "x-studio-name": "a.m4a", "x-studio-language": "en",
                               "x-studio-connection": str(gateway.id), "x-studio-model": "whisperx"})
    assert r.status_code == 202 and r.json()["language"] == "en"
    wait_tr(studio, r.json()["id"], {"done"})
    assert studio.gw.asr_requests[-1].url.params.get("language") == "en"
    again = studio.c.post(f"/api/transcripts/{r.json()['id']}/retry",
                          json={"connection_id": gateway.id, "model": "whisperx", "language": None})
    assert again.status_code == 202 and again.json()["language"] is None
    wait_tr(studio, again.json()["id"], {"done"})
    assert "language" not in studio.gw.asr_requests[-1].url.params
    assert state.DB.execute("SELECT language FROM transcripts WHERE id = ?", (again.json()["id"],)).fetchone()[0] is None
    bad = studio.c.post("/api/transcripts", content=b"\x00", headers={"content-type": "audio/mp4", "x-studio-language": "xx"})
    assert bad.status_code == 422
    # голосовой ввод: язык устройства — поле формы `language` до модели раздела «Голосовой ввод»
    assert studio.c.put("/api/sections/dictation/default", json={"connection_id": gateway.id, "model": "whisperx"}).status_code == 200
    voice = {"audio": ("voice.webm", b"\x00" * 64, "audio/webm")}
    assert studio.c.post("/api/dictation", files=voice, data={"language": "en"}).status_code == 200
    assert b'name="language"\r\n\r\nen' in studio.gw.asr_requests[-1].content
    assert studio.c.post("/api/dictation", files=voice).status_code == 200
    assert b'name="language"' not in studio.gw.asr_requests[-1].content
    assert studio.c.post("/api/dictation", files=voice, data={"language": "xx"}).status_code == 422


def test_mcp_transcribe_tool_limits_reach_the_page(studio):
    """У MCP Studio запись не режет — только сверяет предел: `_meta["studio/limits"]` инструмента — в каталоге раздела."""
    from test_mcp import add
    from test_mcp_sections import service
    add(studio)
    service(studio.gw, uploads=True)
    plain = studio.gw.on["/mcp"]

    async def rpc(request):
        reply = await plain(request)
        if not reply.content:
            return reply
        body = json.loads(reply.content)
        for tool in body.get("result", {}).get("tools", []):
            if tool["name"] == "audio_transcribe":
                tool["_meta"] = {"studio/limits": {"max_seconds": 3600}}
        return httpx.Response(reply.status_code, json=body)
    studio.gw.on["/mcp"] = rpc
    tools = {t["name"]: t for s in studio.c.get("/api/mcp-tools/transcribe").json()["items"] for t in s["tools"]}
    assert tools["audio_transcribe"]["limits"] == {"max_seconds": 3600}
    assert speech.tool_limits({"_meta": {"studio/limits": {"max_seconds": "3600"}}}) is None
