"""Контракт DeepSeek Harness ACP ↔ мост без CLI, Node, сети и квоты: ответы — из фикстур пробы 2026-11-22."""

import asyncio
import copy
import functools
import json
import logging
import subprocess
from pathlib import Path

import pytest

from studio_agent import acp, deepseek, deepseek_rpc, deepseek_settings, limits, mcp, projects
from studio_agent.errors import Refused
from test_deepseek_settings import dsh, node, parsed
from turn_events import asking

FIXTURES = Path(__file__).parent / "fixtures" / "deepseek"
CHAT = "11111111-2222-4333-8444-555555555555"
SESSION = "85219569-668b-433d-b4bf-853d2acbfa59"
WEB_SESSION = "session-3aa7bb24-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
TOOLS = '["srvllm","stub-tools"]'
INSTALL = deepseek_rpc.Install("npm", ("node.exe", "C:/dsh/lib/bin.js"), {}, "web", Path("C:/dsh"))


def fixture(name):
    return json.loads((FIXTURES / name).read_text("utf-8"))


TURN = [json.loads(line) for line in (FIXTURES / "permission_turn.jsonl").read_text("utf-8").splitlines() if line]
INCOMING = [row["msg"] for row in TURN if row["dir"] == "in" and "method" in row["msg"]]
ASK = next(i for i, msg in enumerate(INCOMING) if msg["method"] == "session/request_permission")
BEFORE, AFTER = INCOMING[:ASK + 1], INCOMING[ASK + 1:]
BUSY = fixture("resume_busy_error.json")["error"]
RESULTS = fixture("prompt_result.json")


class FakeRpc:
    def __init__(self, factory, install, cwd, access, overlay):
        self.factory, self.install, self.cwd, self.access, self.overlay = factory, install, cwd, access, overlay
        self.overlay_rows = json.loads(Path(overlay).read_text("ascii")) if overlay else None
        self.info = factory.info or fixture("initialize.json")
        self.inbox = asyncio.Queue()
        self.closed = False
        self.config = {k: v for k, v in fixture("session_new.json").items() if k != "sessionId"}
        self.response = None

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed = True

    async def request(self, method, params, **kwargs):
        self.factory.calls.append((method, params))
        if method == "session/new":
            # D4: ACP DSH сохраняет сессию сразу — в хранилище из наложения или в `$DSH_HOME/sessions`
            root = next((Path(row["config"]["root"]) for row in self.overlay_rows or []
                         if row.get("id") == "session-persistence-jsonl"), None)
            root = root or (self.factory.dsh / "sessions" if self.factory.dsh else None)
            if root is not None:
                (root / "--demo--" / SESSION).mkdir(parents=True, exist_ok=True)
            return {"sessionId": SESSION, **copy.deepcopy(self.config)}
        if method == "session/set_config_option":
            data = fixture("set_config_option.json")
            for option in data["configOptions"]:
                if option["id"] == params["configId"]:
                    option["currentValue"] = params["value"]
            return data
        if method == "session/close":
            self.factory.closed.append(params["sessionId"])
            return {}
        if method == "session/list":
            stored = self.factory.dsh / "sessions" / "--demo--" if self.factory.dsh else None
            listed = [*self.factory.listed, *(p.name for p in (stored.iterdir() if stored and stored.is_dir() else ())
                                              if p.name not in self.factory.listed)]
            return {"sessions": [*({"sessionId": s, "cwd": str(self.factory.project)} for s in listed),
                                 {"sessionId": "0f0f0f0f-0000-4000-8000-000000000000",
                                  "cwd": str(self.factory.project / "other")}]}
        if method == "session/resume":
            if self.factory.busy:
                raise acp.RpcError.of(method, BUSY)
            return copy.deepcopy(self.config)
        if method == "session/prompt":
            self.factory.prompts.append(params)
            if self.factory.prompt_error is not None:
                raise acp.RpcError.of(method, self.factory.prompt_error)
            self.response = asyncio.get_running_loop().create_future()
            if len(self.factory.prompts) == 1:
                for msg in [*self.factory.extra, *BEFORE]:
                    self.inbox.put_nowait(msg)
            else:  # уточнения из очереди — короткий ответ без карточки
                self.inbox.put_nowait({"method": "session/update", "params": {"sessionId": SESSION, "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Дальше"}}}})
                self.response.set_result(RESULTS["end_turn"])
            return await self.response
        raise AssertionError(method)

    async def send(self, frame):
        self.factory.replies.append(frame)
        if frame.get("method") == "session/cancel":
            self.response.set_result(RESULTS["cancelled"])
        elif "result" in frame and self.response is not None and not self.response.done():
            for msg in AFTER:
                self.inbox.put_nowait(msg)
            self.response.set_result(RESULTS["end_turn"])


