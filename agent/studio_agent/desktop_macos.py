"""Экран, ввод и текстовый буфер macOS; только память и уже выданные локально права."""
import base64
import io
import os
import threading
import time

from .desktop_native import (MAX_CAPTURE_EDGE, MAX_CAPTURE_PIXELS, MAX_CLIPBOARD_UNITS,
                             MAX_IMAGE_BYTES, MAX_TEXT_UNITS, _text_bytes)
from .errors import Refused

# Физические коды Apple, а не символы текущей раскладки.
KEYS = dict(zip((f'Key{c}' for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),
                (0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46, 45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6)))
KEYS.update(zip((f'Digit{i}' for i in range(10)), (29, 18, 19, 20, 21, 23, 22, 26, 28, 25)))
KEYS.update(zip((f'F{i}' for i in range(1, 13)), (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111)))
KEYS.update(zip((f'Numpad{i}' for i in range(10)), (82, 83, 84, 85, 86, 87, 88, 89, 91, 92)))
KEYS.update({'Enter': 36, 'Tab': 48, 'Space': 49, 'Backspace': 51, 'Escape': 53, 'CapsLock': 57,
             'MetaLeft': 55, 'MetaRight': 54, 'ShiftLeft': 56, 'ShiftRight': 60,
             'AltLeft': 58, 'AltRight': 61, 'ControlLeft': 59, 'ControlRight': 62,
             'Home': 115, 'End': 119, 'PageUp': 116, 'PageDown': 121, 'Delete': 117,
             'ArrowLeft': 123, 'ArrowRight': 124, 'ArrowDown': 125, 'ArrowUp': 126,
             'Equal': 24, 'Minus': 27, 'BracketLeft': 33, 'BracketRight': 30, 'Quote': 39,
             'Semicolon': 41, 'Backslash': 42, 'Comma': 43, 'Slash': 44, 'Period': 47,
             'Backquote': 50, 'NumpadDecimal': 65, 'NumpadMultiply': 67, 'NumpadAdd': 69,
             'NumLock': 71, 'NumpadDivide': 75, 'NumpadEnter': 76, 'NumpadSubtract': 78})
_UNAVAILABLE = 'Нужен открытый графический сеанс macOS без блокировки экрана'


def permission_status(_quartz=None):
    """Только preflight текущего процесса: без запроса разрешений, снимков и ввода."""
    try:
        if _quartz is None:
            import Quartz as _quartz
        q = _quartz
        session = q.CGSessionCopyCurrentDictionary()
        ready = bool(session and session.get(q.kCGSessionOnConsoleKey) and session.get(q.kCGSessionLoginDoneKey))
        locked = bool(session and session.get('CGSSessionScreenIsLocked', False))
        return {'screen': bool(q.CGPreflightScreenCaptureAccess()), 'input': bool(q.CGPreflightPostEventAccess()),
                'session': 'locked' if locked else 'ready' if ready else 'unavailable'}
    except Exception:
        return {'screen': None, 'input': None, 'session': 'unavailable'}


def persistent_capture_build():
    from pathlib import Path
    from .macos_signed import info, sealed_app

    app = sealed_app(Path(__file__).resolve().parent.parent)
    return bool(app and info(app).get('StudioPersistentCapture') is True)


def request_permission(kind):
    """Системный запрос выполняет главный поток приложения по локальной кнопке."""
    import Quartz as q

    if kind in ('screen', 'remote'):
        remote = kind == 'remote'
        if remote and not persistent_capture_build():
            raise ValueError('persistent capture unavailable')
        pane = 'Privacy_RemoteDesktop' if remote else 'Privacy_ScreenCapture'
        if remote or not q.CGPreflightScreenCaptureAccess():
            from PyObjCTools import AppHelper

            def checked(_content, error):
                # ScreenCaptureKit запрашивает согласие сам. Настройки открываются после его ответа.
                if remote or error is not None:
                    AppHelper.callAfter(_open_permission_settings, pane)

            _shareable_content(checked)
            return
    elif kind == 'input':
        from HIServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        pane = 'Privacy_Accessibility'
        if not q.CGPreflightPostEventAccess():
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    else:
        raise ValueError('permission')
    if not _open_permission_settings(pane):
        raise OSError('permission settings')


