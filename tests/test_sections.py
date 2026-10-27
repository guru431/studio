"""Разделы (app/sections.py): подключения раздела, отмеченные модели, умолчание; прежние столбцы
для отката; API профиля и панели."""
import json
import logging

import pytest
from fastapi import HTTPException

import connections
import sections
import state
from conftest import add_connection, add_gateway, make_user


def uid_of_owner() -> int:
    return state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]


def test_gateway_joins_every_section_with_its_catalog(studio):
    cid = add_gateway(studio)
    uid = uid_of_owner()
    # порядок вкладок студии (спека 2026-10-27-studio-ui-tidy, 4.1): API отдаёт разделы в нём
    assert sections.SECTIONS == ("chat", "photo", "video", "transcribe", "speech", "ocr", "dictation")
    assert sections.sections_of(uid, cid) == list(sections.SECTIONS)
    assert [link.models for link in sections.links(uid, "photo")] == [("lustify", "flux")]
    assert sections.default(uid, "photo")[1] == "lustify" and sections.default(uid, "chat")[1] == "local-gemma-abl"
    assert sections.capabilities(uid) == dict.fromkeys(sections.SECTIONS, True)


def test_new_srvllm_connection_gets_ocr(studio):
    """«Распознавание» — встроенный раздел srvllm: новое подключение шлюза входит в него с hunyuan-ocr и
    умолчанием, раздел — только через сервер."""
    cid = add_gateway(studio)
    ocr = next(s for s in studio.c.get("/api/sections").json()["sections"] if s["id"] == "ocr")
    assert ocr["name"] == "Распознавание" and ocr["links"][0]["catalog"] == ["hunyuan-ocr"]
    assert ocr["links"][0]["models"] == ["hunyuan-ocr"] and ocr["default"] == {"connection_id": cid, "model": "hunyuan-ocr"}
    assert "ocr" in sections.SERVER_ONLY and sections.capabilities(uid_of_owner())["ocr"] is True
    assert studio.c.get(f"/api/sections/ocr/links/{cid}/found").json()["items"] == ["hunyuan-ocr"]


def test_openai_cannot_join_video_and_outside_the_list_only_chat(studio):
    add_gateway(studio)
    uid = uid_of_owner()
    cloud = add_connection(studio, base_url="https://cloud.test/v1", sections=["chat"])
    for section, code in (("video", "kind"), ("ocr", "kind"), ("photo", "server"), ("dictation", "server")):
        with pytest.raises(HTTPException) as e:
            sections.link(uid, section, cloud)
        assert e.value.detail["code"] == code, section
        if code == "kind":                                    # слова — о своём разделе, не всегда о видео
            assert f"«{sections.NAMES[section]}» — только через шлюз srvllm" in e.value.detail["message"], section


def test_unchecking_the_default_moves_it_and_empty_section_has_no_default(studio):
    cid = add_gateway(studio)
    uid = uid_of_owner()
    sections.set_models(uid, "photo", cid, ["flux"])
    assert sections.default(uid, "photo")[1] == "flux"
    sections.unlink(uid, "photo", cid)
    assert sections.default(uid, "photo") is None and sections.capabilities(uid)["photo"] is False
    with pytest.raises(HTTPException) as e:
        sections.resolve(uid, "photo", None, None)
    assert e.value.status_code == 409 and "настройках раздела «Фото»" in e.value.detail["message"]


def test_legacy_columns_follow_the_links(studio):
    """Прежний код (откат) читает connections.sections и users.default_*."""
    cid = add_gateway(studio)
    uid = uid_of_owner()
    sections.unlink(uid, "video", cid)
    sections.unlink(uid, "dictation", cid)
    assert json.loads(state.DB.execute("SELECT sections FROM connections").fetchone()[0]) == ["chat", "photo"]
    assert state.DB.execute("SELECT default_connection_id, default_model FROM users").fetchone()[:] == (cid, "local-gemma-abl")
    sections.unlink(uid, "chat", cid)
    assert state.DB.execute("SELECT default_connection_id, default_model FROM users").fetchone()[:] == (None, None)


