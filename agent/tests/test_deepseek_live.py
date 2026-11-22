"""Ручная проба DeepSeek Harness: один короткий ответ стороннего OpenAI-совместимого провайдера.

Проект и `DSH_HOME` — временные: настройки и чаты человека не трогаются. Провайдер — из окружения теста:
`DSH_LIVE_BASE_URL` (адрес `…/v1`, по умолчанию — шлюз srvllm), `DSH_LIVE_MODEL` (модель с вызовом инструментов) и
`DSH_LIVE_API_KEY` — ключ только окружением процесса: DSH читает его по `apiKeyEnv`, в файлы и argv он не попадает.
`DSH_TEST_PACKAGE` — каталог пакета `@deepseek-ai/dsh`, если DSH не установлен глобально."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from studio_agent import deepseek, deepseek_rpc, projects

pytestmark = [pytest.mark.manual, pytest.mark.timeout(300)]

KEY_ENV = "DSH_LIVE_API_KEY"


def _installs():
    package = os.environ.get("DSH_TEST_PACKAGE")
    if not package:
        return deepseek_rpc.find_installs()
    install = deepseek_rpc._npm(Path(package), os.environ)
    return [install] if install is not None else []


async def test_real_deepseek_turn_history_chats_and_state(home, monkeypatch, caplog):
    base, model, key = (os.environ.get(name) for name in ("DSH_LIVE_BASE_URL", "DSH_LIVE_MODEL", KEY_ENV))
    if not (base and model and key):
        pytest.skip("DSH_LIVE_BASE_URL, DSH_LIVE_MODEL и DSH_LIVE_API_KEY не заданы")
    installs = await asyncio.to_thread(_installs)
    if deepseek_rpc.pick(installs, None) is None:
        pytest.skip("DeepSeek Harness не установлен (или Node не подходит)")
    dsh_home = home / "dsh-home"
    # Профиль Web временного DSH_HOME — источник наложения, как у человека с настроенным DSH Web.
    (dsh_home / "profiles" / "web").mkdir(parents=True)
    (dsh_home / "profiles" / "web" / "cordis.patch.yml").write_text(json.dumps([
        {"id": "llm-pi-ai", "config": {"providers": {"live": {
            "displayName": "live", "apiKeyEnv": KEY_ENV, "api": "openai-completions", "baseURL": base,
            "compat": {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"},
            "models": [{"id": model, "name": model, "contextWindow": 32768, "maxTokens": 2048}]}}}},
        {"id": "agent-default-model", "config": {"provider": "live", "model": model}},
    ]), encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    project = home / "dsh-probe"
    (project / ".git").mkdir(parents=True)  # свой корень: DSH не подмешивает AGENTS.md/CLAUDE.md репозитория вокруг
    chat, pid, sent = str(uuid.uuid4()), projects.pid(project.name), []
    model_id = json.dumps(["live", model], separators=(",", ":"))
    runner = deepseek.DeepSeekRunner(sent.append, home=home / "bridge", find_installs=lambda: installs)
    try:
        assert await runner.probe(project)
        assert model_id in [row["id"] for row in runner.catalog["models"]]
        assert runner.catalog["settings"]["profile"] == "web"
        assert "llm-pi-ai" in runner.catalog["settings"]["applied"]
        assert not any((dsh_home / "sessions").glob("*/*"))  # пробная сессия каталога — во временном хранилище
        runner.start(project, pid, chat, "Ответь одним словом: готово", model_id, None)
        await asyncio.wait_for(runner._turns[chat].task, 180)
        assert sent[-1]["e"] == {"k": "done", "status": "done"}, sent[-1]
        text = "".join(f["e"].get("text", "") for f in sent if f["e"]["k"] == "text")
        assert "готово" in text.lower()
        assert (await runner.history(project, chat, None, 10))["total"] >= 2
        rows = {row["chat"]: row for row in await runner.chats(project)}
        assert rows[chat]["engine"] == "deepseek" and rows[chat]["tag"] == "studio"
        data = await runner.state(project, chat)
        assert data["settings"]["model"] == model_id and data["settings"]["access_fixed"] is True
        assert key not in json.dumps(sent, ensure_ascii=False) and key not in caplog.text
    finally:
        # Сессия живёт во временном DSH_HOME: удаления у ACP DSH нет, `close` закрывает процессы и сессию.
        await runner.close(final=True)
