"""Cleaner integration: reviewed request boundary, isolation, streaming, cancellation and deletion."""
import json
from types import SimpleNamespace

import httpx
import pytest

import cleaner_client as cleaner
import state
from conftest import make_user, signin
from test_chat import attach, messages, new_chat, png, sse, turn

EMAIL = "ivan@contoso.ru"


@pytest.fixture
def protected(studio, monkeypatch):
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    cleaner.TICKETS.clear()
    cleaner.LOCKS.clear()
    sent, deleted, prepared = [], [], []
    remote = SimpleNamespace(sent=sent, deleted=deleted, prepared=prepared, down=False, revoked=False, local=[])
    original = studio.gw.handler

    def handler(request):
        path = request.url.path
        if not path.startswith("/api/v1"):
            return original(request)
        if remote.down:
            raise httpx.ConnectError("offline")
        if path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "protected-model"}]})
        if path == "/api/v1/chats":
            return httpx.Response(201, json={"id": "remote" + str(len(prepared))})
        if path.endswith("/local-requests"):
            body = json.loads(request.content)
            prepared.append(body)
            return httpx.Response(200, json={"receipt": "local-receipt", "messages": body["messages"],
                "counts": {}, "deliverable": True, "blocked": 0, "leaks": 0, "replacements": [], "route": "local",
                "destination": {"url": "http://127.0.0.1:30000/v1", "model": "local-model"}})
        if path.endswith("/local-requests/send"):
            remote.local.append(json.loads(request.content))
            return httpx.Response(202, json={"item_id": "localanswer"})
        if path.endswith("/requests"):
            body = json.loads(request.content)
            prepared.append(body)
            cleaned = json.loads(json.dumps(body["messages"]).replace(EMAIL, "<EMAIL_1>"))
            return httpx.Response(200, json={"receipt": "receipt" + str(len(prepared)), "messages": cleaned,
                                            "counts": {"EMAIL": 1}, "deliverable": True,
                                            "blocked": 0, "leaks": 0, "replacements": []})
        if path.endswith("/requests/send"):
            sent.append(json.loads(request.content))
            return httpx.Response(202, json={"item_id": "answer1"})
        if path.endswith("/events"):
            data = {"status": "done", "text": f"Ответ {EMAIL}", "raw": "Ответ <EMAIL_1>", "error": ""}
            return httpx.Response(200, text="event: text\ndata: {\"text\":\"Ответ \"}\n\n"
                                 + "event: done\ndata: " + json.dumps(data) + "\n\n",
                                 headers={"Content-Type": "text/event-stream"})
        if path.endswith("/texts"):
            return httpx.Response(200, json={"status": "done", "deliverable": True,
                                            "sanitized": json.loads(request.content)["text"].replace(EMAIL, "<EMAIL_1>")})
        if request.method == "DELETE":
            deleted.append((str(request.url), request.headers["authorization"]))
            return httpx.Response(401 if remote.revoked else 204)
        if path.endswith("/stop"):
            return httpx.Response(204)
        raise AssertionError(path)

    monkeypatch.setattr(state.HTTP._transport, "handler", handler)
    response = studio.c.post("/api/connections", json={"name": "Cleaner", "kind": "openai", "cleaner": True,
        "base_url": "http://10.77.1.15:4000", "token": "ck_synthetic", "sections": ["chat", "translate"]})
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    import sections
    sections.set_models(uid, "chat", cid, ["protected-model"])
    sections.set_models(uid, "translate", cid, ["protected-model"])
    remote.conn_id, remote.uid = cid, uid
    return studio, remote


def preview(studio, cid, **kwargs):
    return studio.c.post(f"/api/chats/{cid}/cleaner-preview", json={"text": f"Почта {EMAIL}", **kwargs})


