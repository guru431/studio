"""The new local models must survive dispatch, history, retries and schema upgrades."""
import pytest

import db
import state
import videos
from conftest import add_gateway
from test_images import start, wait_state
from test_videos import create_video


@pytest.mark.parametrize("model,gateway", [("snofs", "snofs-krea2-v1.6"),
    ("big-love", "big-love-eryn1"), ("fineview", "fineview-qw21")])
def test_new_photo_model_is_selected_and_releases_only_the_other_slot(studio, model, gateway):
    add_gateway(studio)
    response = start(studio, model=model, seed=123)
    assert response.status_code == 202, response.text
    done = wait_state(studio, response.json()["id"], {"done", "error"})
    assert done["state"] == "done" and done["model"] == model
    assert studio.gw.generations[-1][0]["model"] == gateway
    releases = [r for r in studio.gw.requests if r.url.path == "/slot/images/release"]
    assert len(releases) == 1
    assert b'flux-3-image-klein' in releases[0].content


def test_h3_is_persisted_and_repeat_keeps_the_model(studio):
    add_gateway(studio)
    response = create_video(studio, model="h3", prompt="boat on a lake", seconds="3", smooth="true")
    assert response.status_code == 201, response.text
    clip = response.json()
    assert clip["model"] == "h3" and clip["smooth"] is False
    assert studio.gw.video_forms[-1][0]["model"] == videos.MODELS["h3"]
    row = state.DB.execute("SELECT * FROM videos WHERE id = ?", (clip["id"],)).fetchone()
    assert videos._form_of(row)["model"] == videos.MODELS["h3"]
    repeated = studio.c.post(f"/api/videos/{clip['id']}/repeat", json={"count": 1})
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["items"][0]["model"] == "h3"
    assert studio.gw.video_forms[-1][0]["model"] == videos.MODELS["h3"]


def test_h3_long_video_is_refused_before_creating_history(studio):
    add_gateway(studio)
    response = create_video(studio, model="h3", seconds="6")
    assert response.status_code == 422
    assert not studio.gw.video_forms
    assert state.DB.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_previous_schema_defaults_old_videos_to_wan(env):
    conn = db.connect(env.db_path)
    try:
        db.migrate(conn)
        conn.execute("ALTER TABLE videos DROP COLUMN model")
        conn.execute("PRAGMA user_version = 43")
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 44
        column = next(row for row in conn.execute("PRAGMA table_info(videos)") if row[1] == "model")
        assert column[4] == "'wan'"
    finally:
        conn.close()
