"""Controller без Tk и трея: канал, привязка, настройка, PIN, выход — с подставными Link, привязкой и автозапуском."""

import asyncio
import time

import pytest
from claude_agent_sdk import PermissionResultDeny

from fake_sdk import FakeFactory, result_ok
from studio_agent import app, autostart, projects, sessions, store, tray, turns
from studio_agent.pin import PinStore

MODELS = [{"id": "opus", "label": "Opus"}]


class FakeLink:
    """Канал без сети: `online` до `stop()`; `result` — чем кончится `run()` сам (`revoked`, `replaced`)."""

    def __init__(self, url, key, agent, on_state, result=None):
        self.url, self.key, self.on_state, self.result = url, key, on_state, result
        self.hello = agent.hello()
        self.stopped = False
        self.ending = None
        self._stop = asyncio.Event()

    async def run(self):
        if self.result is not None:
            self.on_state(self.result)
            return self.result
        self.on_state("connecting")
        self.on_state("online")
        await self._stop.wait()
        state = self.ending or "stopped"
        self.on_state(state)
        return state

    async def stop(self):
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
              cli_version=lambda p: "2.3.1", sdk=None):
        links, probes, probe_paths = [], [], []

        def link_factory(url, key, agent, on_state):
            links.append(FakeLink(url, key, agent, on_state, result))
            return links[-1]

        async def fake_probe(root, cli_path, factory=None):
            probes.append((root, len(links)))
            probe_paths.append(cli_path)
            return (MODELS, ["low", "high"]) if probe is None else probe(len(probes))

        def fake_enroll(url, login, password, name):
            enroll_calls.append((url, login, name)) if enroll_calls is not None else None
            return 7, "k"

        extra = {"sdk_factory": sdk} if sdk is not None else {}
        ctrl = app.Controller(home, link_factory=link_factory, find_cli=find_cli, cli_version=cli_version,
                              probe=fake_probe, enroll_fn=fake_enroll, autostart=FakeAutostart(), sleep=sleep, **extra)
        ctrl.links, ctrl.probes, ctrl.probe_paths = links, probes, probe_paths
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

    ctrl = make(sleep=sleep, probe=lambda n: ([{"id": f"m{n}", "label": "M"}], ["low"]))
    ctrl.start()
    until(lambda: ctrl.state == "online" and pauses)
    assert pauses == [app.PROBE_EVERY_SEC]
    ctrl._loop.call_soon_threadsafe(gate.set)
    until(lambda: len(ctrl.probes) == 2)
    until(lambda: ctrl.agent.models == [{"id": "m2", "label": "M"}])
    assert len(ctrl.links) == 1  # модели уйдут в следующий hello


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

    async def close():
        closed.append(True)
        await real_close()

    ctrl.runner.close = close
    ctrl.quit()
    assert closed == [True]
    assert ctrl.links[0].stopped
    assert not ctrl._thread.is_alive()


def test_autostart_command_runs_the_pyw_of_this_repo():
    repo = autostart.REPO
    assert autostart.command() == f'"{repo}\.venv\Scripts\pythonw.exe" "{repo}\studio_agent.pyw"'
    assert (repo / "studio_agent.pyw").is_file()


def test_tray_icon_colour_and_title_follow_the_state():
    green, grey, red = tray.COLORS["online"], tray.COLORS["offline"], tray.COLORS["revoked"]
    assert len({green, grey, red}) == 3
    for state in ("connecting", "offline", "setup"):
        assert tray.COLORS[state] == grey
    assert tray.COLORS["replaced"] == red
    image = tray.image("online")
    assert image.size == (64, 64) and image.getpixel((32, 32))[:3] == green
    assert tray.title("replaced") == "Studio Agent — ключ используется другим мостом"
