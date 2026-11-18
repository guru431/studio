"""Companion run by the distribution's Python for its GI typelibs. No listening socket."""
import base64
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time


MAX_PACKET = 1024 * 1024
MAX_IMAGE = 700 * 1024
MAX_TEXT = 65536
MIMES = ('text/plain;charset=utf-8', 'UTF8_STRING', 'text/plain')
# DOM physical codes correspond to evdev codes, independent of the user's keyboard layout.
KEYS = dict(zip(('Key' + c for c in 'QWERTYUIOPASDFGHJKLZXCVBNM'),
                [*range(16, 26), *range(30, 39), *range(44, 51)]))
KEYS.update(zip(('Digit' + c for c in '1234567890'), range(2, 12)))
KEYS.update(zip((f'F{n}' for n in range(1, 13)), [*range(59, 69), 87, 88]))
KEYS.update(zip((f'Numpad{n}' for n in range(10)), (82, 79, 80, 81, 75, 76, 77, 71, 72, 73)))
KEYS.update({'Escape': 1, 'Minus': 12, 'Equal': 13, 'Backspace': 14, 'Tab': 15,
             'BracketLeft': 26, 'BracketRight': 27, 'Enter': 28, 'ControlLeft': 29,
             'Semicolon': 39, 'Quote': 40, 'Backquote': 41, 'ShiftLeft': 42, 'Backslash': 43,
             'Comma': 51, 'Period': 52, 'Slash': 53, 'ShiftRight': 54, 'NumpadMultiply': 55,
             'AltLeft': 56, 'Space': 57, 'CapsLock': 58, 'NumLock': 69, 'ScrollLock': 70,
             'NumpadSubtract': 74, 'NumpadAdd': 78, 'NumpadDecimal': 83, 'NumpadEnter': 96,
             'ControlRight': 97, 'NumpadDivide': 98, 'PrintScreen': 99, 'AltRight': 100,
             'Home': 102, 'ArrowUp': 103, 'PageUp': 104, 'ArrowLeft': 105, 'ArrowRight': 106,
             'End': 107, 'ArrowDown': 108, 'PageDown': 109, 'Insert': 110, 'Delete': 111,
             'Pause': 119, 'MetaLeft': 125, 'MetaRight': 126, 'ContextMenu': 127})


class Denied(Exception):
    """Only fixed protocol error codes cross the pipe."""


def send(sock, message):
    data = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode()
    if len(data) > MAX_PACKET:
        raise Denied('too_large')
    sock.sendall(struct.pack('!I', len(data)) + data)


def receive(sock):
    def read(size):
        data = bytearray()
        while len(data) < size:
            part = sock.recv(size - len(data))
            if not part:
                raise EOFError
            data.extend(part)
        return data
    length, = struct.unpack('!I', read(4))
    if not 0 < length <= MAX_PACKET:
        raise ValueError('packet length')
    return json.loads(read(length))


def local_session():
    # "auto" resolves to the user's primary graphical session even under systemd --user.
    try:
        proc = subprocess.run(['/usr/bin/loginctl', 'show-session', 'auto', '--no-pager',
                               '-p', 'Active', '-p', 'LockedHint', '-p', 'Type', '-p', 'Remote', '-p', 'User'],
                              capture_output=True, text=True, timeout=0.8, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    props = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)
    return (props.get('Type') in ('wayland', 'x11') and props.get('Active') == 'yes'
            and props.get('Remote') == 'no' and props.get('LockedHint') == 'no'
            and props.get('User') == str(os.getuid()))


def load_libraries():
    import gi
    for name in ('Xdp', 'Gst'):
        gi.require_version(name, '1.0')
    from gi.repository import Gio, GLib, Gst, Xdp
    Gst.init(None)
    for name in ('pipewiresrc', 'videoconvert', 'videoscale', 'jpegenc', 'appsink'):
        if Gst.ElementFactory.find(name) is None:
            raise ImportError(name)
    return Gio, GLib, Gst, Xdp


def supported(gio, glib):
    bus = gio.bus_get_sync(gio.BusType.SESSION, None)
    for interface, prop, mask in [('RemoteDesktop', 'AvailableDeviceTypes', 3),
                                   ('ScreenCast', 'AvailableSourceTypes', 1)]:
        value = bus.call_sync('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
                              'org.freedesktop.DBus.Properties', 'Get',
                              glib.Variant('(ss)', ('org.freedesktop.portal.' + interface, prop)),
                              None, gio.DBusCallFlags.NONE, 1000, None)
        if (value.unpack()[0] & mask) != mask:
            return False
    return True