def test_reviewed_chat_streams_restored_text_and_preserves_raw_answer(protected):
    studio, remote = protected
    cid = new_chat(studio)
    shown = preview(studio, cid)
    assert shown.status_code == 200, shown.text
    assert EMAIL not in shown.text and "receipt" not in shown.text and not remote.sent
    events = sse(turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown.json()["ticket"]))
    assert any(kind == "replace" and data["text"] == f"Ответ {EMAIL}" for kind, data in events)
    answer = messages(studio, cid)[-1]
    assert answer["content"] == f"Ответ {EMAIL}" and answer["cleaner_raw"] == "Ответ <EMAIL_1>"
    assert answer["status"] == "done" and len(remote.sent) == 1
    assert studio.gw.chat_requests == []


def test_explicit_local_preview_requires_its_own_confirmation_and_never_sends_cloud(protected):
    studio, remote = protected
    cid = new_chat(studio)
    cloud = preview(studio, cid).json()
    shown = preview(studio, cid, cleaner_local=True)
    assert shown.status_code == 200, shown.text
    assert EMAIL in shown.json()["messages"][-1]["content"] and shown.json()["destination"]["model"] == "local-model"
    assert remote.local == remote.sent == []
    assert turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=cloud["ticket"]).status_code == 409
    result = sse(turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown.json()["ticket"]))
    assert result[-1] == ("done", {"status": "done"})
    assert len(remote.local) == 1 and remote.sent == [] and studio.gw.chat_requests == []
    assert messages(studio, cid)[-1]["model"] == "local-model"


def test_missing_stale_and_reused_ticket_cannot_send(protected):
    studio, remote = protected
    cid = new_chat(studio)
    assert turn(studio, cid).status_code == 409
    shown = preview(studio, cid).json()
    assert turn(studio, cid, "Другой текст", cleaner_ticket=shown["ticket"]).status_code == 409
    assert messages(studio, cid) == [] and not remote.sent
    assert turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown["ticket"]).status_code == 200
    assert turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown["ticket"]).status_code == 409
    assert len(remote.sent) == 1


def test_cleaner_preview_and_send_share_the_account_calendar_date(protected):
    studio, remote = protected
    studio.clock[0] = 1_792_616_400
    assert studio.c.patch("/api/me", json={"timezone": "America/New_York"}).status_code == 200
    cid = new_chat(studio)
    shown = preview(studio, cid)
    assert shown.status_code == 200
    assert remote.prepared[-1]["messages"][0] == {
        "role": "system", "content": "Сегодня среда, 21 октября 2026 года (America/New_York)."}
    assert turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown.json()["ticket"]).status_code == 200
    assert len(remote.sent) == 1


def test_image_is_refused_and_text_attachment_including_filename_is_reviewed(protected):
    studio, remote = protected
    cid = new_chat(studio)
    picture = attach(studio, "photo.png", png(), "image/png")
    assert preview(studio, cid, attachment_ids=[picture["id"]]).status_code == 409
    doc = attach(studio, f"{EMAIL}.txt", f"Документ {EMAIL}".encode())
    shown = preview(studio, cid, attachment_ids=[doc["id"]])
    assert shown.status_code == 200 and EMAIL not in shown.text
    assert EMAIL in remote.prepared[-1]["messages"][-1]["content"]
    assert "<document name=" in remote.prepared[-1]["messages"][-1]["content"]


def test_cleaner_outage_never_falls_back_and_search_is_disabled(protected):
    studio, remote = protected
    cid = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{cid}", json={"web": True}).status_code == 409
    remote.down = True
    assert preview(studio, cid).status_code == 502
    assert turn(studio, cid).status_code == 409
    assert not remote.sent and studio.gw.chat_requests == []


def test_key_change_and_cross_user_ticket_are_rejected(protected):
    studio, remote = protected
    cid = new_chat(studio)
    shown = preview(studio, cid).json()
    studio.c.patch(f"/api/connections/{remote.conn_id}", json={"token": "ck_rotated_synthetic"})
    assert turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown["ticket"]).status_code == 409
    make_user(studio, "other")
    signin(studio, "other")
    assert preview(studio, cid).status_code == 404
    assert not remote.sent


