"""Реплики по собеседникам у внешних сервисов (спека 2026-11-21-studio-source-capabilities-design, п. 37) и
дополнительные поля формы раздела по схеме инструмента MCP (п. 11, 26)."""

import asyncio
import base64
import json

import httpx

import speech
from fakes import WAV_FAKE
from test_connections import conn_of
from test_mcp import TOKEN, add
from test_speech import cloud

Seg = speech.Segment
ARTIFACT = "art_" + "3" * 32
UPLOADED = "art_" + "4" * 32
SRT = ("1\n00:00:01,000 --> 00:00:02,500\nSpeaker 1: Привет.\n\n"
       "2\n00:00:02,500 --> 00:00:04,000\nпродолжение\n\n"
       "3\n00:01:04,000 --> 00:01:05,250\n[SPEAKER_01]: Ответ.\n")
TRANSCRIBE = {"artifact_id": {"type": "string"},
              "protocol": {"type": "boolean", "default": False},
              "speakers": {"type": "integer", "minimum": 1, "maximum": 10},
              "language": {"type": "string", "enum": ["ru", "en"]},
              "topic": {"type": "string", "maxLength": 40}}


def transcriber(gateway, result, *, properties=TRANSCRIBE):
    """MCP с `audio_transcribe` (поля «Протокол», «Говорящих», «Язык», «Тема») и итогом `result`; файл итога .srt."""
    calls = []

    async def rpc(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            reply = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            tools = {"audio_transcribe": (properties, ["artifact_id"]), "artifact_upload": ({}, [])}
            reply = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": props, "required": need}}
                               for name, (props, need) in tools.items()]}
        else:
            name, args = (payload["params"][key] for key in ("name", "arguments"))
            calls.append((name, args))
            reply = {"structuredContent": {"id": UPLOADED} if name == "artifact_upload" else result}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": reply})

    async def download(request):
        return httpx.Response(200, content=SRT.encode())

    gateway.on["/mcp"] = rpc
    gateway.on[f"/artifacts/{ARTIFACT}/content"] = download
    return calls


def run(studio, sid, arguments):
    audio = {"data": base64.b64encode(WAV_FAKE).decode(), "mime_type": "audio/wav", "name": "talk.wav"}
    return studio.c.post(f"/api/mcp-tools/transcribe/servers/{sid}/run",
                         json={"tool": "audio_transcribe", "arguments": arguments, "files": {"artifact_id": audio}})