def stream_list(variant):
    streams = []
    for node, props in variant.unpack() if variant else ():
        width, height = props.get('size', (0, 0))
        if not (0 < width <= 16384 and 0 < height <= 16384 and width * height <= 36_000_000):
            raise Denied('too_large')
        streams.append((node, width, height))
    if not 1 <= len(streams) <= 16:
        raise Denied('desktop_unavailable')
    return streams


class Portal:
    def __init__(self, libs):
        self.gio, self.glib, self.gst, self.xdp = libs
        self.session = None
        self.login_bus = None
        self.login_path = None
        self.streams = []
        self.text = None

    def wait(self, begin, end, *args):
        loop = self.glib.MainLoop()
        result = []

        def done(_obj, reply, *_):
            result.append(reply)
            loop.quit()

        begin(*args, None, done, None)
        loop.run()
        return end(result[0])

    def authorize(self):
        xdp = self.xdp
        self.login_bus = self.gio.bus_get_sync(self.gio.BusType.SYSTEM, None)
        # Pin checks to the graphical session that granted access, including after a user switch.
        self.login_path = self._login_property('/org/freedesktop/login1/user/self', 'User', 'Display')[1]
        portal = xdp.Portal.initable_new() if hasattr(xdp.Portal, 'initable_new') else xdp.Portal.new()
        devices = xdp.DeviceType.KEYBOARD | xdp.DeviceType.POINTER
        self.session = self.wait(portal.create_remote_desktop_session, portal.create_remote_desktop_session_finish,
                                 devices, xdp.OutputType.MONITOR, xdp.RemoteDesktopFlags.MULTIPLE,
                                 xdp.CursorMode.EMBEDDED)
        if hasattr(self.session, 'request_clipboard'):
            self.session.request_clipboard()
            self.session.connect('selection-transfer', self._clipboard_transfer)
        self.wait(self.session.start, self.session.start_finish, None)
        self.ready()
        if self.session.get_devices() & devices != devices:
            raise Denied('desktop_unavailable')
        self.streams = stream_list(self.session.get_streams())

    def _login_property(self, path, interface, prop=None):
        name = 'org.freedesktop.login1.' + interface
        args = self.glib.Variant('(ss)', (name, prop)) if prop else self.glib.Variant('(s)', (name,))
        value = self.login_bus.call_sync('org.freedesktop.login1', path, 'org.freedesktop.DBus.Properties',
                                         'Get' if prop else 'GetAll', args, None,
                                         self.gio.DBusCallFlags.NONE, 500, None)
        return value.unpack()[0]

    def ready(self):
        if self.session.get_session_state() != self.xdp.SessionState.ACTIVE:
            raise Denied('desktop_unavailable')
        props = self._login_property(self.login_path, 'Session')
        if (props.get('Type') not in ('wayland', 'x11') or props.get('Active') is not True
                or props.get('LockedHint') is not False or props.get('Remote') is not False
                or props.get('User', (None,))[0] != os.getuid()):
            raise Denied('desktop_unavailable')

    def monitor(self, number):
        if type(number) is not int or not 0 <= number < len(self.streams):
            raise Denied('invalid')
        return self.streams[number]

    def monitors(self):
        return [dict(id=n, width=w, height=h, primary=n == 0) for n, (_, w, h) in enumerate(self.streams)]

    def frame(self, monitor, max_edge):
        gst = self.gst
        node, width, height = self.monitor(monitor)
        if type(max_edge) is not int or not 640 <= max_edge <= 2560:
            raise Denied('invalid')
        scale = min(max_edge / max(width, height), 1)
        for _ in range(6):
            w, h = max(1, round(width * scale)), max(1, round(height * scale))
            fd = self.session.open_pipewire_remote()
            if fd < 0:
                raise Denied('desktop_unavailable')
            try:
                pipeline = gst.parse_launch(f'pipewiresrc fd={fd} path={node} ! videoconvert ! videoscale ! '
                                            f'video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1 ! '
                                            'jpegenc quality=65 ! appsink name=sink max-buffers=1 drop=true sync=false')
                try:
                    pipeline.set_state(gst.State.PLAYING)
                    sample = pipeline.get_by_name('sink').emit('try-pull-sample', 1500 * gst.MSECOND)
                    if sample is None:
                        raise Denied('desktop_unavailable')
                    buffer = sample.get_buffer()
                    if buffer.get_size() <= MAX_IMAGE * 3 // 4:
                        encoded = base64.b64encode(buffer.extract_dup(0, buffer.get_size())).decode('ascii')
                        self.ready()
                        return dict(image=encoded, width=w, height=h, screen_width=width,
                                    screen_height=height, monitor=monitor)
                finally:
                    pipeline.set_state(gst.State.NULL)
            finally:
                os.close(fd)
            scale *= 0.65
        raise Denied('too_large')

    def input(self, events):
        s = self.session
        for event in events:
            kind = event['type']
            if kind == 'move':
                node, w, h = self.monitor(event['monitor'])
                s.pointer_position(node, event['x'] * (w - 1), event['y'] * (h - 1))
            elif kind == 'button':
                s.pointer_button({'left': 272, 'right': 273, 'middle': 274}[event['button']], int(event['down']))
            elif kind == 'wheel':
                s.pointer_axis(False, event['dx'], event['dy'])
                s.pointer_axis(True, 0, 0)
            elif kind == 'key':
                s.keyboard_key(False, KEYS[event['code']], int(event['down']))
            elif kind == 'text':
                # Keysyms carry Unicode independently of the local keymap.
                for index, c in enumerate(event['text']):
                    if index % 32 == 0:
                        self.ready()
                    key = {'\r': 0xff0d, '\n': 0xff0d, '\t': 0xff09}.get(c, ord(c))
                    if ord(c) > 255:
                        key |= 0x01000000
                    s.keyboard_key(True, key, 1)
                    s.keyboard_key(True, key, 0)
        return {}

    def _clipboard_transfer(self, session, mime, serial):
        ok = False
        try:
            self.ready()
            if self.text is not None and mime in MIMES:
                fd = session.selection_write(serial)
                with os.fdopen(fd, 'wb', buffering=0) as output:
                    os.set_blocking(fd, False)
                    content = memoryview(self.text.encode())
                    end = time.monotonic() + 1
                    while content and select.select([], [fd], [], max(0, end - time.monotonic()))[1]:
                        written = output.write(content[:4096])
                        if written:
                            content = content[written:]
                    ok = not content
        except (OSError, ValueError, Denied, self.glib.Error):
            pass
        finally:
            session.selection_write_done(serial, ok)

    def clipboard(self, text):
        s = self.session
        if not hasattr(s, 'is_clipboard_enabled') or not s.is_clipboard_enabled():
            raise Denied('unsupported')
        if text is not None:
            if len(text.encode('utf-16-le')) > MAX_TEXT * 2:
                raise Denied('too_large')
            self.text = text
            s.set_selection(list(MIMES))
            return text
        if s.is_selection_owned_by_session():
            return self.text or ''
        offered = s.get_selection_mime_types() or []
        mime = next((m for m in MIMES if m in offered), None)
        if mime is None:
            return ''
        fd = s.selection_read(mime)
        if fd < 0:
            raise Denied('desktop_unavailable')
        with os.fdopen(fd, 'rb', buffering=0) as stream:
            data = bytearray()
            end = time.monotonic() + 1
            while select.select([fd], [], [], max(0, end - time.monotonic()))[0]:
                chunk = stream.read(16384)
                if not chunk:
                    value = data.decode()
                    if '\x00' in value or len(value.encode('utf-16-le')) > MAX_TEXT * 2:
                        raise Denied('too_large')
                    self.ready()
                    return value
                data.extend(chunk)
                if len(data) > MAX_TEXT * 4:
                    raise Denied('too_large')
            raise Denied('desktop_unavailable')

    def serve(self, sock):
        loop = self.glib.MainLoop()
        self.session.connect('closed', lambda *_: loop.quit())
        calls = {name: getattr(self, name) for name in ('monitors', 'frame', 'input', 'clipboard')}

        def request(_fd, flags):
            if flags & (self.glib.IO_HUP | self.glib.IO_ERR):
                loop.quit()
                return False
            try:
                query = receive(sock)
                self.ready()
                send(sock, {'value': calls[query.pop('op')](**query)})
            except Denied as error:
                send(sock, {'error': str(error)})
                if str(error) == 'desktop_unavailable':
                    loop.quit()
                    return False
            except (EOFError, OSError, ValueError, KeyError, TypeError, self.glib.Error):
                loop.quit()
                return False
            return True

        sock.settimeout(2)
        self.glib.io_add_watch(sock.fileno(), self.glib.PRIORITY_DEFAULT,
                               self.glib.IO_IN | self.glib.IO_HUP | self.glib.IO_ERR, request)
        send(sock, {'value': True})
        loop.run()


def main(arg):
    if not local_session():
        return 2
    try:
        libs = load_libraries()
    except (ImportError, ValueError):
        return 3
    if not supported(libs[0], libs[1]):
        return 4
    if arg == '--check':
        return 0
    with socket.socket(fileno=int(arg)) as sock:
        worker = Portal(libs)
        try:
            # Also bounds a pending chooser if the bridge itself exits while the dialog is open.
            signal.alarm(90)
            worker.authorize()
            signal.alarm(0)
            worker.serve(sock)
        finally:
            if worker.session is not None:
                worker.session.close()
    return 0


if __name__ == '__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        sys.exit(main(sys.argv[1]))
    except Exception:
        sys.exit(4)  # never print portal exceptions or transferred data
