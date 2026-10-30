"""Ручной контракт с настоящим Codex: два коротких ответа и чтение состояния между ними.

Использует квоту аккаунта. Работает в отдельной временной папке только для чтения;
пользовательские чаты не изменяет. Проверяет настоящий writer-lock app-server.
"""

import asyncio
import uuid

import pytest

from studio_agent import cli, codex, codex_rpc, projects

pytestmark = [pytest.mark.manual, pytest.mark.timeout(240)]


async def test_resume_after_state_with_a_real_codex_writer(home):
    if codex_rpc.find_cli() is None:
        pytest.skip("Codex CLI не установлен")
    cli.drop_session_env()
    project = home / "connection-probe"
    project.mkdir()
    chat, pid = str(uuid.uuid4()), projects.pid(project.name)
    sent = []
    runner = codex.CodexRunner(sent.append, home=home)
    try:
        for index in range(2):
            if index:
                state = await runner.state(project, chat)
                assert state["settings"]["model"]
                assert state["settings"]["access"] == "read-only"
                target = runner.aliases.target(project, chat)
                calls = await runner._metadata(project, "thread/loaded/list", {})
                assert target not in calls.get("data", [])
            runner.start(project, pid, chat,
                "Тест соединения. Ответь только STUDIO_OK. Не используй инструменты, не открывай браузер и не меняй файлы.",
                "gpt-6.1-sol", "low", access="read-only")
            await asyncio.wait_for(runner._turns[chat].task, 90)
            assert sent[-1]["e"] == {"k": "done", "status": "done"}
        assert sum(f["e"]["k"] == "done" for f in sent) == 2
        assert "STUDIO_OK" in "".join(f["e"].get("text", "") for f in sent if f["e"]["k"] == "text")
    finally:
        # Архивируем только созданный здесь диагностический thread, сохраняя обычный профиль Codex.
        target = runner.aliases.target(project, chat)
        if target != chat and not runner.live():
            try:
                await runner._metadata(project, "thread/archive", {"threadId": target})
            except codex_rpc.RpcError:
                pass
        await runner.close(final=True)
