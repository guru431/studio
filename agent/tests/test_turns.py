import asyncio
import json

import pytest
from claude_agent_sdk import (CLINotFoundError, PermissionResultAllow, PermissionResultDeny, PermissionUpdate,
                              ResultError)
from claude_agent_sdk.types import PermissionRuleValue

from fake_sdk import (HOLD, FakeFactory, assistant, block_start, delta, result, result_ok, text, tool_result,
                      tool_use, user)
from studio_agent import VERSION, cli, errors, events, limits, sessions, store
from studio_agent.turns import Runner

PID = "a1b2c3d4e5f60718"
CHAT = "0b1e2c3d-4f50-4617-8899-aabbccddeeff"
OTHER = "11111111-2222-4333-8444-555555555555"
THIRD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RULE = PermissionUpdate(type="addRules", rules=[PermissionRuleValue(tool_name="Bash", rule_content="ls")],
                        behavior="allow", destination="localSettings")


@pytest.fixture
def marked(monkeypatch):
    """Сессий SDK нет; метки `studio` — в этот список."""
    marked = []
    monkeypatch.setattr(sessions, "exists", lambda project, chat: False)
    monkeypatch.setattr(sessions, "last_model", lambda project, chat: None)
    monkeypatch.setattr(sessions, "mark_studio", lambda project, chat: marked.append((project, chat)))
    return marked


@pytest.fixture
def project(tmp_path, marked):
    p = tmp_path / "demo"
    p.mkdir()
    return p


async def settle(r: Runner) -> None:
    """Дождаться задач всех ходов."""
    while r._turns:
        await asyncio.wait({t.task for t in r._turns.values()})


async def until(check, tries: int = 2000):
    """Ждать условия: ход ждёт поток (`to_thread`) — короткими шагами."""
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("не дождались")


def kinds(sent):
    return [f["e"]["k"] for f in sent]


def runner(home, f, sent):
    return Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=f)


async def test_turn_streams_text_steps_and_ends_done(home, project):
    sent, f = [], FakeFactory([delta("При"), delta("вет"), assistant(tool_use("t1", "Read", {"file_path": "a"})),
                               user(tool_result("t1", "ok")), result_ok()])
    r = runner(home, f, sent)
    turn = r.start(project, PID, CHAT, "Привет", None, None)
    await settle(r)
    assert kinds(sent) == ["text", "text", "step", "result", "done"]
    assert {e["turn"] for e in sent} == {turn} and sent[-1]["e"] == {"k": "done", "status": "done"}
    assert all(e["t"] == "ev" and e["project"] == PID and e["chat"] == CHAT for e in sent)
    assert sent[-1]["project_name"] == project.name
    assert sent[2]["e"] == {"k": "step", "id": "t1", "tool": "Read", "input": {"file_path": "a"}}
    assert sent[3]["e"] == {"k": "result", "id": "t1", "text": "ok", "error": False}
    opts = f.options[0]
    assert opts.session_id == CHAT and opts.resume is None
    assert opts.setting_sources == ["user", "project", "local"]
    assert opts.cwd == project and opts.include_partial_messages is True and opts.model is None
    assert opts.env == {"CLAUDE_AGENT_SDK_CLIENT_APP": f"studio-agent/{VERSION}"} and opts.stderr is not None
    assert opts.permission_mode is None and opts.can_use_tool is not None
    assert opts.extra_args == {"chrome": None} and opts.tools is None and opts.disallowed_tools == []
    hook = opts.hooks["PreToolUse"][0]
    assert hook.matcher is None and await hook.hooks[0]({}, None, {}) == {"continue_": True}
    assert f.clients[0].queries == ["Привет"] and f.clients[0].disconnected


