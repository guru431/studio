"""Controller без Tk и трея: канал, привязка, настройка, PIN, выход — с подставными Link, привязкой и автозапуском."""

import asyncio
import http.client
import pathlib
import threading
import time

import pytest
from claude_agent_sdk import PermissionResultDeny

from fake_sdk import FakeFactory, result_ok
from studio_agent import app, autostart, enroll, projects, sessions, store, turns
from studio_agent.pin import PinStore

MODELS = [{"id": "opus", "label": "Opus"}]


WELCOME = {"t": "welcome", "agent_id": 3, "name": "Рабочий"}


class FakeLink:
    """Канал без сети: `welcome` и `online` до `stop()`; `result` — чем кончится `run()` сам (`revoked`,
    `replaced`); `order` — список, куда пишется `stop`."""

    def __init__(self, url, key, agent, on_state, result=None, on_welcome=None, welcome=WELCOME, order=None):
        self.url, self.key, self.on_state, self.result = url, key, on_state, result
        self.on_welcome, self.welcome, self.order = on_welcome, welcome, order
        self.hello = agent.hello()
        self.stopped = False
        self.ending = None
        self._stop = asyncio.Event()

    async def run(self):
        if self.result is not None:
            self.on_state(self.result)
            return self.result
        self.on_state("connecting")
        if self.on_welcome is not None:
            self.on_welcome(self.welcome)
        self.on_state("online")
        await self._stop.wait()
        state = self.ending or "stopped"
        self.on_state(state)
        return state

    async def stop(self):
        if self.order is not None:
            self.order.append("stop")
        self.stopped = True
        self._stop.set()

    def end(self, state):
        """Студия закрыла канал (`revoked`, `replaced`); звать в цикле ядра."""
        self.ending = state
        self._stop.set()


class FakeAutostart:
    def __init__(self):
        self.calls = []

    def set_enabled(self, on):
        self.calls.append(on)

    def enabled(self):
        return bool(self.calls and self.calls[-1])


async def forever(sec):
    await asyncio.Event().wait()


def until(check, sec: float = 5):
    deadline = time.monotonic() + sec
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("не дождались")
        time.sleep(0.002)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "projects"
    r.mkdir()
    return r


@pytest.fixture
def configured(home, root):
    store.save_key(home, "k0")
    store.save_config(home, store.Config(agent_id=3, name="Рабочий", projects_root=str(root)))


@pytest.fixture
def make(home):
    made = []

    def build(result=None, probe=None, enroll_calls=None, sleep=forever, find_cli=lambda: None,
              cli_version=lambda p: "2.3.1", sdk=None, unenroll=lambda url, key: (204, True), welcomes=None,
              order=None, codex_factory=None, opencode_factory=None):
        links, probes, probe_paths, unenrolls = [], [], [], []

        def link_factory(url, key, agent, on_state, on_welcome=None):
            welcome = welcomes[min(len(links), len(welcomes) - 1)] if welcomes else WELCOME
            links.append(FakeLink(url, key, agent, on_state, result, on_welcome, welcome, order))
            return links[-1]

        def fake_unenroll(url, key):
            unenrolls.append((url, key))
            if order is not None:
                order.append("studio")
            return unenroll(url, key)

        async def fake_probe(root, cli_path, factory=None):
            probes.append((root, len(links)))
            probe_paths.append(cli_path)
            return (MODELS, ["low", "high"]) if probe is None else probe(len(probes))

        def fake_enroll(url, login, password, name):
            enroll_calls.append((url, login, name)) if enroll_calls is not None else None
            return 7, "k", login.strip().lower()

        extra = {"sdk_factory": sdk} if sdk is not None else {}
        ctrl = app.Controller(home, link_factory=link_factory, find_cli=find_cli, cli_version=cli_version,
                              probe=fake_probe, enroll_fn=fake_enroll, unenroll_fn=fake_unenroll,
                              autostart=FakeAutostart(), sleep=sleep, codex_factory=codex_factory,
                              opencode_factory=opencode_factory, **extra)
        ctrl.links, ctrl.probes, ctrl.probe_paths, ctrl.unenrolls = links, probes, probe_paths, unenrolls
        made.append(ctrl)
        return ctrl

    yield build
    for ctrl in made:
        ctrl.quit()


