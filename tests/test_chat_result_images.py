"""Картинки из результатов MCP (спека 2026-11-25, п. 39): вложениями ответа под ответом; модели, принимающей
изображения, — сообщением user после всех `tool` круга в пределах `max_images`; через Cleaner модели не уходят."""
import base64
import io
import json
import logging
import random
from types import SimpleNamespace

from PIL import Image

import capabilities
import chat
import mcp_client
import mcp_files
import state
from conftest import add_gateway, set_passport
from test_chat import messages, new_chat, png, sse, turn
from test_chat_pending import answer, pending_of
from test_chat_web import call, steps_of
from test_cleaner_route_chat import MCP_KEY, MCP_URL, cleaner_gw, routed_chat  # noqa: F401 (фикстура)
from test_mcp_dialog import add_shared, name_of, server, setup, tool
from test_shared_integrations import MODEL

PIC = base64.b64encode(png("red")).decode()
RESULT = {"content": [{"type": "text", "text": "график готов"}, {"type": "image", "data": PIC, "mimeType": "image/png"},
                      {"type": "image", "data": "не base64", "mimeType": "image/png"},
                      {"type": "image", "data": PIC, "mimeType": "image/gif"}]}


def pictures(n: int) -> dict:
    colors = ("red", "green", "blue", "white", "black")
    return {"content": [{"type": "image", "data": base64.b64encode(png(colors[i % 5])).decode(), "mimeType": "image/png"}
                        for i in range(n)]}


def vision(on=True):
    """Паспорт общего API моделей (`setup`): у MODEL — `vision` или нет."""
    cid = state.DB.execute("SELECT id FROM connections WHERE user_id IS NULL AND kind = 'openai'").fetchone()[0]
    set_passport(cid, {"models": [{"id": MODEL, "caps": [{"name": "vision", "source": "metadata"}] if on else []}]})


def after_tools(request) -> list:
    """Сообщения запроса после последнего сообщения модели с вызовами."""
    sent = request["messages"]
    last = max(i for i, m in enumerate(sent) if m.get("tool_calls"))
    return sent[last + 1:]


def test_result_image_reaches_a_vision_model_and_stays_under_the_answer(studio, caplog):
    caplog.set_level(logging.INFO)
    sid, calls, uid = setup(studio, results={"lookup": RESULT})
    vision()
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Вижу красный график."], ["Да."]]
    sse(turn(studio, cid, "Нарисуй"))
    tool_message, shown = studio.gw.chat_requests[1]["messages"][-2:]
    assert (tool_message["role"], tool_message["content"]) == ("tool", "график готов")
    assert shown["role"] == "user" and [p["type"] for p in shown["content"]] == ["text", "image_url"]
    assert shown["content"][0]["text"] == chat.RESULT_IMAGES_NOTE
    assert shown["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    reply = messages(studio, cid)[1]
    (attached,) = reply["attachments"]
    assert (attached["kind"], attached["name"]) == ("image", "lookup-1.png") and reply["tools"][-1]["pictures"] == 1
    assert studio.c.get(f"/api/media/{attached['media_id']}").status_code == 200
    assert PIC[:60] not in json.dumps(steps_of(reply["id"])) and PIC[:60] not in caplog.text   # не в шаги и не в журнал
    assert "картинок результата 1" in caplog.text
    sse(turn(studio, cid, "Ещё"))
    assert "image_url" not in json.dumps(studio.gw.chat_requests[-1]["messages"])
    task = SimpleNamespace(chat_id=cid, user_id=uid, model=MODEL, user_message_id=None)
    assert mcp_files._candidates(task) == []                                          # серверам MCP файлом не уходит


def test_only_images_and_the_limits_per_result(studio):
    sid, calls, _ = setup(studio, results={"lookup": pictures(mcp_client.RESULT_IMAGES + 2)})
    vision()
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Нарисуй"))
    tool_message = after_tools(studio.gw.chat_requests[1])[0]
    assert tool_message["content"] == mcp_client.ONLY_IMAGES
    assert len(messages(studio, cid)[1]["attachments"]) == mcp_client.RESULT_IMAGES == 4


def test_images_follow_max_images_and_the_answer_limit(studio, monkeypatch):
    sid, calls, _ = setup(studio, results={"lookup": pictures(3)})
    vision()
    monkeypatch.setattr(chat.profiles, "max_images", lambda conn, model=None: 1)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Нарисуй"))
    shown = after_tools(studio.gw.chat_requests[1])[-1]["content"]
    assert [p["type"] for p in shown] == ["text", "text", "text", "image_url"]       # одна — последняя
    assert [p["text"] for p in shown[1:3]] == [chat.DROPPED.format(1)] * 2
    assert len(messages(studio, cid)[1]["attachments"]) == 3                         # под ответом — все три
    assert chat.RESULT_IMAGES_MAX == 16
    many = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q=str(i)) for i in range(4)]},
                             {"tool_calls": [call(name_of(sid), q=str(i)) for i in range(4, 8)]}, ["Готово"]]
    sse(turn(studio, many, "Много"))
    reply = messages(studio, many)[1]
    assert len(reply["attachments"]) == chat.RESULT_IMAGES_MAX                       # 24 картинки — 16 вложений
    assert [s["pictures"] for s in reply["tools"]] == [3, 3, 3, 3, 3, 1, 0, 0]


