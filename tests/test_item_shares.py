"""Ссылка «Поделиться» на запись другого раздела (шаг 69) и «Обновить снимок» под тем же адресом и PIN."""
import json
from urllib.parse import urlsplit

import pytest

import chat_shares
import lock_policy
import media
import state
from conftest import erase_history, make_user, signin
from test_chat_shares import create, regular, unlock
from test_owui_read import BASE, setup, talk  # noqa: F401 — `setup` — фикстура


def owner(s):
    uid = make_user(s, "owner")
    signin(s, "owner")
    return uid


def transcript(uid):
    result = media.save(uid, json.dumps({"segments": [
        {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "text": "Добрый день"},
        {"start": 3.0, "end": 5.0, "speaker": "SPEAKER_01", "text": "Здравствуйте"}]}).encode(),
        "application/json", "result.json")
    audio = media.save(uid, b"mp3 bytes", "audio/mpeg", "meeting.mp3")
    state.DB.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, result_media_id, duration,"
                     " speakers, state, detail, created, edits) VALUES ('t1', ?, 'whisperx', 'meeting.mp3', ?, ?, 5,"
                     " 2, 'done', 'готово', 0, ?)",
                     (uid, audio, result, json.dumps({"names": {"SPEAKER_00": "Анна"}})))
    return "/api/transcripts/t1/share", audio


def translation(uid):
    state.DB.execute("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, result_lang, name,"
                     " source, parts, done_parts, state, detail, created) VALUES ('tl', ?, 'text', 'm', 'en', 'ru',"
                     " 'ru', 'Contract', 'Contract text', 1, 1, 'done', 'готово', 0)", (uid,))
    state.DB.execute("INSERT INTO translation_parts(translation_id, n, source, text, state)"
                     " VALUES ('tl', 1, 'Contract text', 'Текст договора', 'done')")
    return "/api/translate/tl/share", None


def scan(uid):
    pdf = media.save(uid, b"%PDF-1.4 scan", "application/pdf", "scan.pdf")
    state.DB.execute("INSERT INTO scans(id, user_id, model, name, source_media_id, origin, pages, done_pages, state,"
                     " detail, created) VALUES ('sc', ?, 'hunyuan-ocr', 'scan.pdf', ?, 'section', 2, 2, 'done',"
                     " 'готово', 0)", (uid, pdf))
    state.DB.executemany("INSERT INTO scan_pages(scan_id, user_id, n, state, text) VALUES ('sc', ?, ?, ?, ?)",
                         [(uid, 1, "done", "# Страница один"), (uid, 2, "failed", None)])
    return "/api/ocr/sc/share", pdf


def image(uid):
    mid = media.save(uid, b"png bytes", "image/png", "picture.png")
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created)"
                     " VALUES ('im', ?, 'generation', 'flux', 'a red fox', '1024x1024', 1, ?, 'done', 'готово', 0)",
                     (uid, mid))
    return "/api/images/im/share", mid


def video(uid):
    mid = media.save(uid, b"mp4 bytes", "video/mp4", "clip.mp4")
    state.DB.execute("INSERT INTO videos(id, user_id, connection_id, source_media_id, media_id, prompt, seconds, quality,"
                     " smooth, state, detail, created) VALUES ('vd', ?, 1, 'src', ?, 'waves', 5, 'fast', 0, 'done',"
                     " 'готово', 0)", (uid, mid))
    return "/api/videos/vd/share", mid


ITEMS = {"transcripts": (transcript, "t1"), "translations": (translation, "tl"), "scans": (scan, "sc"),
         "images": (image, "im"), "videos": (video, "vd")}