def form(root, **kw):
    return {"url": "https://studio.example.com", "login": "", "password": "", "name": "Рабочий",
            "projects_root": str(root), "pin": "", "pin2": "", "no_pin": False, "autostart": True, **kw}


def test_not_configured_does_not_link(make):
    ctrl = make()
    assert ctrl.state == "setup" and not ctrl.enrolled
    ctrl.start()
    ctrl.restart_link().result(5)
    assert ctrl.state == "setup"
    assert ctrl.links == [] and ctrl.probes == []


def test_models_are_probed_before_the_first_link(make, configured, root):
    ctrl = make()
    states = []
    ctrl.on_state(states.append)
    assert ctrl.state == "connecting"
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.probes == [(str(root), 0)]
    lk = ctrl.links[0]
    assert (lk.url, lk.key) == ("https://studio.example.com", "k0")
    assert lk.hello["models"] == MODELS and lk.hello["efforts"] == ["low", "high"]
    assert lk.hello["cc_version"] == "2.3.1" and lk.hello["projects_root"] == str(root)
    assert states[-1] == "online"


def test_models_are_probed_again_every_hour(make, configured):
    gate, pauses = asyncio.Event(), []

    async def sleep(sec):
        pauses.append(sec)
        await gate.wait()
        gate.clear()

    found = {1: ["m1"], 2: ["m1"], 3: ["m1", "m3"], 4: []}

    def probe(n):
        return [{"id": m, "label": "M"} for m in found[n]], ["low"] if found[n] else []

    ctrl = make(sleep=sleep, probe=probe)
    ctrl.start()
    until(lambda: ctrl.state == "online" and pauses)
    assert pauses == [app.PROBE_EVERY_SEC]
    ctrl._loop.call_soon_threadsafe(gate.set)
    until(lambda: len(ctrl.probes) == 2 and len(pauses) == 2)
    assert len(ctrl.links) == 1                                 # набор тот же — канал прежний
    ctrl._loop.call_soon_threadsafe(gate.set)
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online" and len(pauses) == 3)
    assert ctrl.links[0].stopped                                # набор сменился — новый hello с новыми моделями
    assert [m["id"] for m in ctrl.links[1].hello["models"]] == ["m1", "m3"]
    ctrl._loop.call_soon_threadsafe(gate.set)
    until(lambda: len(ctrl.probes) == 4 and len(pauses) == 4)
    assert len(ctrl.links) == 2 and [m["id"] for m in ctrl.agent.models] == ["m1", "m3"]  # сбой пробы — без канала


def test_catalogs_are_in_hello_and_revocation_stops_all_runners(make, configured, home):
    class Backend:
        def __init__(self, emit, home, choices):
            self.catalog = None
            self.choices = choices
            self.closes = []
            self.reopened = 0

        async def probe(self, root):
            self.catalog = {"version": "test", "models": [{"id": "codex-test", "label": "Codex"}], "efforts": ["ultra"]}
            return True

        async def close(self, **kwargs):
            self.closes.append(kwargs)

        def reopen(self):
            self.reopened += 1

    ctrl = make(codex_factory=Backend, opencode_factory=Backend)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.links[0].hello["codex"]["efforts"] == ["ultra"]
    assert ctrl.links[0].hello["opencode"]["efforts"] == ["ultra"]
    assert ctrl.codex.choices is ctrl.runner.choices
    assert ctrl.opencode.choices.path == home / "opencode" / store.CHOICES
    ctrl.opencode.choices.update("test-chat", access="default")
    assert ctrl.opencode.choices.details("test-chat")["access"] == "default"
    assert ctrl.runner.choices.details("test-chat") == {}
    assert ctrl.codex.reopened == ctrl.opencode.reopened == 1
    ctrl._loop.call_soon_threadsafe(ctrl.links[0].end, "revoked")
    until(lambda: ctrl.codex.closes and ctrl.opencode.closes)
    assert ctrl.codex.closes == ctrl.opencode.closes == [{"hold": True}] and ctrl.runner._closing
    ctrl.quit()
    assert ctrl.codex.closes[-1] == ctrl.opencode.closes[-1] == {"final": True}


