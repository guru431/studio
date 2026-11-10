"""Восстановление изображения сообщения без его хранения на сервере и без обхода PIN/CSRF."""

import base64

import pytest

import bridge
import claude_code
from test_code import A, chat_url, detail, sent, setup
from test_codex import CATALOG
from test_media import png


def prepare(studio, *, opened=True, engine="claude"):
    aid, machine, link = setup(studio, pin="2468", opened=opened)
    machine.history_images = True
    if engine == "codex":
        machine.codex = CATALOG
        machine.projects["alpha"]["chats"][A]["engine"] = engine
    link.hello = bridge.parse_hello(machine.hello)
    raw = png(120, 70)
    payload = {"mime": "image/png", "data": base64.b64encode(raw).decode("ascii")}
    machine.projects["alpha"]["chats"][A]["image_data"] = {("message", 0): payload}
    endpoint = chat_url(aid, "image") + ("?engine=codex" if engine == "codex" else "")
    return machine, link, raw, payload, endpoint


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_history_image_requires_pin_and_returns_private_verified_original(studio, caplog, engine):
    machine, link, raw, payload, endpoint = prepare(studio, opened=False, engine=engine)
    body = {"message_id": "message", "index": 0}
    assert detail(studio.c.post(endpoint, json=body))["code"] == "closed"
    aid = link.agent_id
    studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": "2468"})
    response = studio.c.post(endpoint, json=body)
    assert response.status_code == 200 and response.content == raw
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    request = sent(machine, "history_image")[-1]
    assert request["message_id"] == "message" and request["index"] == 0
    assert request.get("engine", "claude") == engine
    assert payload["data"] not in caplog.text and payload["data"] not in endpoint


def test_history_image_old_bridge_and_missing_message_are_explicit(studio):
    machine, link, _, _, endpoint = prepare(studio)
    link.hello.pop("history_images")
    response = studio.c.post(endpoint, json={"message_id": "message", "index": 0})
    assert response.status_code == 409 and detail(response)["code"] == "unsupported"
    assert not sent(machine, "history_image")
    link.hello["history_images"] = True
    assert studio.c.post(endpoint, json={"message_id": "foreign", "index": 0}).status_code == 404


@pytest.mark.parametrize("body", [{"message_id": "m", "index": True}, {"message_id": "m", "index": -1},
                                  {"message_id": "m", "index": 32}, {"message_id": "m\n", "index": 0},
                                  {"message_id": None, "index": 0}])
def test_invalid_history_image_reference_never_reaches_bridge(studio, body):
    machine, _, _, _, endpoint = prepare(studio)
    assert studio.c.post(endpoint, json=body).status_code == 422
    assert not sent(machine, "history_image")


def test_history_image_size_format_and_revocation_during_decode(studio, monkeypatch):
    machine, _, _, payload, endpoint = prepare(studio)
    body = {"message_id": "message", "index": 0}
    monkeypatch.setattr(claude_code, "IMAGE_MAX", 4)
    assert studio.c.post(endpoint, json=body).status_code == 413
    monkeypatch.setattr(claude_code, "IMAGE_MAX", 5 * 1024 * 1024)
    payload["data"] = base64.b64encode(b"<svg onload='private' />").decode("ascii")
    assert studio.c.post(endpoint, json=body).status_code == 415
    payload["data"] = base64.b64encode(png(2, 2)).decode("ascii")
    original = claude_code._image_bytes

    def revoked(data):
        result = original(data)
        monkeypatch.setattr(claude_code, "_session_open", lambda *a: False)
        return result

    monkeypatch.setattr(claude_code, "_image_bytes", revoked)
    response = studio.c.post(endpoint, json=body)
    assert response.status_code == 409 and detail(response)["code"] == "closed"
