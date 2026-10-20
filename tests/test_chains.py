"""Цепочка промптов картинки и ролика (app/chains.py): снимок шагов при создании, старые строки —
по ссылкам, удалённое — «gone», чужое не утекает, в журнал ничего."""
import json
import logging

from PIL import Image

import chains
import media
import state
from conftest import T0, add_gateway, make_user
from test_images import photo, png, start, wait_state


def kinds(item):
    return [s["kind"] for s in item["chain"]]


def test_generation_is_one_step_with_its_own_picture(studio):
    add_gateway(studio)
    item = wait_state(studio, start(studio, prompt="кот", seed=7).json()["id"], {"done"})
    step, = item["chain"]
    assert step["kind"] == "generation" and step["image_id"] == item["id"] and step["prompt"] == "кот"
    assert step["media_id"] == item["media_id"] and step["model"] == "lustify" and step["gone"] is False


def test_edits_add_steps_and_link_the_pictures(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    edit = wait_state(studio, start(studio, prompt="ярче", size="original", model="flux",
                                    source_media_id=gen["media_id"]).json()["id"], {"done"})
    mask = Image.new("L", (64, 48), 0); mask.paste(255, (0, 0, 16, 16))
    r = studio.c.post("/api/images", data={"prompt": "красное платье", "size": "original", "model": "flux",
                                           "source_media_id": edit["media_id"]},
                      files={"mask": ("mask.png", png(mask), "image/png")})
    brush = wait_state(studio, r.json()["id"], {"done"})
    assert kinds(brush) == ["generation", "edit", "brush"]
    assert [s["prompt"] for s in brush["chain"]] == ["кот", "ярче", "красное платье"]
    assert [s["media_id"] for s in brush["chain"]] == [gen["media_id"], edit["media_id"], brush["media_id"]]


def test_deleted_middle_keeps_its_prompt_and_reads_gone(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    edit = wait_state(studio, start(studio, prompt="ярче", size="original", model="flux",
                                    source_media_id=gen["media_id"]).json()["id"], {"done"})
    last = wait_state(studio, start(studio, prompt="ещё", size="original", model="flux",
                                    source_media_id=edit["media_id"]).json()["id"], {"done"})
    assert studio.c.delete(f"/api/images/{edit['id']}").status_code == 204
    chain = studio.c.get(f"/api/images/{last['id']}").json()["chain"]
    assert [s["prompt"] for s in chain] == ["кот", "ярче", "ещё"] and [s["gone"] for s in chain] == [False, True, False]


def test_photo_from_the_device_starts_with_an_upload_step(studio):
    add_gateway(studio)
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "square", "model": "flux"},
                      files={"image": ("p.png", photo(), "image/png")})
    item = wait_state(studio, r.json()["id"], {"done"})
    assert kinds(item) == ["upload", "edit"] and item["chain"][0]["media_id"] == item["source_media_id"]
    assert "prompt" not in item["chain"][0]


def test_repeat_keeps_the_parents_chain(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    body = dict(prompt="ярче", size="original", model="flux", source_media_id=gen["media_id"])
    wait_state(studio, start(studio, **body).json()["id"], {"done"})
    again = wait_state(studio, start(studio, **body).json()["id"], {"done"})
    assert kinds(again) == ["generation", "edit"]


def test_video_chain_ends_with_its_prompt(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    video = studio.c.post("/api/videos", data={"prompt": "поворачивает голову"},
                          files={"source_media_id": (None, gen["media_id"])}).json()
    assert kinds(video) == ["generation", "video"] and video["chain"][-1]["prompt"] == "поворачивает голову"
    assert video["chain"][0]["media_id"] == gen["media_id"]


def test_old_rows_get_a_chain_from_their_links(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    a = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "a.png")
    b = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "b.png")
    for iid, kind, src, mid in (("ia", "generation", None, a), ("ib", "edit", a, b)):
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                         " state, detail, created) VALUES (?, ?, ?, 'lustify', ?, '8x8', 1, ?, ?, 'done', 'готово', ?)",
                         (iid, uid, kind, "p-" + iid, src, mid, T0))
    assert kinds(studio.c.get("/api/images/ib").json()) == ["generation", "edit"]


def test_old_rows_linked_in_a_circle_still_answer(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    a = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "a.png")
    b = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "b.png")
    for iid, src, mid in (("ia", b, a), ("ib", a, b)):
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                         " state, detail, created) VALUES (?, ?, 'edit', 'flux', 'p', '8x8', 1, ?, ?, 'done', 'готово', ?)",
                         (iid, uid, src, mid, T0))
    r = studio.c.get("/api/images/ib")
    assert r.status_code == 200 and 2 <= len(r.json()["chain"]) <= chains.MAX_DEPTH + 1


def test_a_foreign_source_leaks_nothing(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="чужой секрет").json()["id"], {"done"})
    make_user(studio, "bob")
    bob = state.DB.execute("SELECT id FROM users WHERE login = 'bob'").fetchone()[0]
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                     " state, detail, created) VALUES ('ix', ?, 'edit', 'flux', 'своё', '8x8', 1, ?, NULL, 'done', 'готово', ?)",
                     (bob, gen["media_id"], T0))
    chain = chains.annotate(bob, chains.of_image(bob, state.DB.execute("SELECT * FROM images WHERE id = 'ix'").fetchone()))
    assert chain[0] == {"kind": "upload"} and "чужой секрет" not in json.dumps(chain, ensure_ascii=False)


def test_chain_prompts_never_reach_the_log(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="тайный промпт цепочки").json()["id"], {"done"})
    wait_state(studio, start(studio, prompt="тайная правка", size="original", model="flux",
                             source_media_id=gen["media_id"]).json()["id"], {"done"})
    assert "тайный промпт цепочки" not in caplog.text and "тайная правка" not in caplog.text