@pytest.mark.parametrize("engine", ("codex", "opencode"))
def test_backend_statuses_are_independent_and_do_not_reuse_a_stale_success(make, configured, engine):
    class Backend:
        def __init__(self, *args, **kwargs):
            self.catalog = None
            self.ready = True

        async def probe(self, root):
            if self.ready:
                self.catalog = {"models": [{"id": "codex-test"}], "efforts": []}
            return self.ready

        async def close(self, **kwargs):
            pass

        def reopen(self):
            pass

    ctrl = make(probe=lambda n: ([], []) if n == 1 else (MODELS, ["high"]), **{engine + "_factory": Backend})
    changes = []
    ctrl.on_backends(changes.append)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    other = "opencode" if engine == "codex" else "codex"
    assert ctrl.backend_status() == {"claude": {"status": "unavailable", "models": 0},
                                     engine: {"status": "ready", "models": 1},
                                     other: {"status": "unavailable", "models": 0}}
    backend = getattr(ctrl, engine)
    backend.ready = False
    asyncio.run_coroutine_threadsafe(ctrl._refresh_models(), ctrl._loop).result(5)
    assert backend.catalog is not None
    assert ctrl.backend_status() == {"claude": {"status": "ready", "models": 1},
                                     engine: {"status": "unavailable", "models": 0},
                                     other: {"status": "unavailable", "models": 0}}
    assert changes[-1] == ctrl.backend_status()
    changes[-1][engine]["status"] = "ready"
    assert ctrl.backend_status()[engine]["status"] == "unavailable"


def test_theme_is_saved_without_restarting_the_link(make, configured, home, root):
    ctrl = make()
    ctrl.start()
    until(lambda: ctrl.state == "online")
    link = ctrl.links[0]
    ctrl.set_theme("dark")
    ctrl.set_theme("light")
    cfg = store.load_config(home)
    assert cfg.theme == "light" and cfg.projects_root == str(root) and cfg.agent_id == 3
    assert ctrl.links == [link] and not link.stopped and store.load_key(home) == "k0"
    with pytest.raises(ValueError, match="theme"):
        ctrl.set_theme("invalid")
    assert store.load_config(home).theme == "light"


def test_models_changed_while_revoked_do_not_bring_the_link_back(make, configured):
    gate, pauses = asyncio.Event(), []

    async def sleep(sec):
        pauses.append(sec)
        await gate.wait()
        gate.clear()

    ctrl = make(result="revoked", sleep=sleep, probe=lambda n: ([{"id": f"m{n}", "label": "M"}], ["low"]))
    ctrl.start()
    until(lambda: ctrl.state == "revoked" and pauses)
    ctrl._loop.call_soon_threadsafe(gate.set)
    until(lambda: len(ctrl.probes) == 2 and len(pauses) == 2)
    assert len(ctrl.links) == 1 and ctrl.state == "revoked"


def test_setup_errors_change_nothing(make, root):
    calls = []
    ctrl = make(enroll_calls=calls)
    ctrl.start()
    errors = ctrl.apply_setup(form(root, login="owner", password="x", pin="12", pin2="12", autostart=False))
    assert errors == ["PIN — от 4 до 8 цифр"]
    assert calls == [] and ctrl._autostart.calls == []
    ctrl.restart_link().result(5)
    assert ctrl.links == [] and ctrl.state == "setup"


def test_setup_enrolls_sets_autostart_and_links(make, home, root):
    calls = []
    ctrl = make(enroll_calls=calls)
    ctrl.start()
    assert ctrl.apply_setup(form(root, login="owner", password="пароль", autostart=False)) == []
    assert calls == [("https://studio.example.com", "owner", "Рабочий")]
    assert ctrl._autostart.calls == [False]
    until(lambda: ctrl.state == "online")
    assert ctrl.links[0].key == "k" and ctrl.enrolled
    assert store.load_config(home).agent_id == 7