class Factory:
    def __init__(self, project):
        self.project = project
        self.calls, self.replies, self.clients, self.closed, self.prompts = [], [], [], [], []
        self.listed = [SESSION]
        self.busy = False
        self.prompt_error = None
        self.dsh = None
        self.info = None
        self.extra = []  # сообщения перед ходом фикстуры

    def __call__(self, install, cwd, *, access=None, overlay=None):
        rpc = FakeRpc(self, install, cwd, access, overlay)
        self.clients.append(rpc)
        return rpc


@pytest.fixture
def runner(home, tmp_path, monkeypatch):
    project = home / "demo"
    project.mkdir()
    dsh_home = dsh(tmp_path)  # профиль web из фикстуры пробы, ключи — sk-test
    monkeypatch.setattr(deepseek.deepseek_settings, "build", functools.partial(deepseek_settings.build, run=node(parsed())))
    monkeypatch.setattr(deepseek.deepseek_rpc, "version", lambda install: "0.2.2-rc.5")
    factory, sent = Factory(project), []
    runner = deepseek.DeepSeekRunner(sent.append, home=home, find_installs=lambda: [INSTALL], factory=factory,
                                     environ={"DSH_HOME": str(dsh_home)})
    runner.catalog = {"version": "test", "models": [], "efforts": [],
                      "defaults": {"model": TOOLS, "effort": None, "access": "default"},
                      "settings": {"profile": "web", "applied": ["llm-pi-ai"], "skipped": []}}
    runner.fake, runner.sent, runner.project, runner.pid = factory, sent, project, projects.pid(project.name)
    runner.dsh = factory.dsh = dsh_home
    return runner


def stored(runner, session=SESSION, **header):
    """Файл сессии фикстуры пробы в `$DSH_HOME/sessions` этого проекта."""
    folder = runner.dsh / "sessions" / "--demo--" / session
    folder.mkdir(parents=True, exist_ok=True)
    lines = (FIXTURES / "session.v4.jsonl").read_text("utf-8").splitlines()
    first = {**json.loads(lines[0]), "id": session, "cwd": str(runner.project), **header}
    path = folder / "session.v4.jsonl"
    path.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n", "utf-8")
    return path


def events(runner, kind):
    return [frame["e"] for frame in runner.sent if frame["e"]["k"] == kind]


def overlays(home):
    return list((home / "deepseek").glob("overlay-*"))


async def test_probe_builds_catalog_from_config_options_and_leaves_no_session(runner, home):
    runner.catalog = None
    assert await runner.probe(runner.project)
    methods = [method for method, _ in runner.fake.calls]
    assert methods[0] == "session/new" and methods[-1] == "session/close" and "session/prompt" not in methods
    assert set(methods[1:-1]) <= {"session/set_config_option"} and runner.fake.closed == [SESSION]
    catalog = runner.catalog
    assert catalog["models"] == [{"id": TOOLS, "label": "Stub tools"}]
    assert catalog["efforts"] == ["high", "off"]                 # из `reasoning_effort`, без «Provider default»
    assert catalog["defaults"] == {"model": TOOLS, "effort": None, "access": "default"}   # «Provider default» — без выбора
    assert catalog["version"] == "0.2.2-rc.5" and catalog["settings"]["profile"] == "web"
    # хранилища пробной сессии — во временном каталоге моста, не в $DSH_HOME; после пробы его нет
    rows = runner.fake.clients[0].overlay_rows
    roots = [Path(row["config"]["root"]) for row in rows if row.get("id") in ("session-persistence-jsonl", "storage-json")]
    assert len(roots) == 2 and all(root.parent.parent == home / "deepseek" for root in roots)
    assert not roots[0].parent.exists() and overlays(home) == [] and not (runner.dsh / "sessions").exists()
    assert not any("mcp-manager" in json.dumps(row) for row in rows)
    assert all(client.closed for client in runner.fake.clients)


