"""s06 gateway contracts: native ASR/OCR, stable chat aliases and server-only credentials."""
import asyncio
import base64
import json

import httpx
import pytest

import card
import connections
import db
import ocr
import sections
import speech
import state
import translate_text
import upstream
from conftest import add_gateway, make_user, signin
from fakes import PNG_REAL, TOKEN
from test_chat import sse, turn

BASE = "http://10.77.1.15:4000"


def create(studio, **overrides):
    body = {"name": "s06", "kind": "openai", "s06": True, "base_url": BASE, "token": TOKEN,
            "sections": ["chat", "translate", "transcribe", "ocr"], **overrides}
    return studio.c.post("/api/connections", json=body)


@pytest.fixture
def s06(studio):
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    response = create(studio)
    assert response.status_code == 201, response.text
    return connections.get(1, response.json()["id"])


def test_settings_keep_existing_defaults_and_hide_the_key(studio):
    original = add_gateway(studio)
    defaults = studio.c.get("/api/sections").json()["sections"]
    response = create(studio)
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    assert response.json()["s06"] and "token" not in response.json()
    for section in studio.c.get("/api/sections").json()["sections"]:
        assert section["default"] == next(s["default"] for s in defaults if s["id"] == section["id"])
        if section["id"] in ("chat", "translate", "transcribe", "ocr"):
            link = next(l for l in section["links"] if l["connection"]["id"] == cid)
            assert link["models"] == sections.S06_CATALOG[section["id"]]
            assert section["default"]["connection_id"] == original
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403
    conn = connections.get(1, cid)
    assert connections.chat_url(conn) == BASE + "/v1/chat/completions"
    assert connections.models_url(conn) == BASE + "/v1/models"


def test_invalid_profiles_and_unavailable_models_are_rejected(studio, s06):
    for bad in ({"cleaner": True}, {"kind": "srvllm"}, {"base_url": BASE + "/v1"},
                {"base_url": "https://unlisted.test"}, {"sections": ["speech"]}, {"sections": ["photo"]}):
        assert create(studio, **bad).status_code == 422, bad
    for section, model in (("chat", "qwen-old-version"), ("ocr", "hunyuan-ocr")):
        response = studio.c.put(f"/api/sections/{section}/links/{s06.id}", json={"models": [model]})
        assert response.status_code == 422
    response = studio.c.post("/api/sections/speech/links", json={"connection_id": s06.id})
    assert response.status_code == 422


def test_snapshot_and_passport_are_invalidated_by_a_profile_change(studio, s06):
    snap = connections.snapshot(s06)
    assert studio.c.patch(f"/api/connections/{s06.id}", json={"s06": False, "sections": ["chat"]}).status_code == 422
    response = studio.c.patch(f"/api/connections/{s06.id}",
                              json={"s06": False, "sections": ["chat"], "token": TOKEN})
    assert response.status_code == 200
    with pytest.raises(upstream.ConnectionChanged):
        connections.token(s06)
    assert not connections.save_passport(snap, {"old": True})


def test_removed_address_stays_server_only_and_can_be_renamed(studio, s06):
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")
    assert studio.c.patch(f"/api/connections/{s06.id}", json={"name": "Renamed"}).status_code == 200
    assert studio.c.get(f"/api/connections/{s06.id}/token").status_code == 403
    assert studio.c.get(f"/api/sections/ocr/links/{s06.id}/found").status_code == 409
    with pytest.raises(upstream.UpstreamRefused):
        asyncio.run(upstream.request(connections.get(1, s06.id), "GET", BASE + "/v1/models"))


def test_passport_names_the_s06_catalog(studio, s06):
    """Паспорт s06: WhisperX делит по собеседникам, чат и перевод, сканы — из перечня шлюза, а не «не известны»."""
    studio.gw.reply = lambda q: httpx.Response(200, json={"data": [{"id": "qwen-27b", "context_length": 32768}]})
    r = studio.c.post(f"/api/connections/{s06.id}/check")
    assert r.status_code == 200, r.text
    passport = r.json()["passport"]
    caps = {m["id"]: [(c["name"], c["source"]) for c in m["caps"]] for m in passport["models"]}
    assert passport["profile"] == "s06"
    assert caps["whisperx"] == [("diarize", "catalog"), ("dictation", "catalog")]
    assert caps["qwen-27b"] == caps["qwen-27b-fast"] == [("text", "catalog")] and caps["ocr-ru"] == [("ocr", "catalog")]
    assert next(m for m in passport["models"] if m["id"] == "qwen-27b")["context_length"] == 32768


