"""Перевод, список моделей, «Проверить» и «Пробный ответ» подключения через Cleaner: каждый кусок и определение языка
идут в шлюз Cleaner в одном контексте записи, без инструментов; провайдеру мимо Cleaner запросов нет (узел провайдера
отвечает AssertionError); недоступный Cleaner — отказ словами; журнал без адресов и ключей."""
import json
import logging

import cleaner_route
import sections
import state
import translate_text
from test_cleaner_route_chat import KEY, PROVIDER, cleaner_gw, settings  # noqa: F401 — фикстура
from test_translate import THREE, owner_of, parts_of, start, wait_row

MODEL = "gpt-x"


def translating(s, gw) -> int:
    """Подключение фикстуры — ещё и в «Переводе» с моделью gpt-x."""
    assert s.c.patch(f"/api/connections/{gw.conn_id}", json={"sections": ["chat", "translate"]}).status_code == 200
    sections.set_models(owner_of(gw.conn_id), "translate", gw.conn_id, [MODEL])
    return gw.conn_id


def translate(s, gw, text=THREE, **extra) -> dict:
    return wait_row(s, start(s, text, connection_id=gw.conn_id, model=MODEL, **extra))


def contexts() -> list[tuple]:
    return [tuple(r) for r in state.DB.execute("SELECT resource, item_id, connection_id FROM cleaner_contexts")]


def test_translation_chunks_go_through_cleaner_in_one_context(cleaner_gw, monkeypatch, caplog):
    s, gw = cleaner_gw
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    translating(s, gw)
    gw.script = [["Первый."], ["Второй."], ["Третий."]]
    row = translate(s, gw)
    assert row["state"] == "done" and row["text"] == "Первый.\n\nВторой.\n\nТретий." and row["parts"] == 3
    assert len(gw.chats) == 1 and {r.headers["x-cleaner-chat"] for r in gw.turns} == {gw.chats[0]}
    assert len(gw.turns) == 3 and contexts() == [("translate", row["id"], gw.conn_id)]
    for sent, body in zip(gw.turns, gw.bodies):
        assert str(sent.url) == "https://10.77.1.20:5464/agent/openai/v1/chat/completions"
        assert sent.headers["x-cleaner-key"] == KEY and sent.headers["authorization"] == "Bearer cloud-key"
        assert sent.headers["x-cleaner-upstream"] == PROVIDER and "x-cleaner-hold-tools" not in sent.headers
        assert "tools" not in json.loads(sent.content) and body["stream"] is True and body["model"] == MODEL
    assert "Первый." in gw.bodies[1]["messages"][0]["content"]       # связность кусков — как у обычного подключения
    assert [p["state"] for p in parts_of(s, row["id"])] == ["done"] * 3
    assert "Первый" not in caplog.text and "cloud-key" not in caplog.text and KEY not in caplog.text
    assert "api.cloud.test" not in caplog.text and "First one" not in caplog.text


def test_detect_goes_through_cleaner(cleaner_gw):
    s, gw = cleaner_gw
    translating(s, gw)
    gw.script = [["en"], ["Привет."]]
    row = translate(s, gw, "Hello there.", source="auto")
    assert row["state"] == "done" and row["detected_lang"] == "en" and row["result_lang"] == "ru"
    assert row["text"] == "Привет."
    detect, chunk = gw.bodies
    assert "stream" not in detect and detect["messages"][-1]["content"] == "Hello there."    # определение — без потока
    assert [t.headers["x-cleaner-chat"] for t in gw.turns] == [gw.chats[0]] * 2 and len(gw.chats) == 1
    assert all(t.headers["x-cleaner-upstream"] == PROVIDER for t in gw.turns) and chunk["stream"] is True


def test_models_refresh_goes_through_cleaner(cleaner_gw):
    s, gw = cleaner_gw
    response = s.c.get(f"/api/connections/{gw.conn_id}/models")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{"id": "gpt-x"}, {"id": "gpt-y", "context_length": 64000}]
    sent = gw.requests[-1]
    assert sent.method == "GET" and str(sent.url) == "https://10.77.1.20:5464/agent/openai/v1/models"
    assert sent.headers["x-cleaner-key"] == KEY and sent.headers["authorization"] == "Bearer cloud-key"
    assert sent.headers["x-cleaner-upstream"] == PROVIDER and "x-cleaner-chat" not in sent.headers
    assert not gw.chats                                              # контекст списку моделей не нужен
    found = s.c.get(f"/api/sections/chat/links/{gw.conn_id}/found")           # «Обновить модели» раздела
    assert found.status_code == 200 and found.json()["items"] == ["gpt-x", "gpt-y"]
    assert gw.requests[-1].url.path == "/agent/openai/v1/models"