async def test_probe_with_unreadable_profile_still_keeps_sessions_out_of_dsh_home(runner, home, monkeypatch):
    """C1: тайм-аут Node и установка без Node для разбора — профиль `unreadable`, а хранилища пробы всё равно
    во временном каталоге; без наложения пробный процесс не запускается вовсе."""
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, deepseek_settings.PARSE_SEC)

    python = deepseek_rpc.Install("python", ("runtime.exe",), {}, "web")
    runner.fake.listed = []
    for install, run in ((INSTALL, timeout), (python, None)):
        runner.find_installs = lambda install=install: [install]
        monkeypatch.setattr(deepseek.deepseek_settings, "build", functools.partial(deepseek_settings.build, run=run))
        runner.catalog = {"version": "test", "models": [], "efforts": []}
        before = await runner.chats(runner.project)
        runner.catalog = None
        assert await runner.probe(runner.project)
        probe = runner.fake.clients[-1]
        assert runner.catalog["settings"]["skipped"] == [{"name": "web", "reason": "unreadable"}]
        roots = {row["id"]: Path(row["config"]["root"]) for row in probe.overlay_rows}
        assert set(roots) == {"session-persistence-jsonl", "storage-json"}
        assert all(root.parent.parent == home / "deepseek" for root in roots.values())
        assert not (runner.dsh / "sessions").exists() and await runner.chats(runner.project) == before
        await runner.close()
    # вторая линия: наложения нет — процесс не запускается
    monkeypatch.setattr(deepseek.deepseek_settings, "build", lambda *args, **kwargs: (None, {}))
    clients = len(runner.fake.clients)
    async with runner._scratch():
        with pytest.raises(Refused, match="unsupported"):
            await runner._connect(runner.project)
    assert len(runner.fake.clients) == clients


async def test_probe_without_install_names_the_reason(runner):
    runner.find_installs = lambda: []
    assert not await runner.probe(runner.project)
    assert (runner.unavailable, runner.unavailable_message) == ("missing", None)
    runner.find_installs = lambda: [deepseek_rpc.Install("npm", ("node", "bin.js"), reason="node")]
    assert not await runner.probe(runner.project)
    assert (runner.unavailable, runner.unavailable_message) == ("node", deepseek.NODE)   # I3: причина для студии
    with pytest.raises(Refused) as error:
        await runner._connect(runner.project)
    assert (error.value.code, error.value.message) == ("unsupported", deepseek.NODE)
    runner.find_installs = lambda: []
    with pytest.raises(Refused, match="unsupported") as error:
        await runner._connect(runner.project)
    assert error.value.message == deepseek.MISSING


async def test_chats_list_external_sessions_as_other_origin(runner, home):
    runner.fake.listed = [SESSION, WEB_SESSION]
    rows = await runner.chats(runner.project)
    assert [row["engine"] for row in rows] == ["deepseek", "deepseek"]
    assert all(row["tag"] is None and limits.CHAT_ID.fullmatch(row["chat"]) for row in rows)
    assert [runner.aliases.target(runner.project, row["chat"]) for row in rows] == [SESSION, WEB_SESSION]
    assert [row["chat"] for row in await runner.chats(runner.project)] == [row["chat"] for row in rows]
    await runner.close()
    assert all(client.closed for client in runner.fake.clients) and overlays(home) == []


