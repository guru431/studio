"""Ручная проба установленного OpenCode: один короткий ответ квоты, без инструментов."""

import asyncio
import uuid

import pytest

from studio_agent import cli, opencode, opencode_rpc, projects

pytestmark = [pytest.mark.manual, pytest.mark.timeout(180)]


async def test_real_opencode_turn_history_and_state(home):
    if opencode_rpc.find_cli() is None:
        pytest.skip("OpenCode CLI не установлен")
    cli.drop_session_env()
    project = home / "connection-probe"
    project.mkdir()
    chat, pid, sent = str(uuid.uuid4()), projects.pid(project.name), []
    runner = opencode.OpenCodeRunner(sent.append, home=home)
    target = None
    try:
        assert await runner.probe(project)
        runner.start(project, pid, chat,
            "Тест соединения. Ответь только STUDIO_OK. Не используй инструменты, не открывай браузер и не меняй файлы.",
            None, None)
        await asyncio.wait_for(runner._turns[chat].task, 90)
        target = runner.aliases.target(project, chat)
        assert sent[-1]["e"] == {"k": "done", "status": "done"}
        assert "STUDIO_OK" in "".join(f["e"].get("text", "") for f in sent if f["e"]["k"] == "text")
        assert (await runner.history(project, chat, None, 10))["total"] >= 2
        assert (await runner.chats(project))[0]["chat"] == chat
        data = await runner.state(project, chat)
        assert data["settings"]["model"] and data["settings"]["access"] == "default"
    finally:
        await runner.close(final=True)
        target = target or runner.aliases.target(project, chat)
        if target is not None:
            rpc = await runner._connect(project)
            try:
                # Только диагностическая сессия, созданная этим тестом в его временной папке.
                await rpc.request("session/delete", {"sessionId": target})
            finally:
                await rpc.disconnect()
