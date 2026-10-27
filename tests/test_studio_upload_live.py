"""Manual production acceptance: the studio PWA (https://studio.example.com) and the local clients.

Studio part — needs STUDIO_ADMIN_LOGIN and STUDIO_ADMIN_PASSWORD of an administrator who already
has the «srvllm» connection (`manage.py create-admin` with GATEWAY_KEY_OWNER_IMAGES). It logs in,
generates a Lustify picture, edits half of it with the brush, describes it, makes a 1 s clip of it
and asks local-gemma-abl about a PDF made here; then deletes only what it created (the owner's
history stays) and releases the card. Gateway and PowerShell part — needs GATEWAY_KEY_OWNER_IMAGES
and goes to the gateway directly, as the clients do. Pictures are synthetic: no user photos.
"""
import base64
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from conftest import CLIENT

pytestmark = pytest.mark.manual
MODEL = "lustify-v10-krea2-turbo"
PROMPT = "a simple watercolor landscape, green hills, a blue lake, no people"
STUDIO = os.environ.get("STUDIO_LIVE_URL", "https://studio.example.com").rstrip("/")
GATEWAY = os.environ.get("STUDIO_LIVE_GATEWAY", "http://10.77.1.15:4000/slot")
CSRF = "x-studio-csrf"
CHAT_MODEL = "local-gemma-abl"
BUSY_SEC = 300                 # the owner may be using the studio: wait for their job, never touch it
IMAGE_SEC = 1400               # gateway holds a picture request up to 1300 s (card wait + generation)
VIDEO_SEC = 1800
SPEECH_SEC = 1800              # speech and transcription wait for the card up to 30 min (WAIT_MAX_SEC)
PHRASE = "Проверка студии: озвучка, расшифровка и голосовой ввод работают."
# cold gemma loads up to ~300 s; the studio pings every 15 s, so a silent minute is a broken line
STREAM = httpx.Timeout(60, connect=30, read=400)


def png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def pdf(text: str) -> bytes:
    """One page with one ASCII line (Helvetica) — what documents.extract reads back."""
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 14 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET")
    page[NameObject("/Contents")] = w._add_object(stream)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def poll(client, url, done, seconds, pause=2):
    """GET url until done(body) — the body is returned; deadline — fail with the last state."""
    deadline, body = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        r = client.get(url)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        body = r.json()
        if done(body):
            return body
        time.sleep(pause)
    pytest.fail(f"{url} did not finish in {seconds} s: {str(body)[:300]}")


def submit(client, url, expected, **kw):
    """POST that the owner's own running job may refuse with 409 busy — wait, do not cancel it."""
    deadline = time.monotonic() + BUSY_SEC
    while True:
        r = client.post(url, **kw)
        busy = r.status_code == 409 and (r.json().get("detail") or {}).get("code") == "busy"
        if not busy or time.monotonic() > deadline:
            assert r.status_code == expected, (r.status_code, r.text[:500])
            return r.json()
        time.sleep(3)


def finished(body):
    return body.get("state") in ("done", "error", "cancelled", "failed")


@pytest.fixture(scope="module")
def studio():
    """Logged-in client with the CSRF header; teardown deletes what the tests registered and releases the card."""
    login, password = os.environ.get("STUDIO_ADMIN_LOGIN"), os.environ.get("STUDIO_ADMIN_PASSWORD")
    if not login or not password:
        pytest.skip("STUDIO_ADMIN_LOGIN and STUDIO_ADMIN_PASSWORD are required")
    created = SimpleNamespace(chats=[], videos=[], images=[], descriptions=[], transcripts=[], speeches=[], voices=[])
    with httpx.Client(timeout=60, trust_env=False) as client:
        r = client.post(STUDIO + "/login", data={"login": login, "password": password},
                        headers={"Origin": STUDIO})
        assert r.status_code == 303, f"login refused: {r.status_code}"      # the body never echoes the password
        me = client.get(STUDIO + "/api/me")
        assert me.status_code == 200, me.status_code
        me = me.json()
        assert me["is_admin"], "STUDIO_ADMIN_LOGIN must be an administrator (release)"
        assert all(me["capabilities"].values()), \
            "the owner needs the «srvllm» connection in every section: manage.py create-admin with GATEWAY_KEY_OWNER_IMAGES"
        client.headers[CSRF] = me["csrf"]
        try:
            yield SimpleNamespace(c=client, created=created)
        finally:
            cleanup(client, created)
            client.post(STUDIO + "/logout", headers={"Origin": STUDIO})