def test_enrolled_but_new_address_needs_login(make, configured, root):
    ctrl = make()
    assert ctrl.apply_setup(form(root, url="https://other.example")) == ["Неверный логин или пароль"]


def test_pin_change_restarts_link_with_new_hello(make, configured, root):
    ctrl = make()
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.links[0].hello["has_pin"] is False
    assert ctrl.apply_setup(form(root, pin="2468", pin2="2468")) == []
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")
    assert ctrl.links[0].stopped
    assert ctrl.links[1].hello["has_pin"] is True
    assert len(ctrl.probes) == 1  # модели уже есть


@pytest.mark.parametrize("result", ["revoked", "replaced"])
def test_revoked_or_replaced_link_stays_down(make, configured, result):
    ctrl = make(result=result)
    ctrl.start()
    until(lambda: ctrl.state == result)
    time.sleep(0.05)
    assert len(ctrl.links) == 1 and ctrl.state == result


def test_a_vanished_cli_is_found_again_and_its_version_goes_to_the_next_hello(make, configured, tmp_path):
    old, new = tmp_path / "2.1.311.exe", tmp_path / "2.1.312.exe"
    old.write_bytes(b"")
    found = [old]
    ctrl = make(find_cli=lambda: found[-1], cli_version=lambda p: p.stem if p is not None else "встроенный")
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.links[0].hello["cc_version"] == "2.1.311" and ctrl.probe_paths == [old]
    old.unlink()                                                # VS Code обновил расширение
    new.write_bytes(b"")
    found.append(new)
    ctrl.restart_link().result(5)
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")
    assert ctrl.links[1].hello["cc_version"] == "2.1.312" and ctrl.runner.cli_path == new


@pytest.mark.parametrize("result", ["revoked", "replaced"])
def test_revoked_or_replaced_link_stops_running_turns(make, configured, root, monkeypatch, result):
    monkeypatch.setattr(sessions, "exists", lambda project, chat: False)
    monkeypatch.setattr(sessions, "last_model", lambda project, chat: None)
    monkeypatch.setattr(sessions, "mark_studio", lambda project, chat: None)
    (root / "demo").mkdir()
    chat, session = "0b1e2c3d-4f50-4617-8899-aabbccddeeff", "3b1e" * 8
    sdk = FakeFactory([("ask", "Bash", {"command": "ls"}, []), result_ok()])
    ctrl = make(sdk=sdk)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    sent = []
    ctrl.agent.send = sent.append
    ctrl._loop.call_soon_threadsafe(ctrl.agent.open.open, session)
    ctrl._loop.call_soon_threadsafe(ctrl.runner.start, root / "demo", projects.pid("demo"), chat, "x", None, None)
    until(lambda: dict(ctrl.runner.live()).get(chat))           # карточка открыта
    ctrl._loop.call_soon_threadsafe(ctrl.links[0].end, result)
    until(lambda: ctrl.state == result and not ctrl.runner._turns)
    client = sdk.clients[0]
    assert client.decisions == [PermissionResultDeny(message="Остановлено", interrupt=True)] and client.disconnected
    until(lambda: sent and sent[-1]["e"]["k"] == "done")
    assert sent[-1]["e"] == {"k": "done", "status": "error", "message": turns.BRIDGE_STOPPED}
    until(lambda: not ctrl.agent.open._seen)                    # открытые сессии прочь
    assert len(ctrl.links) == 1


def test_a_broken_config_starts_as_not_configured(make, home, caplog):
    store.save_key(home, "k0")
    (home / "config.json").write_text("{битый", encoding="utf-8")
    ctrl = make()
    assert ctrl.state == "setup"                                # окно настройки, а не тихий незапуск
    ctrl.start()
    ctrl.restart_link().result(5)
    assert ctrl.state == "setup" and ctrl.links == []
    assert (home / "config.json").read_text(encoding="utf-8") == "{битый"
    assert "config.json не читается: JSONDecodeError" in caplog.text


