"""Картинки раздела «Код»: ограниченная ретрансляция и совместимость старого моста."""

import base64

import pytest

import bridge
import claude_code
from test_code import A, DONE, chat_url, sent, setup
from test_codex import CATALOG

PNG = {"mime": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nscreenshot").decode()}


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_image_only_turn_is_relayed_without_metadata_or_storage(studio, engine, caplog):
    caplog.set_level("INFO", logger="studio")
    aid, machine, link = setup(studio)
    link.hello = bridge.parse_hello({**machine.hello, "codex": CATALOG})
    assert studio.c.get("/api/agents").json()["items"][0]["images"] is True
    machine.script = [DONE]
    endpoint = chat_url(aid, "turn") + ("?engine=codex" if engine == "codex" else "")
    r = studio.c.post(endpoint, json={"text": "", "images": [PNG]})
    assert r.status_code == 200
    [turn] = sent(machine, "turn")
    assert turn["images"] == [PNG] and turn["text"] == ""
    assert turn.get("engine", "claude") == engine
    assert PNG["data"] not in caplog.text


def test_old_bridge_cannot_silently_ignore_images(studio):
    aid, machine, link = setup(studio)
    link.hello.pop("images")
    assert studio.c.get("/api/agents").json()["items"][0]["images"] is False
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "Скрин", "images": [PNG]})
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "unsupported")
    assert not sent(machine, "turn")
    machine.script = [DONE]
    assert studio.c.post(chat_url(aid, "turn"), json={"text": "Без картинки"}).status_code == 200


@pytest.mark.parametrize("images", [[PNG] * 5, [{"mime": "image/svg+xml", "data": "PHN2Zz4="}],
    [{"mime": "image/png", "data": "not base64"}], [{"mime": "image/png", "data": ""}],
    [{"mime": "image/png", "url": "https://example.org/x.png"}]])
def test_bad_images_never_reach_the_bridge(studio, images):
    aid, machine, _ = setup(studio)
    assert studio.c.post(chat_url(aid, "turn"), json={"text": "Вопрос", "images": images}).status_code == 422
    assert not sent(machine, "turn") and not bridge.WATCHERS


def test_image_and_request_limits(studio, monkeypatch):
    aid, machine, _ = setup(studio)
    monkeypatch.setattr(claude_code, "IMAGES_MAX", 20)
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "Вопрос", "images": [PNG, PNG]})
    assert r.status_code == 413
    monkeypatch.setattr(claude_code, "TURN_BODY_MAX", 30)
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "Вопрос", "images": [PNG]})
    assert r.status_code == 413 and not sent(machine, "turn")
