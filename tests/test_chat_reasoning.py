"""Кнопка «Рассуждение» диалога (`chats.thinking`) и рассуждение модели в потоке ответа (спека
2026-10-22-studio-chat-tools-badge-reasoning, раздел 4)."""
from conftest import add_connection, add_gateway
from test_chat import new_chat, sse, turn
from test_chat_web import chat_of, patch


def test_switch_is_stored_only_for_a_local_model(studio):
    gw = add_gateway(studio); cid = new_chat(studio)                      # local-gemma-abl
    assert (chat_of(studio, cid)["thinking"], chat_of(studio, cid)["thinking_ok"]) == (False, True)
    assert patch(studio, cid, thinking=True)["thinking"] is True
    assert patch(studio, cid, thinking=None)["thinking"] is True
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    view = patch(studio, cid, model="gemma4-abl")                          # облачная роль: хранится, не действует
    assert (view["thinking"], view["thinking_ok"]) == (True, False)
    other = new_chat(studio, connection_id=gw, model="gemma4-abl")
    r = studio.c.patch(f"/api/chats/{other}", json={"thinking": True, "title": "x"})
    assert r.status_code == 409 and r.json()["detail"] == {"code": "no_thinking",
                                                           "message": "эта модель не умеет рассуждать по кнопке"}
    assert chat_of(studio, other)["title"] is None
    assert patch(studio, other, thinking=False)["thinking"] is False      # выключить можно всегда


def test_request_carries_enable_thinking_only_for_a_local_model(studio):
    gw = add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid)); patch(studio, cid, thinking=True); sse(turn(studio, cid, "ещё"))
    assert [r["chat_template_kwargs"] for r in studio.gw.chat_requests] == [
        {"enable_thinking": False}, {"enable_thinking": True}]
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    patch(studio, cid, model="gemma4-abl"); sse(turn(studio, cid, "и ещё"))
    assert "chat_template_kwargs" not in studio.gw.chat_requests[-1]
    direct = new_chat(studio, connection_id=add_connection(studio))       # прямое: тело собирает браузер
    assert "chat_template_kwargs" not in turn(studio, direct).json()