async def test_turn_streams_text_step_and_permission_card_then_done(runner, home):
    runner.start(runner.project, runner.pid, CHAT, "Запиши файл вне проекта", None, None)
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    card = runner._turns[CHAT].asks[rid].event
    assert card["tool"] == "write" and card["input"]["sandbox_permissions"] == "danger-full-access"
    assert not card["always"]
    runner.answer(runner.pid, CHAT, rid, "allow", None, None)
    await task
    assert runner.fake.replies[0] == {"id": 0, "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}}}
    assert "Готово, инструмент отработал." in "".join(e["text"] for e in events(runner, "text"))
    steps = [e["id"] for e in events(runner, "step")]
    assert "call_probe_6" in steps and "call_probe_7" in steps
    assert [e["error"] for e in events(runner, "result") if e["id"] == "call_probe_6"] == [True]
    settings = events(runner, "settings")[0]
    assert settings["model"] == TOOLS and settings["access_fixed"] and settings["overlay"]["profile"] == "web"
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}
    assert runner.aliases.target(runner.project, CHAT) == SESSION
    assert runner.choices.details(CHAT)["context"]["used"] == 7342
    prompt = runner.fake.prompts[0]["prompt"]
    assert prompt == [{"type": "text", "text": "Запиши файл вне проекта"}]


async def test_resume_of_busy_session_is_writer_active_and_returns_queue(runner, monkeypatch):
    runner.aliases.put(runner.project, CHAT, SESSION)
    runner.fake.busy = True
    entered, release = asyncio.Event(), asyncio.Event()
    original = runner._mcp

    async def held(rpc, project):
        entered.set()
        await release.wait()
        return await original(rpc, project)

    monkeypatch.setattr(runner, "_mcp", held)
    runner.start(runner.project, runner.pid, CHAT, "Продолжи", None, None)
    t = runner._turns[CHAT]
    await entered.wait()
    t.queued.append(("a" * 16, "уточнение", []))           # уточнение, принятое до отказа, не теряется
    t.followups.append({"id": "a" * 16, "text": "уточнение"})
    release.set()
    await t.task
    done = runner.sent[-1]["e"]
    assert done["status"] == "error" and done["message"] == deepseek.WRITER and done["code"] == "writer_active"
    assert done["unsent"] == [{"id": "a" * 16, "text": "уточнение"}] and done["not_started"]
    assert not runner.fake.prompts
    # настройки занятого чата читаются из файла: просмотр не берёт блокировку записи (I1)
    assert (await runner.state(runner.project, CHAT))["settings"]["access_fixed"]


async def test_state_of_existing_chat_reads_session_file_and_never_resumes(runner):
    """I1: `state` чата с сессией не возобновляет её (`session/resume` взял бы блокировку записи и мог дописать
    файл человека): модель, усилие, контекст и права — из файла сессии, без процесса DSH."""
    path = stored(runner)
    runner.fake.busy = True                                   # чат открыт в DSH Web
    external = (await runner.chats(runner.project))[0]["chat"]
    before, clients = path.read_bytes(), len(runner.fake.clients)
    state = await runner.state(runner.project, external)
    assert "session/resume" not in [method for method, _ in runner.fake.calls]
    assert len(runner.fake.clients) == clients and path.read_bytes() == before
    settings = state["settings"]
    assert (settings["model"], settings["effort"], settings["access"]) == (TOOLS, "high", "workspace-write")
    assert settings["access_fixed"] and settings["overlay"] == runner.catalog["settings"]
    assert (state["context"]["used"], state["context"]["limit"]) == (1212, 131072)
    # незнакомый формат — умолчания каталога
    (path.parent / "session.v9.jsonl").write_text(json.dumps({"type": "session", "version": 9}) + "\n", "utf-8")
    settings = (await runner.state(runner.project, external))["settings"]
    assert (settings["model"], settings["effort"], settings["access"]) == (TOOLS, None, "default")
    assert len(runner.fake.clients) == clients
    # права, которые ответил `state`, ход принимает (Minor-5: пресет из файла, а не «по настройкам DSH»)
    (path.parent / "session.v9.jsonl").unlink()
    runner.fake.busy = False
    runner.start(runner.project, runner.pid, external, "Дальше", None, None, access="workspace-write")
    rid = await asking(runner)
    runner.answer(runner.pid, external, rid, "allow", None, None)
    await runner._turns[external].task
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_chats_take_title_and_times_from_session_files(runner):
    """I4: `session/list` DSH отдаёт только id и cwd — название и время берутся из файлов сессий."""
    runner.fake.listed = [SESSION, WEB_SESSION]
    path = stored(runner)
    rows = {runner.aliases.load()[row["chat"]]["session"]: row for row in await runner.chats(runner.project)}
    assert rows[SESSION]["title"] == "Ответь одним словом: г"
    assert rows[SESSION]["created"] == 1795363689.603
    assert rows[SESSION]["modified"] == path.stat().st_mtime_ns / 1e9
    assert (rows[WEB_SESSION]["title"], rows[WEB_SESSION]["created"], rows[WEB_SESSION]["modified"]) == ("", None, None)
    # файл изменился — название новое; повреждённый файл — строка без сведений, список не падает
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"type": "session/title", "seq": 99, "time": 1, "data": {"title": "Новое"}}) + "\n")
    rows = {runner.aliases.load()[row["chat"]]["session"]: row for row in await runner.chats(runner.project)}
    assert rows[SESSION]["title"] == "Новое"
    path.write_text("мусор\n", "utf-8")
    rows = {runner.aliases.load()[row["chat"]]["session"]: row for row in await runner.chats(runner.project)}
    assert rows[SESSION]["title"] == ""