def test_asr_preserves_speakers_and_dictation_disables_diarization(studio, s06, tmp_path):
    studio.gw.asr_reply = {"text": "Hello", "segments": [
        {"start": 0, "end": 1, "speaker": "SPEAKER_01", "text": "Hello"}]}
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"ID3test")
    result = asyncio.run(speech.transcribe(s06, "whisperx", audio, "audio/mpeg"))
    assert result == [speech.Segment(0, 1, "SPEAKER_01", "Hello")]
    request = studio.gw.asr_requests[-1]
    assert request.url.path == "/speech/transcriptions" and not request.url.query
    assert b'name="file"' in request.content and b'name="diarize"\r\n\r\ntrue' in request.content
    assert b'name="model"\r\n\r\nwhisperx' in request.content
    assert asyncio.run(speech.dictate(s06, "whisperx", b"audio", "audio/webm", "audio.webm")) == "Hello"
    assert b'name="diarize"\r\n\r\nfalse' in studio.gw.asr_requests[-1].content
    assert b'name="model"\r\n\r\nwhisperx' in studio.gw.asr_requests[-1].content
    asyncio.run(speech.free_card(s06))
    assert not studio.gw.releases


def test_ocr_uses_native_image_and_markdown_without_slot_release(studio, s06):
    studio.gw.reply = lambda q: httpx.Response(200, json={"markdown": "# Page", "ocr_ver": "test"})
    assert asyncio.run(ocr.recognize(s06, PNG_REAL)) == "# Page"
    request = studio.gw.requests[-1]
    assert request.url.path == "/pages/ocr"
    assert json.loads(request.content) == {"model": "ocr-ru", "image": base64.b64encode(PNG_REAL).decode("ascii")}
    before = len(studio.gw.requests)
    assert asyncio.run(ocr.release(s06))
    with pytest.raises(ocr.OcrError, match="координат"):
        asyncio.run(ocr.recognize(s06, PNG_REAL, "coordinates"))
    assert len(studio.gw.requests) == before
    studio.gw.reply = lambda q: httpx.Response(200, json={"markdown": ""})
    with pytest.raises(ocr.PageFailed):
        asyncio.run(ocr.recognize(s06, PNG_REAL))


def test_s06_models_are_local_everywhere(studio, s06):
    """Один признак локальной модели (`sections.is_local`, спека п. 35): s06 — на карте, как роли `local-*` srvllm."""
    assert sections.is_local(s06, "qwen-27b") and not hasattr(card, "_local")
    cid = studio.c.post("/api/chats", json={"connection_id": s06.id, "model": "qwen-27b"}).json()["id"]
    view = studio.c.get(f"/api/chats/{cid}").json()["chat"]
    assert view["local"] and view["thinking_ok"] and view["transcript_consent_free"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"thinking": True}).status_code == 200   # было 409 no_thinking
    busy = [1]

    async def reply(request):
        if busy[0]:
            busy[0] -= 1
            return httpx.Response(503, json={"error": {"message": "slot busy"}})
        return await studio.gw.builtin(request)
    studio.gw.on["/v1/chat/completions"] = reply
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_requests[-1]["chat_template_kwargs"] == {"enable_thinking": True}
    assert "/slot/images/release" not in [r.url.path for r in studio.gw.requests]    # release картинок — только srvllm
    body = translate_text._body(s06, "qwen-27b", "system", "text", 0.2, 100)
    assert (body["temperature"], body["max_tokens"], body["chat_template_kwargs"]) == (0.2, 100, {"enable_thinking": False})
    links = next(s for s in studio.c.get("/api/sections").json()["sections"] if s["id"] == "chat")["links"]
    link = next(l for l in links if l["connection"]["id"] == s06.id)
    assert link["local_models"] == link["transcript_local_models"] == sections.S06_CATALOG["chat"]


def test_step_32_preserves_connections_and_defaults(env):
    connection = db.connect(env.db_path)
    connection.executescript(db.SCHEMA.split(db.SPEECH_45, 1)[0].removesuffix(db.MEDIA_44).removesuffix(db.MCP_43).removesuffix(db.VOICE_42).removesuffix(db.CODE_41).removesuffix(db.EMAIL_40).removesuffix(db.AGENT_EVENTS_39).removesuffix(db.PROJECTS_38).removesuffix(db.DEVICES_37).removesuffix(db.SHARES_36).removesuffix(db.CHAT_35).removesuffix(db.SECURITY_34).removesuffix(db.SHARES_33).removesuffix(db.S06_32) + "\nPRAGMA user_version=31;")
    connection.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'owner','Owner','hash',0)")
    connection.execute("INSERT INTO connections(id,user_id,name,kind,base_url,token_enc,sections,created)"
                       " VALUES(1,1,'Keep','openai','https://test.invalid/v1',x'1234','[]',0)")
    connection.execute("INSERT INTO section_defaults VALUES(1,'chat',1,'keep-model')")
    db.migrate(connection)
    assert tuple(connection.execute("SELECT name,s06,token_enc FROM connections").fetchone()) == ("Keep", 0, b"\x12\x34")
    assert connection.execute("SELECT model FROM section_defaults").fetchone()[0] == "keep-model"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