def test_reset_pin_unlocks(make, home, configured):
    pins = PinStore(home)
    pins.set_pin("2468")
    for _ in range(5):
        pins.check("0000")
    ctrl = make()
    assert ctrl.pin_locked
    ctrl.reset_pin()
    assert not ctrl.pin_locked


def test_quit_closes_runner_and_link(make, configured):
    ctrl = make()
    ctrl.start()
    until(lambda: ctrl.state == "online")
    closed = []
    real_close = ctrl.runner.close

    async def close(**kw):
        closed.append(kw)
        await real_close(**kw)

    ctrl.runner.close = close
    ctrl.quit()
    assert closed == [{"final": True}]                          # ходы после выхода не начинаются
    assert ctrl.links[0].stopped
    assert not ctrl._thread.is_alive()


async def test_quit_waits_for_welcome_write(make, monkeypatch):
    """Запись welcome держится до выхода ядра; паузу потока задают события, а не часы."""
    ctrl = make()
    ctrl._built, ctrl._quit = asyncio.Event(), asyncio.Event()
    ctrl._relink_lock = asyncio.Lock()
    started, release, dropped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_thread, real_drop = asyncio.to_thread, ctrl._drop_link

    async def to_thread(fn, *args, **kw):
        if fn == ctrl._remember:
            started.set()
            await release.wait()
        return await real_thread(fn, *args, **kw)

    async def drop():
        await real_drop()
        if ctrl._quit.is_set():
            dropped.set()

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    monkeypatch.setattr(ctrl, "_drop_link", drop)
    main = asyncio.create_task(ctrl._main())
    try:
        await ctrl._built.wait()
        await ctrl._relink_task()
        ctrl._on_welcome(ctrl._gen, {"login": "owner"})
        await started.wait()
        ctrl._quit.set()
        await dropped.wait()
        assert not main.done(), "ядро вышло, пока запись welcome ещё ждёт"
        release.set()
        await main
        assert not ctrl._background
    finally:
        ctrl._quit.set()
        release.set()
        await main
        await asyncio.gather(*ctrl._background)


def test_autostart_command_runs_the_pyw_of_this_repo():
    repo = autostart.REPO
    assert autostart.command() == rf'"{repo}\.venv\Scripts\pythonw.exe" "{repo}\studio_agent.pyw"'
    assert (repo / "studio_agent.pyw").is_file()



@pytest.fixture
def enrolled(home, configured):
    """Привязан: ключ, PIN, логин в config.json, автозапуск выключен."""
    PinStore(home).set_pin("2468")
    cfg = store.load_config(home)
    cfg.login, cfg.autostart = "owner", False
    store.save_config(home, cfg)


def forgotten(home, root):
    """Ключ, PIN, id и логин стёрты; адрес, имя, папка и автозапуск — прежние."""
    assert not (home / "key.bin").exists() and not (home / "pin.bin").exists()
    assert store.load_config(home) == store.Config(agent_id=None, name="Рабочий", projects_root=str(root),
                                                   autostart=False, login="")


def test_unenroll_asks_studio_and_forgets(make, home, root, enrolled):
    ctrl = make()
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.unenroll() == (True, "")
    assert ctrl.unenrolls == [("https://studio.example.com", "k0")]
    forgotten(home, root)
    assert ctrl.state == "setup" and ctrl.links[0].stopped and not ctrl.enrolled
    ctrl.restart_link().result(5)                               # канал без ключа не поднимается
    assert len(ctrl.links) == 1 and ctrl.state == "setup"
    assert ctrl.links[0].hello["has_pin"] is True and ctrl.agent.has_pin is False


@pytest.mark.parametrize("answer", [405, 403, 401, 500, "unavailable", "broken"])
def test_unenroll_without_studio_still_forgets(make, home, root, enrolled, answer):
    def unenroll(url, key):
        if answer == "unavailable":
            raise enroll.EnrollError("unavailable", "Студия недоступна (URLError)")
        if answer == "broken":  # не обёрнутое urllib — и такое не роняет отвязку
            raise http.client.IncompleteRead(b"")
        return answer, False

    ctrl = make(unenroll=unenroll)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    ok, words = ctrl.unenroll()
    assert ok and words == ("Студия недоступна — компьютер остался в её списке: отзовите его в «Настройки» → "
                            "«Компьютеры»")
    forgotten(home, root)
    assert ctrl.state == "setup"