def speech_chat(uid):
    """Чат озвучки `sc1`: две готовые записи с mp3 голосом «Анна» и отменённая третья."""
    a = media.save(uid, b"mp3-1", "audio/mpeg", "one.mp3")
    b = media.save(uid, b"mp3-2", "audio/mpeg", "two.mp3")
    state.DB.executemany(
        "INSERT INTO speeches(id, user_id, model, voice, voice_name, text, media_id, duration, state, detail, created,"
        " chat_id, chat_created) VALUES (?, ?, 'qwen3-tts', 'v1', 'Анна', ?, ?, 2.5, ?, 'готово', ?, 'sc1', 0)",
        [("s1", uid, "Первый текст", a, "done", 1), ("s2", uid, "Второй текст", b, "done", 2),
         ("s3", uid, "Третий текст", None, "cancelled", 3)])
    return "/api/speeches/chats/sc1/share", (a, b)


def test_recipient_sees_each_record_and_previous_code_reads_only_a_placeholder(studio):
    uid = owner(studio)
    opened = {}
    for kind, (make, _) in ITEMS.items():
        endpoint, _ = make(uid)
        out, token = create(studio, endpoint)
        assert studio.c.get(endpoint).json() == out
        opened[kind] = unlock(studio, token).json()
        # прежний код (откат) показывает `messages` — заглушку, а не запись
        assert opened[kind]["messages"] == chat_shares.PLACEHOLDER["messages"]
        assert opened[kind]["item"]["kind"] == kind and "attachments" not in opened[kind]["item"]
    segments = opened["transcripts"]["item"]["segments"]
    assert [(s["speaker"], s["start"], s["text"]) for s in segments] == [("Анна", 0.0, "Добрый день"),
                                                                          ("SPEAKER_01", 3.0, "Здравствуйте")]
    assert opened["transcripts"]["title"] == "meeting.mp3"
    assert opened["translations"]["item"] | {"kind": None} == {
        "kind": None, "source": "Contract text", "text": "Текст договора", "source_lang": "en", "target_lang": "ru",
        "markdown": False}
    assert [(p["n"], p["state"], p["text"]) for p in opened["scans"]["item"]["pages"]] == [
        (1, "done", "# Страница один"), (2, "failed", None)]
    picture = opened["images"]["item"]
    assert picture["prompt"] == "a red fox" and picture["file"]["mime"] == "image/png"
    studio.c.cookies.clear()
    response = studio.c.post("/share/file", json={"access": opened["images"]["access"], "file": picture["file"]["file"]})
    assert response.status_code == 200 and response.content == b"png bytes"
    assert response.headers["cache-control"] == "no-store"
    clip = opened["videos"]["item"]["file"]["file"]
    assert studio.c.post("/share/file", json={"access": opened["videos"]["access"], "file": clip}).content == b"mp4 bytes"
    # файл чужой ссылки этим доступом не взять
    assert studio.c.post("/share/file", json={"access": opened["images"]["access"], "file": clip}).status_code == 404


def test_scan_snapshot_takes_the_edited_page_text(studio):
    """05:I6: снимок ссылки на скан — правленый текст страницы, не распознанный."""
    uid = owner(studio)
    endpoint, _ = scan(uid)
    assert studio.c.patch("/api/ocr/sc/pages/1", json={"text": "Сумма: 1 600 руб."}).status_code == 200
    _, token = create(studio, endpoint)
    assert unlock(studio, token).json()["item"]["pages"][0]["text"] == "Сумма: 1 600 руб."


@pytest.mark.parametrize("kind", ["transcripts", "scans"])
def test_source_file_goes_into_the_link_only_when_chosen(studio, kind):
    uid = owner(studio)
    endpoint, source = ITEMS[kind][0](uid)
    _, token = create(studio, endpoint)
    assert "attachments" not in unlock(studio, token).json()["item"]
    response = studio.c.post(endpoint, json={"pin": "0123", "source": True})
    opened = unlock(studio, urlsplit(response.json()["url"]).fragment).json()
    assert opened["item"]["attachments"][0]["file"] == source
    assert studio.c.post("/share/file", json={"access": opened["access"], "file": source}).status_code == 200


