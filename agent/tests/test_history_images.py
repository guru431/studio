"""Изображения после повторного открытия: компактные ссылки и чтение только своего сообщения."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import SessionMessage

from studio_agent import codex, history_images, opencode, projects, sessions
from studio_agent.errors import Refused
from test_agent import CHAT, agent, opened, req, root
from test_codex import runner
from test_image_input import PNG


def claude_message(mid="user", *, parent=None):
    return SessionMessage(type="user", uuid=mid, session_id=CHAT, parent_tool_use_id=parent,
                          message={"content": [{"type": "image", "source": {
                              "type": "base64", "media_type": PNG["mime"], "data": PNG["data"]}}]})


def test_claude_image_only_history_retains_an_opaque_reference_and_original_payload(monkeypatch, tmp_path):
    raw = [claude_message()]
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **kw: raw)
    message = sessions.history(tmp_path, CHAT, None, 100)["items"][0]
    assert message["id"] == "user" and message["text"] == ""
    assert message["images"] == [{"index": 0, "name": "Изображение 1"}]
    assert PNG["data"] not in json.dumps(message)
    assert sessions.history_image(tmp_path, CHAT, "user", 0) == PNG
    for mid in ("foreign", "child"):
        raw.append(claude_message("child", parent="tool"))
        with pytest.raises(Refused, match="not_found"):
            sessions.history_image(tmp_path, CHAT, mid, 0)


def test_large_image_data_does_not_consume_the_history_page_budget(monkeypatch, tmp_path):
    message = claude_message()
    message.message["content"][0]["source"]["data"] = "A" * (600 * 1024)
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **kw: [message])
    page = sessions.history(tmp_path, CHAT, None, 100)
    assert page["items"][0]["images"] == [{"index": 0, "name": "Изображение 1"}]
    assert len(json.dumps(page, ensure_ascii=True)) < 1024


async def test_codex_image_reference_survives_history_and_is_read_only_from_the_selected_chat(runner):
    content = [{"type": "image", "url": "data:image/png;base64," + PNG["data"]}, {"type": "text", "text": "Скрин"}]
    turns = [{"items": [{"id": "message", "type": "userMessage", "content": content}]}]
    message = codex.normalize(turns)[0]
    assert message["text"] == "Скрин" and message["images"] == [{"index": 0, "name": "Изображение 1"}]
    assert PNG["data"] not in json.dumps(message)
    runner._metadata = AsyncMock(return_value={"thread": {"cwd": str(runner.project), "turns": turns}})
    assert await runner.history_image(runner.project, CHAT, "message", 0) == PNG
    with pytest.raises(Refused, match="not_found"):
        await runner.history_image(runner.project, CHAT, "foreign", 0)
    runner._metadata.return_value["thread"]["cwd"] = str(runner.project / "foreign")
    with pytest.raises(Refused, match="not_found"):
        await runner.history_image(runner.project, CHAT, "message", 0)


def test_opencode_chunks_keep_stable_user_ids_and_image_indexes_without_inline_data(tmp_path):
    content = {"type": "image", "mimeType": PNG["mime"], "data": PNG["data"]}
    updates = [{"sessionUpdate": "user_message_chunk", "messageId": "u", "content": content},
               {"sessionUpdate": "user_message_chunk", "messageId": "u", "content": {"type": "text", "text": "Скрин"}},
               {"sessionUpdate": "user_message_chunk", "messageId": "u", "content": content}]
    blocks = {}
    [message] = opencode.normalize(updates, image_blocks=blocks)
    assert message["id"] == "0" and message["text"] == "Скрин"
    assert [ref["index"] for ref in message["images"]] == [0, 1]
    assert PNG["data"] not in json.dumps(message)
    assert history_images.payload(blocks[message["id"]], 1, "opencode", tmp_path) == PNG


@pytest.mark.parametrize("url", ["https://example.com/private.png", "file:///private.png", "data:image/svg+xml;base64,PHN2Zy8+"])
def test_history_images_never_fetch_remote_urls_or_serve_active_formats(tmp_path, url):
    with pytest.raises(Refused):
        history_images.payload([{"type": "image", "url": url}], 0, "codex", tmp_path)


def test_history_payload_is_bounded_and_project_local_images_cannot_escape(tmp_path, monkeypatch):
    content = [{"type": "image", "url": "data:image/png;base64," + PNG["data"]}]
    monkeypatch.setattr(history_images.image_input, "FILE_MAX", 4)
    with pytest.raises(Refused, match="too_large"):
        history_images.payload(content, 0, "codex", tmp_path)
    with pytest.raises(Refused, match="outside"):
        history_images.payload([{"type": "localImage", "path": "../secret.png"}], 0, "codex", tmp_path)


def test_project_local_image_returns_bytes_without_exposing_its_path(tmp_path):
    path = tmp_path / "screenshot.png"
    path.write_bytes(base64.b64decode(PNG["data"]))
    assert history_images.payload([{"type": "localImage", "path": str(path)}], 0, "codex", tmp_path) == PNG


async def test_history_image_requires_open_pin_and_rechecks_it_after_read(agent, monkeypatch):
    pid = projects.pid("demo")
    frame = req("history_image", project=pid, chat=CHAT, message_id="user", index=0)
    assert (await agent.handle(frame))[0]["code"] == "closed"
    await opened(agent)
    monkeypatch.setattr(sessions, "history_image", lambda *a: PNG)
    assert (await agent.handle(frame))[0]["data"] == PNG

    def revoked(*a):
        agent.close_session(frame["session"])
        return PNG

    monkeypatch.setattr(sessions, "history_image", revoked)
    assert (await agent.handle(frame))[0]["code"] == "closed"


@pytest.mark.parametrize("change", [{"message_id": None}, {"index": True}, {"index": -1}, {"index": 32}, {"message_id": "bad\n"}])
async def test_invalid_history_image_identifiers_are_refused_before_read(agent, monkeypatch, change):
    await opened(agent)
    monkeypatch.setattr(sessions, "history_image", lambda *a: pytest.fail("неверный ввод дошёл до чтения"))
    frame = req("history_image", project=projects.pid("demo"), chat=CHAT, message_id="user", index=0)
    assert (await agent.handle({**frame, **change}))[0]["code"] == "invalid"
