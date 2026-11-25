"""Ручная проба DeepSeek Harness: один короткий ответ стороннего OpenAI-совместимого провайдера.

Проект и `DSH_HOME` — временные: настройки и чаты человека не трогаются. Провайдер — из окружения теста:
`DSH_LIVE_BASE_URL` (адрес `…/v1`), `DSH_LIVE_MODEL` (модель с вызовом инструментов) и
`DSH_LIVE_API_KEY` — ключ только окружением процесса: DSH читает его по `apiKeyEnv`, в файлы и argv он не попадает.
`DSH_LIVE_PROFILE` — `web` или `desktop`, если нужно выбрать установку с нужным профилем.
`DSH_LIVE_SHELL=1` — проверка инструмента bash на macOS/Linux без записи файлов.
`DSH_TEST_PACKAGE` — каталог пакета `@deepseek-ai/dsh`, если DSH не установлен глобально."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from studio_agent import deepseek, deepseek_rpc, deepseek_settings, projects
from turn_events import bare

pytestmark = [pytest.mark.manual, pytest.mark.timeout(300)]

KEY_ENV = "DSH_LIVE_API_KEY"


def _installs():
    package = os.environ.get("DSH_TEST_PACKAGE")
    if not package:
        return deepseek_rpc.find_installs()
    install = deepseek_rpc._npm(Path(package), os.environ)
    return [install] if install is not None else []


async def test_live_dsh_turn(home, monkeypatch, caplog):
    base, model, key = (os.environ.get(name) for name in ("DSH_LIVE_BASE_URL", "DSH_LIVE_MODEL", KEY_ENV))
    if not (base and model and key):
        pytest.skip("DSH_LIVE_BASE_URL, DSH_LIVE_MODEL и DSH_LIVE_API_KEY не заданы")
    profile = os.environ.get("DSH_LIVE_PROFILE") or None
    if profile is not None and profile not in deepseek_settings.PROFILES:
        pytest.fail("DSH_LIVE_PROFILE должен быть web или desktop")
    dsh_home = home / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    monkeypatch.setenv("DSH_TELEMETRY_MODE", "DISABLED")
    installs = await asyncio.to_thread(_installs)
    install = deepseek_rpc.pick(installs, profile)
    if install is None:
        pytest.skip("Нет подходящей установки DeepSeek Harness для выбранного профиля")
    profile = install.profile
    source = dsh_home / "profiles" / profile
    source.mkdir(parents=True)
    (source / "cordis.patch.yml").write_text(json.dumps([
        {"id": "llm-pi-ai", "config": {"providers": {"live": {
            "displayName": "live", "apiKeyEnv": KEY_ENV, "api": "openai-completions", "baseURL": base,
            "compat": {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"},
            "models": [{"id": model, "name": model, "contextWindow": 32768, "maxTokens": 2048}]}}}},
        {"id": "agent-default-model", "config": {"provider": "live", "model": model}},
    ]), encoding="utf-8")
    project = home / "dsh-probe"
    (project / ".git").mkdir(parents=True)  # свой корень: DSH не подмешивает AGENTS.md/CLAUDE.md репозитория вокруг
    chat, pid, sent = str(uuid.uuid4()), projects.pid(project.name), []
    model_id = json.dumps(["live", model], separators=(",", ":"))
    runner = deepseek.DeepSeekRunner(sent.append, home=home / "bridge", find_installs=lambda: installs)
    try:
        assert await runner.probe(project)
        assert model_id in [row["id"] for row in runner.catalog["models"]]
        assert runner.catalog["settings"]["profile"] == profile
        assert "llm-pi-ai" in runner.catalog["settings"]["applied"]
        assert not any((dsh_home / "sessions").glob("*/*"))  # пробная сессия каталога — во временном хранилище
        shell = os.environ.get("DSH_LIVE_SHELL") == "1"
        if shell:
            marker = "STUDIO_DSH_SHELL_OK"
            prompt = f"Вызови инструмент bash, выполни только `printf '%s\\n' {marker}` и повтори его вывод."
        else:
            marker = "СТУДИЯ-42"
            prompt = f"Повтори контрольную строку без кавычек: {marker}"
        runner.start(project, pid, chat, prompt, model_id, None)
        await asyncio.wait_for(runner._turns[chat].task, 180)
        if key in json.dumps(sent, ensure_ascii=False) or key in caplog.text:
            caplog.clear()
            pytest.fail("Ключ провайдера попал в события или журнал", pytrace=False)
        emitted = [frame["e"] for frame in sent]
        assert bare(emitted[-1]) == {"k": "done", "status": "done"}, emitted[-1]
        text = "".join(e.get("text", "") for e in emitted if e["k"] == "text")
        if shell:
            steps = {e["id"] for e in emitted if e["k"] == "step" and e["tool"].casefold() == "bash"}
            assert steps, f"Модель не вызвала bash. Ответ модели — {text[:512]}"
            results = [e for e in emitted if e["k"] == "result" and e["id"] in steps]
            assert any(not e["error"] and marker in e["text"] for e in results), results
        assert marker.casefold() in text.casefold()
        assert (await runner.history(project, chat, None, 10))["total"] >= 2
        rows = {row["chat"]: row for row in await runner.chats(project)}
        assert rows[chat]["engine"] == "deepseek" and rows[chat]["tag"] == "studio"
        data = await runner.state(project, chat)
        assert data["settings"]["model"] == model_id and data["settings"]["access_fixed"] is True
    finally:
        # Сессия живёт во временном DSH_HOME: удаления у ACP DSH нет, `close` закрывает процессы и сессию.
        await runner.close(final=True)