@pytest.mark.parametrize("kind", list(ITEMS))
def test_trash_hides_the_link_restore_reopens_it_and_purge_deletes_it(studio, kind):
    uid = owner(studio)
    make, item_id = ITEMS[kind]
    endpoint, _ = make(uid)
    _, token = create(studio, endpoint)
    access = unlock(studio, token).json()["access"]
    state.DB.execute(f"UPDATE {kind} SET deleted = 1 WHERE id = ?", (item_id,))
    assert unlock(studio, token).status_code == 404
    assert studio.c.post("/share/check", json={"access": access}).status_code == 404
    listed = studio.c.get("/api/me/access").json()["shares"]
    assert [(s["kind"], s["item_id"], s["trashed"]) for s in listed] == [(chat_shares.ITEMS[kind][0], item_id, True)]
    state.DB.execute(f"UPDATE {kind} SET deleted = NULL WHERE id = ?", (item_id,))
    assert unlock(studio, token).status_code == 200
    state.DB.execute(f"DELETE FROM {kind} WHERE id = ?", (item_id,))      # окончательное удаление — триггер шага 69
    assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0


def test_item_link_follows_section_lock_ownership_history_and_journal(studio):
    uid = owner(studio)
    endpoint, mid = image(uid)
    _, token = create(studio, endpoint)
    assert lock_policy.media_sections(uid, mid) == {"photo"}
    listed = studio.c.get("/api/me/access").json()["shares"][0]
    assert (listed["kind"], listed["title"], listed["trashed"]) == ("photo", "a red fox", False)
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"photo\"]' WHERE id=?", (uid,))
    state.DB.execute("UPDATE sessions SET seen=0")
    assert studio.c.put(endpoint, json={}).status_code == 423
    state.DB.execute("UPDATE users SET screen_lock=0 WHERE id=?", (uid,))
    assert studio.c.put(endpoint, json={}).status_code == 200
    make_user(studio, "friend")
    signin(studio, "friend")
    for method in ("GET", "POST", "PUT", "DELETE"):
        assert studio.c.request(method, endpoint, json={"pin": "0123"}).status_code == 404
    signin(studio, "owner")
    kinds = [(e["kind"], e["detail"]) for e in studio.c.get("/api/me/security-events").json()["items"]]
    assert ("share_create", "photo") in kinds and ("share_refresh", "photo") in kinds
    assert erase_history(studio).status_code == 200
    assert unlock(studio, token).status_code == 404


def test_unfinished_records_cannot_be_shared(studio):
    uid = owner(studio)
    image(uid)
    state.DB.execute("UPDATE images SET state = 'working' WHERE id = 'im'")
    assert studio.c.post("/api/images/im/share", json={"pin": "0123"}).json()["detail"]["code"] == "running"
    translation(uid)
    state.DB.execute("UPDATE translation_parts SET state = 'waiting'")
    assert studio.c.post("/api/translate/tl/share", json={"pin": "0123"}).status_code == 409
    assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0


def test_refresh_keeps_address_pin_lifetime_and_counters_and_replaces_the_snapshot(studio):
    endpoint, _ = regular(studio)
    out, token = create(studio, endpoint)
    visitor = unlock(studio, token).json()["access"]
    assert unlock(studio, token, "9999").status_code == 403
    state.DB.execute("UPDATE messages SET content = 'Changed' WHERE id = 'chat-a'")
    refreshed = studio.c.put(endpoint, json={"title": "Чат"})
    assert refreshed.status_code == 200, refreshed.text
    after = refreshed.json()
    assert (after["url"], after["created"], after["expires"], after["views"], after["fails"]) == (
        out["url"], out["created"], out["expires"], 1, 1)
    assert studio.c.post("/share/check", json={"access": visitor}).status_code == 204
    opened = unlock(studio, token).json()
    assert opened["messages"][1]["text"] == "Changed" and opened["messages"][1]["reasoning"] is None
    assert studio.c.delete(endpoint).status_code == 204
    assert studio.c.put(endpoint, json={}).json()["detail"]["code"] == "share_gone"


