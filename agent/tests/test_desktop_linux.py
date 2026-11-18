"""Portal consent, monitor scoping and lock checks without opening a real desktop."""
import asyncio
import socket
import struct
from types import SimpleNamespace

import pytest

from studio_agent import desktop, desktop_linux, desktop_portal as portal, host
from studio_agent.errors import Refused


def test_remote_start_cannot_open_system_chooser(monkeypatch):
    monkeypatch.setattr(desktop_linux.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('started chooser'))
    backend = desktop_linux.NativeDesktop()
    for request in (backend.monitors, lambda: backend.frame(0), lambda: backend.clipboard(None)):
        with pytest.raises(Refused, match='desktop_unavailable'):
            request()


async def test_local_grant_survives_enabling_then_lease_end_closes_it(monkeypatch):
    monkeypatch.setattr(host, 'LINUX', True)
    monkeypatch.setattr(host, 'MACOS', False)
    closed = []
    backend = SimpleNamespace(authorize_local=lambda: None,
        monitors=lambda: [{'id': 0, 'width': 800, 'height': 600, 'primary': True}],
        release=lambda: closed.append(True))
    monkeypatch.setattr(desktop_linux, 'NativeDesktop', lambda: backend)
    service = desktop.Desktop()
    try:
        assert not service.capable
        service.authorize_local()
        service.set_enabled(True)
        service.disconnect()  # capabilities renegotiated after transition from headless
        assert service.capable and not closed
        connection = await service.start('owner')
        with pytest.raises(Refused, match='busy'):
            service.authorize_local()
        service.stop('owner', connection['control'])
        await asyncio.wrap_future(service._pool.submit(lambda: None))
        assert closed
    finally:
        await service.close()


def test_local_disable_during_system_chooser_discards_late_permission(monkeypatch):
    service = desktop.Desktop()
    closed = []
    backend = SimpleNamespace(authorize_local=lambda: service.set_enabled(False),
                              release=lambda: closed.append(True))
    monkeypatch.setattr(desktop_linux, 'NativeDesktop', lambda: backend)
    try:
        with pytest.raises(Refused, match='gone'):
            service.authorize_local()
        assert closed and service._native is None
    finally:
        asyncio.run(service.close())


@pytest.mark.parametrize('changed, allowed', [
    ({}, True), ({'Type': 'x11'}, True), ({'Type': 'tty'}, False),
    ({'LockedHint': 'yes'}, False), ({'Active': 'no'}, False),
    ({'Remote': 'yes'}, False), ({'User': '9'}, False)])
def test_only_local_unlocked_graphical_session(monkeypatch, changed, allowed):
    props = {'Active': 'yes', 'LockedHint': 'no', 'User': '11', 'Type': 'wayland', 'Remote': 'no', **changed}
    monkeypatch.setattr(portal.os, 'getuid', lambda: 11, raising=False)
    monkeypatch.setattr(portal.subprocess, 'run',
                        lambda *_a, **_k: SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k, v in props.items())))
    assert portal.local_session() is allowed


def test_monitor_index_maps_to_only_locally_selected_stream(monkeypatch):
    worker = portal.Portal((None, None, None, None))
    worker.streams = portal.stream_list(SimpleNamespace(unpack=lambda: [
        (42, {'size': (1024, 768)}), (51, {'size': (800, 600)})]))
    calls = []
    worker.session = SimpleNamespace(pointer_position=lambda *a: calls.append(a),
                                     keyboard_key=lambda *a: calls.append(a))
    monkeypatch.setattr(worker, 'ready', lambda: None)
    worker.input([{'type': 'move', 'monitor': 1, 'x': 1, 'y': 0},
                  {'type': 'key', 'code': 'KeyA', 'down': True},
                  {'type': 'text', 'text': 'я'}])
    assert calls == [(51, 799, 0), (False, 30, 1), (True, 0x0100044f, 1), (True, 0x0100044f, 0)]
    with pytest.raises(portal.Denied, match='invalid'):
        worker.frame(-1, 800)
    with pytest.raises(portal.Denied, match='too_large'):
        portal.stream_list(SimpleNamespace(unpack=lambda: [(42, {'size': (10000, 10000)})]))


def test_clipboard_requires_portal_permission_and_bounds():
    worker = portal.Portal((None, None, None, None))
    worker.session = SimpleNamespace(is_clipboard_enabled=lambda: False)
    with pytest.raises(portal.Denied, match='unsupported'):
        worker.clipboard('secret')
    selected = []
    worker.session = SimpleNamespace(is_clipboard_enabled=lambda: True,
        set_selection=selected.append, is_selection_owned_by_session=lambda: True)
    with pytest.raises(portal.Denied, match='too_large'):
        worker.clipboard('😀' * (portal.MAX_TEXT // 2 + 1))
    assert not selected
    assert worker.clipboard('Привет') == worker.clipboard(None) == 'Привет'


def test_private_socket_bounds_rejects_oversize_before_body():
    first, second = socket.socketpair()
    with first, second:
        portal.send(first, {'value': 'Привет'})
        assert portal.receive(second) == {'value': 'Привет'}
        first.sendall(struct.pack('!I', portal.MAX_PACKET + 1))
        with pytest.raises(ValueError, match='packet length'):
            portal.receive(second)


def test_lock_during_frame_discards_image_and_releases_pipewire_fd(monkeypatch):
    import os
    from unittest.mock import Mock
    read_fd, write_fd = os.pipe()
    pipeline = Mock()
    buffer = pipeline.get_by_name.return_value.emit.return_value.get_buffer.return_value
    buffer.get_size.return_value = 4
    buffer.extract_dup.return_value = b'JPEG'
    gst = SimpleNamespace(parse_launch=lambda *_: pipeline, State=SimpleNamespace(PLAYING=1, NULL=0), MSECOND=1)
    worker = portal.Portal((None, None, gst, None))
    worker.streams = [(7, 1600, 900)]
    worker.session = SimpleNamespace(open_pipewire_remote=lambda: read_fd)
    def locked():
        raise portal.Denied('desktop_unavailable')
    monkeypatch.setattr(worker, 'ready', locked)
    try:
        with pytest.raises(portal.Denied, match='desktop_unavailable'):
            worker.frame(0, 800)
        with pytest.raises(OSError):
            os.fstat(read_fd)
        pipeline.set_state.assert_called_with(gst.State.NULL)
    finally:
        os.close(write_fd)
