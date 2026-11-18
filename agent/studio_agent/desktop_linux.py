"""Linux remote desktop. Only an explicit local action may open the system chooser."""
from pathlib import Path
import socket
import struct
import subprocess

from . import desktop_portal as portal
from .errors import Refused


_PYTHON = '/usr/bin/python3'
_HELPER = str(Path(portal.__file__).resolve())
_LOCAL = 'Выберите экраны в локальном меню Studio Agent и подтвердите системный запрос'
_REASONS = {
    0: 'Доступен системный выбор экранов и устройств ввода',
    2: 'Нужен активный разблокированный графический сеанс Linux с systemd-logind',
    3: 'Нужны системные Python GI, libportal, GStreamer и модуль PipeWire',
    4: 'Графическая оболочка должна поддерживать порталы RemoteDesktop и ScreenCast',
}


def _probe():
    try:
        return subprocess.run([_PYTHON, '-IB', _HELPER, '--check'], timeout=4,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 3


def available():
    return _probe() == 0


def diagnostic():
    return _REASONS.get(_probe(), _REASONS[4])


class NativeDesktop:
    def __init__(self):
        self.sock = self.process = None

    def authorize_local(self):
        """The caller is the local agent menu. The remote protocol cannot start this process."""
        code = _probe()
        if code:
            raise Refused('desktop_unavailable', _REASONS.get(code, _REASONS[4]))
        self.sock, peer = socket.socketpair()
        try:
            with peer:
                self.process = subprocess.Popen([_PYTHON, '-IB', _HELPER, str(peer.fileno())],
                    pass_fds=(peer.fileno(),), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.sock.settimeout(90)
            self._receive()
            self.sock.settimeout(3.8)
        except BaseException:
            self.release()
            raise

    def _receive(self):
        try:
            value = portal.receive(self.sock)
        except (OSError, EOFError, ValueError, struct.error):
            self.release()
            raise Refused('desktop_unavailable', _LOCAL) from None
        if 'error' in value:
            code = value['error']
            messages = {'unsupported': 'Системный портал не предоставил доступ к буферу обмена',
                        'too_large': 'Данные рабочего стола превышают предел', 'invalid': 'Неверные параметры'}
            raise Refused(code, messages.get(code, _LOCAL))
        return value['value']

    def _call(self, op, **args):
        if self.sock is None:
            raise Refused('desktop_unavailable', _LOCAL)
        try:
            portal.send(self.sock, {'op': op, **args})
        except OSError:
            self.release()
            raise Refused('desktop_unavailable', _LOCAL) from None
        return self._receive()

    def monitors(self):
        return self._call('monitors')

    def frame(self, monitor, *, max_edge=1600):
        return self._call('frame', monitor=monitor, max_edge=max_edge)

    def input(self, events):
        from .desktop import validate_events
        validate_events(events)
        self._call('input', events=events)

    def clipboard(self, text=None):
        return self._call('clipboard', text=text)

    def release(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.process is not None:
            try:
                self.process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