def _open_permission_settings(pane):
    from AppKit import NSWorkspace
    from Foundation import NSURL

    url = NSURL.URLWithString_('x-apple.systempreferences:com.apple.preference.security?' + pane)
    return NSWorkspace.sharedWorkspace().openURL_(url)


def _shareable_content(callback):
    from ScreenCaptureKit import SCShareableContent

    SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        True, True, callback)


class NativeDesktop:
    """Вызовы последовательно в рабочем потоке; импорт сам не запрашивает разрешения."""

    def __init__(self, *, _quartz=None, _appkit=None, _capture=None, now=time.monotonic):
        self.q, self.appkit = _quartz, _appkit
        self._capture = _capture
        self._keys, self._buttons = set(), set()
        self._now = now
        self._clicks = {}

    def _ready(self):
        if self.q is None:
            import Quartz
            import AppKit
            self.q, self.appkit = Quartz, AppKit
        q = self.q
        status = permission_status(q)
        if status['session'] != 'ready':
            raise Refused('desktop_unavailable', _UNAVAILABLE)
        if status['screen'] is not True:
            raise Refused('desktop_unavailable', 'В агенте на Mac откройте «Запись экрана…» и разрешите доступ')
        if status['input'] is not True:
            raise Refused('desktop_unavailable', 'В агенте на Mac откройте «Универсальный доступ…» и разрешите доступ')
        return q

    def _displays(self):
        q = self._ready()
        error, ids, count = q.CGGetActiveDisplayList(17, None, None)
        if error or not 1 <= count <= 16:
            raise Refused('desktop_unavailable', _UNAVAILABLE)
        ids = sorted(ids[:count], key=lambda value: (value != q.CGMainDisplayID(), value))
        displays = [(value, q.CGDisplayBounds(value)) for value in ids]
        for value, bounds in displays:
            width, height = int(bounds.size.width), int(bounds.size.height)
            pixels_w, pixels_h = q.CGDisplayPixelsWide(value), q.CGDisplayPixelsHigh(value)
            if (not 0 < width <= MAX_CAPTURE_EDGE or not 0 < height <= MAX_CAPTURE_EDGE
                    or not 0 < pixels_w <= MAX_CAPTURE_EDGE or not 0 < pixels_h <= MAX_CAPTURE_EDGE
                    or pixels_w * pixels_h > MAX_CAPTURE_PIXELS):
                raise Refused('too_large', 'Размер рабочего стола превышает предел')
        return displays

    def monitors(self):
        return [{'id': i, 'width': int(bounds.size.width), 'height': int(bounds.size.height),
                 'primary': value == self.q.CGMainDisplayID()}
                for i, (value, bounds) in enumerate(self._displays())]

    def _display(self, number):
        displays = self._displays()
        if type(number) is not int or not 0 <= number < len(displays):
            raise Refused('invalid', 'Неверный монитор')
        return displays[number]

    def _capture_image(self, display, width, height):
        import ScreenCaptureKit as sc
        if not hasattr(sc, 'SCScreenshotManager'):  # macOS 13, до появления screenshot API
            return self.q.CGDisplayCreateImage(display)

        def wait(start):
            done, result = threading.Event(), []

            def complete(value, error):
                result.append((value, error))
                done.set()

            start(complete)
            if not done.wait(1.5) or not result or result[0][1] is not None or result[0][0] is None:
                raise Refused('desktop_unavailable', 'macOS не удалось получить изображение экрана')
            return result[0][0]

        content = wait(_shareable_content)
        selected = next((item for item in content.displays() if item.displayID() == display), None)
        if selected is None:
            raise Refused('desktop_unavailable', 'Монитор отключён — подключитесь снова')
        filter_ = sc.SCContentFilter.alloc().initWithDisplay_excludingWindows_(selected, [])
        config = sc.SCStreamConfiguration.alloc().init()
        config.setWidth_(width)
        config.setHeight_(height)
        config.setShowsCursor_(True)
        return wait(lambda callback: sc.SCScreenshotManager.
                    captureImageWithFilter_configuration_completionHandler_(filter_, config, callback))

    def frame(self, monitor, *, max_edge=1600):
        if type(max_edge) is not int or not 640 <= max_edge <= 2560:
            raise Refused('invalid')
        value, bounds = self._display(monitor)
        width, height = int(bounds.size.width), int(bounds.size.height)
        pixels_w, pixels_h = self.q.CGDisplayPixelsWide(value), self.q.CGDisplayPixelsHigh(value)
        scale = min(1, max_edge / max(pixels_w, pixels_h))
        from PIL import Image
        import objc
        with objc.autorelease_pool():
            captured = (self._capture or self._capture_image)(value, max(1, round(pixels_w * scale)),
                                                             max(1, round(pixels_h * scale)))
            if captured is None:
                raise Refused('desktop_unavailable', 'macOS не удалось получить изображение экрана')
            bitmap = self.appkit.NSBitmapImageRep.alloc().initWithCGImage_(captured)
            data = bitmap.representationUsingType_properties_(self.appkit.NSBitmapImageFileTypePNG, {})
            if data is None:
                raise Refused('desktop_unavailable', 'macOS не удалось прочитать изображение экрана')
            with io.BytesIO(bytes(data)) as source, Image.open(source) as raw:
                image = raw.convert('RGB')
            try:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                while True:
                    with io.BytesIO() as output:
                        image.save(output, format='JPEG', quality=65)
                        encoded = base64.b64encode(output.getvalue()).decode('ascii')
                    if len(encoded) <= MAX_IMAGE_BYTES:
                        break
                    smaller = image.resize((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                                           Image.Resampling.LANCZOS)
                    image.close()
                    image = smaller
                self._ready()  # блокировка/отзыв прав за время захвата не отдаёт кадр
                return {'image': encoded, 'width': image.width, 'height': image.height,
                        'screen_width': width, 'screen_height': height, 'monitor': monitor}
            finally:
                image.close()

    def _guard_keyboard(self):
        app = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            raise Refused('desktop_unavailable', _UNAVAILABLE)
        if app.processIdentifier() == os.getpid():
            raise Refused('outside', 'Окном агента можно управлять только на самом компьютере')

    def _guard_pointer(self, point):
        self._guard_keyboard()  # меню и захват мыши активного агента тоже недоступны
        q = self.q
        windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID)
        if windows is None:
            raise Refused('desktop_unavailable', _UNAVAILABLE)
        for window in windows:
            if window.get(q.kCGWindowOwnerPID) == os.getpid():
                rect = window.get(q.kCGWindowBounds, {})
                if (rect.get('X', 0) <= point[0] < rect.get('X', 0) + rect.get('Width', 0)
                        and rect.get('Y', 0) <= point[1] < rect.get('Y', 0) + rect.get('Height', 0)):
                    raise Refused('outside', 'Окном агента можно управлять только на самом компьютере')

    def _flags(self):
        q = self.q
        return ((q.kCGEventFlagMaskShift if self._keys & {'ShiftLeft', 'ShiftRight'} else 0)
                | (q.kCGEventFlagMaskControl if self._keys & {'ControlLeft', 'ControlRight'} else 0)
                | (q.kCGEventFlagMaskAlternate if self._keys & {'AltLeft', 'AltRight'} else 0)
                | (q.kCGEventFlagMaskCommand if self._keys & {'MetaLeft', 'MetaRight'} else 0))

    def _post(self, event):
        if event is None:
            raise Refused('desktop_unavailable', 'macOS не удалось создать событие ввода')
        self.q.CGEventSetFlags(event, self._flags())
        self.q.CGEventPost(self.q.kCGHIDEventTap, event)

    def _button(self, name, down, point):
        q = self.q
        button, pressed, released = {
            'left': (q.kCGMouseButtonLeft, q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp),
            'right': (q.kCGMouseButtonRight, q.kCGEventRightMouseDown, q.kCGEventRightMouseUp),
            'middle': (q.kCGMouseButtonCenter, q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp)}[name]
        event = q.CGEventCreateMouseEvent(None, pressed if down else released, point, button)
        if down:
            previous, location, count = self._clicks.get(name, (float('-inf'), point, 0))
            now = self._now()
            repeated = (now - previous <= self.appkit.NSEvent.doubleClickInterval()
                        and abs(location[0] - point[0]) <= 4 and abs(location[1] - point[1]) <= 4)
            self._clicks[name] = now, tuple(point), min(3, count + 1) if repeated else 1
        if event is not None:
            q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, self._clicks.get(name, (0, point, 1))[2])
        return event

    def input(self, events):
        from .desktop import validate_events
        validate_events(events)  # весь пакет проверяется до первого события
        import objc
        with objc.autorelease_pool():
            self._input(events)

    def _input(self, events):
        q = self._ready()
        for event in events:
            self._ready()
            kind = event['type']
            point = q.CGEventGetLocation(q.CGEventCreate(None))
            if kind == 'move':
                _, bounds = self._display(event['monitor'])
                point = (bounds.origin.x + round(event['x'] * (bounds.size.width - 1)),
                         bounds.origin.y + round(event['y'] * (bounds.size.height - 1)))
                self._guard_pointer(point)
                name = next((b for b in ('left', 'right', 'middle') if b in self._buttons), None)
                move = {'left': q.kCGEventLeftMouseDragged, 'right': q.kCGEventRightMouseDragged,
                        'middle': q.kCGEventOtherMouseDragged}.get(name, q.kCGEventMouseMoved)
                button = {'left': 0, 'right': 1, 'middle': 2}.get(name, 0)
                self._post(q.CGEventCreateMouseEvent(None, move, point, button))
            elif kind == 'button':
                name, down = event['button'], event['down']
                if down:
                    self._guard_pointer(point)
                self._post(self._button(name, down, point))
                (self._buttons.add if down else self._buttons.discard)(name)
            elif kind == 'wheel':
                self._guard_pointer(point)
                self._post(q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitPixel, 2,
                                                          -event['dy'], -event['dx']))
            elif kind == 'key':
                code, down = event['code'], event['down']
                if code not in KEYS:
                    raise Refused('unsupported', 'Эта клавиша не поддерживается')
                if down:
                    self._guard_keyboard()
                (self._keys.add if down else self._keys.discard)(code)
                self._post(q.CGEventCreateKeyboardEvent(None, KEYS[code], down))
            else:
                text = event['text'].replace('\r\n', '\n')
                for offset in range(0, len(text), 16):
                    self._ready()
                    self._guard_keyboard()
                    for char in text[offset:offset + 16]:
                        data = _text_bytes(char, MAX_TEXT_UNITS)
                        # PyObjC UniChar* ожидает последовательность символов UTF-16, включая пары суррогатов.
                        units = tuple(chr(int.from_bytes(data[i:i + 2], 'little')) for i in range(0, len(data), 2))
                        code = KEYS['Enter'] if char in ('\r', '\n') else KEYS['Tab'] if char == '\t' else KEYS['Space']
                        for down in (True, False):
                            key = q.CGEventCreateKeyboardEvent(None, code, down)
                            if key is None:
                                raise Refused('desktop_unavailable', 'macOS не удалось создать событие ввода')
                            if char not in ('\r', '\n', '\t'):
                                q.CGEventKeyboardSetUnicodeString(key, len(units), units)
                            self._post(key)

    def clipboard(self, text=None):
        self._ready()
        if text is not None:
            _text_bytes(text, MAX_CLIPBOARD_UNITS)
        import objc
        with objc.autorelease_pool():
            board = self.appkit.NSPasteboard.generalPasteboard()
            if text is None:
                value = board.stringForType_(self.appkit.NSPasteboardTypeString)
                result = '' if value is None else str(value)
            else:
                board.clearContents()
                if not board.setString_forType_(text, self.appkit.NSPasteboardTypeString):
                    raise Refused('desktop_unavailable', 'Буфер обмена сейчас недоступен')
                result = text
            _text_bytes(result, MAX_CLIPBOARD_UNITS)
            self._ready()
            return result

    def release(self):
        """Отпускание нашего ввода не требует доступа к окну агента или чтения экрана."""
        if self.q is None:
            return
        for code in tuple(self._keys):
            self._keys.discard(code)
            try:
                self._post(self.q.CGEventCreateKeyboardEvent(None, KEYS[code], False))
            except Exception:
                pass
        for name in tuple(self._buttons):
            self._buttons.discard(name)
            try:
                point = self.q.CGEventGetLocation(self.q.CGEventCreate(None))
                self._post(self._button(name, False, point))
            except Exception:
                pass
        self._clicks.clear()
