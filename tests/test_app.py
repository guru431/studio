"""Controller без Tk и трея: канал, привязка, настройка, PIN, выход — с подставными Link, привязкой и автозапуском."""

import asyncio
import time

import pytest

from studio_agent import app, store
from studio_agent.pin import PinStore

MODELS = [{"id": "opus", "label": "Opus"}]


class FakeLink:
    """Канал без сети: `online` до `stop()`; `result` — чем кончится `run()` сам (`revoked`, `replaced`)."""

    def __init__(self, url, key, agent, on_state, result=None):
        self.url, self.key, self.on_state, self.result = url, key, on_state, result
        self.hello = agent.hello()
        self.stopped = False
        self._stop = asyncio.Event()

    async def run(self):
        if self.result is not None:
            self.on_state(self.result)
            return self.result
        self.on_state("connecting")
        self.on_state("online")
        await self._stop.wait()
        self.on_state("stopped")
        return "stopped"

    async def stop(self):
        self.stopped = True
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

    def build(result=None, probe=None, enroll_calls=None, sleep=forever):
        links, probes = [], []

        def link_factory(url, key, agent, on_state):
            links.append(FakeLink(url, key, agent, on_state, result))
            return links[-1]

        async def fake_probe(root, cli_path, factory=None):
            probes.append((root, len(links)))
            return (MODELS, ["low", "high"]) if probe is None else probe(len(probes))

        def fake_enroll(url, login, password, name):
            enroll_calls.append((url, login, name)) if enroll_calls is not None else None
            return 7, "k"

        ctrl = app.Controller(home, link_factory=link_factory, find_cli=lambda: None, cli_version=lambda p: "2.3.1",
                              probe=fake_probe, enroll_fn=fake_enroll, autostart=FakeAutostart(), sleep=sleep)
        ctrl.links, ctrl.probes = links, probes
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
