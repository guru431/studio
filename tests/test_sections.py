"""Разделы (app/sections.py): подключения раздела, отмеченные модели, умолчание; прежние столбцы
для отката; API профиля и панели."""
import json

import pytest
from fastapi import HTTPException

import sections
import state
from conftest import add_connection, add_gateway


def uid_of_owner() -> int:
    return state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]


def test_gateway_joins_every_section_with_its_catalog(studio):
    cid = add_gateway(studio)
    uid = uid_of_owner()
    assert sections.sections_of(uid, cid) == list(sections.SECTIONS)
    assert [link.models for link in sections.links(uid, "photo")] == [("lustify", "flux")]
    assert sections.default(uid, "photo")[1] == "lustify" and sections.default(uid, "chat")[1] == "local-gemma-abl"
    assert sections.capabilities(uid) == dict.fromkeys(sections.SECTIONS, True)


def test_openai_cannot_join_video_and_outside_the_list_only_chat(studio):
    add_gateway(studio)
    uid = uid_of_owner()
    cloud = add_connection(studio, base_url="https://cloud.test/v1", sections=["chat"])
    for section, code in (("video", "kind"), ("photo", "server"), ("dictation", "server")):
        with pytest.raises(HTTPException) as e:
            sections.link(uid, section, cloud)
        assert e.value.detail["code"] == code, section


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
