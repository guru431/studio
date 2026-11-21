"""Встроенные инструменты чата в каждом ходе через сервер (спека 2026-11-21-studio-source-capabilities-design, п. 17):
шлюз, который не принимает `tools`, получает один повтор без них, и пара «подключение + модель» на
`chat.NO_TOOLS_SEC` идёт без встроенных инструментов."""
import json

import httpx

import chat
from conftest import add_gateway
from test_chat import new_chat, sse, turn


def test_gateway_refusing_tools_retried_without_them(studio):
    conn = add_gateway(studio)
    cid = new_chat(studio)
    model, sent = studio.gw.builtin, []

    async def picky(request):
        body = json.loads(request.content)
        sent.append("tools" in body)
        if "tools" in body:
            return httpx.Response(400, json={"error": {"message": "tool_choice auto is not supported by this model"}})
        return await model(request)

    studio.gw.on["/v1/chat/completions"] = picky
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert sent == [True, False]
    assert chat.NO_TOOLS[(conn, "local-gemma-abl")] == studio.clock[0]
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})
    assert sent == [True, False, False]                  # метка: сразу без инструментов
    studio.clock[0] += chat.NO_TOOLS_SEC
    sse(turn(studio, cid, "Снова"))
    assert sent[3:] == [True, False]