def test_unenroll_401_counts_as_done(make, home, root, enrolled):
    ctrl = make(unenroll=lambda url, key: (401, True))
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.unenroll() == (True, "")
    forgotten(home, root)


def test_unenroll_stops_turns_and_link_first(make, home, root, enrolled, monkeypatch):
    monkeypatch.setattr(sessions, "exists", lambda project, chat: False)
    monkeypatch.setattr(sessions, "last_model", lambda project, chat: None)
    monkeypatch.setattr(sessions, "mark_studio", lambda project, chat: None)
    (root / "demo").mkdir()
    chat = "0b1e2c3d-4f50-4617-8899-aabbccddeeff"
    order = []
    sdk = FakeFactory([("ask", "Bash", {"command": "ls"}, []), result_ok()])
    ctrl = make(sdk=sdk, order=order)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    sent = []

    def send(frame):
        sent.append(frame)
        if frame.get("e", {}).get("k") == "done":
            order.append("done")

    ctrl.agent.send = send
    ctrl._loop.call_soon_threadsafe(ctrl.runner.start, root / "demo", projects.pid("demo"), chat, "x", None, None)
    until(lambda: dict(ctrl.runner.live()).get(chat))           # карточка открыта
    real_close = ctrl.runner.close

    async def close(**kw):
        order.append("close")
        await real_close(**kw)

    ctrl.runner.close = close
    assert ctrl.unenroll() == (True, "")
    assert order == ["close", "done", "stop", "studio"]         # ход прерван до закрытия канала, запрос — после
    assert sent[-1]["e"] == {"k": "done", "status": "error", "message": turns.BRIDGE_STOPPED}
    assert sdk.clients[0].disconnected
    time.sleep(0.05)
    assert len(ctrl.links) == 1 and ctrl.state == "setup"


def test_a_turn_already_on_its_way_does_not_start_after_unenroll(make, home, root, enrolled, monkeypatch):
    """Просьба `turn`, чей `find_project` ещё идёт, пока идёт отвязка: ход не начат, `claude.exe` не поднят."""
    (root / "demo").mkdir()
    entered, release = threading.Event(), threading.Event()
    real = projects.find_project

    def slow(projects_root, pid):
        entered.set()
        release.wait(5)
        return real(projects_root, pid)

    monkeypatch.setattr(projects, "find_project", slow)
    sdk = FakeFactory([result_ok()])
    ctrl = make(sdk=sdk)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    session = "3b1e" * 8
    ctrl._loop.call_soon_threadsafe(ctrl.agent.open.open, session)
    frame = {"t": "req", "id": "0123456789abcdef", "op": "turn", "session": session, "project": projects.pid("demo"),
             "chat": "0b1e2c3d-4f50-4617-8899-aabbccddeeff", "text": "x", "model": None, "effort": None}
    answer = asyncio.run_coroutine_threadsafe(ctrl.agent.handle(frame), ctrl._loop)
    assert entered.wait(5)
    ctrl.unenroll()
    release.set()
    res = answer.result(5)[0]
    assert (res["ok"], res["code"], res["message"]) == (False, "busy", turns.CLOSING)
    assert sdk.clients == [] and ctrl.runner.live() == {} and ctrl.state == "setup"


@pytest.mark.parametrize("result", ["revoked", "replaced"])
def test_turns_stay_refused_after_revoked_until_a_new_link(make, configured, root, result):
    ctrl = make(result=result)
    ctrl.start()
    until(lambda: ctrl.state == result and ctrl.runner._closing)
    with pytest.raises(Exception) as e:
        asyncio.run_coroutine_threadsafe(_start(ctrl, root), ctrl._loop).result(5)
    assert getattr(e.value, "code", None) == "busy"


async def _start(ctrl, root):
    (root / "demo").mkdir(exist_ok=True)
    ctrl.runner.start(root / "demo", projects.pid("demo"), "0b1e2c3d-4f50-4617-8899-aabbccddeeff", "x", None, None)


