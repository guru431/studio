"""Снимок Windows, частичные отказы и PIN перед чтением системных сведений."""
import subprocess
import threading

import pytest

from studio_agent import computer_info
from test_agent import agent_with_pin, req, root


def test_memory_status_reports_available_and_total_or_unavailable(monkeypatch):
    def read(pointer):
        data = pointer._obj
        assert data.length == 64
        data.total, data.available = 16 * 1024 ** 3, 4 * 1024 ** 3
        return 1

    monkeypatch.setattr(computer_info.ctypes.windll.kernel32, "GlobalMemoryStatusEx", read)
    assert computer_info.memory_status() == {"total": 16 * 1024 ** 3, "available": 4 * 1024 ** 3}
    monkeypatch.setattr(computer_info.ctypes.windll.kernel32, "GlobalMemoryStatusEx", lambda p: 0)
    assert computer_info.memory_status() is None


def test_snapshot_preserves_all_roots_and_distinguishes_unavailable_values(monkeypatch):
    monkeypatch.setattr(computer_info, '_system_data', lambda: {
        'os': 'Windows 11 Pro', 'version': '10.0.26100', 'memory_total': 16000, 'memory_free': 4000,
        'internal_ips': ['127.0.0.1', '::1', 'fe80::1', '10.77.1.8', '10.77.1.8', '::', 'bad'],
        'disks': [{'path': 'C:\\', 'total': 1000, 'free': 0}, {'path': 'D:\\', 'total': None, 'free': None}]})
    monkeypatch.setattr(computer_info, '_external_ip', lambda: '8.8.8.8')
    monkeypatch.setattr(computer_info.socket, 'gethostname', lambda: 'WORKSTATION')
    monkeypatch.setattr(computer_info.getpass, 'getuser', lambda: 'worker')
    roots = ['C:\\work\\projects', 'D:\\Проекты']
    data = computer_info.read(roots, now=lambda: 200)
    assert data == {'hostname': 'WORKSTATION', 'username': 'worker', 'os': 'Windows 11 Pro 10.0.26100',
                    'external_ip': '8.8.8.8', 'internal_ips': ['10.77.1.8'],
                    'memory': {'total': 16384000, 'available': 4096000, 'used': 12288000},
                    'disks': [{'path': 'C:\\', 'total': 1000, 'free': 0},
                              {'path': 'D:\\', 'total': None, 'free': None}],
                    'projects_roots': roots, 'checked_at': 200}
    roots.clear()
    assert len(data['projects_roots']) == 2


def test_failed_windows_probe_and_external_lookup_still_return_identity_and_roots(monkeypatch):
    real_run = subprocess.run

    def fail(command, *args, **kwargs):
        # Отказывает только проба PowerShell: `computer_info.subprocess` — общий модуль, и
        # `platform.platform()` с холодным кэшем (Python 3.14) зовёт тот же `subprocess.run`.
        if command[0] != 'powershell.exe':
            return real_run(command, *args, **kwargs)
        raise subprocess.TimeoutExpired('powershell', 10)

    class Unavailable:
        def open(self, url, timeout):
            assert url == 'https://api64.ipify.org' and timeout == 3
            raise OSError('private network error')

    monkeypatch.setattr(computer_info.subprocess, 'run', fail)
    monkeypatch.setattr(computer_info.urllib.request, 'build_opener', lambda *args: Unavailable())
    data = computer_info.read(['C:\\projects'], now=lambda: 200)
    assert data['memory'] is None and data['disks'] == [] and data['internal_ips'] == []
    assert data['external_ip'] is None and data['hostname'] and data['os']
    assert data['projects_roots'] == ['C:\\projects']


@pytest.mark.parametrize('response, expected', [(b'8.8.8.8', '8.8.8.8'),
    (b'2606:4700:4700::1111', '2606:4700:4700::1111'), (b'10.77.1.1', None), (b'<html>', None)])
def test_external_ip_accepts_only_public_addresses(monkeypatch, response, expected):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 128
            return response

    class Opener:
        def open(self, url, timeout):
            return Response()

    monkeypatch.setattr(computer_info.urllib.request, 'build_opener', lambda *args: Opener())
    assert computer_info._external_ip() == expected


async def test_computer_snapshot_obeys_pin_and_runs_outside_event_loop(agent_with_pin, monkeypatch):
    agent = agent_with_pin
    calls = []
    main_thread = threading.get_ident()

    def read(roots):
        assert threading.get_ident() != main_thread
        calls.append(roots)
        return {'hostname': 'private-computer', 'projects_roots': roots}

    monkeypatch.setattr(computer_info, 'read', read)
    assert agent.hello()['computer_info'] is True
    assert (await agent.handle(req('computer_info')))[0]['code'] == 'closed'
    assert not calls
    assert (await agent.handle(req('open', pin='2468')))[0]['ok']
    data = (await agent.handle(req('computer_info')))[0]['data']
    assert data['projects_roots'] == agent.cfg.projects_roots
    assert len(calls) == 1


@pytest.mark.integration
def test_real_windows_probe_reports_memory_disks_and_addresses():
    data = computer_info._system_data()
    assert data['os'] and data['version']
    assert data['memory_total'] > 0 and 0 <= data['memory_free'] <= data['memory_total']
    assert data['disks'] and any(d['total'] > 0 and 0 <= d['free'] <= d['total'] for d in data['disks'])
    assert isinstance(data['internal_ips'], list) and data['internal_ips']
    memory = computer_info.memory_status()
    assert abs(memory['total'] - data['memory_total'] * 1024) < 1024
    assert 0 <= memory['available'] <= memory['total']
