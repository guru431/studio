"""Контракт вложений: данные картинки доходят до SDK; пути и URL мост не принимает."""

import base64

import pytest

from fake_sdk import FakeFactory, result_ok
from studio_agent import image_input, projects
from studio_agent.errors import Refused
from test_agent import CHAT, agent, opened, req, root, settle

PNG = {"mime": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nscreenshot").decode()}


async def test_image_only_turn_reaches_claude_as_an_image_block(agent):
    factory = FakeFactory([result_ok()])
    agent.runner.factory = factory
    await opened(agent)
    response = (await agent.handle(req("turn", project=projects.pid("demo"), chat=CHAT, text="", images=[PNG])))[0]
    assert response["ok"]
    await settle(agent)
    assert factory.clients[0].queries == [[{"type": "user", "parent_tool_use_id": None, "message": {
        "role": "user", "content": [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": PNG["data"]}}]}}]]


@pytest.mark.parametrize("images", [None, {}, [PNG] * 5, [{"mime": "image/svg+xml", "data": "PHN2Zz4="}],
    [{"mime": "image/png", "data": "not base64"}], [{"mime": "image/png", "data": ""}],
    [{"mime": "image/jpeg", "data": PNG["data"]}], [{"mime": "image/png", "url": "https://example.org/x.png"}],
    [{"mime": "image/png", "path": "C:/private.png"}]])
async def test_invalid_images_do_not_start_a_turn(agent, images):
    await opened(agent)
    response = (await agent.handle(req("turn", project=projects.pid("demo"), chat=CHAT, text="Вопрос", images=images)))[0]
    assert response["code"] == "invalid" and not agent.runner.live()


def test_image_limits_count_decoded_bytes(monkeypatch):
    image_input.validate([PNG])
    monkeypatch.setattr(image_input, "TOTAL_MAX", 20)
    with pytest.raises(Refused, match="too_large"):
        image_input.validate([PNG, PNG])
    monkeypatch.setattr(image_input, "FILE_MAX", 8)
    with pytest.raises(Refused, match="too_large"):
        image_input.validate([PNG])
