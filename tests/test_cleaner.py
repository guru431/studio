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
    remote = SimpleNamespace(sent=sent, deleted=deleted, prepared=prepared, down=False, revoked=False)
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


def test_connection_requires_server_route_and_cannot_expose_browser_token(protected):
    studio, remote = protected
    body = {"name": "C", "kind": "openai", "cleaner": True, "base_url": "https://unlisted.example",
            "token": "ck_test", "sections": ["chat"]}
    assert studio.c.post("/api/connections", json=body).status_code == 422
    body.update(base_url="http://10.77.1.15:4000", sections=["photo"])
    assert studio.c.post("/api/connections", json=body).status_code == 422
    assert studio.c.get(f"/api/connections/{remote.conn_id}/token").status_code == 403


def test_translation_uses_checked_request_and_shared_document_context(protected):
    studio, remote = protected
    result = studio.c.post("/api/translate", json={"text": f"Текст {EMAIL}", "source_lang": "auto", "target_lang": "en",
                            "connection_id": remote.conn_id, "model": "protected-model"})
    assert result.status_code == 202, result.text
    from test_translate import wait_row
    assert wait_row(studio, result.json()["id"])["state"] == "done"
    assert len(remote.sent) == 1 and studio.gw.chat_requests == []
    assert state.DB.execute("SELECT resource FROM cleaner_contexts").fetchone()[0] == "translate"
