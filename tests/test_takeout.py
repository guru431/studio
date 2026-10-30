"""Выгрузка своей истории одним ZIP (app/takeout.py, спека 2026-10-21-studio-export-restore-check)."""
import asyncio
import builtins
import dataclasses
import gc
import hashlib
import io
import json
import logging
import struct
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import auth
import db
import media
import security
import state
import takeout
from conftest import T0, add_connection, make_user, signin

DATA = bytes(range(250)) * 4               # 1000 байт
ALL = list(takeout.SECTIONS)


def put(sql, *args):
    state.DB.execute(sql, args)


def save(uid, data, mime, name):
    """Файл на «шаре» и строка media — короче media.save (временный файл и os.replace на Windows
    дороги, а здесь их девять на историю)."""
    media_id, rel = media._plan(uid, mime)
    full = state.CFG.media_dir / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    put("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?)", media_id, uid, rel, mime, len(data), name, T0)
    return media_id


def history(uid, tag):
    """По записи в каждом разделе: файлы на «шаре», промпты и тексты с меткой `tag`.
    Возвращает id файлов по ролям."""
    m = {role: save(uid, DATA + role.encode(), mime, f"{tag}-{role}{ext}")
         for role, mime, ext in (("doc", "application/pdf", ".pdf"), ("img", "image/png", ".png"),
                                 ("src", "image/jpeg", ".jpg"), ("mask", "image/png", ".png"),
                                 ("clip", "video/mp4", ".mp4"), ("rec", "audio/mpeg", ".mp3"),
                                 ("result", "application/json", ".json"), ("voice", "audio/wav", ".wav"),
                                 ("speech", "audio/mpeg", ".mp3"))}
    for n in (1, 2):
        chat = f"{tag}chat{n}"
        put("INSERT INTO chats(id, user_id, title, connection_id, model, created, updated) VALUES (?, ?, ?, NULL, 'm', ?, ?)",
            chat, uid, f"{tag} title {n}", T0, T0)
        put("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
            " VALUES (?, ?, 'user', ?, 'done', 'server', NULL, ?)", f"{chat}-q", chat, f"{tag} question {n}", T0)
        put("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
            " VALUES (?, ?, 'assistant', ?, 'done', 'server', 'm', ?)", f"{chat}-a", chat, f"{tag} answer {n}", T0 + 1)
    put("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
        " VALUES (?, ?, ?, 'document', ?, ?, ?, 5, ?)", f"{tag}att", uid, f"{tag}chat1-q", f"{tag}.pdf", m["doc"],
        f"{tag} doc text", T0)
    put("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, strength, source_media_id, media_id, state,"
        " detail, created, finished, mask_media_id, chain) VALUES (?, ?, 'edit', 'lustify', ?, '1024x1024', 42, 0.5,"
        " ?, ?, 'done', '', ?, ?, ?, NULL)", f"{tag}img", uid, f"{tag} prompt", m["src"], m["img"], T0, T0 + 5, m["mask"])
    put("INSERT INTO videos(id, user_id, connection_id, gateway_id, source_media_id, media_id, prompt, prompt_source,"
        " seconds, quality, smooth, seed, state, progress, detail, created, finished, chain) VALUES (?, ?, 1, 'gw', ?, ?,"
        " ?, NULL, 3, 'fast', 0, 7, 'done', 100, '', ?, ?, NULL)", f"{tag}vid", uid, m["img"], m["clip"],
        f"{tag} motion", T0, T0 + 9)
    put("INSERT INTO transcripts(id, user_id, connection_id, model, name, source_media_id, result_media_id, duration,"
        " speakers, state, detail, created, finished) VALUES (?, ?, 1, 'whisperx', ?, ?, ?, 61, 2, 'done', '', ?, ?)",
        f"{tag}tr", uid, f"{tag} meeting", m["rec"], m["result"], T0, T0 + 30)
    put("INSERT INTO voices(id, user_id, name, ref_text, media_id, duration, created) VALUES (?, ?, ?, ?, ?, 9, ?)",
        f"{tag}voice", uid, f"{tag} voice", f"{tag} ref text", m["voice"], T0)
    put("INSERT INTO speeches(id, user_id, connection_id, model, voice, text, media_id, duration, state, detail,"
        " created, finished) VALUES (?, ?, 1, 'qwen3-tts', 'v', ?, ?, 4, 'done', '', ?, ?)",
        f"{tag}speech", uid, f"{tag} spoken", m["speech"], T0, T0 + 3)
    return m