def cleanup(client, created):
    """Only the rows this run made, newest first (an edit and a clip reference the generated picture)."""
    for kind in ("transcripts", "speeches"):
        for row_id in getattr(created, kind):
            client.post(f"{STUDIO}/api/{kind}/{row_id}/cancel")
            client.delete(f"{STUDIO}/api/{kind}/{row_id}")
    for voice_id in created.voices:
        client.delete(f"{STUDIO}/api/voices/{voice_id}")
    for chat_id in created.chats:
        client.delete(f"{STUDIO}/api/chats/{chat_id}")
    for video_id in created.videos:
        client.delete(f"{STUDIO}/api/videos/{video_id}")     # a running clip is dropped at the gateway first
    for job_id in created.descriptions:                       # not in history; cancel if it still runs
        client.post(f"{STUDIO}/api/images/{job_id}/cancel")
    for image_id in reversed(created.images):
        r = client.get(f"{STUDIO}/api/images/{image_id}")
        if r.status_code == 200 and not finished(r.json()):
            client.post(f"{STUDIO}/api/images/{image_id}/cancel")
            try:                   # cancel waits for the gateway request to end, then frees the card
                poll(client, f"{STUDIO}/api/images/{image_id}", finished, IMAGE_SEC)
            except (AssertionError, pytest.fail.Exception, httpx.HTTPError):
                continue           # cleanup goes on; the row stays for the owner to delete
        client.delete(f"{STUDIO}/api/images/{image_id}")
    r = client.post(STUDIO + "/api/images/release")
    print("release:", r.status_code, r.json().get("results") if r.status_code == 200 else r.text[:200])


@pytest.mark.timeout(3 * IMAGE_SEC + VIDEO_SEC + 600)
def test_photo_edit_description_and_video_through_the_studio(studio):
    c, made = studio.c, studio.created
    # 1. Lustify from text, portrait 832x1216
    job = submit(c, STUDIO + "/api/images", 202,
                 json={"model": "lustify", "size": "portrait", "seed": 23, "prompt": PROMPT})
    made.images.append(job["id"])
    generated = poll(c, f"{STUDIO}/api/images/{job['id']}", finished, IMAGE_SEC)
    assert generated["state"] == "done", generated
    assert generated["kind"] == "generation" and generated["size"] == "832x1216" and generated["media_id"]
    full = c.get(f"{STUDIO}/api/media/{generated['media_id']}")
    assert full.status_code == 200 and full.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(full.content)) as image:
        assert image.size == (832, 1216)
        source = image.convert("RGB")
    thumb = c.get(f"{STUDIO}/api/media/{generated['media_id']}", params={"thumb": "1"})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(thumb.content)) as small:
        assert small.format == "WEBP" and max(small.size) <= 384

    # 2. Brush edit of the left half (Lustify, strength as the editor's default): outside the mask every
    # pixel is the source's. Lustify, not FLUX: the gateway cannot swap comfy→image within its 300 s
    # budget while comfy's lease is active, so a model switch here would fail with no_capacity.
    area = (0, 0, source.width // 2, source.height)
    mask = Image.new("L", source.size)
    mask.paste(255, area)
    job = submit(c, STUDIO + "/api/images", 202,
                 data={"model": "lustify", "strength": "0.25", "size": "original", "seed": "21",
                       "prompt": "Turn the left half into a snowy winter scene. Keep everything else unchanged.",
                       "source_media_id": generated["media_id"]},
                 files={"mask": ("mask.png", png(mask), "image/png")})
    made.images.append(job["id"])
    edited = poll(c, f"{STUDIO}/api/images/{job['id']}", finished, IMAGE_SEC)
    assert edited["state"] == "done", edited
    assert edited["kind"] == "edit" and edited["source_media_id"] == generated["media_id"]
    result = c.get(f"{STUDIO}/api/media/{edited['media_id']}")
    assert result.status_code == 200 and result.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(result.content)) as image:
        assert image.size == source.size
        difference = ImageChops.difference(image.convert("RGB"), source)
    assert difference.crop(area).getbbox() is not None, "model returned the original unchanged"
    difference.paste((0, 0, 0), area)
    assert difference.getbbox() is None, "pixels outside the mask changed"

    # 3. Description by gemma-abl of the generated picture; the text comes with «done» until received
    job = submit(c, STUDIO + "/api/images/descriptions", 202,
                 files={"source_media_id": (None, generated["media_id"])})
    made.descriptions.append(job["id"])
    described = poll(c, f"{STUDIO}/api/images/descriptions/{job['id']}", finished, IMAGE_SEC)
    assert described["state"] == "done", described
    assert len(described["text"]) >= 300, described["text"][:300]
    assert c.delete(f"{STUDIO}/api/images/descriptions/{job['id']}").status_code == 204

    # 4. A 1 s clip of the generated picture; the server watcher brings it into history
    r = c.post(STUDIO + "/api/videos", data={"prompt": "slow camera pan over the hills", "seconds": "1",
                                             "quality": "fast"},
               files={"source_media_id": (None, generated["media_id"])})
    assert r.status_code == 201, (r.status_code, r.text[:500])
    made.videos.append(r.json()["id"])
    video = poll(c, f"{STUDIO}/api/videos/{r.json()['id']}", finished, VIDEO_SEC, pause=5)
    assert video["state"] == "done", video
    assert video["seconds"] == 1 and video["source_media_id"] == generated["media_id"] and video["media_id"]
    clip = c.get(f"{STUDIO}/api/media/{video['media_id']}", headers={"Range": "bytes=0-99"})
    assert clip.status_code == 206 and clip.headers["content-type"] == "video/mp4"
    assert clip.headers["content-range"].startswith("bytes 0-99/") and len(clip.content) == 100
    assert clip.content[4:8] == b"ftyp"


