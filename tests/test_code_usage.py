"""Контракт лимитов: владелец, PIN, совместимость и ограниченный ответ без лишних данных моста."""

import copy

import pytest

from conftest import make_user, signin
from test_code import setup, url
from test_codex import CATALOG

USAGE = {"engine": "codex", "available": True, "source": "Codex app-server", "checked_at": 1000,
         "cache_ttl": 60, "plan": "pro", "windows": [
             {"id": "five_hour", "label": "5 часов", "available": True, "used_percent": 20,
              "remaining_percent": 80, "window_minutes": 300, "resets_at": 2000},
             {"id": "monthly", "label": "Месяц", "available": False, "used_percent": None,
              "remaining_percent": None, "window_minutes": None, "resets_at": None}],
         "statistics": {"available": True, "source": "Аккаунт Codex", "summary": {"lifetime_tokens": 123},
                        "daily": [{"date": "2026-10-30", "tokens": 10}]}}


def prepare(studio, *, opened=True):
    aid, machine, link = setup(studio, opened=opened)
    link.hello.update(subscription_usage=True, codex=CATALOG)
    machine._op_usage = lambda req, events: copy.deepcopy(USAGE)
    return aid, machine, link


def test_usage_requires_own_agent_and_pin_session(studio):
    aid, machine, _ = prepare(studio, opened=False)
    r = studio.c.get(url(aid, "usage") + "?engine=codex")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "closed"
    make_user(studio, "friend")
    signin(studio, "friend")
    before = len(machine.requests)
    assert studio.c.get(url(aid, "usage") + "?engine=codex").status_code == 404
    assert len(machine.requests) == before


def test_usage_forwards_engine_refresh_and_preserves_unknown_monthly(studio):
    aid, machine, _ = prepare(studio)
    r = studio.c.get(url(aid, "usage") + "?engine=codex&refresh=true")
    assert r.status_code == 200
    data = r.json()
    assert data["windows"][0]["remaining_percent"] == 80
    assert data["windows"][1]["remaining_percent"] is None
    assert data["statistics"]["summary"] == {"lifetime_tokens": 123}
    assert machine.requests[-1]["engine"] == "codex" and machine.requests[-1]["refresh"] is True
    assert data["links"][0]["url"].startswith("https://chatgpt.com/")


def test_usage_old_bridge_and_invalid_engine_are_rejected_before_request(studio):
    aid, machine, link = prepare(studio)
    before = len(machine.requests)
    assert studio.c.get(url(aid, "usage") + "?engine=other").status_code == 422
    assert studio.c.get(url(aid, "usage") + "?refresh=bad").status_code == 422
    link.hello.pop("subscription_usage")
    assert studio.c.get(url(aid, "usage")).status_code == 409
    assert len(machine.requests) == before


@pytest.mark.parametrize("broken", [False, True])
def test_usage_discards_unexpected_fields_and_rejects_invalid_values(studio, broken):
    aid, machine, _ = prepare(studio)
    data = copy.deepcopy(USAGE)
    data.update(accessToken="secret", links=[{"url": "https://evil.test"}])
    data["statistics"]["summary"]["private"] = "secret"
    if broken:
        data["windows"][0]["used_percent"] = -1
    machine._op_usage = lambda req, events: data
    r = studio.c.get(url(aid, "usage") + "?engine=codex")
    assert r.status_code == (502 if broken else 200)
    assert "secret" not in r.text and "evil.test" not in r.text
