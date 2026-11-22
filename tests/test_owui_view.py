"""Вид Open WebUI 0.13 для страницы (`owui_view`): ветка по `currentId`, рассуждение и текст из `output`, инструменты и
их результаты, пауза (вопрос модели и подтверждение), неизвестные элементы, цикл и пропавший родитель, прежние сообщения
без `output`. Формы элементов — по исходнику Open WebUI v0.13.4 (`utils/middleware.py`, `utils/ask_user.py`)."""
import json

import pytest

import owui_view


def chat(messages, current, **kw):
    """`ChatResponse` Open WebUI с историей `messages` (список сообщений) и текущим сообщением `current`."""
    return {"id": "c1", "title": "T", "folder_id": None,
            "chat": {"models": ["m1"], "history": {"messages": {m["id"]: m for m in messages}, "currentId": current}},
            **kw}


def user(mid, parent=None, text="hi", **kw):
    return {"id": mid, "parentId": parent, "role": "user", "content": text, "timestamp": 100, **kw}


def answer(mid, parent, output=None, **kw):
    return {"id": mid, "parentId": parent, "role": "assistant", "model": "m1", "content": "", "done": True,
            "timestamp": 101, **({"output": output} if output is not None else {}), **kw}


def text_item(text):
    return {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def call_item(call_id, name="search", args="{}", status="completed", **kw):
    return {"type": "function_call", "id": call_id, "call_id": call_id, "name": name, "arguments": args,
            "status": status, **kw}


def result_item(call_id, text, status="completed", **kw):
    return {"type": "function_call_output", "id": "fco_" + call_id, "call_id": call_id,
            "output": [{"type": "input_text", "text": text}], "status": status, **kw}


def test_branch_follows_current_id_not_siblings():
    messages = [user("u1"), answer("a1", "u1", [text_item("first")]),
                user("u2", "a1", "ask"), answer("a2", "u2", [text_item("old branch")]),
                user("u3", "a1", "ask again"), answer("a3", "u3", [text_item("current branch")])]
    for m in messages:
        m["childrenIds"] = [x["id"] for x in messages if x.get("parentId") == m["id"]]
    view = owui_view.branch(chat(messages, "a3"))
    assert [m["id"] for m in view["messages"]] == ["u1", "a1", "u3", "a3"]
    assert view["messages"][-1]["text"] == "current branch"
    assert (view["id"], view["title"], view["models"], view["folder_id"]) == ("c1", "T", ["m1"], None)
    first = view["messages"][0]
    assert (first["role"], first["text"], first["timestamp"], first["done"]) == ("user", "hi", 100, True)
    assert (first["reasoning"], first["tools"], first["files"], first["other"], first["error"], first["pending"]) \
        == (None, [], [], [], None, None)


def test_reasoning_and_text_split_from_output():
    output = [
        {"type": "reasoning", "id": "r_1", "status": "completed", "content": [{"type": "output_text", "text": "Let me "},
                                                                                 {"type": "output_text", "text": "think."}],
         "summary": None, "started_at": 10.2, "ended_at": 15.9, "duration": 5},
        text_item("The answer."),
    ]
    msg = owui_view.branch(chat([user("u1"), answer("a1", "u1", output, content="The answer.")], "a1"))["messages"][1]
    assert msg["reasoning"] == {"text": "Let me think.", "seconds": 5}
    assert msg["text"] == "The answer."
    # без `duration` — по ended_at − started_at; без обоих — длительность неизвестна
    output[0].pop("duration")
    msg = owui_view.branch(chat([user("u1"), answer("a1", "u1", output)], "a1"))["messages"][1]
    assert msg["reasoning"]["seconds"] == 5
    for key in ("started_at", "ended_at"):
        output[0].pop(key)
    msg = owui_view.branch(chat([user("u1"), answer("a1", "u1", output)], "a1"))["messages"][1]
    assert msg["reasoning"] == {"text": "Let me think.", "seconds": None}


def test_tools_and_results_are_paired_and_truncated():
    long = "x" * (owui_view.RESULT_MAX + 500)
    output = [call_item("fc_1", "web_search", '{"q": "a"}'), result_item("fc_1", "short result"),
              call_item("fc_2", "fetch", status="completed"), result_item("fc_2", long),
              call_item("fc_3", "broken"), result_item("fc_3", "boom", status="failed"),
              call_item("fc_4", "running", status="in_progress"),
              call_item("fc_h", "hidden_skill", hidden=True), result_item("fc_h", "secret"),
              text_item("done")]
    tools = owui_view.branch(chat([user("u1"), answer("a1", "u1", output)], "a1"))["messages"][1]["tools"]
    assert [(t["call_id"], t["name"], t["status"]) for t in tools] == [
        ("fc_1", "web_search", "completed"), ("fc_2", "fetch", "completed"), ("fc_3", "broken", "failed"),
        ("fc_4", "running", "in_progress")]
    assert tools[0]["result"] == "short result"
    assert len(tools[1]["result"]) == owui_view.RESULT_MAX and tools[1]["result"].endswith("…")
    assert tools[1]["result"].startswith("x" * 100)
    assert tools[3]["result"] is None


def question_args():
    return json.dumps({"questions": [
        {"id": "q1", "header": "Style", "question": "Which style?",
         "options": [{"label": "Short", "description": "Brief"}, {"label": "Long", "description": "Detailed"}],
         "allow_other": True}], "allow_other": True, "timeout_ms": 120000})


def test_pending_question_and_approval():
    asking = answer("a1", "u1", [call_item("fc_q", "ask_user", question_args(), status="pending")], done=False)
    msg = owui_view.branch(chat([user("u1"), asking], "a1"))["messages"][1]
    assert msg["done"] is False and msg["tools"] == []
    assert msg["pending"] == {"call_id": "fc_q", "kind": "question", "allow_other": True, "questions": [
        {"id": "q1", "header": "Style", "question": "Which style?", "allow_other": True,
         "options": [{"label": "Short", "description": "Brief"}, {"label": "Long", "description": "Detailed"}]}]}
    long_args = json.dumps({"path": "p" * (owui_view.ARGS_MAX + 100)})
    approving = answer("a2", "u1", [call_item("fc_1", "read", "{}"), result_item("fc_1", "ok"),
                                    call_item("fc_2", "write_file", long_args, status="requires_approval")],
                       done=False)
    msg = owui_view.branch(chat([user("u1"), approving], "a2"))["messages"][1]
    pending = msg["pending"]
    assert (pending["kind"], pending["call_id"], pending["name"]) == ("approval", "fc_2", "write_file")
    assert len(pending["arguments"]) == owui_view.ARGS_MAX and pending["arguments"].endswith("…")
    assert [t["call_id"] for t in msg["tools"]] == ["fc_1"]            # вызов на паузе — в `pending`, не в `tools`
    # решённый вызов и законченный ответ паузой не считаются
    finished = answer("a3", "u1", [call_item("fc_q", "ask_user", question_args()), result_item("fc_q", "{}")])
    assert owui_view.branch(chat([user("u1"), finished], "a3"))["messages"][1]["pending"] is None
    stale = answer("a4", "u1", [call_item("fc_2", "write_file", status="pending")])
    assert owui_view.branch(chat([user("u1"), stale], "a4"))["messages"][1]["pending"] is None


def test_broken_question_stays_visible_without_pause_card():
    msg = owui_view.branch(chat([user("u1"), answer("a1", "u1", [call_item("fc_q", "ask_user", "{not json", "pending")],
                                                   done=False)], "a1"))["messages"][1]
    assert msg["pending"] is None and msg["other"] == ["ask_user"] and [t["name"] for t in msg["tools"]] == ["ask_user"]


def test_unknown_output_item_goes_to_other():
    output = [{"type": "web_search_call", "id": "ws_1", "status": "completed"}, {"type": "web_search_call", "id": "ws_2"},
              {"type": "image_generation_call", "id": "ig_1"}, {"no": "type"}, "not an item", text_item("ok")]
    msg = owui_view.branch(chat([user("u1"), answer("a1", "u1", output)], "a1"))["messages"][1]
    assert msg["other"] == ["web_search_call", "image_generation_call", "unknown"]
    assert msg["text"] == "ok"


def test_branch_survives_missing_parent_and_cycle():
    lost = chat([answer("a1", "gone", [text_item("tail")]), ], "a1")
    assert [m["id"] for m in owui_view.branch(lost)["messages"]] == ["a1"]            # родитель пропал — конец ветки
    loop = chat([user("u1", "a1"), answer("a1", "u1", [text_item("x")]), user("u9")], "a1")
    assert [m["id"] for m in owui_view.branch(loop)["messages"]] == ["u1", "a1"]        # цикл — каждое сообщение раз
    self_loop = chat([answer("a1", "a1")], "a1")
    assert [m["id"] for m in owui_view.branch(self_loop)["messages"]] == ["a1"]
    odd = chat([user("u1"), answer("a1", "u1")], "a1")
    odd["chat"]["history"]["messages"]["u1"] = "not a message"
    assert [m["id"] for m in owui_view.branch(odd)["messages"]] == ["a1"]
    empty = chat([], None)
    assert owui_view.branch(empty)["messages"] == []
    missing_current = chat([user("u1")], "ghost")
    assert owui_view.branch(missing_current)["messages"] == []


def test_old_messages_without_output_use_content():
    old = answer("a1", "u1", None, content="plain old answer", done=True)
    msg = owui_view.branch(chat([user("u1", text="question"), old], "a1"))["messages"]
    assert [m["text"] for m in msg] == ["question", "plain old answer"]
    assert msg[1]["reasoning"] is None and msg[1]["tools"] == []
    odd = answer("a2", None, [], content=None)
    assert owui_view.branch(chat([odd], "a2"))["messages"][0]["text"] == ""


def test_files_errors_and_models():
    attached = user("u1", files=[{"type": "file", "id": "f1", "name": "doc.pdf", "file": {"id": "f1"}},
                                 {"type": "image", "url": "/api/v1/files/f2/content", "name": "pic.png"},
                                 {"type": "image", "url": "data:image/png;base64,AAAA"}])
    shown = answer("a1", "u1", [result_item("fc_1", "ok", files=[{"type": "image", "url": "/api/v1/files/f3/content"}]),
                                call_item("fc_1"), text_item("t")], error={"content": "model failed"})
    msgs = owui_view.branch(chat([attached, shown], "a1"))["messages"]
    assert msgs[0]["files"] == [{"id": "f1", "name": "doc.pdf", "kind": "file"},
                                {"id": "f2", "name": "pic.png", "kind": "image"}]
    assert msgs[1]["files"] == [{"id": "f3", "name": "", "kind": "image"}] and msgs[1]["error"] == "model failed"
    bare = answer("a2", "u1", [], error=True)
    assert owui_view.branch(chat([attached, bare], "a2"))["messages"][1]["error"] == ""
    assert owui_view.model_item({"id": "agent:term"}) is None
    assert owui_view.model_item({"id": "m", "info": {"meta": {"hidden": True}}}) is None
    assert owui_view.model_item({"id": "m"}) == {"id": "m", "name": "m", "vision": True, "file_upload": True}
    caps = {"id": "m", "name": "M", "info": {"meta": {"capabilities": {"vision": False}}}}
    assert owui_view.model_item(caps) == {"id": "m", "name": "M", "vision": False, "file_upload": True}


@pytest.mark.parametrize("raw", [[], "x", {"id": "c", "chat": []}, {"id": "c", "chat": {}},
                                 {"id": "c", "chat": {"history": []}}, {"id": "c", "chat": {"history": {"messages": []}}},
                                 {"chat": {"history": {"messages": {}}}},
                                 {"id": "c", "chat": {"history": {"messages": {"a": {"id": "a"}}}}}])
def test_unexpected_shape_raises_shape(raw):
    with pytest.raises(owui_view.Shape):
        owui_view.branch(raw)


def test_list_items_validate():
    item = owui_view.chat_item({"id": "c1", "title": "T", "updated_at": 5, "active": True, "pending_approval": True})
    assert item == {"id": "c1", "title": "T", "updated": 5, "folder_id": None, "active": True, "pending": True}
    assert owui_view.folder_item({"id": "f", "name": "N", "parent_id": "p"}) == {"id": "f", "name": "N", "parent_id": "p"}
    for call, raw in ((owui_view.chat_item, {"title": "x"}), (owui_view.chat_item, []), (owui_view.folder_item, {}),
                      (owui_view.model_item, {"name": "x"})):
        with pytest.raises(owui_view.Shape):
            call(raw)
