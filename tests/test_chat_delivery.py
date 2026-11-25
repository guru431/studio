"""Durable acknowledgement of a turn whose HTTP response may be lost."""
import chat
import state
from conftest import add_gateway, make_user, signin
from test_chat import attach, events_of, messages, new_chat, png, sse, turn, window

KEY = '11111111-1111-4111-8111-111111111111'


def test_retry_of_accepted_turn_does_not_call_model_again(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    ids = sse(turn(studio, cid, request_id=KEY))[0][1]
    # In-memory stream state is already gone; acknowledgement must survive it.
    assert not chat.STREAMS
    response = turn(studio, cid, request_id=KEY)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/json')
    assert response.json() == {'replayed': True, **ids}
    assert len(studio.gw.chat_requests) == 1
    assert len(messages(studio, cid)) == 2
    status = studio.c.get(f'/api/chats/{cid}/turns/{KEY}')
    assert status.json() == {'accepted': True, **ids}
    # The same words intentionally sent as a new operation must still work.
    sse(turn(studio, cid, request_id='22222222-2222-4222-8222-222222222222'))
    assert len(studio.gw.chat_requests) == 2


def test_turn_key_is_bound_to_content_and_receipt_to_its_owner(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    assert studio.c.get(f'/api/chats/{cid}/turns/{KEY}').json() == {'accepted': False}
    sse(turn(studio, cid, request_id=KEY))
    conflict = turn(studio, cid, text='Different', request_id=KEY)
    assert conflict.status_code == 409
    assert conflict.json()['detail']['code'] == 'request_conflict'
    make_user(studio, 'outsider')
    signin(studio, 'outsider')
    assert studio.c.get(f'/api/chats/{cid}/turns/{KEY}').status_code == 404
    assert turn(studio, cid, request_id=KEY).status_code == 404


def test_old_acknowledgement_cannot_regenerate_a_deleted_answer(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    sse(turn(studio, cid, request_id=KEY))
    sse(turn(studio, cid, regenerate=True, request_id='33333333-3333-4333-8333-333333333333'))
    response = turn(studio, cid, request_id=KEY)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'request_gone'
    assert len(studio.gw.chat_requests) == 2
    assert studio.c.delete(f'/api/chats/{cid}').status_code == 204
    assert studio.c.request("DELETE", f'/api/trash/chats/{cid}', json={"password": "password-123"}).status_code == 204     # окончательно — с отметками ходов
    assert state.DB.execute('SELECT count(*) FROM chat_turn_requests').fetchone()[0] == 0


def test_retry_while_stream_runs_keeps_its_original_reader_and_attachment(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    attachment = attach(studio, 'synthetic.png', png(), 'image/png')
    studio.gw.chat_hold = True
    body = {'text': 'Image', 'request_id': KEY, 'attachment_ids': [attachment['id']]}
    with studio.c.stream('POST', f'/api/chats/{cid}/turns', json=body) as response:
        events = events_of(response.iter_lines())
        _, ids = next(events)
        next(data for event, data in events if event == 'delta')
        retry = studio.c.post(f'/api/chats/{cid}/turns', json=body)
        assert retry.json() == {'replayed': True, **ids}
        assert chat.STREAMS[ids['assistant_message_id']].listening
        studio.gw.chat_hold = False
        assert list(events)[-1][0] == 'done'
    assert len(studio.gw.chat_requests) == 1
    assert messages(studio, cid)[0]['attachments'][0]['id'] == attachment['id']


def test_edit_turn_replaces_suffix_atomically_and_preserves_attachment(studio, monkeypatch):
    add_gateway(studio)
    cid = new_chat(studio)
    keep = attach(studio, "keep.txt", b"original attachment")
    first = sse(turn(studio, cid, "Before", attachment_ids=[keep["id"]]))[0][1]
    second = sse(turn(studio, cid, "Later"))[0][1]
    original = messages(studio, cid)
    target = {"id": first["user_message_id"], "last_id": second["assistant_message_id"]}
    stale = turn(studio, cid, text="Edit", edit={**target, "last_id": first["assistant_message_id"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "edit_stale"
    assert messages(studio, cid) == original

    window(studio, 1)
    too_large = turn(studio, cid, text="Edit", edit=target)
    assert too_large.status_code == 413
    window(studio, chat.CONTEXT["local-gemma-abl"])
    assert messages(studio, cid) == original
    accepted = sse(turn(studio, cid, text="Edit", request_id=KEY, edit=target))[0][1]
    assert accepted["user_message_id"] == target["id"]
    edited = messages(studio, cid)
    assert len(edited) == 2 and edited[0]["content"] == "Edit"
    assert [a["id"] for a in edited[0]["attachments"]] == [keep["id"]]
    request = studio.gw.chat_requests[-1]["messages"][-1]["content"]
    assert "original attachment" in request and "Edit" in request and "Later" not in request
    replay = turn(studio, cid, text="Edit", request_id=KEY, edit=target)
    assert replay.json() == {"replayed": True, **accepted}


def test_delete_turn_leaves_subsequent_history_and_wont_delete_an_assistant_alone(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    first = sse(turn(studio, cid, "First", request_id=KEY))[0][1]
    second = sse(turn(studio, cid, "Second"))[0][1]
    path = f"/api/chats/{cid}/turns/"
    assert studio.c.delete(path + first["assistant_message_id"]).status_code == 404
    assert studio.c.delete(path + first["user_message_id"]).status_code == 204
    assert [m["id"] for m in messages(studio, cid)] == list(second.values())
    assert studio.c.get(path + KEY).json()["detail"]["code"] == "request_gone"
    make_user(studio, "outsider")
    signin(studio, "outsider")
    assert studio.c.delete(path + second["user_message_id"]).status_code == 404