def test_diarized_json_and_labeled_text_and_srt_become_turns(studio, monkeypatch, tmp_path):
    # внешний сервис: модель *-diarize* — diarized_json, реплики по speaker
    conn = conn_of(cloud(studio, monkeypatch))
    seen = []

    async def transcriptions(q):
        seen.append(q)
        return httpx.Response(200, json={"text": "Привет. Здравствуйте. Как дела?", "segments": [
            {"type": "transcript.text.segment", "id": "seg_1", "start": 0.0, "end": 1.2, "speaker": "A", "text": " Привет."},
            {"type": "transcript.text.segment", "id": "seg_2", "start": 1.3, "end": 2.0, "speaker": "B", "text": "Здравствуйте."},
            {"type": "transcript.text.segment", "id": "seg_3", "start": 2.1, "end": 3.0, "speaker": "A", "text": "Как дела?"}]})
    studio.gw.on["/v1/audio/transcriptions"] = transcriptions
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3fake")
    segs = speech.speakers(asyncio.run(speech.transcribe(conn, "gpt-4o-transcribe-diarize", f, "audio/mpeg")))
    assert b"\r\n\r\ndiarized_json\r\n" in seen[0].content and b"verbose_json" not in seen[0].content
    assert [(s.speaker, s.text, s.start) for s in segs] == [
        ("Собеседник 1", "Привет.", 0.0), ("Собеседник 2", "Здравствуйте.", 1.3), ("Собеседник 1", "Как дела?", 2.1)]
    asyncio.run(speech.transcribe(conn, "whisper-1", f, "audio/mpeg"))
    assert b"verbose_json" in seen[1].content and b"diarized_json" not in seen[1].content

    # текст с метками: номер собеседника сохраняется, строки без метки — продолжение реплики
    text = "Итоги встречи\nГоворящий 1: Здравствуйте.\nкак дела\nSpeaker 2: Хорошо.\n\nСобеседник 1: Отлично."
    assert speech.segments_from_labeled(text) == [Seg(0.0, 0.0, None, "Итоги встречи"),
        Seg(0.0, 0.0, "Собеседник 1", "Здравствуйте. как дела"), Seg(0.0, 0.0, "Собеседник 2", "Хорошо."),
        Seg(0.0, 0.0, "Собеседник 1", "Отлично.")]
    assert speech.segments_from_labeled("Просто текст.\nБез меток.") == []

    # SRT: время субтитров, метка — у первого субтитра реплики, следующие — тот же собеседник
    assert speech.segments_from_srt(SRT) == [Seg(1.0, 2.5, "Собеседник 1", "Привет."),
        Seg(2.5, 4.0, "Собеседник 1", "продолжение"), Seg(64.0, 65.25, "Собеседник 2", "Ответ.")]
    assert speech.segments_from_srt("1\n00:00:00,000 --> 00:00:01,000\nРаз.\n") == [Seg(0.0, 1.0, None, "Раз.")]
    assert speech.segments_from_srt("Просто текст.") == []

    # результат MCP: строки с метками и файл .srt — реплики по собеседникам для вида расшифровки
    sid = add(studio)
    transcriber(studio.gw, {"text": "Speaker 1: Hi.\nSpeaker 2: Hello."})
    reply = run(studio, sid, {})
    assert reply.status_code == 200, reply.text
    assert reply.json()["turns"] == [{"start": 0.0, "end": 0.0, "speaker": "Собеседник 1", "text": "Hi."},
                                     {"start": 0.0, "end": 0.0, "speaker": "Собеседник 2", "text": "Hello."}]
    transcriber(studio.gw, {"text": "Готово", "artifacts": [
        {"id": ARTIFACT, "mime_type": "application/x-subrip", "name": "talk.srt", "download_path": f"/artifacts/{ARTIFACT}/content"}]})
    reply = run(studio, sid, {}).json()
    assert reply["turns"] == [{"start": 1.0, "end": 4.0, "speaker": "Собеседник 1", "text": "Привет. продолжение"},
                              {"start": 64.0, "end": 65.25, "speaker": "Собеседник 2", "text": "Ответ."}]
    [file] = reply["files"]
    assert file["name"] == "talk.srt"
    download = studio.c.get(file["url"])
    assert download.status_code == 200 and "talk.srt" in download.headers["content-disposition"]
    transcriber(studio.gw, {"text": "Просто текст без собеседников."})
    assert "turns" not in run(studio, sid, {}).json()


def test_mcp_extra_fields_pass_bounds_check(studio):
    sid = add(studio)
    calls = transcriber(studio.gw, {"text": "ok"})
    good = {"protocol": True, "speakers": 3, "language": "en", "topic": "Планёрка"}
    reply = run(studio, sid, good)
    assert reply.status_code == 200, reply.text
    assert calls[-1] == ("audio_transcribe", {**good, "artifact_id": UPLOADED})
    refused = [({"speakers": 11}, "Значение «speakers» — от 1 до 10"), ({"speakers": 2.5}, "Значение «speakers» — целое число"),
               ({"protocol": "yes"}, "Значение «protocol» — да или нет"), ({"protocol": 1}, "Значение «protocol» — да или нет"),
               ({"language": "de"}, "Значение «language» — не из списка инструмента"),
               ({"topic": "т" * 41}, "Значение «topic» — строка до 40 знаков"), ({"topic": 5}, "Значение «topic» — строка до 40 знаков")]
    calls.clear()
    for arguments, words in refused:
        reply = run(studio, sid, arguments)
        assert reply.status_code == 422 and reply.json()["detail"]["message"] == words, (arguments, reply.text)
    assert [name for name, _ in calls if name != "artifact_upload"] == []
