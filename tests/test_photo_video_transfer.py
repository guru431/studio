"""Контракт переноса фото в видео: своя история, PIN и удаление исходника/истории."""
import logging

import media
import state
from conftest import add_gateway, make_user, signin
from test_images import start, wait_state
from test_screenlock import lock_on, phone, set_pin
from test_videos import create_video


def test_browser_multiline_prompt_uses_the_visible_character_limit(studio):
    add_gateway(studio)
    prompt = "строка\n" * 571 + "ещё"
    assert len(prompt) == 4000
    made = create_video(studio, prompt=prompt.replace("\n", "\r\n"))
    assert made.status_code == 201, made.text
    assert studio.gw.video_forms[0][0]["prompt"] == prompt
    assert made.json()["chain"][-1]["prompt"] == prompt


def test_photo_video_context_keeps_ownership_lock_and_history_contracts(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    photo = wait_state(studio, start(studio, prompt="частный кот у окна").json()["id"], {"done"})
    source = photo["media_id"]
    combined = f"Описание исходного фото:\n{photo['prompt']}\n\nДвижение в видео:\nповорачивает голову"

    def create():
        return studio.c.post("/api/videos", data={"prompt": combined}, files={"source_media_id": (None, source)})

    make_user(studio, "foreign")
    signin(studio, "foreign")
    add_gateway(studio)
    assert studio.c.get(f"/api/images/{photo['id']}").status_code == 404
    assert create().status_code == 404
    assert studio.gw.video_forms == []

    signin(studio, "owner")
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    lock_on(uid)
    phone(studio)
    set_pin(uid, "2580")
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.get(f"/api/images/{photo['id']}").status_code == 423
    assert create().status_code == 423
    assert studio.c.post("/api/lock/pin", json={"pin": "2580"}).status_code == 204

    made = create()
    assert made.status_code == 201, made.text
    video = made.json()
    assert video["source_media_id"] == source
    assert [step["prompt"] for step in video["chain"]] == [photo["prompt"], combined]
    assert studio.gw.video_forms[0][0]["prompt"] == combined
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    assert studio.c.delete(f"/api/images/{photo['id']}").status_code == 204
    assert media.path(uid, source).is_file()
    repeated = studio.c.post(f"/api/videos/{video['id']}/repeat", json={"count": 1})
    assert repeated.status_code == 201
    assert studio.gw.video_forms[-1][0]["prompt"] == combined

    assert studio.c.delete("/api/history").status_code == 200
    assert create().status_code == 404
    assert studio.c.get(f"/api/images/{photo['id']}").status_code == 404
    assert studio.c.get("/api/videos").json()["items"] == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert "частный кот у окна" not in caplog.text and "поворачивает голову" not in caplog.text