def erase_pauses():
    """Паузы повторов стирания — без настоящего времени; часовая проба — навсегда."""
    pauses = []

    async def sleep(sec):
        if sec == app.PROBE_EVERY_SEC:
            await forever(sec)
        pauses.append(sec)

    return pauses, sleep


def busy(monkeypatch, name, times):
    """Файл `name` первые `times` раз не удаляется: WinError 32, его читает другой поток."""
    left, real = [times], pathlib.Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == name and left[0]:
            left[0] -= 1
            raise PermissionError(13, "файл занят другим процессом")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)


def test_unenroll_retries_a_busy_file(make, home, root, enrolled, monkeypatch):
    pauses, sleep = erase_pauses()
    busy(monkeypatch, "pin.bin", 3)
    ctrl = make(sleep=sleep)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.unenroll() == (True, "")
    assert pauses == [app.ERASE_PAUSE_SEC] * 3
    forgotten(home, root)
    assert ctrl.state == "setup"


def test_unenroll_that_cannot_erase_the_key_brings_the_link_back(make, home, root, enrolled, monkeypatch, caplog):
    pauses, sleep = erase_pauses()
    busy(monkeypatch, "key.bin", 99)
    ctrl = make(sleep=sleep)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.unenroll() == (False, "Не удалось стереть ключ компьютера — закройте окно моста и нажмите "
                                      "«Отключиться» снова")
    assert pauses == [app.ERASE_PAUSE_SEC] * 9
    assert "отвязка: ключ не стёрт: PermissionError" in caplog.text
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")   # не «на связи» без канала — канал заново
    assert ctrl.links[1].key == "k0" and PinStore(home).has_pin
    monkeypatch.undo()
    assert ctrl.unenroll() == (True, "")                              # «Отключиться» снова
    forgotten(home, root)


def test_unenroll_that_erased_the_key_but_not_the_pin_is_not_linked(make, home, root, enrolled, monkeypatch):
    pauses, sleep = erase_pauses()
    busy(monkeypatch, "pin.bin", 99)
    ctrl = make(sleep=sleep)
    ctrl.start()
    until(lambda: ctrl.state == "online")
    ok, words = ctrl.unenroll()
    assert not ok and not (home / "key.bin").exists() and PinStore(home).has_pin   # PIN остался — сторона безопасная
    assert words == ("Компьютер отключён, но прежний PIN стереть не удалось — после новой привязки смените или "
                     "уберите его в окне моста")                     # вид «Вход»: кнопки «Отключиться» там нет
    until(lambda: ctrl.state == "setup")
    assert len(ctrl.links) == 1


def test_reenroll_after_revoked_skips_studio(make, home, root, enrolled):
    ctrl = make(result="revoked")
    ctrl.start()
    until(lambda: ctrl.state == "revoked")
    assert ctrl.unenroll(ask_studio=False) == (True, "")
    assert ctrl.unenrolls == []
    forgotten(home, root)
    assert ctrl.state == "setup"


def test_unenroll_before_start_forgets_too(make, home, root, enrolled):
    ctrl = make()
    assert ctrl.unenroll() == (True, "")
    forgotten(home, root)
    assert ctrl.state == "setup"


def test_a_new_login_from_welcome_reaches_subscribers_after_it_is_saved(make, home, root):
    welcomes = [{"t": "welcome", "agent_id": 7, "name": "Рабочий", "login": "ivanov"},
                {"t": "welcome", "agent_id": 7, "name": "Рабочий"}]
    ctrl = make(welcomes=welcomes)
    logins = []
    ctrl.on_login(lambda login: logins.append((login, ctrl.login)))
    ctrl.start()
    assert ctrl.apply_setup(form(root, login=" Owner ", password="x")) == []
    until(lambda: logins)
    assert logins == [("ivanov", "ivanov")]                    # окно и трей прочтут уже записанный
    ctrl.restart_link().result(5)
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")
    time.sleep(0.05)
    assert logins == [("ivanov", "ivanov")]                    # логин тот же — события нет