def code_history(uid, tag):
    """Компьютер Claude Code учётки с двумя событиями, чатом студии и подпиской push (на новую сессию).
    Возвращает (id компьютера, хеш ключа устройства, адрес подписки)."""
    key_hash = hashlib.sha256(f"{tag}-device-key".encode()).hexdigest()
    aid = state.DB.execute("INSERT INTO agents(user_id, name, key_hash, has_pin, projects_root, cc_version, created,"
                           " last_seen) VALUES (?, ?, ?, 1, ?, '2.3.1', ?, ?)",
                           (uid, f"{tag} ПК", key_hash, f"C:\\{tag}-root", T0, T0 + 1)).lastrowid
    put("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
        " VALUES (?, ?, ?, ?, 'enroll', NULL, '10.77.48.5')", uid, aid, f"{tag} ПК", T0)
    put("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
        " VALUES (?, ?, ?, ?, 'pin_ok', 'телефон Safari', '10.77.1.1')", uid, aid, f"{tag} ПК", T0 + 3)
    put("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
        " VALUES (?, ?, '0123456789abcdef', ?, 'closed', ?, ?)", uid, aid, f"{tag}-session", T0, T0 + 2)
    auth.create_session(uid)
    digest = state.DB.execute("SELECT digest FROM sessions WHERE user_id = ? ORDER BY rowid DESC", (uid,)).fetchone()[0]
    endpoint = f"https://fcm.googleapis.com/fcm/send/{tag}-endpoint-secret"
    put("INSERT INTO push_subs(endpoint, user_id, digest, p256dh, auth, created) VALUES (?, ?, ?, ?, ?, ?)",
        endpoint, uid, digest, f"{tag}-p256dh-secret", f"{tag}-auth-secret", T0)
    return aid, key_hash, endpoint


def prepare(studio, sections=None):
    return studio.c.post("/api/me/export", json={"sections": ALL if sections is None else sections})


def download(studio, sections=None):
    r = prepare(studio, sections)
    assert r.status_code == 200, r.text
    z = studio.c.get(f"/api/me/export/{r.json()['id']}")
    assert z.status_code == 200, z.text
    return r.json(), z


def unzip(z) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert zf.testzip() is None
    return zf


def test_export_holds_every_section_its_files_and_a_manifest(studio, caplog):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    m = history(uid, "mine")
    with caplog.at_level(logging.INFO, logger="studio"):
        est, z = download(studio)
    assert est["files"] == 9 and est["bytes"] == sum(len(DATA) + len(k) for k in m)
    day = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
    assert z.headers["content-type"] == "application/zip" and "content-length" not in z.headers
    assert z.headers["content-disposition"] == f'attachment; filename="studio-export-{day}.zip"'
    assert z.headers["cache-control"] == "no-store" and z.headers["x-content-type-options"] == "nosniff"
    zf = unzip(z)
    names = zf.namelist()
    assert names[0] == "README.txt" and names[-1] == "manifest.json"
    files = {f"files/{mid}{media.EXT[mime]}" for mid, mime in
             ((r["id"], r["mime"]) for r in state.DB.execute("SELECT id, mime FROM media"))}
    assert set(names) == {"README.txt", "settings.json", "chats/minechat1.json", "chats/minechat2.json", "images/0001.json",
                          "videos/0001.json", "transcripts/0001.json", "scans/0001.json", "speeches/0001.json",
                          "voices/0001.json", "translations/0001.json", "code_chats/0001.json",
                          "code_chats/workspace.json", "agent_events/0001.json", "manifest.json"} | files
    assert {zipfile.ZIP_STORED} == {i.compress_type for i in zf.infolist()}
    chat = json.loads(zf.read("chats/minechat1.json"))
    assert chat["title"] == "mine title 1" and [x["content"] for x in chat["messages"]] == ["mine question 1",
                                                                                              "mine answer 1"]
    att = chat["messages"][0]["attachments"]
    assert att == [{"id": "mineatt", "kind": "document", "name": "mine.pdf", "chars": 5, "text": "mine doc text",
                    "file": f"files/{m['doc']}.pdf"}]
    image = json.loads(zf.read("images/0001.json"))[0]
    assert image["prompt"] == "mine prompt" and image["file"] == f"files/{m['img']}.png"
    assert image["source_file"] == f"files/{m['src']}.jpg" and image["mask_file"] == f"files/{m['mask']}.png"
    assert image["chain"][-1]["prompt"] == "mine prompt"
    video = json.loads(zf.read("videos/0001.json"))[0]
    assert video["file"] == f"files/{m['clip']}.mp4" and video["source_file"] == f"files/{m['img']}.png"
    tr = json.loads(zf.read("transcripts/0001.json"))[0]
    assert tr["name"] == "mine meeting" and tr["file"].endswith(".mp3") and tr["result_file"].endswith(".json")
    assert json.loads(zf.read("speeches/0001.json"))[0]["text"] == "mine spoken"
    assert json.loads(zf.read("voices/0001.json"))[0]["ref_text"] == "mine ref text"
    assert zf.read(f"files/{m['clip']}.mp4") == DATA + b"clip"
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "studio-export" and manifest["version"] == 1 and manifest["schema"] == db.VERSION
    assert manifest["user"] == {"login": "owner", "name": "owner"} and manifest["sections"] == ALL
    assert manifest["missing"] == [] and manifest["counts"]["chats"] == 2 and manifest["counts"]["files"] == 9
    # суммы манифеста сходятся с байтами — каждого файла и каждой записи JSON
    for item in manifest["files"] + manifest["entries"]:
        assert hashlib.sha256(zf.read(item["path"])).hexdigest() == item["sha256"], item["path"]
    assert {e["path"] for e in manifest["entries"]} == set(names) - files - {"manifest.json"}
    clip = next(f for f in manifest["files"] if f["media_id"] == m["clip"])
    assert clip == {"path": f"files/{m['clip']}.mp4", "media_id": m["clip"], "mime": "video/mp4", "size": 1004,
                    "sha256": hashlib.sha256(DATA + b"clip").hexdigest(), "download_name": "mine-clip.mp4",
                    "created": T0}
    assert "export: 9" in caplog.text
    for secret in ("mine", m["clip"], "owner"):
        assert secret not in caplog.text


