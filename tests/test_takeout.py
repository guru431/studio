"""Выгрузка своей истории одним ZIP (app/takeout.py, спека 2026-10-21-studio-export-restore-check)."""
import asyncio
import dataclasses
import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime

import auth
import db
import media
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
    assert set(names) == {"README.txt", "settings.json", "chats/minechat1.json", "chats/minechat2.json", "images.json",
                          "videos.json", "transcripts.json", "speeches.json", "voices.json", "manifest.json"} | files
    assert {zipfile.ZIP_STORED} == {i.compress_type for i in zf.infolist()}
    chat = json.loads(zf.read("chats/minechat1.json"))
    assert chat["title"] == "mine title 1" and [x["content"] for x in chat["messages"]] == ["mine question 1",
                                                                                              "mine answer 1"]
    att = chat["messages"][0]["attachments"]
    assert att == [{"id": "mineatt", "kind": "document", "name": "mine.pdf", "chars": 5, "text": "mine doc text",
                    "file": f"files/{m['doc']}.pdf"}]
    image = json.loads(zf.read("images.json"))[0]
    assert image["prompt"] == "mine prompt" and image["file"] == f"files/{m['img']}.png"
    assert image["source_file"] == f"files/{m['src']}.jpg" and image["mask_file"] == f"files/{m['mask']}.png"
    assert image["chain"][-1]["prompt"] == "mine prompt"
    video = json.loads(zf.read("videos.json"))[0]
    assert video["file"] == f"files/{m['clip']}.mp4" and video["source_file"] == f"files/{m['img']}.png"
    tr = json.loads(zf.read("transcripts.json"))[0]
    assert tr["name"] == "mine meeting" and tr["file"].endswith(".mp3") and tr["result_file"].endswith(".json")
    assert json.loads(zf.read("speeches.json"))[0]["text"] == "mine spoken"
    assert json.loads(zf.read("voices.json"))[0]["ref_text"] == "mine ref text"
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
    assert {n for n in names if not n.startswith("files/")} == {"README.txt", "settings.json", "images.json",
                                                               "manifest.json"}
    assert json.loads(unzip(z).read("manifest.json"))["sections"] == ["photo"]


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
    history(uid, "mine")
    _, z = download(studio)
    settings = json.loads(unzip(z).read("settings.json"))
    assert settings["connections"][0] == {"id": settings["connections"][0]["id"], "name": "openai", "kind": "openai",
                                          "base_url": "https://cloud.test/v1"}
    assert settings["profile"]["login"] == "owner" and settings["sections"]["links"]
    pw_hash = state.DB.execute("SELECT pw_hash FROM users WHERE id = ?", (uid,)).fetchone()[0]
    token_enc = state.DB.execute("SELECT token_enc FROM connections").fetchone()[0]
    session = state.DB.execute("SELECT digest, csrf FROM sessions WHERE user_id = ?", (uid,)).fetchone()
    for secret in (pw_hash.split("$")[-1].encode(), token_enc, b"cloud-key-very-secret", session["digest"].encode(),
                   session["csrf"].encode(), b"pk-credential-id", b"token", b"pw_hash", b"csrf"):
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

    def broken(file):
        raise OSError(f"[Errno 5] Input/output error: '{file}'")

    monkeypatch.setattr(takeout, "_sums", broken)
    with caplog.at_level(logging.INFO, logger="studio"):
        try:
            studio.c.get(f"/api/me/export/{token}")
        except media.StorageError:
            pass
        else:
            raise AssertionError("ответ не оборвался")
    assert "export: хранилище оборвало чтение" in caplog.text
    assert str(env.media_dir) not in caplog.text and "mine" not in caplog.text


def test_export_holds_transcript_edits_and_presets_when_the_base_has_them(studio):
    """Правки расшифровки (шаг 11) и пресеты фото (шаг 12) есть в базе — есть и в выгрузке, чужие — нет."""
    bob = make_user(studio, "bob")
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    put("UPDATE transcripts SET edits = ?", json.dumps({"segments": {"0": "fixed"}}))
    put("INSERT INTO presets VALUES ('p1', ?, 'photo', 'night', ?, ?)", uid, json.dumps({"model": "lustify"}), T0)
    put("INSERT INTO presets VALUES ('p2', ?, 'photo', 'theirs', '{}', ?)", bob, T0)
    _, z = download(studio)
    zf = unzip(z)
    assert json.loads(zf.read("transcripts.json"))[0]["edits"] == {"segments": {"0": "fixed"}}
    assert json.loads(zf.read("settings.json"))["presets"] == [
        {"id": "p1", "section": "photo", "name": "night", "params": {"model": "lustify"}, "created": T0}]


def test_export_without_edits_and_presets_says_nothing_of_them(studio):
    """База без столбца правок и таблицы пресетов (как до шагов 11 и 12) выгружается без них."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    history(uid, "mine")
    state.DB.executescript("ALTER TABLE transcripts DROP COLUMN edits; DROP TABLE presets;")
    _, z = download(studio)
    zf = unzip(z)
    assert "edits" not in json.loads(zf.read("transcripts.json"))[0]
    assert "presets" not in json.loads(zf.read("settings.json"))