def test_login_from_enroll_and_welcome(make, home, root):
    welcomes = [{"t": "welcome", "agent_id": 7, "name": "Рабочий ПК", "login": "ivanov"},
                {"t": "welcome", "agent_id": 7, "name": "Рабочий ПК"}]
    ctrl = make(welcomes=welcomes)
    ctrl.start()
    assert ctrl.apply_setup(form(root, login=" Owner ", password="x")) == []
    assert ctrl.fresh                                           # первый online — уведомление трея
    until(lambda: ctrl.login == "ivanov")                      # welcome сильнее ответа привязки
    assert store.load_config(home).name == "Рабочий ПК"
    ctrl.restart_link().result(5)
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")
    time.sleep(0.05)
    assert ctrl.login == "ivanov"                              # без login в welcome — прежний


def test_login_comes_from_the_enroll_answer(make, home, root):
    ctrl = make(welcomes=[{"t": "welcome", "agent_id": 7, "name": "Рабочий"}])
    assert ctrl.apply_setup(form(root, login=" Owner ", password="x")) == []
    assert ctrl.login == "owner"


def test_set_pin_restarts_link(make, home, configured):
    ctrl = make()
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.pin_status() == "none" and not ctrl.has_pin
    ctrl.agent.open.open("ab" * 16)
    ctrl.set_pin("2468")
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")
    assert ctrl.links[0].stopped and ctrl.links[1].hello["has_pin"] is True
    assert ctrl.has_pin and ctrl.pin_status() == "set"
    assert not ctrl.agent.open.touch("ab" * 16)
    for _ in range(5):
        PinStore(home).check("0000")
    assert ctrl.pin_status() == "locked"
    ctrl.set_pin(None)
    until(lambda: len(ctrl.links) == 3 and ctrl.state == "online")
    assert ctrl.links[2].hello["has_pin"] is False and ctrl.pin_status() == "none"


def test_set_pin_while_revoked_keeps_the_link_down(make, home, configured):
    ctrl = make(result="replaced")
    ctrl.start()
    until(lambda: ctrl.state == "replaced")
    ctrl.set_pin("2468")
    time.sleep(0.05)
    assert len(ctrl.links) == 1 and ctrl.state == "replaced" and ctrl.has_pin


def test_pin_changes_from_the_window_reach_the_tray(make, home, configured):
    ctrl = make()
    hits = []
    ctrl.on_pin_changed = lambda: hits.append(ctrl.pin_status())
    ctrl.set_pin("2468")
    for _ in range(5):
        PinStore(home).check("0000")
    ctrl.reset_pin()                                           # «Снять блок» окна — меню трея заново
    ctrl.set_pin(None)
    assert hits == ["set", "set", "none"]


def test_set_folder_validates_and_restarts(make, home, configured, tmp_path):
    ctrl = make()
    ctrl.start()
    until(lambda: ctrl.state == "online")
    assert ctrl.set_folder(str(tmp_path / "нет")) == ["Папки нет"]
    assert ctrl.set_folder("  ") == ["Папки нет"]
    other = tmp_path / "other"
    other.mkdir()
    assert ctrl.set_folder(f" {other} ") == []
    until(lambda: len(ctrl.links) == 2 and ctrl.state == "online")
    assert ctrl.links[1].hello["projects_root"] == str(other)
    assert store.load_config(home).projects_root == str(other)


def test_autostart_box_follows_the_registry_not_the_config(make, home, configured):
    ctrl = make()
    cfg = store.load_config(home)
    cfg.autostart = True
    store.save_config(home, cfg)
    assert not ctrl.autostart_enabled()                        # в реестре нет — снята
    ctrl._autostart.set_enabled(True)
    assert ctrl.autostart_enabled()


def test_set_autostart_writes_registry_and_config(make, home, configured):
    ctrl = make()
    ctrl.set_autostart(False)
    assert ctrl._autostart.calls == [False] and store.load_config(home).autostart is False
    ctrl.set_autostart(True)
    assert ctrl._autostart.calls == [False, True] and store.load_config(home).autostart is True
