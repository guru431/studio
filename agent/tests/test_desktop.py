"""Разрешение только с машины, сохранение выбора и границы сеанса до Windows API."""
import asyncio
import json
import threading

import pytest

from studio_agent import app, desktop, store
from studio_agent.errors import Refused
from test_agent import Clock, make, req


class Native:
    def __init__(self):
        self.events = []
        self.releases = 0
        self.text = ''
        self.enter = self.leave = None
        self.resolutions = []

    def monitors(self):
        return [{'id': 0, 'width': 1920, 'height': 1080, 'primary': True}]

    def frame(self, monitor, *, max_edge=1600):
        self.resolutions.append(max_edge)
        if self.enter is not None:
            self.enter.set()
            self.leave.wait(2)
        return {'image': 'private-screen'}

    def input(self, events):
        self.events.extend(events)

    def clipboard(self, text):
        if text is not None:
            self.text = text
        return self.text

    def release(self):
        self.releases += 1


@pytest.fixture
async def service():
    native, clock = Native(), Clock()
    made = []

    def create():
        made.append(True)
        return native

    service = desktop.Desktop(factory=create, now=clock)
    yield service, native, clock, made
    await service.close()


def test_local_choice_defaults_off_and_survives_controller_and_config_restart(home):
    assert not store.load_config(home).desktop_enabled
    (home / store.CONFIG).write_text(json.dumps({'name': 'old-agent'}), encoding='utf-8')
    assert not store.load_config(home).desktop_enabled
    ctrl = app.Controller(home)
    ctrl.set_desktop_enabled(True)
    assert app.Controller(home).config().desktop_enabled is True
    ctrl.set_theme('dark')
    assert store.load_config(home).desktop_enabled is True
    ctrl.set_desktop_enabled(False)
    assert app.Controller(home).config().desktop_enabled is False
    (home / store.CONFIG).write_text('{"desktop_enabled": "true"}', encoding='utf-8')
    assert not store.load_config(home).desktop_enabled


async def test_disabled_permission_never_constructs_native_backend(service):
    desk, native, clock, made = service
    assert desk.status() == {'enabled': False, 'active': False, 'resolutions': list(desktop.RESOLUTIONS)}
    with pytest.raises(Refused, match='desktop_disabled'):
        await desk.start('owner')
    with pytest.raises(Refused, match='desktop_disabled'):
        await desk.frame('owner', '0' * 32, 0)
    with pytest.raises(Refused, match='desktop_disabled'):
        await desk.input('owner', '0' * 32, [{'type': 'key', 'code': 'KeyA', 'down': True}])
    with pytest.raises(Refused, match='desktop_disabled'):
        await desk.clipboard('owner', '0' * 32, None)
    assert made == [] and native.events == []


async def test_one_control_bound_to_browser_session_and_disable_revokes_every_operation(service):
    desk, native, clock, made = service
    desk.set_enabled(True)
    control = (await desk.start('owner'))['control']
    with pytest.raises(Refused, match='busy'):
        await desk.start('owner')
    for owner, token in [('other', control), ('owner', '0' * 32)]:
        with pytest.raises(Refused, match='owned'):
            await desk.input(owner, token, [{'type': 'text', 'text': 'private'}])
    await desk.input('owner', control, [{'type': 'key', 'code': 'ControlLeft', 'down': True}])
    assert await desk.clipboard('owner', control, 'текст') == {'text': 'текст'}
    assert await desk.clipboard('owner', control, None) == {'text': 'текст'}
    desk.set_enabled(False)
    await asyncio.wrap_future(desk._pool.submit(lambda: None))
    assert native.releases and desk.status() == {'enabled': False, 'active': False,
                                               'resolutions': list(desktop.RESOLUTIONS)}
    with pytest.raises(Refused, match='desktop_disabled'):
        await desk.frame('owner', control, 0)
    desk.set_enabled(True)
    with pytest.raises(Refused, match='owned'):
        await desk.clipboard('owner', control, None)


async def test_disable_during_capture_discards_frame_and_new_link_revokes_control(service):
    desk, native, clock, made = service
    desk.set_enabled(True)
    token = (await desk.start('owner'))['control']
    native.enter, native.leave = threading.Event(), threading.Event()
    pending = asyncio.create_task(desk.frame('owner', token, 0))
    try:
        assert await asyncio.to_thread(native.enter.wait, 0.5)
        desk.set_enabled(False)
    finally:
        native.leave.set()
    with pytest.raises(Refused, match='desktop_disabled'):
        await pending
    assert not desk.status()['active']