def test_context_deletion_retries_with_original_address_and_credential(protected):
    studio, remote = protected
    cid = new_chat(studio)
    preview(studio, cid)
    studio.c.delete(f"/api/chats/{cid}")
    assert not cleaner.TICKETS
    assert state.DB.execute("SELECT COUNT(*) FROM cleaner_contexts").fetchone()[0] == 0
    assert state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions").fetchone()[0] == 1
    remote.down = True
    studio.c.portal.call(cleaner.purge)
    assert not remote.deleted
    remote.down = False
    studio.c.delete(f"/api/connections/{remote.conn_id}")
    studio.c.portal.call(cleaner.purge)
    assert not remote.deleted                       # сбой отложил повтор
    studio.clock[0] += cleaner.RETRY_MAX_SEC
    studio.c.portal.call(cleaner.purge)
    assert remote.deleted[0][0].startswith("http://10.77.1.15:4000/api/v1/chats/")
    assert remote.deleted[0][1] == "Bearer ck_synthetic"
    assert state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions").fetchone()[0] == 0


def test_cleaned_copy_contains_document_and_does_not_call_model(protected):
    studio, remote = protected
    cid = new_chat(studio)
    shown = preview(studio, cid).json()
    turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown["ticket"])
    result = studio.c.post(f"/api/chats/{cid}/cleaner-copy", json={"message_id": messages(studio, cid)[0]["id"]})
    assert result.status_code == 200 and EMAIL not in result.text and "<EMAIL_1>" in result.json()["text"]
    assert len(remote.sent) == 1


def test_revoked_cleanup_key_is_backed_off_and_pending_is_visible(protected):
    studio, remote = protected
    cid = new_chat(studio)
    preview(studio, cid)
    studio.c.delete(f"/api/chats/{cid}")
    remote.revoked = True
    studio.c.portal.call(cleaner.purge)
    studio.c.portal.call(cleaner.purge)
    assert len(remote.deleted) == 1
    assert studio.c.get("/api/me/storage").json()["cleaner_pending"] == 1


def queue_deletion(uid, base_url, queued, token="ck_synthetic", n=1):
    import security
    for i in range(n):
        state.DB.execute("INSERT INTO cleaner_deletions(id, user_id, remote_id, base_url, token_enc, queued)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (f"{base_url}-{queued}-{i}", uid, f"r{i}", base_url,
                                                       token if isinstance(token, bytes) else security.encrypt(token),
                                                       queued + i))


def test_unreachable_address_does_not_hold_the_head_of_the_deletion_queue(protected, monkeypatch):
    """Находка 2026-11-09: десять старейших строк недоступного адреса навсегда стояли в голове очереди."""
    studio, remote = protected
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                     (json.dumps(["10.77.1.15:4000", "10.77.1.16:4000"]),))
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "10.77.1.16":
            raise httpx.ConnectError("offline")
        return httpx.Response(204)

    monkeypatch.setattr(state.HTTP._transport, "handler", handler)
    now = studio.clock[0]
    queue_deletion(remote.uid, "http://10.77.1.16:4000", now - 600, n=10)
    queue_deletion(remote.uid, "http://10.77.1.15:4000", now - 100)
    studio.c.portal.call(cleaner.purge)
    assert calls == ["10.77.1.16", "10.77.1.15"]      # один запрос на недоступный адрес за проход
    left = state.DB.execute("SELECT base_url, retry_at FROM cleaner_deletions").fetchall()
    assert {r["base_url"] for r in left} == {"http://10.77.1.16:4000"} and len(left) == 10
    assert all(r["retry_at"] == now + 600 for r in left)  # пауза — сколько ждёт старейшая строка
    studio.c.portal.call(cleaner.purge)
    assert len(calls) == 2
    studio.clock[0] = now + 601
    studio.c.portal.call(cleaner.purge)
    assert len(calls) == 3
    pause = state.DB.execute("SELECT MIN(retry_at) FROM cleaner_deletions").fetchone()[0] - studio.clock[0]
    assert pause == 1201                                 # пауза растёт вместе с ожиданием
    stalled = studio.c.get("/api/me/storage").json()
    assert stalled["cleaner_pending"] == 10
    assert stalled["cleaner_stalled"] == [{"base_url": "http://10.77.1.16:4000", "count": 10}]