def test_check_and_probe_go_through_cleaner(cleaner_gw, caplog):
    s, gw = cleaner_gw
    caplog.set_level(logging.DEBUG)
    response = s.c.post(f"/api/connections/{gw.conn_id}/check")
    assert response.status_code == 200, response.text
    passport = response.json()["passport"]
    assert [r["path"] for r in passport["routes"]] == ["/agent/openai/v1/models", "/agent/openai/v1/cleaner/check"]
    assert all(r["reach"]["ok"] and r["status"] == 200 for r in passport["routes"])
    assert passport["routes"][0]["key"]["state"] == "accepted" and passport["routes"][0]["count"] == 2
    assert [m["id"] for m in passport["models"]] == ["gpt-x", "gpt-y"] and passport["source"] == "server"
    assert response.json()["found"] == {"chat": ["gpt-x", "gpt-y"]}
    assert gw.checked[0].headers["x-cleaner-upstream"] == PROVIDER and gw.checked[0].headers["x-cleaner-key"] == KEY
    assert gw.checked[0].headers["authorization"] == "Bearer cloud-key"
    assert not gw.chats                                              # проверка без контекста

    probe = s.c.post(f"/api/connections/{gw.conn_id}/probe", json={"model": MODEL})
    assert probe.status_code == 200, probe.text
    assert probe.json()["probe"]["ok"] and probe.json()["probe"]["words"].startswith("ответила за ")
    assert contexts() == [("probe", str(gw.conn_id), gw.conn_id)] and len(gw.chats) == 1
    sent, body = gw.turns[-1], gw.bodies[-1]
    assert sent.headers["x-cleaner-chat"] == gw.chats[0] and sent.headers["x-cleaner-upstream"] == PROVIDER
    assert body["stream"] is False and body["model"] == MODEL and "tools" not in body
    assert s.c.post(f"/api/connections/{gw.conn_id}/probe", json={"model": MODEL}).status_code == 200
    assert len(gw.chats) == 1                                        # свой контекст подключения — один на все пробы
    again = s.c.post(f"/api/connections/{gw.conn_id}/check").json()["passport"]
    assert again["probes"][MODEL]["ok"] and len(gw.chats) == 1        # пробы прошлых проверок сохраняются
    assert "cloud-key" not in caplog.text and KEY not in caplog.text and "api.cloud.test" not in caplog.text


def test_check_tells_what_cleaner_did_not_pass(cleaner_gw):
    s, gw = cleaner_gw
    gw.check_body = {"ok": False, "checks": [{"name": "key", "ok": True, "detail": "ключ действует"},
                                             {"name": "provider", "ok": False, "detail": "нет ответа"}]}
    layers = s.c.post(f"/api/connections/{gw.conn_id}/check").json()["passport"]["routes"][1]
    assert layers["reach"] == {"ok": False, "words": "Cleaner: не пройдено — provider"}


def test_cleaner_refusal_of_the_provider_address_is_told_in_words(cleaner_gw):
    s, gw = cleaner_gw
    state.DB.execute("UPDATE connections SET base_url = ? WHERE id = ?", ("https://10.77.1.99/v1", gw.conn_id))
    response = s.c.get(f"/api/connections/{gw.conn_id}/models")
    assert response.status_code == 502 and "публичные https-адреса" in response.json()["detail"]["message"]
    passport = s.c.post(f"/api/connections/{gw.conn_id}/check").json()["passport"]
    assert passport["routes"][0]["key"]["state"] == "unknown"
    assert "публичные https-адреса" in passport["routes"][0]["key"]["words"]


