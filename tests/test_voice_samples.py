"""Editable voice samples, ownership, selected references and export/cleanup contracts."""
import asyncio

from fastapi import HTTPException

import media
import state
import voices
from conftest import add_gateway, make_user, signin
from test_voices import SAMPLE, create


def add(studio, vid, text="Второй образец"):
    return studio.c.post(f"/api/voices/{vid}/samples", data={"text": text}, files=SAMPLE)


def test_voice_list_is_alphabetical_and_rename_updates_its_position(studio, fake_audio):
    add_gateway(studio)
    fake_audio.duration = 8
    names = ["Яна", "ёж", "Ева", "борис", "Анна", "Anna"]
    ids = {name: create(studio, name=name).json()["id"] for name in names}
    assert [v["name"] for v in studio.c.get("/api/voices").json()["items"]] == [
        "Anna", "Анна", "борис", "Ева", "ёж", "Яна"]
    studio.c.patch(f"/api/voices/{ids['Яна']}", json={"name": "Ада"})
    assert [v["name"] for v in studio.c.get("/api/voices").json()["items"]] == [
        "Anna", "Ада", "Анна", "борис", "Ева", "ёж"]


def test_every_saved_sample_has_private_playable_media(studio, fake_audio):
    add_gateway(studio)
    fake_audio.duration = 8
    vid = create(studio).json()["id"]
    add(studio, vid)
    samples = studio.c.get(f"/api/voices/{vid}").json()["samples"]
    assert len({s["media_id"] for s in samples}) == 2
    for sample in samples:
        response = studio.c.get(f"/api/media/{sample['media_id']}")
        assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    make_user(studio, "bob"); signin(studio, "bob")
    for sample in samples:
        assert studio.c.get(f"/api/media/{sample['media_id']}").status_code == 404


def test_minute_sample_and_long_transcript_are_kept(studio, fake_audio):
    add_gateway(studio)
    fake_audio.duration = 75
    text = "Речь. " * 160
    vid = create(studio, text=text).json()["id"]
    voice = studio.c.get(f"/api/voices/{vid}").json()
    assert voice["duration"] == 60 and voice["text"] == text.strip()
    assert voice["sample_count"] == 1 and voice["samples"][0]["active"]