def test_unlisted_address_and_undecryptable_key_are_postponed_for_a_day(protected, monkeypatch):
    studio, remote = protected
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(204)

    monkeypatch.setattr(state.HTTP._transport, "handler", handler)
    now = studio.clock[0]
    queue_deletion(remote.uid, "http://10.77.1.99:4000", now - 10, n=3)       # убран из списка
    queue_deletion(remote.uid, "http://10.77.1.15:4000", now - 5, token=b"not-a-fernet-token")
    make_user(studio, "other")
    other = state.DB.execute("SELECT id FROM users WHERE login = 'other'").fetchone()[0]
    queue_deletion(other, "http://10.77.1.98:4000", now - 1)
    studio.c.portal.call(cleaner.purge)
    assert calls == []
    rows = state.DB.execute("SELECT user_id, retry_at FROM cleaner_deletions").fetchall()
    assert len(rows) == 5 and all(r["retry_at"] == now + cleaner.REFUSED_SEC for r in rows)
    shown = studio.c.get("/api/me/storage").json()
    assert shown["cleaner_pending"] == 4                 # чужая строка не считается и адрес не виден
    assert shown["cleaner_stalled"] == [{"base_url": "http://10.77.1.15:4000", "count": 1},
                                        {"base_url": "http://10.77.1.99:4000", "count": 3}]


def test_connection_requires_server_route_and_cannot_expose_browser_token(protected):
    studio, remote = protected
    body = {"name": "C", "kind": "openai", "cleaner": True, "base_url": "https://unlisted.example",
            "token": "ck_test", "sections": ["chat"]}
    assert studio.c.post("/api/connections", json=body).status_code == 422
    body.update(base_url="http://10.77.1.15:4000", sections=["photo"])
    assert studio.c.post("/api/connections", json=body).status_code == 422
    assert studio.c.get(f"/api/connections/{remote.conn_id}/token").status_code == 403


def test_translation_through_cleaner_sends_no_context_of_the_previous_chunk(protected, monkeypatch):
    """Конец перевода предыдущего куска — восстановленный Cleaner текст: в указание следующего куска он не идёт."""
    import translate_text
    from test_translate import wait_row
    studio, remote = protected
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    result = studio.c.post("/api/translate", json={"text": "First one is here.\n\nSecond one is here.",
                                                   "source_lang": "en", "target_lang": "ru",
                                                   "connection_id": remote.conn_id, "model": "protected-model"})
    assert result.status_code == 202, result.text
    assert wait_row(studio, result.json()["id"])["state"] == "done"
    assert [p["messages"][0]["content"] for p in remote.prepared] == [translate_text.system_prompt("en", "ru")] * 2


def test_translation_uses_checked_request_and_shared_document_context(protected):
    studio, remote = protected
    result = studio.c.post("/api/translate", json={"text": f"Текст {EMAIL}", "source_lang": "auto", "target_lang": "en",
                            "connection_id": remote.conn_id, "model": "protected-model"})
    assert result.status_code == 202, result.text
    from test_translate import wait_row
    assert wait_row(studio, result.json()["id"])["state"] == "done"
    assert len(remote.sent) == 1 and studio.gw.chat_requests == []
    assert state.DB.execute("SELECT resource FROM cleaner_contexts").fetchone()[0] == "translate"