async def test_permission_card_allow_always_deny(home, project):
    sent, f = [], FakeFactory([("ask", "Bash", {"command": "ls"}, [RULE]), ("ask", "Write", {"file_path": "b"}, []),
                               ("ask", "Bash", {"command": "pwd"}, []), result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Сделай", None, None)
    await until(lambda: r.live().get(CHAT))
    ask = sent[-1]["e"]
    assert ask == {"k": "ask", "request_id": "toolu_1", "kind": "permission", "tool": "Bash",
                   "input": {"command": "ls"}, "always": True}
    with pytest.raises(errors.Refused) as e:
        r.answer(PID, CHAT, "toolu_1", "maybe", None, None)
    assert e.value.code == "invalid"
    with pytest.raises(errors.Refused) as e:
        r.answer(PID, CHAT, "toolu_1", "allow", None, {"вопрос": "ответ"})
    assert e.value.code == "invalid"
    with pytest.raises(errors.Refused) as e:
        r.answer(PID, CHAT, "toolu_1", "deny", "x" * 2001, None)
    assert e.value.code == "invalid"
    r.answer(PID, CHAT, "toolu_1", "always", None, None)
    with pytest.raises(errors.Refused) as e:
        r.answer(PID, CHAT, "toolu_1", "allow", None, None)
    assert e.value.code == "gone" and e.value.message == "запроса уже нет"
    await until(lambda: len(f.clients[0].decisions) == 1 and r.live().get(CHAT))
    assert sent[-1]["e"]["always"] is False and sent[-1]["e"]["request_id"] == "toolu_2"
    r.answer(PID, CHAT, "toolu_2", "deny", None, None)
    await until(lambda: len(f.clients[0].decisions) == 2 and r.live().get(CHAT))
    r.answer(PID, CHAT, "toolu_3", "deny", "Не так", None)
    await settle(r)
    assert f.clients[0].decisions == [
        PermissionResultAllow(updated_input={"command": "ls"}, updated_permissions=[RULE]),
        PermissionResultDeny(message="Пользователь отказал"),
        PermissionResultDeny(message="Не так")]
    assert kinds(sent) == ["ask", "asked", "ask", "asked", "ask", "asked", "done"]
    assert sent[1]["e"] == {"k": "asked", "request_id": "toolu_1"}
    with pytest.raises(errors.Refused) as e:
        r.answer(PID, CHAT, "toolu_1", "allow", None, None)
    assert e.value.code == "gone"


async def test_plain_allow_and_odd_tool_use_id(home, project):
    sent, f = [], FakeFactory([("ask", "Edit", {"file_path": "c"}, [], "плохой id"), result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Правь", None, None)
    await until(lambda: r.live().get(CHAT))
    rid = sent[-1]["e"]["request_id"]
    assert rid != "плохой id" and len(rid) == 16
    r.answer(PID, CHAT, rid, "allow", None, None)
    await settle(r)
    assert f.clients[0].decisions == [PermissionResultAllow(updated_input={"file_path": "c"})]


async def test_question_returns_answers_in_updated_input(home, project):
    data = {"questions": [{"question": "Какой?", "header": "Выбор", "multiSelect": True,
                           "options": [{"label": "А", "description": "первый"}, {"label": "Б", "description": ""}]}]}
    sent, f = [], FakeFactory([("ask", "AskUserQuestion", data, []), result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Спроси", None, None)
    await until(lambda: r.live().get(CHAT))
    assert sent[-1]["e"] == {"k": "ask", "request_id": "toolu_1", "kind": "question", "questions": [
        {"question": "Какой?", "header": "Выбор", "multi": True,
         "options": [{"label": "А", "description": "первый"}, {"label": "Б", "description": ""}]}]}
    for decision, answers in (("deny", None), ("always", {"Какой?": "А"}), ("allow", None),
                              ("allow", {"Какой?": 5}), ("allow", {"Какой?": ["А", 1]}), ("allow", ["А"])):
        with pytest.raises(errors.Refused) as e:
            r.answer(PID, CHAT, "toolu_1", decision, None, answers)
        assert e.value.code == "invalid"
    r.answer(PID, CHAT, "toolu_1", "allow", None, {"Какой?": ["А", "Б"]})
    await settle(r)
    assert f.clients[0].decisions == [PermissionResultAllow(updated_input={**data, "answers": {"Какой?": ["А", "Б"]}})]
    assert kinds(sent) == ["ask", "asked", "done"]


async def test_stop_during_a_card_denies_it_and_ends_stopped(home, project):
    sent, f = [], FakeFactory([("ask", "Bash", {"command": "rm"}, []), result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Удали", None, None)
    await until(lambda: r.live().get(CHAT))
    await r.stop(PID, CHAT)
    await settle(r)
    assert kinds(sent) == ["ask", "asked", "done"]
    assert sent[-1]["e"] == {"k": "done", "status": "stopped"}
    assert f.clients[0].decisions == [PermissionResultDeny(message="Остановлено", interrupt=True)]
    assert f.clients[0].interrupted and f.clients[0].disconnected
    with pytest.raises(errors.Refused) as e:
        await r.stop(PID, CHAT)
    assert e.value.code == "idle" and e.value.message == "хода нет"


async def test_stop_without_a_card_interrupts(home, project):
    sent, f = [], FakeFactory([delta("Думаю"), HOLD, result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Долго", None, None)
    await until(lambda: kinds(sent) == ["text"])
    await r.stop(PID, CHAT)
    await settle(r)
    assert kinds(sent) == ["text", "done"] and sent[-1]["e"]["status"] == "stopped"


async def test_second_turn_in_the_chat_is_running_and_third_chat_is_busy(home, project):
    sent, f = [], FakeFactory([HOLD])
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=f, max_turns=2)
    r.start(project, PID, CHAT, "1", None, None)
    with pytest.raises(errors.Refused) as e:
        r.start(project, PID, CHAT, "2", None, None)
    assert e.value.code == "running" and e.value.message == "в чате уже идёт ход"
    r.start(project, PID, OTHER, "3", None, None)
    with pytest.raises(errors.Refused) as e:
        r.start(project, PID, THIRD, "4", None, None)
    assert e.value.code == "busy" and e.value.message == "достигнут лимит одновременных заданий — дождитесь завершения"
    assert r.live() == {CHAT: False, OTHER: False}
    await r.stop(PID, CHAT)
    await r.stop(PID, OTHER)
    await settle(r)
    assert r.live() == {}


async def test_missing_cli_ends_with_words_and_frees_the_chat(home, project, marked):
    sent, f = [], FakeFactory([], fail=CLINotFoundError("нет"))
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Привет", None, None)
    await settle(r)
    assert [s["e"] for s in sent] == [{"k": "done", "status": "error",
                                       "message": "Claude Code не найден на компьютере", "not_started": True}]
    assert f.clients[0].disconnected and marked == []  # запроса не было — метки нет
    f.fail = None
    f.script = [result_ok()]
    r.start(project, PID, CHAT, "Ещё", None, None)
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_other_failures_end_with_their_words(home, project):
    cases = [
        ([RuntimeError("секрет")], "Claude Code не запустился"),
        ([ResultError("x", {"subtype": "error_max_turns", "errors": ["слишком много ходов"]})], "слишком много ходов"),
        ([result("success", is_error=True, result="API Error: 529")], "API Error: 529"),
        ([result("error_max_budget_usd", is_error=True)], "Claude Code: error_max_budget_usd"),
        ([result("error_during_execution", is_error=True, errors=["о" * 400])], "о" * 299 + "…"),
    ]
    for script, words in cases:
        sent = []
        r = runner(home, FakeFactory(script), sent)
        r.start(project, PID, CHAT, "Привет", None, None)
        await settle(r)
        assert sent[-1]["e"] == {"k": "done", "status": "error", "message": words}


async def test_existing_session_resumes_with_the_remembered_choice_then_the_last_model(home, project, marked,
                                                                                       monkeypatch):
    monkeypatch.setattr(sessions, "exists", lambda p, chat: True)
    monkeypatch.setattr(sessions, "last_model", lambda p, chat: "claude-sonnet-5")
    choices = store.Choices(home)
    choices.put(CHAT, "opus", "high")
    sent, f = [], FakeFactory([result_ok()])
    r = Runner(sent.append, choices=choices, cli_path=None, factory=f)
    r.start(project, PID, CHAT, "1", None, None)
    await settle(r)
    o = f.options[0]
    assert o.resume == CHAT and o.session_id is None and (o.model, o.effort) == ("opus", "high")
    r.start(project, PID, CHAT, "2", None, "low")  # усилие — явно, модель — запомненная
    await settle(r)
    assert (f.options[1].model, f.options[1].effort) == ("opus", "low") and choices.get(CHAT) == ("opus", "low")
    r.start(project, PID, OTHER, "3", None, None)  # выбора нет — модель последнего ответа, усилие по умолчанию
    await settle(r)
    assert (f.options[2].model, f.options[2].effort) == ("claude-sonnet-5", None)
    assert choices.get(OTHER) == (None, None) and marked == []


async def test_new_session_is_marked_studio_after_disconnect(home, project, monkeypatch):
    seen = []
    monkeypatch.setattr(sessions, "mark_studio",
                        lambda p, chat: seen.append((p, chat, f.clients[0].disconnected)))
    sent, f = [], FakeFactory([result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", "sonnet", None)
    await settle(r)
    assert seen == [(project, CHAT, True)]
    assert f.options[0].model == "sonnet" and store.Choices(home).get(CHAT) == ("sonnet", None)


async def test_watch_gives_a_snapshot_with_the_open_card_and_answer_still_works(home, project):
    sent, f = [], FakeFactory([delta("Смотрю"), assistant(tool_use("t1", "Read", {"file_path": "a"})),
                               user(tool_result("t1", [{"type": "text", "text": "строки"}], error=True)),
                               block_start(), delta("Дальше"), ("ask", "Bash", {"command": "ls"}, []),
                               delta("."), result_ok()])
    r = runner(home, f, sent)
    assert await r.watch(PID, CHAT) == (False, [])
    turn = r.start(project, PID, CHAT, "Привет", None, None)
    await until(lambda: r.live().get(CHAT))
    sent.clear()  # канал оборвался: прежние события пропали
    running, frames = await r.watch(PID, CHAT)
    assert running is True and frames == [{"t": "ev", "project": PID, "chat": CHAT, "turn": turn, "e": {
        "k": "snapshot", "text": "Смотрю\n\nДальше",
        "steps": [{"id": "t1", "tool": "Read", "input": {"file_path": "a"}, "text": "строки", "error": True, "status": "failed"}],
        "timeline": [{"kind": "text", "start": 0, "end": 6}, {"kind": "step", "id": "t1"},
                     {"kind": "text", "start": 6, "end": 14}],
        "asks": [{"request_id": "toolu_1", "kind": "permission", "tool": "Bash", "input": {"command": "ls"},
                  "always": False}]}}]
    r.answer(PID, CHAT, "toolu_1", "allow", None, None)
    await settle(r)
    assert kinds(sent) == ["asked", "text", "done"] and sent[-1]["e"]["status"] == "done"


async def test_done_comes_after_the_chat_left_running(home, project):
    seen = []
    r = None

    def emit(frame):
        if frame["e"]["k"] == "done" and not seen:
            seen.append(dict(r.live()))
            r.start(project, PID, CHAT, "снова", None, None)  # не бросает running

    f = FakeFactory([result_ok()])
    r = Runner(emit, choices=store.Choices(home), cli_path=None, factory=f)
    r.start(project, PID, CHAT, "1", None, None)
    while len(f.clients) < 2:
        await settle(r)
    await settle(r)
    assert seen[0] == {}


async def test_a_failing_emit_does_not_leave_the_chat_running(home, project):
    def emit(frame):
        raise ConnectionError("канал")

    f = FakeFactory([delta("x"), ("ask", "Bash", {"command": "ls"}, []), result_ok()])
    r = Runner(emit, choices=store.Choices(home), cli_path=None, factory=f)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: r.live().get(CHAT))
    await r.stop(PID, CHAT)
    await settle(r)
    assert r.live() == {}


async def test_close_stops_every_turn_with_words(home, project):
    sent, f = [], FakeFactory([("ask", "Bash", {"command": "ls"}, []), HOLD])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", None, None)
    r.start(project, PID, OTHER, "2", None, None)
    await until(lambda: r.live() == {CHAT: True, OTHER: True})
    await r.close()
    assert r.live() == {}
    done = [s["e"] for s in sent if s["e"]["k"] == "done"]
    assert done == [{"k": "done", "status": "error", "message": "ход прерван: мост остановлен"}] * 2
    assert all(c.decisions == [PermissionResultDeny(message="Остановлено", interrupt=True)] for c in f.clients)


async def test_each_turn_takes_the_newest_cli_when_the_old_one_is_gone(home, project, tmp_path):
    def ext(version):
        d = tmp_path / "ext" / f"anthropic.claude-code-{version}-win32-x64" / "resources" / "native-binary"
        d.mkdir(parents=True)
        (d / "claude.exe").write_bytes(b"")
        return d / "claude.exe"

    old = ext("2.1.311")
    sent, f = [], FakeFactory([result_ok()])
    r = Runner(sent.append, choices=store.Choices(home), cli_path=old, factory=f,
               find_cli=lambda: cli.find_cli(tmp_path / "ext"))
    r.start(project, PID, CHAT, "1", None, None)
    await settle(r)
    new = ext("2.1.312")
    r.start(project, PID, CHAT, "2", None, None)              # прежний ещё есть — не меняется
    await settle(r)
    old.unlink()                                               # VS Code обновил расширение и удалил прежнюю папку
    r.start(project, PID, CHAT, "3", None, None)
    await settle(r)
    assert [o.cli_path for o in f.options] == [old, old, new] and r.cli_path == new
    assert [s["e"]["status"] for s in sent] == ["done"] * 3


async def test_second_stop_cancels_a_turn_that_ignored_interrupt(home, project):
    sent, f = [], FakeFactory([delta("Думаю"), HOLD, result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Долго", None, None)
    await until(lambda: kinds(sent) == ["text"])

    async def deaf():                                          # interrupt прошёл, а CLI не остановился
        f.clients[0].interrupted = False

    f.clients[0].interrupt = deaf
    await r.stop(PID, CHAT)
    await asyncio.sleep(0.01)
    assert r.live() == {CHAT: False} and kinds(sent) == ["text"]
    await r.stop(PID, CHAT)                                         # повторное «Остановить» — отмена задачи хода
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "stopped"} and r.live() == {}
    assert f.clients[0].disconnected


async def test_a_failing_interrupt_cancels_the_turn_at_once(home, project):
    sent, f = [], FakeFactory([delta("Думаю"), HOLD, result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "Долго", None, None)
    await until(lambda: kinds(sent) == ["text"])

    async def broken():
        raise RuntimeError("interrupt")

    f.clients[0].interrupt = broken
    await r.stop(PID, CHAT)
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "stopped"} and f.clients[0].disconnected


async def test_a_turn_during_close_is_busy(home, project):
    gate = asyncio.Event()
    sent, f = [], FakeFactory([HOLD])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: f.clients and f.clients[0].queries)
    real = f.clients[0].interrupt

    async def slow():
        await gate.wait()
        await real()

    f.clients[0].interrupt = slow
    closing = asyncio.create_task(r.close())
    await asyncio.sleep(0.01)
    with pytest.raises(errors.Refused) as e:
        r.start(project, PID, OTHER, "2", None, None)          # мост закрывает ходы — новый не начинается
    assert (e.value.code, e.value.message) == ("busy", "мост выходит") and len(f.clients) == 1
    gate.set()
    await closing
    assert r.live() == {}
    f.script = [result_ok()]
    r.start(project, PID, OTHER, "3", None, None)              # закрытие кончилось — ходы снова можно
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_after_the_final_close_turns_stay_refused(home, project):
    sent, f = [], FakeFactory([HOLD])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: f.clients and f.clients[0].queries)
    await r.close(final=True)                                  # мост выходит: канал ещё досылает очередь
    assert r.live() == {} and sent[-1]["e"]["message"] == "ход прерван: мост остановлен"
    with pytest.raises(errors.Refused) as e:
        r.start(project, PID, OTHER, "2", None, None)
    assert (e.value.code, e.value.message) == ("busy", "мост выходит") and len(f.clients) == 1
    r.reopen()                                                 # выход моста reopen не отменяет
    with pytest.raises(errors.Refused):
        r.start(project, PID, OTHER, "3", None, None)


async def test_a_held_close_refuses_turns_until_reopen(home, project):
    sent, f = [], FakeFactory([result_ok()])
    r = runner(home, f, sent)
    await r.close(hold=True)                                   # отвязка: просьба turn в пути хода не начнёт
    with pytest.raises(errors.Refused) as e:
        r.start(project, PID, CHAT, "1", None, None)
    assert (e.value.code, e.value.message) == ("busy", "мост выходит") and f.clients == []
    r.reopen()                                                 # новый канал — ходы снова можно
    r.start(project, PID, CHAT, "2", None, None)
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_stop_while_the_turn_finishes_does_not_cancel_it(home, project, marked):
    gate = asyncio.Event()
    sent, f = [], FakeFactory([delta("x"), result_ok()])

    def factory(options):
        client = f(options)
        real = client.disconnect

        async def slow():                                      # disconnect гасит claude.exe — секунды
            await gate.wait()
            await real()

        async def broken():                                    # транспорт уже закрывается
            raise RuntimeError("interrupt")

        client.disconnect, client.interrupt = slow, broken
        return client

    r = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=factory)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: CHAT in r._turns and getattr(r._turns[CHAT], "finishing", False))
    client = f.clients[0]
    await r.stop(PID, CHAT)                                    # первое: interrupt упал
    await r.stop(PID, CHAT)                                    # второе
    await asyncio.sleep(0.01)
    assert not r._turns[CHAT].task.cancelled() and not r._turns[CHAT].task.done()
    gate.set()
    await settle(r)
    assert client.disconnected and marked == [(project, CHAT)]  # claude.exe погашен, метка studio — есть
    assert sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_answer_stop_watch_of_another_project_see_no_turn(home, project):
    sent, f = [], FakeFactory([("ask", "Bash", {"command": "ls"}, []), result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: r.live().get(CHAT))
    other = "0" * 16
    assert await r.watch(other, CHAT) == (False, [])
    with pytest.raises(errors.Refused) as e:
        r.answer(other, CHAT, "toolu_1", "allow", None, None)
    assert e.value.code == "gone"
    with pytest.raises(errors.Refused) as e:
        await r.stop(other, CHAT)
    assert e.value.code == "idle"
    assert r.live() == {CHAT: True} and f.clients[0].decisions == []
    r.answer(PID, CHAT, "toolu_1", "allow", None, None)
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_events_while_the_snapshot_is_made_follow_it(home, project, monkeypatch):
    sent, f = [], FakeFactory([delta("А"), HOLD, result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: kinds(sent) == ["text"])
    entered, release = asyncio.Event(), __import__("threading").Event()
    loop = asyncio.get_running_loop()
    real = sessions.fit

    def fit(items, budget):                                    # в потоке: снимок готовится долго
        loop.call_soon_threadsafe(entered.set)
        release.wait(5)
        return real(items, budget)

    monkeypatch.setattr(sessions, "fit", fit)
    watching = asyncio.create_task(r.watch(PID, CHAT))
    await entered.wait()
    t = r._turns[CHAT]
    t.feed.text += "Б"                                         # ход идёт дальше, пока снимок в потоке
    r._send(t, {"k": "text", "text": "Б"})
    item = {"id": "f", "text": "Уточнение", "mode": "steer", "position": t.feed.position()}
    t.followups.append(item)
    r._send(t, {"k": "followup", **item})
    assert kinds(sent) == ["text"]                             # событие — не в канал раньше снимка
    release.set()
    running, frames = await watching
    assert running and [json.loads(frames[0].text)["e"]["text"], frames[1]["e"]] == ["А", {"k": "text", "text": "Б"}]
    assert "followups" not in frames[0]["e"]
    assert frames[2]["e"] == {"k": "followup", **item}
    assert json.loads(frames[0].text) == frames[0]             # снимок уже закодирован — писатель шлёт строку
    r._send(t, {"k": "text", "text": "В"})
    assert kinds(sent) == ["text", "text"]                     # после снимка — снова прямо в канал
    await r.stop(PID, CHAT)
    await settle(r)


def _ascii_size(e) -> int:
    return len(json.dumps(e, ensure_ascii=True))


def test_huge_step_input_is_clipped_harder_then_hidden():
    feed = events.Feed()
    lines = {f"строка {i}": "ж" * 30_000 for i in range(30)}  # 30 × 20 000 × 6 байт — больше 1 МиБ
    [step] = feed.events(assistant(tool_use("t1", "mcp__x", lines)))
    assert _ascii_size(step) <= limits.EVENT_MAX
    assert step["input"]["строка 0"] == "ж" * 1999 + "…" and feed.steps[0]["input"] == step["input"]
    many = {f"строка {i}": "ж" * 30_000 for i in range(100)}  # и по 2000 знаков больше 1 МиБ — пометка
    [step] = feed.events(assistant(tool_use("t2", "mcp__x", many)))
    assert step["input"] == {"…": "ввод не показан — слишком большой"}
    assert feed.steps[1]["input"] == step["input"]
    [res] = feed.events(user(tool_result("t2", "ж" * 200_000 + "конец")))
    assert len(res["text"]) == limits.CLIP and _ascii_size(res) < limits.EVENT_MAX  # одна строка — CLIP хватает
    ask = events.ask_event("r1", "mcp__x", many, False)
    assert ask["input"] == {"…": "ввод не показан — слишком большой"} and ask["tool"] == "mcp__x"
    small = events.ask_event("r2", "Bash", {"command": "ls"}, True)
    assert small["input"] == {"command": "ls"} and small["always"] is True


async def test_a_card_with_a_huge_input_still_gets_the_whole_input_back(home, project):
    many = {f"строка {i}": "ж" * 30_000 for i in range(100)}
    sent, f = [], FakeFactory([("ask", "mcp__x", many, []), result_ok()])
    r = runner(home, f, sent)
    r.start(project, PID, CHAT, "1", None, None)
    await until(lambda: r.live().get(CHAT))
    assert all(_ascii_size(s) <= limits.EVENT_MAX + 1000 for s in sent)
    running, [snap] = await r.watch(PID, CHAT)
    assert _ascii_size(snap) < limits.EVENT_MAX + 10_000
    r.answer(PID, CHAT, sent[-1]["e"]["request_id"], "allow", None, None)
    await settle(r)
    assert f.clients[0].decisions == [PermissionResultAllow(updated_input=many)]  # CLI получает ввод целиком


def test_feed_falls_back_to_assistant_text_and_skips_subagents():
    feed = events.Feed()
    assert feed.events(assistant(text("Раз"), tool_use("t1", "Bash", {"command": "x" * 30_000}))) == [
        {"k": "text", "text": "Раз"}, {"k": "step", "id": "t1", "tool": "Bash", "input": {"command": "x" * 19_999 + "…"}}]
    assert feed.events(assistant(text("Два"))) == [{"k": "text", "text": "\n\n"}, {"k": "text", "text": "Два"}]
    assert feed.events(delta("под", parent="t1")) == [] and feed.events(assistant(text("п"), parent="t1")) == []
    assert feed.events(user(tool_result("t1", "x"), parent="t1")) == []
    assert feed.events(result_ok()) == []
    assert feed.text == "Раз\n\nДва" and feed.steps[0]["text"] == ""
    assert len(feed.steps[0]["input"]["command"]) == limits.CLIP


def test_feed_with_deltas_ignores_assistant_text():
    feed = events.Feed()
    assert feed.events(block_start()) == []  # текста ещё нет — без разделителя
    assert feed.events(delta("А\n")) == [{"k": "text", "text": "А\n"}]
    assert feed.events(block_start()) == []  # текст уже на \n
    assert feed.events(block_start("tool_use")) == []
    assert feed.events(assistant(text("А\n"))) == []
    assert feed.text == "А\n"