def test_edit_select_replace_and_remove_samples(studio, fake_audio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    fake_audio.duration = 8
    vid = create(studio).json()["id"]
    second = add(studio, vid).json()["id"]
    third = add(studio, vid, "Третий").json()["id"]
    assert studio.c.patch(f"/api/voices/{vid}", json={"name": "Новое имя"}).status_code == 200
    assert studio.c.patch(f"/api/voices/{vid}/samples/{second}",
                          json={"text": "Исправлено", "active": True}).status_code == 200
    assert asyncio.run(voices.reference(uid, vid))[1] == "Исправлено"
    old_mid = state.DB.execute("SELECT media_id FROM voice_samples WHERE id = ?", (second,)).fetchone()[0]
    fake_audio.duration = 60
    assert studio.c.put(f"/api/voices/{vid}/samples/{second}",
                        data={"text": "Новая запись"}, files=SAMPLE).status_code == 200
    voice = studio.c.get(f"/api/voices/{vid}").json()
    assert voice["name"] == "Новое имя" and voice["duration"] == 60
    assert voice["text"] == "Новая запись" and voice["sample_count"] == 3
    assert state.DB.execute("SELECT 1 FROM media WHERE id = ?", (old_mid,)).fetchone() is None
    assert studio.c.delete(f"/api/voices/{vid}/samples/{second}").status_code == 204
    assert studio.c.delete(f"/api/voices/{vid}/samples/{third}").status_code == 204
    assert studio.c.delete(f"/api/voices/{vid}/samples/{vid}").status_code == 422
    assert studio.c.get(f"/api/voices/{vid}").json()["samples"][0]["active"]
    assert studio.c.delete(f"/api/voices/{vid}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM voice_samples").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_samples_are_private_bounded_and_exported(studio, fake_audio, monkeypatch):
    monkeypatch.setattr(voices, "SAMPLES_MAX", 3)
    add_gateway(studio)
    fake_audio.duration = 8
    vid = create(studio).json()["id"]
    sid = add(studio, vid).json()["id"]
    owner = state.DB.execute("SELECT user_id FROM voices WHERE id = ?", (vid,)).fetchone()[0]
    mid = state.DB.execute("SELECT media_id FROM voice_samples WHERE id = ?", (sid,)).fetchone()[0]
    assert media.referenced(owner, mid)
    assert studio.c.patch(f"/api/voices/{vid}/samples/{sid}", json={"text": " "}).status_code == 422
    other = create(studio, name="Другой").json()["id"]
    assert studio.c.patch(f"/api/voices/{other}/samples/{sid}", json={"active": True}).status_code == 404
    make_user(studio, "bob"); signin(studio, "bob")
    assert add(studio, vid).status_code == 404
    assert studio.c.patch(f"/api/voices/{vid}", json={"name": "Чужое"}).status_code == 404
    assert studio.c.put(f"/api/voices/{vid}/samples/{sid}", data={"text": "Чужое"}, files=SAMPLE).status_code == 404
    assert studio.c.patch(f"/api/voices/{vid}/samples/{sid}", json={"active": True}).status_code == 404
    assert studio.c.delete(f"/api/voices/{vid}/samples/{sid}").status_code == 404
    signin(studio, "owner")
    for _ in range(voices.SAMPLES_MAX - 2):
        assert add(studio, vid).status_code == 201
    assert add(studio, vid).status_code == 422
    import takeout
    rows = takeout._media_rows(owner, ("voices",))
    exported = takeout._voices(owner, [voices._row(owner, vid)], {r["id"]: takeout._file_name(r) for r in rows})
    assert len(exported[0]["samples"]) == voices.SAMPLES_MAX
    assert all(s["file"] for s in exported[0]["samples"])
    assert studio.c.delete("/api/history").status_code == 200
    assert state.DB.execute("SELECT count(*) FROM voice_samples").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_voice_deleted_during_conversion_does_not_leave_a_sample(studio, fake_audio, monkeypatch):
    import audio
    add_gateway(studio)
    fake_audio.duration = 8
    vid = create(studio).json()["id"]
    original = audio.to_wav

    async def remove(src, dst, *, max_sec):
        duration = await original(src, dst, max_sec=max_sec)
        state.DB.execute("DELETE FROM voices WHERE id = ?", (vid,))
        return duration

    monkeypatch.setattr(audio, "to_wav", remove)
    before = state.DB.execute("SELECT count(*) FROM media").fetchone()[0]
    assert add(studio, vid).status_code == 404
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == before
    assert state.DB.execute("SELECT count(*) FROM voice_samples").fetchone()[0] == 0


def test_sample_save_after_history_clear_is_rejected(studio, fake_audio, monkeypatch):
    import operations
    add_gateway(studio)
    fake_audio.duration = 8
    vid = create(studio).json()["id"]
    uid = state.DB.execute("SELECT user_id FROM voices WHERE id = ?", (vid,)).fetchone()[0]
    original = media.save_async

    async def clear_after_save(*args, **kwargs):
        mid = await original(*args, **kwargs)
        with operations.barrier(uid):
            state.DB.execute("DELETE FROM voices WHERE user_id = ?", (uid,))
        return mid

    monkeypatch.setattr(media, "save_async", clear_after_save)
    assert add(studio, vid).status_code == 409
    assert state.DB.execute("SELECT count(*) FROM voice_samples").fetchone()[0] == 0
    # Only the original file remains for the history cleanup path; the new file was removed.
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 1


def test_failed_replacement_rolls_back_both_references_and_deletes_new_file(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    fake_audio.duration = 8
    vid = create(studio).json()["id"]
    before = studio.c.get(f"/api/voices/{vid}").json()

    def reject(*args):
        raise HTTPException(409, {"code": "changed", "message": "changed"})

    monkeypatch.setattr(voices, "_select", reject)
    response = studio.c.put(f"/api/voices/{vid}/samples/{vid}", data={"text": "Замена"}, files=SAMPLE)
    assert response.status_code == 409
    assert studio.c.get(f"/api/voices/{vid}").json() == before
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 1


def test_sample_storage_failure_keeps_the_previous_recording(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    fake_audio.duration = 8
    vid = create(studio).json()["id"]

    async def fail(*args):
        raise media.StorageError()

    monkeypatch.setattr(media, "save_async", fail)
    response = studio.c.put(f"/api/voices/{vid}/samples/{vid}", data={"text": "Замена"}, files=SAMPLE)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "storage"
    assert studio.c.get(f"/api/voices/{vid}").json()["sample_count"] == 1