def test_text_model_gets_no_pictures_but_the_answer_shows_them(studio):
    sid, calls, _ = setup(studio, results={"lookup": RESULT})
    vision(False)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Нарисуй"))
    assert [m["role"] for m in after_tools(studio.gw.chat_requests[1])] == ["tool"]
    assert len(messages(studio, cid)[1]["attachments"]) == 1


def test_pictures_wait_for_the_pause_and_follow_both_tool_messages(studio):
    sid, calls, _ = setup(studio, results={"lookup": RESULT})
    vision()
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1"), call("ask_user", question="Дальше?")]}]
    shown = pending_of(sse(turn(studio, cid, "Нарисуй")))
    studio.gw.chat_script = [["Да."]]
    assert sse(answer(studio, cid, shown["id"], "answer", "Да"))[-1] == ("done", {"status": "done"})
    tail = after_tools(studio.gw.chat_requests[-1])
    assert [m["role"] for m in tail] == ["tool", "tool", "user"]
    assert [p["type"] for p in tail[-1]["content"]] == ["text", "image_url"]
    assert tail[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_big_picture_result_keeps_its_text_and_picture(studio):
    """Ревью, п. 1: ответ `tools/call` больше MAX_BYTES (картинка ~3 МБ base64) не отвергается целиком —
    предел вызова CALL_MAX_BYTES вмещает одну картинку предельного размера сверх MAX_BYTES текста."""
    raw = io.BytesIO()
    Image.frombytes("RGB", (900, 900), random.Random(1).randbytes(900 * 900 * 3)).save(raw, "PNG")
    data = base64.b64encode(raw.getvalue()).decode()
    assert len(data) > 2 * mcp_client.MAX_BYTES
    assert mcp_client.CALL_MAX_BYTES == mcp_client.RESULT_IMAGE_CHARS + mcp_client.MAX_BYTES
    big = {"content": [{"type": "text", "text": "большой график"}, {"type": "image", "data": data, "mimeType": "image/png"}]}
    sid, calls, _ = setup(studio, results={"lookup": big})
    vision()
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Вижу."]]
    sse(turn(studio, cid, "Нарисуй"))
    tool_message, shown = after_tools(studio.gw.chat_requests[1])
    assert (tool_message["role"], tool_message["content"]) == ("tool", "большой график")
    assert [p["type"] for p in shown["content"]] == ["text", "image_url"]
    reply = messages(studio, cid)[1]
    assert len(reply["attachments"]) == 1 and reply["tools"][-1]["pictures"] == 1 and "error" not in reply["tools"][-1]


def test_a_result_that_did_not_fit_keeps_its_pictures_from_the_model(studio, monkeypatch):
    """Ревью, п. 2: шаг, ставший ошибкой NO_ROOM (`_fit`), модели не дошёл — его картинки ей тоже не идут,
    под ответом они есть."""
    sid, calls, _ = setup(studio, results={"lookup": RESULT})
    vision()
    monkeypatch.setattr(chat.StreamTask, "_fit", lambda self, step: {
        **{k: v for k, v in step.items() if k != "text"}, "error": chat.NO_ROOM})
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Не влезло."]]
    sse(turn(studio, cid, "Нарисуй"))
    tail = after_tools(studio.gw.chat_requests[1])
    assert [m["role"] for m in tail] == ["tool"] and "image_url" not in json.dumps(studio.gw.chat_requests[1])
    reply = messages(studio, cid)[1]
    assert reply["tools"][-1]["error"] == chat.NO_ROOM and len(reply["attachments"]) == 1


def test_srvllm_vision_comes_from_the_catalog_without_a_passport(studio):
    """Ревью, п. 3: `local-gemma-abl` шлюза srvllm видит картинки по перечню `capabilities.CATALOG_CAPS`, паспорта нет."""
    gw = add_gateway(studio)
    server(studio, results={"lookup": RESULT})
    sid = add_shared(studio, "Shared tools", "/mcp")
    assert state.DB.execute("SELECT passport FROM connections WHERE id = ?", (gw,)).fetchone()[0] is None
    assert "vision" in capabilities.CATALOG_CAPS["local-gemma-abl"]
    cid = new_chat(studio, connection_id=gw, model="local-gemma-abl")
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Вижу."]]
    sse(turn(studio, cid, "Нарисуй"))
    tail = after_tools(studio.gw.chat_requests[1])
    assert [m["role"] for m in tail] == ["tool", "user"]
    assert tail[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_pictures_do_not_go_through_cleaner(cleaner_gw):
    s, gw = cleaner_gw
    server(s, tools=[tool("lookup")], results={"lookup": RESULT})
    response = s.c.post("/api/admin/shared/mcp", json={"name": "Shared tools", "url": MCP_URL, "token": MCP_KEY})
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    cid = routed_chat(s, gw)
    gw.script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    assert sse(turn(s, cid, "Нарисуй"))[-1][0] == "done"
    assert len(gw.turns) == 2 and "image_url" not in json.dumps(gw.bodies)
    assert len(messages(s, cid)[1]["attachments"]) == 1