async def test_access_sets_permission_env_only_for_presets(runner, home, monkeypatch):
    monkeypatch.delenv("DSH_PERMISSION_MODE", raising=False)
    runner.start(runner.project, runner.pid, CHAT, "Начни", None, None, access="read-only")
    rid = await asking(runner)
    runner.answer(runner.pid, CHAT, rid, "deny", None, None)
    await runner._turns[CHAT].task
    first = runner.fake.clients[-1]
    assert first.access == "read-only"
    assert not any(row.get("id") == "permission" for row in first.overlay_rows)   # пресет чата сильнее профиля
    env = deepseek_rpc.Client(INSTALL, runner.project, access="read-only").env
    assert env["DSH_PERMISSION_MODE"] == "read-only"
    assert "DSH_PERMISSION_MODE" not in deepseek_rpc.Client(INSTALL, runner.project, access="default").env
    # у чата с сессией права закреплены при создании (D3: переменная не действует на resume)
    calls = len(runner.fake.calls)
    runner.start(runner.project, runner.pid, CHAT, "Дальше", None, None, access="danger-full-access")
    await runner._turns[CHAT].task
    done = runner.sent[-1]["e"]
    assert done["status"] == "error" and done["message"] == deepseek.FIXED and done["not_started"]
    assert len(runner.fake.calls) == calls
    state = await runner.state(runner.project, CHAT)
    assert state["settings"]["access"] == "read-only" and state["settings"]["access_fixed"]
    assert state["settings"]["overlay"]["profile"] == "web"
    runner.start(runner.project, runner.pid, CHAT, "Дальше", None, None, access="read-only")
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}
    assert any(method == "session/resume" for method, _ in runner.fake.calls)
    # новый чат: предпросмотр без закрепления, пробная сессия — во временном хранилище
    fresh = await runner.state(runner.project, "22222222-2222-4333-8444-555555555555", TOOLS, "off", "workspace-write")
    assert (fresh["settings"]["effort"], fresh["settings"]["access"]) == ("off", "workspace-write")
    assert "access_fixed" not in fresh["settings"] and overlays(home) == []