def test_translation_with_cleaner_down_stops_without_provider(cleaner_gw, caplog):
    s, gw = cleaner_gw
    caplog.set_level(logging.DEBUG)
    translating(s, gw)
    gw.down = True
    row = translate(s, gw, "Hello there.", source="auto")
    assert row["state"] == "error" and cleaner_route.DOWN in row["detail"]
    assert gw.turns == [] and gw.chats == [] and contexts() == []
    assert s.gw.chat_requests == [] and "api.cloud.test" not in caplog.text and "Hello" not in caplog.text
    response = s.c.get(f"/api/connections/{gw.conn_id}/models")
    assert response.status_code == 502 and response.json()["detail"]["message"] == cleaner_route.DOWN
    passport = s.c.post(f"/api/connections/{gw.conn_id}/check").json()["passport"]
    assert [r["reach"] for r in passport["routes"]] == [{"ok": False, "words": cleaner_route.DOWN}] * 2
    probe = s.c.post(f"/api/connections/{gw.conn_id}/probe", json={"model": MODEL}).json()["probe"]
    assert probe["ok"] is False and probe["words"] == cleaner_route.DOWN


def test_translation_with_cleaner_unset_is_refused_in_words(cleaner_gw):
    s, gw = cleaner_gw
    translating(s, gw)
    state.DB.execute("DELETE FROM settings WHERE key = ?", (cleaner_route.SETTINGS_KEY,))
    row = translate(s, gw, "Hello there.")
    assert row["state"] == "error" and cleaner_route.UNSET in row["detail"] and gw.turns == []


def test_translation_refused_by_cleaner_is_told_in_words(cleaner_gw):
    s, gw = cleaner_gw
    translating(s, gw)
    gw.refuse = (429, "busy")
    row = translate(s, gw, "Hello there.")
    assert row["state"] == "error" and "Cleaner занят" in row["detail"]


def test_lost_context_is_forgotten_and_the_next_translation_makes_a_new_one(cleaner_gw):
    s, gw = cleaner_gw
    translating(s, gw)
    gw.script = [["Привет."]]
    first = translate(s, gw, "Hello there.")
    assert first["state"] == "done" and contexts() == [("translate", first["id"], gw.conn_id)]
    lost = first["id"]
    state.DB.execute("UPDATE translation_parts SET state = 'failed' WHERE translation_id = ?", (lost,))
    state.DB.execute("UPDATE translations SET state = 'error', done_parts = 0 WHERE id = ?", (lost,))
    gw.chats.clear()                                                 # Cleaner удалил контекст
    assert s.c.post(f"/api/translate/{lost}/continue").status_code == 200
    row = wait_row(s, lost)
    assert row["state"] == "error" and "контекст" in row["detail"] and contexts() == []
    assert len(gw.turns) == 1 and gw.chats == []                     # ход не принят (404 chat); провайдеру запрос не ушёл
    assert s.c.post(f"/api/translate/{lost}/continue").status_code == 200
    assert wait_row(s, lost)["state"] == "done" and len(contexts()) == 1 and len(gw.chats) == 1


def test_forced_mode_sends_a_direct_external_connection_through_cleaner(cleaner_gw):
    s, gw = cleaner_gw
    translating(s, gw)
    settings(s, forced=True)
    row = translate(s, gw, "Hello there.", source="auto")
    assert row["state"] == "done" and len(gw.turns) == 2 and s.gw.chat_requests == []
    assert all(t.headers["x-cleaner-chat"] == gw.chats[0] for t in gw.turns)


def test_connection_not_through_cleaner_is_unchanged(studio):
    """Обычное подключение (не через Cleaner): перевод идёт на его адрес, Cleaner не задействован."""
    from conftest import add_gateway
    add_gateway(studio)
    row = wait_row(studio, start(studio, "Hello there."))
    assert row["state"] == "done" and studio.gw.chat_requests


def test_chat_context_limit_is_read_through_cleaner(cleaner_gw):
    """Предел контекста модели хода — из списка моделей, полученного через Cleaner, а не у провайдера."""
    from test_cleaner_route_chat import routed_chat
    from test_chat import sse, turn
    s, gw = cleaner_gw
    gw.models = [{"id": MODEL, "context_length": 64000}]
    assert sse(turn(s, routed_chat(s, gw), "Привет"))[-1][0] == "done"
    listing = [r for r in gw.requests if r.url.path == "/agent/openai/v1/models"]
    assert len(listing) == 1 and listing[0].headers["x-cleaner-upstream"] == PROVIDER