def test_refresh_of_a_record_link_replaces_its_files(studio):
    uid = owner(studio)
    endpoint, audio = transcript(uid)
    response = studio.c.post(endpoint, json={"pin": "0123", "source": True})
    token = urlsplit(response.json()["url"]).fragment
    access = unlock(studio, token).json()["access"]
    assert studio.c.put(endpoint, json={}).status_code == 200
    assert studio.c.post("/share/file", json={"access": access, "file": audio}).status_code == 404
    assert "attachments" not in unlock(studio, token).json()["item"]


def test_speech_chat_link_carries_texts_and_audio_of_ready_records(studio):
    uid = owner(studio); endpoint, (a, b) = speech_chat(uid); out, token = create(studio, endpoint)
    assert studio.c.get(endpoint).json() == out
    opened = unlock(studio, token).json()
    assert opened["messages"] == chat_shares.PLACEHOLDER["messages"] and opened["title"] == "Первый текст"
    assert [(r["text"], r["voice"], r["file"]["mime"]) for r in opened["item"]["records"]] == [
        ("Первый текст", "Анна", "audio/mpeg"), ("Второй текст", "Анна", "audio/mpeg")]
    assert opened["item"]["kind"] == "speech_chats" and opened["item"]["records"][0]["duration"] == 2.5
    second = opened["item"]["records"][1]["file"]["file"]
    assert second == b
    assert studio.c.post("/share/file", json={"access": opened["access"], "file": second}).content == b"mp3-2"
    row = state.DB.execute("SELECT item_kind, item_id FROM chat_shares").fetchone()
    assert tuple(row) == ("speech_chats", "sc1")
    listed = studio.c.get("/api/me/access").json()["shares"]
    assert [(s["kind"], s["item_id"], s["title"], s["trashed"]) for s in listed] == [("speech", "sc1", "Первый текст", False)]
    kinds = [(e["kind"], e["detail"]) for e in studio.c.get("/api/me/security-events").json()["items"]]
    assert ("share_create", "speech") in kinds
    # блокировка раздела «Озвучка» закрывает и ссылку
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"speech\"]' WHERE id=?", (uid,))
    state.DB.execute("UPDATE sessions SET seen=0")
    locked = studio.c.put(endpoint, json={})
    assert locked.status_code == 423 and locked.json()["detail"]["code"] == "screen_locked"
    state.DB.execute("UPDATE users SET screen_lock=0 WHERE id=?", (uid,))
    assert studio.c.put(endpoint, json={}).status_code == 200
    # чат с идущей записью и чат без готовых — 409 `running`, ссылки нет
    state.DB.executemany(
        "INSERT INTO speeches(id, user_id, model, voice, voice_name, text, state, detail, created, chat_id, chat_created)"
        " VALUES (?, ?, 'qwen3-tts', 'v1', 'Анна', 'Ещё', ?, '', ?, ?, 5)",
        [("r1", uid, "done", 5, "sc2"), ("r2", uid, "working", 6, "sc2"), ("r3", uid, "cancelled", 7, "sc3")])
    for cid in ("sc2", "sc3"):
        refused = studio.c.post(f"/api/speeches/chats/{cid}/share", json={"pin": "0123"})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "running"
    assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 1
    # чужой чат — 404 на все четыре метода
    make_user(studio, "friend")
    signin(studio, "friend")
    for method in ("GET", "POST", "PUT", "DELETE"):
        assert studio.c.request(method, endpoint, json={"pin": "0123"}).status_code == 404
    signin(studio, "owner")
    assert studio.c.delete("/api/speeches/chats/sc1").status_code == 204             # чат — в корзину
    assert unlock(studio, token).status_code == 404 and studio.c.get("/api/me/access").json()["shares"][0]["trashed"] is True
    state.DB.execute("UPDATE speeches SET deleted = NULL WHERE chat_id = 'sc1'"); assert unlock(studio, token).status_code == 200
    state.DB.execute("DELETE FROM speeches WHERE id = 's1'"); assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 1
    assert lock_policy.media_sections(uid, a) == {"speech"}      # файл удалённой записи держит только снимок ссылки
    state.DB.execute("DELETE FROM speeches WHERE chat_id = 'sc1'"); assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0