async def test_history_reads_session_file_and_unsupported_format_gives_notice_and_turn_still_runs(runner):
    runner.aliases.put(runner.project, CHAT, SESSION)
    folder = runner.dsh / "sessions" / "--demo--" / SESSION
    folder.mkdir(parents=True)
    lines = (FIXTURES / "session.v4.jsonl").read_text("utf-8").splitlines()
    header = {**json.loads(lines[0]), "id": SESSION, "cwd": str(runner.project)}
    (folder / "session.v4.jsonl").write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", "utf-8")
    page = await runner.history(runner.project, CHAT, None, 100)
    assert page["engine"] == "deepseek" and page["total"] > 0 and "notice" not in page
    assert page["items"][0]["role"] == "user" and page["items"][0]["images"]
    # DSH обновился: файл новой версии формата — пояснение вместо истории
    (folder / "session.v9.jsonl").write_text(json.dumps({**header, "version": 9}) + "\n", "utf-8")
    assert await runner.history(runner.project, CHAT, None, 100) == {
        "items": [], "start": 0, "total": 0, "engine": "deepseek", "notice": "unsupported_format"}
    runner.start(runner.project, runner.pid, CHAT, "Дальше", None, None)
    rid = await asking(runner)
    runner.answer(runner.pid, CHAT, rid, "allow", None, None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_history_with_other_spelling_of_cwd_is_a_notice_not_gone(runner):
    """Minor-6: сессия есть, а `cwd` заголовка не сводится к проекту — пояснение, а не «нет такого чата»."""
    runner.aliases.put(runner.project, CHAT, SESSION)
    stored(runner, cwd=str(runner.project.parent / "elsewhere"))
    assert await runner.history(runner.project, CHAT, None, 100) == {
        "items": [], "start": 0, "total": 0, "engine": "deepseek", "notice": "unsupported_format"}


async def test_history_too_large_is_refused(runner, monkeypatch):
    runner.aliases.put(runner.project, CHAT, SESSION)

    async def huge(*args, **kwargs):
        raise deepseek.deepseek_history.Unsupported("too_large")

    monkeypatch.setattr(runner, "_history_items", huge)
    with pytest.raises(Refused, match="too_large"):
        await runner.history(runner.project, CHAT, None, 10)


async def test_followup_during_answer_is_queued_until_end(runner):
    runner.start(runner.project, runner.pid, CHAT, "Начни", None, None)
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    result = await runner.steer(runner.pid, CHAT, "И ещё", [])
    assert result["mode"] == "queue" and len(runner.fake.prompts) == 1
    runner.answer(runner.pid, CHAT, rid, "allow", None, None)
    await task
    assert [p["prompt"] for p in runner.fake.prompts[1:]] == [[{"type": "text", "text": "И ещё"}]]
    assert [e["id"] for e in events(runner, "followup_sent")] == [result["id"]]
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}


def chunk(text, message):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": SESSION, "update": {
        "sessionUpdate": "agent_message_chunk", "messageId": message, "content": {"type": "text", "text": text}}}}


async def test_whole_messages_are_separated_like_history(runner):
    """Minor-9: DSH присылает сообщения целиком — между ними пустая строка, как в `deepseek_history`."""
    runner.fake.extra = [chunk("Call MCP echo.", "m1"), chunk("Tool returned.", "m2")]
    runner.start(runner.project, runner.pid, CHAT, "Начни", None, None)
    rid = await asking(runner)
    runner.answer(runner.pid, CHAT, rid, "allow", None, None)
    await runner._turns[CHAT].task
    text = "".join(e["text"] for e in events(runner, "text"))
    assert text.startswith("Call MCP echo.\n\nTool returned.\n\nГотово")