def letters(text: str) -> str:
    """Only capitals and digits: the model may wrap the code in markdown or change its case."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def read_turn(client, url, body) -> tuple[str, str]:
    """(answer text, final status) from the SSE stream of a turn: turn, delta…, done | error."""
    text, status = [], None
    with client.stream("POST", url, json=body, timeout=STREAM) as r:
        if r.status_code != 200:
            r.read()
            pytest.fail(f"turn refused: {r.status_code} {r.text[:500]}")
        assert r.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):])
                if event == "delta":
                    text.append(data["text"])
                elif event == "done":
                    status = data["status"]
                    break
                elif event == "error":
                    pytest.fail(f"model error: {data['message'][:300]}")
    return "".join(text), status


@pytest.mark.timeout(900)
def test_chat_answers_from_an_attached_pdf(studio):
    c, made = studio.c, studio.created
    code = f"AZURE-OTTER-{secrets.randbelow(9000) + 1000}"
    r = c.post(STUDIO + "/api/chats", json={"model": CHAT_MODEL})
    assert r.status_code == 201, (r.status_code, r.text[:300])
    chat_id = r.json()["id"]
    made.chats.append(chat_id)
    assert r.json()["model"] == CHAT_MODEL

    r = c.post(STUDIO + "/api/attachments", files={"file": ("live-acceptance.pdf",
                                                            # от 40 знаков без пробелов: иначе — скан для ocr
                                                            pdf(f"The secret code phrase of this document is {code}."),
                                                            "application/pdf")})
    assert r.status_code == 201, (r.status_code, r.text[:300])
    attachment = r.json()
    assert attachment["kind"] == "document" and attachment["chars"] > 0

    answer, status = read_turn(c, f"{STUDIO}/api/chats/{chat_id}/turns", {
        "text": "What is the secret code phrase in the attached document? Reply with the code phrase only.",
        "attachment_ids": [attachment["id"]]})
    assert status == "done", (status, answer[:300])
    assert letters(code) in letters(answer), answer[:300]

    saved = c.get(f"{STUDIO}/api/chats/{chat_id}").json()["messages"]
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[0]["attachments"][0]["id"] == attachment["id"]
    assert saved[1]["status"] == "done" and letters(code) in letters(saved[1]["content"])


@pytest.mark.timeout(1500)
def test_photo_api_maximum_size():
    token = os.environ.get("GATEWAY_KEY_OWNER_IMAGES")
    if not token:
        pytest.skip("GATEWAY_KEY_OWNER_IMAGES is required")
    source = io.BytesIO()
    Image.new("RGB", (512, 512), "forestgreen").save(source, "PNG")
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=1300, trust_env=False) as client:
        try:
            response = client.post(GATEWAY + "/images/edits", headers=headers,
                                   data={"model": MODEL, "prompt": PROMPT, "strength": "0.65",
                                         "size": "1536x1536", "response_format": "b64_json", "seed": "20"},
                                   files={"image": ("source.png", source.getvalue(), "image/png")})
            assert response.status_code == 200, response.text[:500]
            with Image.open(io.BytesIO(base64.b64decode(response.json()["data"][0]["b64_json"]))) as result:
                assert result.size == (1440, 1440)
        finally:
            client.post(GATEWAY + "/images/release", json={"model": MODEL}, headers=headers, timeout=60)


@pytest.mark.timeout(3000)
def test_photo_through_the_gateway_and_the_powershell_client(tmp_path):
    token = os.environ.get("GATEWAY_KEY_OWNER_IMAGES")
    if not token:
        pytest.skip("GATEWAY_KEY_OWNER_IMAGES is required")
    ps = shutil.which("powershell") or shutil.which("pwsh")
    local = CLIENT / "studio-batch.local.ps1"
    if not ps or not local.exists():
        pytest.skip("PowerShell and studio-batch.local.ps1 are required")
    source = Image.new("RGB", (512, 512), "skyblue")
    draw = ImageDraw.Draw(source)
    draw.polygon([(0, 512), (0, 300), (160, 190), (320, 330), (512, 210), (512, 512)], fill="forestgreen")
    photo = tmp_path / "source.png"
    photo.write_bytes(png(source))
    fields = {"model": MODEL, "prompt": PROMPT, "strength": "0.4", "seed": "17",
              "size": "1024x1024", "response_format": "b64_json"}
    with httpx.Client(timeout=1300, trust_env=False) as client:
        try:
            response = client.post(GATEWAY + "/images/edits", data=fields,
                                   files={"image": ("source.png", photo.read_bytes(), "image/png")},
                                   headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200, response.text[:500]
            with Image.open(io.BytesIO(base64.b64decode(response.json()["data"][0]["b64_json"]))) as result:
                assert result.size == (1024, 1024)

            prompts = tmp_path / "prompts.txt"
            prompts.write_text(PROMPT + "\n", encoding="utf-8")
            out = tmp_path / "out"
            run = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                  "-File", str(local), "-PromptsFile", str(prompts), "-ImagePath", str(photo),
                                  "-Strength", "0.4", "-Seed", "19", "-Size", "square",
                                  "-OutDir", str(out), "-Url", GATEWAY], capture_output=True, timeout=1350)
            assert run.returncode == 0, run.stderr.decode(errors="replace")
            results = list(out.glob("*.png"))
            assert len(results) == 1, run.stdout.decode(errors="replace")
            with Image.open(results[0]) as image:
                assert image.size == (1024, 1024)
        finally:
            client.post(GATEWAY + "/images/release", json={"model": MODEL},
                        headers={"Authorization": "Bearer " + token}, timeout=60)


def words_of(text: str) -> set[str]:
    return {w for w in letters(text).split() if len(w) > 3}


@pytest.mark.timeout(3 * SPEECH_SEC + 300)
def test_voice_speech_dictation_and_transcription(studio):
    """Голос по образцу владельца → озвучка фразы (Qwen3-TTS) → голосовой ввод (GigaAM) и расшифровка
    (WhisperX) того же mp3. Образец — STUDIO_LIVE_VOICE (5–15 с речи), его текст — STUDIO_LIVE_VOICE_TEXT."""
    sample, ref_text = os.environ.get("STUDIO_LIVE_VOICE"), os.environ.get("STUDIO_LIVE_VOICE_TEXT")
    if not sample or not ref_text:
        pytest.skip("STUDIO_LIVE_VOICE (запись 5–15 с) and STUDIO_LIVE_VOICE_TEXT are required")
    c = studio.c
    r = c.post(STUDIO + "/api/voices", data={"name": "приёмка", "text": ref_text},
               files={"audio": (Path(sample).name, Path(sample).read_bytes(), "application/octet-stream")})
    assert r.status_code == 201, (r.status_code, r.text[:300])
    voice_id = r.json()["id"]
    studio.created.voices.append(voice_id)

    speech = submit(c, STUDIO + "/api/speeches", 202, json={"text": PHRASE, "voice": voice_id})
    studio.created.speeches.append(speech["id"])
    speech = poll(c, f"{STUDIO}/api/speeches/{speech['id']}", finished, SPEECH_SEC, pause=5)
    assert speech["state"] == "done", speech["detail"]
    mp3 = c.get(f"{STUDIO}/api/media/{speech['media_id']}").content
    assert mp3[:3] == b"ID3" or mp3[:2] == b"\xff\xfb", mp3[:8]

    r = c.post(STUDIO + "/api/dictation", files={"audio": ("phrase.mp3", mp3, "audio/mpeg")}, timeout=200)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert len(words_of(r.json()["text"]) & words_of(PHRASE)) >= 3, r.json()["text"]

    deadline = time.monotonic() + BUSY_SEC
    while True:
        r = c.post(STUDIO + "/api/transcripts", content=mp3,
                   headers={"content-type": "audio/mpeg", "x-studio-name": "acceptance.mp3"})
        if r.status_code != 409 or time.monotonic() > deadline:
            break
        time.sleep(3)
    assert r.status_code == 202, (r.status_code, r.text[:300])
    studio.created.transcripts.append(r.json()["id"])
    item = poll(c, f"{STUDIO}/api/transcripts/{r.json()['id']}", finished, SPEECH_SEC, pause=5)
    assert item["state"] == "done", item["detail"]
    heard = " ".join(s["text"] for s in item["segments"])
    assert len(words_of(heard) & words_of(PHRASE)) >= 3, heard
    r = c.get(f"{STUDIO}/api/transcripts/{item['id']}/export?format=docx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