def test_export_takes_only_chosen_sections(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    m = history(uid, "mine")
    est, z = download(studio, ["photo"])
    names = unzip(z).namelist()
    assert est["files"] == 3 and sorted(n for n in names if n.startswith("files/")) == sorted(
        f"files/{m[k]}{'.jpg' if k == 'src' else '.png'}" for k in ("img", "src", "mask"))
    assert {n for n in names if not n.startswith("files/")} == {"README.txt", "settings.json", "images/0001.json",
                                                               "manifest.json"}
    assert json.loads(unzip(z).read("manifest.json"))["sections"] == ["photo"]
    # разделы — по порядку вкладок студии (спека 2026-10-27-studio-ui-tidy, 4.1), голоса — за озвучкой
    assert takeout.SECTIONS == ("chat", "photo", "video", "transcribe", "speech", "voices", "ocr", "translate",
                                "code", "agents")


def test_export_never_holds_another_users_rows_or_files(studio):
    bob = make_user(studio, "bob"); uid = make_user(studio, "owner")
    theirs = history(bob, "theirs")
    history(uid, "mine")
    signin(studio, "owner")
    _, z = download(studio)
    assert b"theirs" not in z.content
    for mid in theirs.values():
        assert mid.encode() not in z.content


def test_export_holds_no_secrets(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    add_connection(studio, token="cloud-key-very-secret")
    put("INSERT INTO passkeys(id, user_id, public_key, sign_count, transports, name, created)"
        " VALUES ('pk-credential-id', ?, x'0102', 0, '[]', 'phone', ?)", uid, T0)
    pin_hash = security.hash_password("0007")
    put("UPDATE users SET pin_hash = ? WHERE id = ?", pin_hash, uid)
    history(uid, "mine")
    _, key_hash, endpoint = code_history(uid, "mine")
    _, z = download(studio)
    settings = json.loads(unzip(z).read("settings.json"))
    assert settings["connections"][0] == {"id": settings["connections"][0]["id"], "name": "openai", "kind": "openai",
                                          "base_url": "https://cloud.test/v1"}
    assert settings["profile"]["login"] == "owner" and settings["sections"]["links"]
    pw_hash = state.DB.execute("SELECT pw_hash FROM users WHERE id = ?", (uid,)).fetchone()[0]
    token_enc = state.DB.execute("SELECT token_enc FROM connections").fetchone()[0]
    session = state.DB.execute("SELECT digest, csrf FROM sessions WHERE user_id = ?", (uid,)).fetchone()
    for secret in (pw_hash.split("$")[-1].encode(), token_enc, b"cloud-key-very-secret", session["digest"].encode(),
                   session["csrf"].encode(), b"pk-credential-id", b"token", b"pw_hash", b"csrf",
                   pin_hash.split("$")[-1].encode(), pin_hash.split("$")[-2].encode(), b"pin_hash",
                   key_hash.encode(), b"key_hash", b"mine-device-key", endpoint.encode(), b"endpoint-secret",
                   b"mine-p256dh-secret", b"mine-auth-secret"):
        assert secret not in z.content, secret


def test_export_missing_file_goes_to_manifest(studio):
    """Файла нет на шаре или строка ушла после «Подготовить» — пропуск и запись в missing, архив цел."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    m = history(uid, "mine")
    token = prepare(studio).json()["id"]
    media.path(uid, m["clip"]).unlink()
    asyncio.run(media.delete_async(uid, m["voice"]))
    zf = unzip(studio.c.get(f"/api/me/export/{token}"))
    manifest = json.loads(zf.read("manifest.json"))
    assert sorted(manifest["missing"]) == sorted([m["clip"]])            # строки голоса уже нет — в план не попал
    assert not any(m["clip"] in n or m["voice"] in n for n in zf.namelist() if n.startswith("files/"))
    assert manifest["counts"]["files"] == 7


def test_export_without_files_needs_no_share(studio, env, monkeypatch):
    """Только диалоги — выгрузка законна и без шары; с файлами шара обязательна — 503 словами."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    put("INSERT INTO chats(id, user_id, title, connection_id, model, created, updated) VALUES ('c1', ?, 't', NULL,"
        " 'm', ?, ?)", uid, T0, T0)
    monkeypatch.setattr(state, "CFG", dataclasses.replace(state.CFG, media_dir=env.media_dir / "not-mounted"))
    est, z = download(studio, ["chat"])
    assert est == {"id": est["id"], "files": 0, "bytes": 0}
    assert "chats/c1.json" in unzip(z).namelist()
    media_id = "f" * 32
    put("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
        " VALUES (?, ?, 'u1/x.png', 'image/png', 3, 0, 'x.png', ?)", media_id, uid, T0)
    put("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
        " VALUES ('msg', 'c1', 'user', 'q', 'done', 'server', NULL, ?)", T0)
    put("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
        " VALUES ('a1', ?, 'msg', 'image', 'x.png', ?, NULL, 0, ?)", uid, media_id, T0)
    r = prepare(studio, ["chat"])
    assert r.status_code == 503 and r.json()["detail"]["code"] == "storage"


def test_export_refuses_bad_section_lists(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    for bad in ([], ["chat", "secrets"], ["../x"]):
        r = prepare(studio, bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid", bad


def test_export_token_is_personal_expires_and_few_live(studio):
    uid = make_user(studio, "owner"); make_user(studio, "bob"); signin(studio, "owner")
    history(uid, "mine")
    tokens = [prepare(studio).json()["id"] for _ in range(takeout.EXPORT_KEEP + 1)]
    assert studio.c.get(f"/api/me/export/{tokens[0]}").status_code == 404          # старейший вытеснен
    url = f"/api/me/export/{tokens[-1]}"
    assert studio.c.get(url).status_code == 200 and studio.c.get(url).status_code == 200
    studio.clock[0] += takeout.EXPORT_TTL_SEC + 1
    r = studio.c.get(url)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    token = prepare(studio).json()["id"]
    signin(studio, "bob")
    assert studio.c.get(f"/api/me/export/{token}").status_code == 404
    assert studio.c.get("/api/me/export/unknown").status_code == 404


def test_export_needs_a_session_and_csrf(studio):
    make_user(studio, "owner")
    assert prepare(studio).status_code == 401
    csrf = signin(studio, "owner")
    del studio.c.headers[auth.CSRF_HEADER]
    assert prepare(studio).status_code == 403
    studio.c.headers[auth.CSRF_HEADER] = csrf
    assert prepare(studio).status_code == 200


def test_export_streams_files_in_blocks(studio, monkeypatch):
    """Поток не держит архив и файл целиком: куски файла — не больше блока, и отданы до конца архива."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA * 50, "video/mp4", "big.mp4")
    put("INSERT INTO speeches(id, user_id, connection_id, model, voice, text, media_id, duration, state, detail,"
        " created, finished) VALUES ('s1', ?, 1, 'q', 'v', 't', ?, 4, 'done', '', ?, ?)", uid, mid, T0, T0)
    monkeypatch.setattr(takeout, "CHUNK", 4096)
    token = prepare(studio, ["speech"]).json()["id"]
    pieces = []
    with studio.c.stream("GET", f"/api/me/export/{token}") as r:
        for piece in r.iter_raw():
            pieces.append(len(piece))
    assert sum(pieces) > 50_000 and max(pieces) < 50_000


def test_export_read_failure_breaks_the_answer_without_names(studio, env, monkeypatch, caplog):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    token = prepare(studio).json()["id"]

    def broken(file, *args, **kwargs):
        raise OSError(f"[Errno 5] Input/output error: '{file}'")

    monkeypatch.setattr(media, "open_summed", broken)
    with caplog.at_level(logging.INFO, logger="studio"):
        try:
            studio.c.get(f"/api/me/export/{token}")
        except media.StorageError:
            pass
        else:
            raise AssertionError("ответ не оборвался")
    assert "export: хранилище оборвало чтение" in caplog.text
    assert str(env.media_dir) not in caplog.text and "mine" not in caplog.text


def test_export_holds_transcript_edits_and_presets(studio):
    """Правки расшифровки и пресеты фото — в выгрузке, чужие — нет."""
    bob = make_user(studio, "bob")
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    put("UPDATE transcripts SET edits = ?", json.dumps({"segments": {"0": "fixed"}}))
    put("INSERT INTO presets VALUES ('p1', ?, 'photo', 'night', ?, ?)", uid, json.dumps({"model": "lustify"}), T0)
    put("INSERT INTO presets VALUES ('p2', ?, 'photo', 'theirs', '{}', ?)", bob, T0)
    _, z = download(studio)
    zf = unzip(z)
    assert json.loads(zf.read("transcripts/0001.json"))[0]["edits"] == {"segments": {"0": "fixed"}}
    assert json.loads(zf.read("settings.json"))["presets"] == [
        {"id": "p1", "section": "photo", "name": "night", "params": {"model": "lustify"}, "created": T0}]


def test_export_holds_the_steps_of_the_internet_but_not_the_exa_key(studio):
    """Шаги «Интернета» ответа — `tools` целиком (с текстом для модели), у прочих сообщений — []; ключа Exa
    учётки в архиве нет ни открытым, ни шифром."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    assert studio.c.put("/api/me/exa-key", json={"key": "exa-secret-key-123"}).status_code == 204
    history(uid, "mine")
    steps = [{"kind": "search", "query": "mine query", "results": [{"title": "t", "url": "https://example.test/",
                                                                     "published": ""}], "text": "1. t\nfound text"},
             {"kind": "page", "url": "https://example.test/x", "error": "страница не найдена"}]
    put("UPDATE messages SET tools = ? WHERE id = 'minechat1-a'", json.dumps(steps, ensure_ascii=False))
    put("UPDATE messages SET reasoning = ?, reasoning_sec = ? WHERE id = 'minechat1-a'", "Ход мысли", 11.5)
    _, z = download(studio, ["chat"])
    chat = json.loads(unzip(z).read("chats/minechat1.json"))
    assert [m["tools"] for m in chat["messages"]] == [[], steps]
    assert [(m["reasoning"], m["reasoning_sec"]) for m in chat["messages"]] == [(None, None), ("Ход мысли", 11.5)]
    exa_enc = state.DB.execute("SELECT exa_key FROM users WHERE id = ?", (uid,)).fetchone()[0]
    for secret in (b"exa-secret-key-123", bytes(exa_enc), b"exa_key"):
        assert secret not in z.content, secret


def test_export_survives_broken_tools_json(studio):
    """Битая строка `messages.tools` не обрывает выгрузку посреди потока: идёт как есть, manifest на месте."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    put("UPDATE messages SET tools = ? WHERE id = 'minechat1-a'", '[{"kind": "search"')
    _, z = download(studio, ["chat"])
    zf = unzip(z)
    chat = json.loads(zf.read("chats/minechat1.json"))
    assert [m["tools"] for m in chat["messages"]] == [[], '[{"kind": "search"']
    assert json.loads(zf.read("manifest.json"))["counts"]["chats"] == 2


def as_user(uid):
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=uid)))


def drain(uid, token, on_piece=lambda piece: None) -> list:
    """Куски ответа GET выгрузки — прямо из генератора, как их отдаёт Starlette."""
    async def go():
        pieces = []
        async for piece in (await takeout.get_export(token, as_user(uid))).body_iterator:
            pieces.append(piece)
            on_piece(piece)
        return pieces
    return asyncio.run(go())


def test_export_builds_json_off_the_event_loop(studio, monkeypatch):
    """JSON разделов, диалогов, настроек и манифеста — в потоке: 64 МиБ картинок не стоят чату цикл."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    calls, real = [], takeout._json

    def watched(value):
        try:
            asyncio.get_running_loop()
            calls.append("loop")
        except RuntimeError:
            calls.append("thread")
        return real(value)

    monkeypatch.setattr(takeout, "_json", watched)
    download(studio)
    assert calls and set(calls) == {"thread"}, calls


def test_export_sends_entry_headers_apart_from_their_data(studio):
    """Заголовок записи и её данные — разными кусками: склейка — ещё одна копия страницы в памяти."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    pieces = drain(uid, prepare(studio).json()["id"])
    headers = [p for p in pieces if p[:4] == b"PK\x03\x04"]
    # README, settings, 2 диалога, 9 разделов, рабочее место Claude Code, 9 файлов, манифест
    assert len(headers) == 24
    for p in headers:
        name_len, extra_len = struct.unpack_from("<HH", p, 26)
        assert len(p) == 30 + name_len + extra_len, p[30:30 + name_len]
    zf = zipfile.ZipFile(io.BytesIO(b"".join(pieces)))
    assert zf.testzip() is None


def test_export_pages_big_sections(studio, monkeypatch):
    """Раздел — страницами по PAGE записей (`images/0001.json` …): память — на страницу, а не на раздел."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    for n in range(4):                                   # то же время создания — порядок по id
        put("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, strength, source_media_id, media_id,"
            " state, detail, created, finished, mask_media_id, chain) VALUES (?, ?, 'generation', 'lustify', ?,"
            " '1024x1024', 1, NULL, NULL, NULL, 'error', 'x', ?, ?, NULL, NULL)", f"extra{n}", uid, f"p{n}", T0, T0)
    monkeypatch.setattr(takeout, "PAGE", 2)
    zf = unzip(download(studio, ["photo", "voices"])[1])
    names = zf.namelist()
    assert [n for n in names if n.startswith("images/")] == ["images/0001.json", "images/0002.json", "images/0003.json"]
    assert [n for n in names if n.startswith("voices/")] == ["voices/0001.json"]
    pages = [json.loads(zf.read(n)) for n in names if n.startswith("images/")]
    assert [len(p) for p in pages] == [2, 2, 1]
    assert sorted(x["id"] for p in pages for x in p) == sorted(["mineimg", *(f"extra{n}" for n in range(4))])
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["counts"]["images"] == 5 and manifest["counts"]["voices"] == 1
    assert {"images/0001.json", "images/0003.json"} <= {e["path"] for e in manifest["entries"]}


def test_takeout_has_scans_and_their_page_files(studio):
    """Документ распознавания — JSON со страницами, исходник и картинки страниц — файлами; чужое — нет."""
    bob = make_user(studio, "bob"); uid = make_user(studio, "owner")
    signin(studio, "owner")
    ids = {}
    for who, tag in ((bob, "theirs"), (uid, "mine")):
        src, page = (save(who, DATA + role.encode(), mime, f"{tag}-{role}") for role, mime in
                     (("pdf", "application/pdf"), ("page", "image/png")))
        put("INSERT INTO scans(id, user_id, connection_id, model, name, source_media_id, origin, pages, done_pages,"
            " state, detail, text, created, finished) VALUES (?, ?, 1, 'hunyuan-ocr', ?, ?, 'section', 2, 2, 'done',"
            " 'готово', ?, ?, ?)", f"{tag}scan", who, f"{tag}.pdf", src, f"{tag} text", T0, T0 + 7)
        put("INSERT INTO scan_pages(scan_id, user_id, n, media_id, state, text, detail) VALUES (?, ?, 1, ?, 'done', ?,"
            " NULL)", f"{tag}scan", who, page, f"{tag} page text")
        put("INSERT INTO scan_pages(scan_id, user_id, n, media_id, state, text, detail) VALUES (?, ?, 2, NULL,"
            " 'failed', NULL, 'на странице не найден текст')", f"{tag}scan", who)
        ids[tag] = (src, page)
    est, z = download(studio, ["ocr"])
    zf = unzip(z)
    src, page = ids["mine"]
    assert est["files"] == 2 and sorted(n for n in zf.namelist() if n.startswith("files/")) == sorted(
        [f"files/{src}.pdf", f"files/{page}.png"])
    assert zf.read(f"files/{page}.png") == DATA + b"page"
    assert json.loads(zf.read("scans/0001.json")) == [{
        "id": "minescan", "model": "hunyuan-ocr", "name": "mine.pdf", "origin": "section", "state": "done",
        "detail": "готово", "created": T0, "finished": T0 + 7, "file": f"files/{src}.pdf",
        "pages": [{"n": 1, "state": "done", "text": "mine page text", "detail": None, "file": f"files/{page}.png"},
                  {"n": 2, "state": "failed", "text": None, "detail": "на странице не найден текст", "file": None}]}]
    assert json.loads(zf.read("manifest.json"))["counts"]["scans"] == 1
    assert "scans/" in zf.read("README.txt").decode("utf-8")
    assert not any("theirs" in zf.read(n).decode("utf-8", "replace") for n in zf.namelist() if n.endswith(".json"))


def test_export_holds_translations_with_parts_and_files(studio):
    """Запись перевода — JSON с исходником, переводом и кусками (у снимка — рамка), исходный файл — файлом;
    чужое — нет."""
    bob = make_user(studio, "bob"); uid = make_user(studio, "owner")
    signin(studio, "owner")
    files = {}
    for who, tag in ((bob, "theirs"), (uid, "mine")):
        files[tag] = save(who, DATA + b"photo", "image/png", f"{tag}.png")
        put("INSERT INTO translations(id, user_id, kind, connection_id, model, source_lang, target_lang, detected_lang,"
            " result_lang, name, source_media_id, scan_id, source, text, parts, done_parts, state, detail, created,"
            " finished) VALUES (?, ?, 'photo', 1, 'm', 'auto', 'ru', 'en', 'ru', ?, ?, NULL, ?, ?, 2, 1, 'error',"
            " 'кусок 2 из 2: отказ', ?, ?)", f"{tag}tr", who, f"{tag}.png", files[tag], f"{tag} exit\n{tag} staff",
            f"{tag} выход", T0, T0 + 4)
        put("INSERT INTO translation_parts(translation_id, n, source, sep, box, text, state, detail)"
            " VALUES (?, 1, ?, '\n', '[93, 146, 491, 214]', ?, 'done', NULL)", f"{tag}tr", f"{tag} exit", f"{tag} выход")
        put("INSERT INTO translation_parts(translation_id, n, source, sep, box, text, state, detail)"
            " VALUES (?, 2, ?, '\n', NULL, NULL, 'failed', 'отказ')", f"{tag}tr", f"{tag} staff")
    est, z = download(studio, ["translate"])
    zf = unzip(z)
    mine = files["mine"]
    assert est["files"] == 1 and [n for n in zf.namelist() if n.startswith("files/")] == [f"files/{mine}.png"]
    assert json.loads(zf.read("translations/0001.json")) == [{
        "id": "minetr", "kind": "photo", "model": "m", "name": "mine.png", "source_lang": "auto", "target_lang": "ru",
        "detected_lang": "en", "result_lang": "ru", "source": "mine exit\nmine staff", "text": "mine выход",
        "state": "error", "detail": "кусок 2 из 2: отказ", "created": T0, "finished": T0 + 4,
        "file": f"files/{mine}.png",
        "parts": [{"n": 1, "source": "mine exit", "box": [93, 146, 491, 214], "text": "mine выход", "state": "done",
                   "detail": None},
                  {"n": 2, "source": "mine staff", "box": None, "text": None, "state": "failed", "detail": "отказ"}]}]
    assert json.loads(zf.read("manifest.json"))["counts"]["translations"] == 1
    assert "translations/" in zf.read("README.txt").decode("utf-8")
    assert not any("theirs" in zf.read(n).decode("utf-8", "replace") for n in zf.namelist() if n.endswith(".json"))


def test_takeout_text_translation_without_parts_texts(studio):
    """Ревью M16: у текста и файла исходник и перевод — только в записи (кусками их не дублировать), куски — номер,
    состояние и слова отказа; тексты кусков с рамками — только у снимка (предыдущий тест)."""
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    put("INSERT INTO translations(id, user_id, kind, connection_id, model, source_lang, target_lang, detected_lang,"
        " result_lang, name, source_media_id, scan_id, source, text, parts, done_parts, state, detail, created,"
        " finished) VALUES ('tx', ?, 'text', 1, 'm', 'en', 'ru', NULL, 'ru', 'First.', NULL, NULL,"
        " 'First.\n\nSecond.', 'Первый.', 2, 1, 'error', 'кусок 2 из 2: отказ', ?, ?)", uid, T0, T0 + 3)
    put("INSERT INTO translation_parts(translation_id, n, source, sep, box, text, state, detail)"
        " VALUES ('tx', 1, 'First.', '\n\n', NULL, 'Первый.', 'done', NULL)")
    put("INSERT INTO translation_parts(translation_id, n, source, sep, box, text, state, detail)"
        " VALUES ('tx', 2, 'Second.', '', NULL, NULL, 'failed', 'отказ')")
    _, z = download(studio, ["translate"])
    [item] = json.loads(unzip(z).read("translations/0001.json"))
    assert (item["kind"], item["source"], item["text"]) == ("text", "First.\n\nSecond.", "Первый.")
    assert item["parts"] == [{"n": 1, "state": "done", "detail": None}, {"n": 2, "state": "failed", "detail": "отказ"}]


def test_export_has_code_chats_and_agent_events(studio):
    """Claude Code: отметки чатов студии и история подключений (с именем компьютера) — страницами; файлов у них
    нет; ключа устройства, подписки push и чужого — нет (спека claude-code 6.1)."""
    bob = make_user(studio, "bob"); uid = make_user(studio, "owner")
    signin(studio, "owner")
    code_history(bob, "theirs")
    aid, _, _ = code_history(uid, "mine")
    est, z = download(studio, ["code", "agents"])
    zf = unzip(z)
    assert est["files"] == 0
    assert set(zf.namelist()) == {"README.txt", "settings.json", "code_chats/0001.json", "code_chats/workspace.json",
                                  "agent_events/0001.json", "manifest.json"}
    assert json.loads(zf.read("code_chats/workspace.json")) == {"panel": None, "place": None, "projects": []}
    assert json.loads(zf.read("code_chats/0001.json")) == [{
        "agent_id": aid, "project": "0123456789abcdef", "session_id": "mine-session", "state": "closed",
        "created": T0, "updated": T0 + 2}]
    events = json.loads(zf.read("agent_events/0001.json"))
    assert [{k: v for k, v in e.items() if k != "id"} for e in events] == [
        {"created": T0, "kind": "enroll", "agent_id": aid, "agent_name": "mine ПК", "device": None, "ip": "10.77.48.5"},
        {"created": T0 + 3, "kind": "pin_ok", "agent_id": aid, "agent_name": "mine ПК", "device": "телефон Safari",
         "ip": "10.77.1.1"}]
    assert list(events[0]) == ["id", "created", "kind", "agent_id", "agent_name", "device", "ip"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["sections"] == ["code", "agents"]
    assert manifest["counts"] == {"code_chats": 1, "agent_events": 2, "files": 0}
    readme = zf.read("README.txt").decode("utf-8")
    assert ("- code_chats/ — чаты Claude Code: компьютер, проект (id), номер сессии, открыт или закрыт; текста нет —\n"
            "  он на компьютере.") in readme
    assert "- agent_events/ — история подключений компьютеров Claude Code." in readme.splitlines()
    assert "ключей устройств" in readme
    assert max(len(line) for line in readme.splitlines()) <= 110          # строки — как у соседних
    assert not any("theirs" in zf.read(n).decode("utf-8", "replace") for n in zf.namelist() if n.endswith(".json"))


def test_export_of_an_empty_section_is_one_empty_page(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    zf = unzip(download(studio, ["speech"])[1])
    assert json.loads(zf.read("speeches/0001.json")) == []
    assert json.loads(zf.read("manifest.json"))["counts"]["speeches"] == 0


def test_export_runs_one_at_a_time_per_user(studio):
    """Вторая загрузка, пока идёт первая, — 409 словами; кончилась первая — можно снова; другим не мешает."""
    uid = make_user(studio, "owner"); bob = make_user(studio, "bob"); signin(studio, "owner")
    history(uid, "mine")
    token = prepare(studio).json()["id"]
    signin(studio, "bob")
    theirs = prepare(studio, ["chat"]).json()["id"]

    async def go():
        first = (await takeout.get_export(token, as_user(uid))).body_iterator
        await first.__anext__()
        with pytest.raises(HTTPException) as busy:
            await takeout.get_export(token, as_user(uid))
        other = await takeout.get_export(theirs, as_user(bob))               # у другого пользователя — своя
        await first.aclose()
        again = await takeout.get_export(token, as_user(uid))
        return (busy.value.status_code, busy.value.detail), other.status_code, again.status_code

    busy, other, again = asyncio.run(go())
    assert busy[0] == 409 and busy[1]["code"] == "busy" and "выгрузка" in busy[1]["message"]
    assert other == again == 200
    gc.collect()                                         # не начатый ответ отпускает очередь, уходя из памяти
    assert not takeout.RUNNING
    assert zipfile.ZipFile(io.BytesIO(b"".join(drain(uid, token)))).testzip() is None


def test_export_keeps_a_file_that_vanishes_after_it_was_opened(studio, monkeypatch):
    """Файл открывается один раз: суммы и данные — с одного дескриптора (на Linux удалённый открытый файл
    читается). Второе открытие пропавшего файла рвало бы всю выгрузку."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    m = history(uid, "mine")
    target = str(media.path(uid, m["clip"]))
    real, opened = builtins.open, []

    def gone_after_first(file, *args, **kwargs):
        if str(file) == target:
            opened.append(1)
            if len(opened) > 1:
                raise FileNotFoundError(target)
        return real(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", gone_after_first)
    zf = unzip(download(studio)[1])
    assert zf.read(f"files/{m['clip']}.mp4") == DATA + b"clip" and len(opened) == 1
    assert json.loads(zf.read("manifest.json"))["missing"] == []


def test_export_with_the_share_gone_midway_breaks_the_answer(studio, env, monkeypatch, caplog):
    """Шару отмонтировали посреди выгрузки: остальные файлы — не «missing» в «целом» архиве, а обрыв."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    token = prepare(studio).json()["id"]

    def unmount(piece):
        if piece[:4] == b"PK\x03\x04" and piece[30:36] == b"files/":
            monkeypatch.setattr(state, "CFG", dataclasses.replace(state.CFG, media_dir=env.media_dir / "gone"))

    with caplog.at_level(logging.INFO, logger="studio"), pytest.raises(media.StorageError):
        drain(uid, token, unmount)
    assert "export: хранилище" in caplog.text and str(env.media_dir) not in caplog.text
    assert not takeout.RUNNING                           # оборванная выгрузка очередь не держит