async def test_first_turn_refusals_come_before_session_new(runner):
    """Minor-7: картинка при `promptCapabilities.image: false` и модель вне каталога — отказ до `session/new`:
    пустую сессию ACP DSH сохранил бы сразу (D4), а удалить её нечем."""
    info = fixture("initialize.json")
    info["agentCapabilities"]["promptCapabilities"]["image"] = False
    runner.fake.info = info
    runner.start(runner.project, runner.pid, CHAT, "Что на картинке?", None, None,
                 images=[{"mime": "image/png", "data": "cG5n"}])
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"]["message"] == deepseek.IMAGES
    runner.fake.info = None
    runner.start(runner.project, runner.pid, CHAT, "Привет", '["srvllm","gone"]', None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"]["message"] == deepseek.UNAVAILABLE
    assert "session/new" not in [method for method, _ in runner.fake.calls] and not runner.fake.prompts
    assert runner.aliases.target(runner.project, CHAT) is None


def test_session_gone_is_recognized_in_dsh_details():
    """Minor-8: DSH кладёт подробности в `data.details` при `message: "Internal error"`."""
    error = acp.RpcError.of("session/resume", {"code": -32603, "message": "Internal error",
                                               "data": {"details": f"session \"{SESSION}\" not found"}})
    assert error.not_found and not error.owned
    assert not acp.RpcError.of("session/resume", {"code": -32603, "message": "Internal error"}).not_found


async def test_image_route_refusal_names_the_model(runner):
    runner.fake.prompt_error = RESULTS["image_refused_error"]
    runner.start(runner.project, runner.pid, CHAT, "Что на картинке?", None, None,
                 images=[{"mime": "image/png", "data": "cG5n"}])
    await runner._turns[CHAT].task
    prompt = runner.fake.prompts[0]["prompt"]
    assert prompt[1] == {"type": "image", "mimeType": "image/png", "data": "cG5n"}
    assert runner.sent[-1]["e"]["message"] == deepseek.IMAGES


async def test_mcp_servers_passed_on_new_and_resume(runner, home):
    config = {"mcpServers": {"tools": {"command": "python", "args": ["-m", "example"], "env": {"KEY": "private"}},
                             "remote": {"type": "http", "url": "https://mcp.example.com/mcp"}}}
    mcp.save(home, runner.project, "deepseek", config)
    for _ in range(2):
        runner.start(runner.project, runner.pid, CHAT, "Привет", None, None)
        rid = await asking(runner)
        runner.answer(runner.pid, CHAT, rid, "allow", None, None)
        await runner._turns[CHAT].task
        runner.fake.prompts.clear()
    starts = [(method, params) for method, params in runner.fake.calls if method in ("session/new", "session/resume")]
    assert [method for method, _ in starts] == ["session/new", "session/resume"]
    assert all(params["mcpServers"] == mcp.deepseek(config, fixture("initialize.json")) for _, params in starts)
    assert starts[1][1]["sessionId"] == SESSION and starts[1][1]["cwd"] == str(runner.project)


async def test_overlay_removed_after_disconnect(runner, home):
    runner.start(runner.project, runner.pid, CHAT, "Привет", None, None)
    rid = await asking(runner)
    turn = runner.fake.clients[-1]
    assert turn.overlay is not None and Path(turn.overlay).exists()
    assert any(row.get("id") == "llm-pi-ai" for row in turn.overlay_rows)
    runner.answer(runner.pid, CHAT, rid, "allow", None, None)
    await runner._turns[CHAT].task
    assert turn.closed and not Path(turn.overlay).exists() and overlays(home) == []
    await runner.chats(runner.project)                        # процесс списка — без наложения: ни MCP, ни провайдеров
    listing = runner.fake.clients[-1]
    assert listing.overlay is None and not listing.closed and overlays(home) == []
    await runner.close()
    assert listing.closed and overlays(home) == []


async def test_journal_has_no_texts_or_paths(runner, caplog):
    caplog.set_level(logging.DEBUG, logger="studio_agent")
    runner.start(runner.project, runner.pid, CHAT, "СЕКРЕТНЫЙ ЗАПРОС", None, None)
    rid = await asking(runner)
    runner.answer(runner.pid, CHAT, rid, "allow", None, None)
    await runner._turns[CHAT].task
    runner.fake.prompt_error = {"code": -32603, "message": "Internal error", "data": {"details": str(runner.project)}}
    runner.start(runner.project, runner.pid, CHAT, "СЕКРЕТНЫЙ ЗАПРОС", None, None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"]["message"] == deepseek.FAILED
    runner.fake.busy = True
    runner.start(runner.project, runner.pid, CHAT, "СЕКРЕТНЫЙ ЗАПРОС", None, None)
    await runner._turns[CHAT].task
    assert caplog.records
    for secret in ("СЕКРЕТНЫЙ", str(runner.project), "demo", "sk-test", "probe-outside", str(runner.dsh)):
        assert secret not in caplog.text
