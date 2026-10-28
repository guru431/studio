import json

import pytest
from claude_agent_sdk import SDKSessionInfo, SessionMessage

from studio_agent import errors, events, limits, sessions

CHAT = "0b1e2c3d-4f50-4617-8899-aabbccddeeff"


def U(uid, content):
    return SessionMessage(type="user", uuid=uid, session_id=CHAT, message={"role": "user", "content": content})


def A(uid, content, parent=None, model="claude-opus-5-5"):
    return SessionMessage(type="assistant", uuid=uid, session_id=CHAT, parent_tool_use_id=parent,
                          message={"role": "assistant", "model": model, "content": content})


def text(t):
    return {"type": "text", "text": t}


def tool(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid, content, error=False):
    return {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": error}


def test_normalize_merges_one_answer_and_attaches_results():
    msgs = [U("u1", "Сделай"), A("a1", [text("Смотрю."), tool("t1", "Bash", {"command": "git status"})]),
            U("u2", [result("t1", "чисто")]), A("a2", [text("Готово.")]), A("s1", [text("подагент")], parent="t9"),
            U("u3", "Ещё")]
    assert sessions.normalize(msgs) == [
        {"id": "u1", "role": "user", "text": "Сделай", "steps": [], "time": None},
        {"id": "a1", "role": "assistant", "text": "Смотрю.\n\nГотово.", "time": None,
         "steps": [{"id": "t1", "tool": "Bash", "input": {"command": "git status"}, "text": "чисто", "error": False}]},
        {"id": "u3", "role": "user", "text": "Ещё", "steps": [], "time": None}]


def test_normalize_blocks_errors_images_and_thinking():
    msgs = [U("u1", [text("Глянь"), {"type": "image", "source": {}}]),
            A("a1", [{"type": "thinking", "thinking": "хм"}, tool("t1", "Read", {"file_path": "x" * 30_000})]),
            U("u2", [result("t1", [text("нет файла")], error=True)])]
    items = sessions.normalize(msgs)
    assert items[0]["text"] == "Глянь\n[изображение]"
    step = items[1]["steps"][0]
    assert items[1]["text"] == "" and step["error"] is True and step["text"] == "нет файла"
    assert len(step["input"]["file_path"]) == limits.CLIP and step["input"]["file_path"].endswith("…")


def test_history_page_keeps_start_true_when_the_budget_cuts_it(monkeypatch, tmp_path):
    big = [A(f"a{i}", [text("x" * 1_000_000)]) for i in range(12)]
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: big)
    page = sessions.history(tmp_path, CHAT, None, 100)
    assert page["total"] == 1 and len(json.dumps(page, ensure_ascii=True)) <= limits.HISTORY_BUDGET + 1000
    assert page["items"][0]["text"].startswith("… начало не показано\nxxx")


def test_history_of_an_unknown_chat_is_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: [])
    with pytest.raises(errors.Refused) as e:
        sessions.history(tmp_path, CHAT, None, 100)
    assert e.value.code == "not_found" and e.value.message == "нет такого чата"


def test_history_pages_before_a_number(monkeypatch, tmp_path):
    msgs = [U(f"u{i}", f"вопрос {i}") for i in range(300)]
    seen = {}

    def read(chat, directory=None, **k):
        seen.update(chat=chat, directory=directory)
        return msgs

    monkeypatch.setattr(sessions, "get_session_messages", read)
    page = sessions.history(tmp_path, CHAT, 250, 100)
    assert page["start"] == 150 and page["total"] == 300 and len(page["items"]) == 100
    assert page["items"][0]["id"] == "u150" and page["items"][-1]["id"] == "u249"
    assert seen == {"chat": CHAT, "directory": str(tmp_path)}
    assert sessions.history(tmp_path, CHAT, 9999, 10)["start"] == 290


def test_history_page_cut_by_the_budget_is_the_tail(monkeypatch, tmp_path):
    msgs = [U(f"u{i}", "x" * 1_000_000) for i in range(12)]
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: msgs)
    page = sessions.history(tmp_path, CHAT, None, 100)
    n = len(page["items"])
    assert 1 <= n < 12 and page["start"] == 12 - n and page["start"] > 12 - 100
    assert page["items"][-1]["id"] == "u11"
    assert len(json.dumps(page["items"], ensure_ascii=True)) <= limits.HISTORY_BUDGET


def test_fit_drops_first_steps_of_a_single_huge_answer():
    steps = [{"id": f"t{i}", "tool": "Bash", "input": {"command": "y" * 5000}, "text": "z" * 5000, "error": False}
             for i in range(10)]
    item = {"id": "a1", "role": "assistant", "text": "Ответ", "steps": steps, "time": None}
    out = sessions.fit([item], budget=7000)
    assert len(out) == 1 and len(json.dumps(out, ensure_ascii=True)) <= 7000
    got = out[0]
    assert got["text"] == "… первые шаги не показаны\nОтвет"
    assert got["steps"] and got["steps"][-1]["id"] == "t9" and len(got["steps"]) < 10
    assert len(got["steps"][-1]["text"]) == 2000 and got["steps"][-1]["text"].endswith("…")
    assert item["text"] == "Ответ" and len(steps[0]["text"]) == 5000  # исходник не тронут