async def test_idle_lease_releases_input_and_never_reactivates_it(service):
    desk, native, clock, made = service
    desk.set_enabled(True)
    token = (await desk.start('owner'))['control']
    clock.t += desktop.LEASE_SEC + 1
    desk.expire()
    await asyncio.wrap_future(desk._pool.submit(lambda: None))
    assert native.releases and not desk.status()['active']
    with pytest.raises(Refused, match='owned'):
        await desk.frame('owner', token, 0)


async def test_local_stop_releases_input_without_changing_permanent_permission(service, home):
    from types import SimpleNamespace

    desk, native, clock, made = service
    desk.set_enabled(True)
    ctrl = app.Controller(home)
    store.save_config(home, store.Config(desktop_enabled=True))
    ctrl.agent = SimpleNamespace(desktop=desk)
    token = (await desk.start('owner'))['control']
    await desk.input('owner', token, [{'type': 'key', 'code': 'ControlLeft', 'down': True}])
    ctrl.stop_desktop_session()
    await asyncio.wrap_future(desk._pool.submit(lambda: None))
    assert native.releases and desk.status()['enabled'] and not desk.status()['active']
    assert ctrl.config().desktop_enabled
    with pytest.raises(Refused, match='owned'):
        await desk.input('owner', token, [{'type': 'text', 'text': 'stale'}])
    assert (await desk.start('next'))['control'] != token


async def test_frame_and_input_are_serialized_without_losing_normal_input(service):
    desk, native, clock, made = service
    desk.set_enabled(True)
    token = (await desk.start('owner'))['control']
    native.enter, native.leave = threading.Event(), threading.Event()
    pending = asyncio.create_task(desk.frame('owner', token, 0))
    try:
        assert await asyncio.to_thread(native.enter.wait, 0.5)
        key = {'type': 'key', 'code': 'KeyA', 'down': True}
        sent = asyncio.create_task(desk.input('owner', token, [key]))
        await asyncio.sleep(0)
        assert not sent.done()
    finally:
        native.leave.set()
    await asyncio.gather(pending, sent)
    assert native.events == [key]


async def test_rpc_cannot_change_permission_and_pin_is_checked_before_desktop(home, tmp_path):
    root = tmp_path / 'projects'
    root.mkdir()
    agent = make(home, root)
    try:
        assert (await agent.handle(req('desktop_start')))[0]['code'] == 'closed'
        assert (await agent.handle(req('open')))[0]['ok']
        assert (await agent.handle(req('desktop_status')))[0]['data'] == {
            'enabled': False, 'active': False, 'resolutions': list(desktop.RESOLUTIONS)}
        for op, values in [('desktop_enable', {}), ('desktop_status', {'enabled': True}),
                           ('desktop_start', {'enabled': True}), ('desktop_start', {})]:
            assert not (await agent.handle(req(op, **values)))[0]['ok']
            assert not agent.desktop.status()['enabled']
        agent.desktop.set_enabled(True)
        agent.desktop._factory = Native
        token = (await agent.handle(req('desktop_start')))[0]['data']['control']
        for edge in desktop.RESOLUTIONS:
            assert (await agent.handle(req('desktop_frame', control=token, monitor=0, max_edge=edge)))[0]['ok']
        assert agent.desktop._native.resolutions == list(desktop.RESOLUTIONS)
        for edge in (True, 639, 2561, '1280'):
            assert (await agent.handle(req('desktop_frame', control=token, monitor=0, max_edge=edge)))[0]['code'] == 'invalid'
        assert agent.desktop._native.resolutions == list(desktop.RESOLUTIONS)
        agent.new_link(reconnect=True)
        assert not agent.desktop.status()['active'] and agent.desktop.status()['enabled']
        assert (await agent.handle(req('desktop_frame', control=token, monitor=0)))[0]['code'] == 'owned'
    finally:
        await agent.desktop.close()


@pytest.mark.parametrize('event', [
    {'type': 'move', 'monitor': True, 'x': 0, 'y': 0},
    {'type': 'move', 'monitor': 0, 'x': float('nan'), 'y': 0},
    {'type': 'key', 'code': 'KeyA', 'down': 1},
    {'type': 'button', 'button': 'left', 'down': True, 'enabled': True},
    {'type': 'text', 'text': '\x00'}, {'type': 'wheel', 'dx': 0, 'dy': 1201},
])
def test_invalid_input_rejected_before_native(event):
    with pytest.raises(Refused, match='invalid'):
        desktop.validate_events([event])