def test_deleting_a_connection_drops_its_links_and_defaults(studio):
    cid = add_gateway(studio)
    chat_id = studio.c.post("/api/chats", json={}).json()["id"]
    assert studio.c.delete(f"/api/connections/{cid}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM section_links").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM section_defaults").fetchone()[0] == 0
    assert studio.c.get(f"/api/chats/{chat_id}").status_code == 200
    r = studio.c.post(f"/api/chats/{chat_id}/turns", json={"text": "hi"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_resolve_takes_an_explicit_checked_pair_and_finds_a_bare_model(studio):
    cid = add_gateway(studio)
    uid = uid_of_owner()
    assert sections.resolve(uid, "photo", cid, "flux")[1] == "flux"
    assert sections.resolve(uid, "photo", None, "flux") == sections.resolve(uid, "photo", cid, "flux")
    with pytest.raises(HTTPException) as e:
        sections.resolve(uid, "photo", cid, "sdxl")
    assert e.value.status_code == 422 and e.value.detail["code"] == "model"


# ---- API ---------------------------------------------------------------------------

def test_sections_api_round_trip(studio):
    cid = add_gateway(studio)
    body = studio.c.get("/api/sections").json()["sections"]
    assert [s["id"] for s in body] == list(sections.SECTIONS)
    photo = body[1]
    assert photo["name"] == "Фото" and photo["links"][0]["catalog"] == ["lustify", "flux"]
    assert photo["links"][0]["connection"]["id"] == cid and photo["default"] == {"connection_id": cid, "model": "lustify"}
    assert body[0]["links"][0]["catalog"] is None                           # чат srvllm — из /v1/models
    r = studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["lustify"]})
    assert r.status_code == 200 and r.json()["links"][0]["models"] == ["lustify"]
    r = studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["sdxl"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "model"
    assert studio.c.put("/api/sections/photo/default", json={"connection_id": cid, "model": "flux"}).status_code == 422
    assert "local-gemma-abl" in studio.c.get(f"/api/sections/chat/links/{cid}/found").json()["items"]
    assert studio.c.get(f"/api/sections/photo/links/{cid}/found").json()["items"] == ["lustify", "flux"]
    assert studio.c.delete(f"/api/sections/photo/links/{cid}").status_code == 204
    assert studio.c.get("/api/sections").json()["sections"][1]["default"] is None
    r = studio.c.post("/api/sections/photo/links", json={"connection_id": cid})
    assert r.status_code == 200 and r.json()["default"] == {"connection_id": cid, "model": "lustify"}
    assert studio.c.put("/api/sections/music/default", json={"connection_id": cid, "model": "x"}).status_code == 404


def test_foreign_connection_is_gone_everywhere(studio):
    add_gateway(studio)
    bob = make_user(studio, "bob")
    theirs = state.DB.execute("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created)"
                              " VALUES (?, 'b', 'srvllm', 'http://10.77.1.15:4000', x'00', '[]', 0)", (bob,)).lastrowid
    for method, path, body in (("post", "/api/sections/photo/links", {"connection_id": theirs}),
                               ("put", f"/api/sections/photo/links/{theirs}", {"models": ["lustify"]}),
                               ("put", "/api/sections/photo/default", {"connection_id": theirs, "model": "lustify"}),
                               ("delete", f"/api/sections/photo/links/{theirs}", None),
                               ("get", f"/api/sections/chat/links/{theirs}/found", None)):
        r = getattr(studio.c, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone", path
        assert "192.168" not in r.text


def test_direct_connection_models_come_from_the_browser(studio):
    add_gateway(studio)
    cloud = add_connection(studio)
    r = studio.c.get(f"/api/sections/chat/links/{cloud}/found")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "direct"


def test_admin_edits_a_users_sections(studio):
    add_gateway(studio)                                   # owner — администратор
    bob = make_user(studio, "bob")
    cid = studio.c.post(f"/api/admin/users/{bob}/connections", json={"name": "gw", "kind": "srvllm",
        "base_url": "http://10.77.1.15:4000", "token": "k", "sections": ["photo"]}).json()["id"]
    r = studio.c.put(f"/api/admin/users/{bob}/sections/photo/links/{cid}", json={"models": ["flux"]})
    assert r.status_code == 200 and r.json()["default"] == {"connection_id": cid, "model": "flux"}
    assert [s["id"] for s in studio.c.get(f"/api/admin/users/{bob}/sections").json()["sections"]] == list(sections.SECTIONS)
    assert studio.c.get("/api/admin/users/9999/sections").status_code == 404


def test_local_model_is_a_local_role_of_the_gateway_through_the_server(studio):
    """Локальна только `local-*` подключения srvllm через сервер: облачная роль того же шлюза и
    openai-подключение с тем же именем — нет."""
    gw = add_gateway(studio)
    cloud = add_connection(studio, chat_models=("local-x",))
    by_id = {c.id: c for c in connections.list_for(uid_of_owner())}
    assert sections.is_local(by_id[gw], "local-gemma-abl") and sections.is_local(by_id[gw], "local-chat")
    for conn, model in ((gw, "gemma4-abl"), (gw, "claude-sonnet"), (gw, "chat"), (cloud, "local-x")):
        assert not sections.is_local(by_id[conn], model), model
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["gemma4-abl", "local-gemma-abl"]})
    chat = studio.c.get("/api/sections").json()["sections"][0]
    assert {l["connection"]["id"]: l["local_models"] for l in chat["links"]} == {gw: ["local-gemma-abl"], cloud: []}


def test_section_changes_log_only_ids(studio, caplog):
    caplog.set_level(logging.DEBUG)
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["flux"]})
    studio.c.put("/api/sections/photo/default", json={"connection_id": cid, "model": "flux"})
    assert "flux" not in caplog.text and "192.168" not in caplog.text