def test_fit_keeps_what_fits():
    items = [{"id": f"u{i}", "role": "user", "text": "т", "steps": [], "time": None} for i in range(5)]
    assert sessions.fit(items) == items
    assert sessions.fit([]) == []


def _info(sid, **kw):
    return SDKSessionInfo(session_id=sid, summary=kw.pop("summary", ""), last_modified=kw.pop("modified"), **kw)


def test_chats_lists_sessions_of_the_project(monkeypatch, tmp_path):
    other = "11111111-2222-4333-8444-555555555555"
    rows = [_info(other, modified=1_700_000_000_500, first_prompt="п" * 300),
            _info(CHAT, modified=1_800_000_000_999, created_at=1_790_000_000_000, custom_title="Свой",
                  summary="Сводка", tag="studio"),
            _info("not-a-uuid", modified=1_900_000_000_000, summary="чужое"),
            _info("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", modified=1_750_000_000_000, summary="С" * 300)]
    seen = {}

    def lst(**kw):
        seen.update(kw)
        return rows

    monkeypatch.setattr(sessions, "list_sessions", lst)
    got = sessions.chats(tmp_path, {CHAT: True, other: False})
    assert seen == {"directory": str(tmp_path), "limit": limits.CHATS_MAX, "include_worktrees": False}
    assert [c["chat"] for c in got] == [CHAT, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", other]
    assert got[0] == {"chat": CHAT, "title": "Свой", "created": 1_790_000_000, "modified": 1_800_000_000,
                      "tag": "studio", "running": True, "asking": True}
    assert len(got[1]["title"]) == 200 and got[1]["running"] is False and got[1]["asking"] is False
    assert got[2]["title"] == "п" * 120 and got[2]["created"] == got[2]["modified"] == 1_700_000_000
    assert got[2]["running"] is True and got[2]["asking"] is False and got[2]["tag"] is None


def test_last_model_skips_synthetic_and_subagents(monkeypatch, tmp_path):
    msgs = [A("a1", [text("1")], model="claude-opus-5-5"), A("a2", [text("2")], model="claude-sonnet-5"),
            A("s1", [text("3")], parent="t1", model="claude-haiku-5"), A("a3", [text("4")], model="<synthetic>")]
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: msgs)
    assert sessions.last_model(tmp_path, CHAT) == "claude-sonnet-5"
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: [U("u1", "привет")])
    assert sessions.last_model(tmp_path, CHAT) is None


def test_exists_and_mark_studio(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "get_session_info", lambda chat, directory=None: None)
    monkeypatch.setattr(sessions, "get_session_messages", lambda chat, directory=None, limit=None: [])
    assert sessions.exists(tmp_path, CHAT) is False
    # Сессия без извлекаемого названия: info — None, а записи есть.
    seen = {}

    def first(chat, directory=None, limit=None):
        seen.update(chat=chat, directory=directory, limit=limit)
        return [U("u1", "привет")]

    monkeypatch.setattr(sessions, "get_session_messages", first)
    assert sessions.exists(tmp_path, CHAT) is True
    assert seen == {"chat": CHAT, "directory": str(tmp_path), "limit": 1}
    monkeypatch.setattr(sessions, "get_session_info",
                        lambda chat, directory=None: _info(chat, modified=1))
    assert sessions.exists(tmp_path, CHAT) is True
    tags = []
    monkeypatch.setattr(sessions, "tag_session", lambda chat, tag, directory=None: tags.append((chat, tag, directory)))
    sessions.mark_studio(tmp_path, CHAT)
    assert tags == [(CHAT, "studio", str(tmp_path))]

    def broken(*a, **k):
        raise OSError("занято")

    monkeypatch.setattr(sessions, "tag_session", broken)
    sessions.mark_studio(tmp_path, CHAT)  # не бросает


def test_clip_cuts_nested_strings():
    value = {"a": ["x" * 30_000, {"b": "коротко"}], "n": 5}
    got = events.clip(value)
    assert len(got["a"][0]) == 20_000 and got["a"][0].endswith("…") and got["a"][0][:5] == "xxxxx"
    assert got["a"][1] == {"b": "коротко"} and got["n"] == 5 and len(value["a"][0]) == 30_000


def test_text_of():
    assert events.text_of("как есть") == "как есть"
    assert events.text_of([text("a"), {"type": "image"}, {"type": "tool_use"}, text("b")]) == "a\n[изображение]\nb"
    assert events.text_of(None) == ""