def cancelled_then_voiced(studio) -> str:
    """Чат озвучки `sc9`: первая запись отменена, вторая озвучена; ссылка создана — её токен."""
    uid = owner(studio)
    voiced = media.save(uid, b"mp3", "audio/mpeg", "x.mp3")
    state.DB.executemany(
        "INSERT INTO speeches(id, user_id, model, voice, voice_name, text, media_id, duration, state, detail, created,"
        " chat_id, chat_created) VALUES (?, ?, 'qwen3-tts', 'v1', 'Анна', ?, ?, 1, ?, '', ?, 'sc9', 0)",
        [("c1", uid, "Отменённая", None, "cancelled", 1), ("c2", uid, "Озвученная", voiced, "done", 2)])
    return create(studio, "/api/speeches/chats/sc9/share")[1]


def test_speech_chat_snapshot_title_is_the_first_voiced_record(studio):
    assert unlock(studio, cancelled_then_voiced(studio)).json()["title"] == "Озвученная"


def test_speech_chat_title_in_access_list_skips_trashed_records(studio):
    """Название в «Доступах и ссылках» — первая запись не из корзины, пока такие есть; весь чат в корзине — первая."""
    cancelled_then_voiced(studio)
    assert studio.c.get("/api/me/access").json()["shares"][0]["title"] == "Отменённая"
    state.DB.execute("UPDATE speeches SET deleted = 5 WHERE id = 'c1'")          # первая запись — в корзину
    listed = studio.c.get("/api/me/access").json()["shares"]
    assert [(s["title"], s["trashed"]) for s in listed] == [("Озвученная", False)]
    state.DB.execute("UPDATE speeches SET deleted = 5")                          # весь чат — в корзину
    listed = studio.c.get("/api/me/access").json()["shares"]
    assert [(s["title"], s["trashed"]) for s in listed] == [("Отменённая", True)]


def test_every_share_kind_has_a_live_rule():
    """Вид ссылки без условия в `_ALIVE` для LIVE «недоступен» всегда — новый вид не забыть."""
    assert set(chat_shares._ALIVE) == set(chat_shares.ITEMS) | {chat_shares.OWUI}
    assert set(chat_shares.SECTIONS) == set(chat_shares._ALIVE)


def test_previous_live_rule_hides_links_of_the_new_kinds(setup):                    # откат: SQL-обход не нужен
    s, fake, aid = setup
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    create(s, f"{BASE}/{aid}/chats/{talk(fake)}/share")
    create(s, speech_chat(uid)[0])
    old = (" AND (s.chat_id IS NULL OR s.chat_id IN (SELECT id FROM chats WHERE deleted IS NULL AND protected_used = 0))"
           " AND (s.item_kind IS NULL OR " + " OR ".join(f"(s.item_kind = '{t}' AND s.item_id IN (SELECT id FROM {t}"
           " WHERE deleted IS NULL))" for t in ("transcripts", "translations", "scans", "images", "videos")) + ")")
    for kind in ("speech_chats", "owui"):                # строки ссылок обоих видов созданы новым кодом
        assert state.DB.execute("SELECT 1 FROM chat_shares s WHERE s.item_kind = ?" + old, (kind,)).fetchone() is None
        assert state.DB.execute("SELECT 1 FROM chat_shares s WHERE s.item_kind = ?" + chat_shares.LIVE, (kind,)).fetchone()
