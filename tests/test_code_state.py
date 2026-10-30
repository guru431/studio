"""Настройки/контекст перед ходом, права отдельно для помощников и запрет молчаливого fallback."""
import pytest

from test_code import DONE, chat_url, sent, setup
from test_codex import CATALOG


@pytest.mark.parametrize("engine,access", [("claude", "plan"), ("codex", "read-only")])
def test_state_and_turn_relay_access_without_mixing_assistants(studio, engine, access):
    aid, machine, link = setup(studio)
    link.hello["codex"] = CATALOG
    machine.context = {"used": 25000, "limit": 100000, "percent": 25}
    suffix = "?engine=codex" if engine == "codex" else ""
    state = studio.c.post(chat_url(aid, "state") + suffix, json={"access": access})
    assert state.status_code == 200 and state.json()["settings"]["access"] == access
    assert state.json()["context"] == machine.context
    machine.script = [DONE]
    r = studio.c.post(chat_url(aid, "turn") + suffix, json={"text": "Вопрос", "access": access})
    assert r.status_code == 200
    assert sent(machine, "turn")[-1]["access"] == access
    assert sent(machine, "state")[-1].get("engine", "claude") == engine


def test_wrong_engine_access_and_old_bridge_are_rejected_before_turn(studio):
    aid, machine, link = setup(studio)
    assert studio.c.post(chat_url(aid, "turn"), json={"text": "Вопрос", "access": "read-only"}).status_code == 422
    assert not sent(machine, "turn")
    link.hello.pop("chat_state")
    assert studio.c.post(chat_url(aid, "turn"), json={"text": "Вопрос", "access": "plan"}).status_code == 409
    assert studio.c.post(chat_url(aid, "state"), json={}).status_code == 409
    assert not sent(machine, "state") and not sent(machine, "turn")
