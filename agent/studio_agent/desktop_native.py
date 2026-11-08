"""Рабочий стол текущего пользователя Windows; данные остаются только в памяти.

Доступ разрешает вызывающий слой. Все методы одного экземпляра вызываются последовательно
на одном потоке: этому потоку принадлежит невидимое окно буфера обмена.
"""

import base64
import ctypes
import io
import math
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import Refused


MAX_EDGE = 1600
MAX_IMAGE_BYTES = 700 * 1024  # предел уже закодированной строки base64
MAX_CAPTURE_PIXELS = 36_000_000
MAX_CAPTURE_EDGE = 16384
MAX_CLIPBOARD_UNITS = 65536
MAX_TEXT_UNITS = 4096
MAX_EVENTS = 128
ES_DISPLAY_REQUIRED = 0x00000002

_UNAVAILABLE = "рабочий стол недоступен: нужен открытый сеанс Windows"
_OWN_WINDOW = "окном агента можно управлять только на самом компьютере"

DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("flags", DWORD), ("hwndActive", HANDLE),
                ("hwndFocus", HANDLE), ("hwndCapture", HANDLE), ("hwndMenuOwner", HANDLE),
                ("hwndMoveSize", HANDLE), ("hwndCaret", HANDLE), ("rcCaret", RECT)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", DWORD), ("data", _INPUT_UNION)]


# Второе поле означает KEYEVENTF_EXTENDEDKEY; произвольных virtual key/флагов снаружи нет.
KEYS = {
    "Backspace": (0x08, False), "Tab": (0x09, False), "Enter": (0x0D, False),
    "Escape": (0x1B, False), "Space": (0x20, False), "CapsLock": (0x14, False),
    "ShiftLeft": (0xA0, False), "ShiftRight": (0xA1, False),
    "ControlLeft": (0xA2, False), "ControlRight": (0xA3, True),
    "AltLeft": (0xA4, False), "AltRight": (0xA5, True),
    "MetaLeft": (0x5B, True), "MetaRight": (0x5C, True), "ContextMenu": (0x5D, True),
    "PageUp": (0x21, True), "PageDown": (0x22, True), "End": (0x23, True),
    "Home": (0x24, True), "ArrowLeft": (0x25, True), "ArrowUp": (0x26, True),
    "ArrowRight": (0x27, True), "ArrowDown": (0x28, True), "Insert": (0x2D, True),
    "Delete": (0x2E, True), "NumLock": (0x90, True), "ScrollLock": (0x91, False),
    "Semicolon": (0xBA, False), "Equal": (0xBB, False), "Comma": (0xBC, False),
    "Minus": (0xBD, False), "Period": (0xBE, False), "Slash": (0xBF, False),
    "Backquote": (0xC0, False), "BracketLeft": (0xDB, False), "Backslash": (0xDC, False),
    "BracketRight": (0xDD, False), "Quote": (0xDE, False),
    "NumpadMultiply": (0x6A, False), "NumpadAdd": (0x6B, False),
    "NumpadSubtract": (0x6D, False), "NumpadDecimal": (0x6E, False),
    "NumpadDivide": (0x6F, True), "NumpadEnter": (0x0D, True),
    **{f"Key{chr(i)}": (i, False) for i in range(65, 91)},
    **{f"Digit{i}": (0x30 + i, False) for i in range(10)},
    **{f"Numpad{i}": (0x60 + i, False) for i in range(10)},
    **{f"F{i}": (0x6F + i, False) for i in range(1, 13)},
}
_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040)}


@dataclass(frozen=True)
class _Monitor:
    left: int
    top: int
    right: int
    bottom: int
    primary: bool = False

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top


def _geometry(monitors):
    if not monitors or len(monitors) > 16:
        raise Refused("desktop_unavailable", _UNAVAILABLE)
    for monitor in monitors:
        if not (0 < monitor.width <= MAX_CAPTURE_EDGE and 0 < monitor.height <= MAX_CAPTURE_EDGE):
            raise Refused("too_large", "размер рабочего стола превышает предел")
    bounds = _Monitor(min(m.left for m in monitors), min(m.top for m in monitors),
                      max(m.right for m in monitors), max(m.bottom for m in monitors))
    # ImageGrab(all_screens=True) сначала захватывает весь виртуальный экран, потом обрезает.
    if (bounds.width > MAX_CAPTURE_EDGE or bounds.height > MAX_CAPTURE_EDGE
            or bounds.width * bounds.height > MAX_CAPTURE_PIXELS):
        raise Refused("too_large", "размер рабочего стола превышает предел")
    return bounds


def _monitor_at(monitors, number):
    if type(number) is not int or not 0 <= number < len(monitors):
        raise Refused("invalid", "неверный монитор")
    return monitors[number]


def _text_bytes(text, limit):
    if not isinstance(text, str) or "\0" in text:
        raise Refused("invalid", "неверный текст")
    if len(text) > limit:
        raise Refused("too_large", "текст превышает предел")
    try:
        encoded = text.encode("utf-16-le")
    except UnicodeError:
        raise Refused("invalid", "неверный текст") from None
    if len(encoded) > limit * 2:
        raise Refused("too_large", "текст превышает предел")
    return encoded


def _mouse(flags, *, x=0, y=0, data=0):
    return INPUT(type=0, mi=MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0))


def _key(vk, extended=False, *, down=True, scan=0, unicode=False):
    flags = (0x0001 if extended else 0) | (0 if down else 0x0002) | (0x0004 if unicode else 0)
    return INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


class _Win32:
    """Явные типы всех WinAPI: HWND, HDESK и ULONG_PTR не обрезаются на 64 битах."""

    def __init__(self):
        if sys.platform != "win32":
            raise Refused("unsupported", "рабочий стол поддерживается только в Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.clipboard_window = None
        callback = ctypes.WINFUNCTYPE(BOOL, HANDLE, HANDLE, ctypes.POINTER(RECT), ctypes.c_ssize_t)
        self.monitor_callback = callback
        declarations = [
            (self.user, "OpenInputDesktop", [DWORD, BOOL, DWORD], HANDLE),
            (self.user, "CloseDesktop", [HANDLE], BOOL),
            (self.user, "GetThreadDesktop", [DWORD], HANDLE),
            (self.user, "GetUserObjectInformationW", [HANDLE, ctypes.c_int, HANDLE, DWORD,
                                                    ctypes.POINTER(DWORD)], BOOL),
            (self.user, "SetThreadDpiAwarenessContext", [HANDLE], HANDLE),
            (self.user, "EnumDisplayMonitors", [HANDLE, ctypes.POINTER(RECT), callback,
                                              ctypes.c_ssize_t], BOOL),
            (self.user, "GetMonitorInfoW", [HANDLE, ctypes.POINTER(MONITORINFO)], BOOL),
            (self.user, "GetCursorPos", [ctypes.POINTER(POINT)], BOOL),
            (self.user, "WindowFromPoint", [POINT], HANDLE),
            (self.user, "GetForegroundWindow", [], HANDLE),
            (self.user, "GetGUIThreadInfo", [DWORD, ctypes.POINTER(GUITHREADINFO)], BOOL),
            (self.user, "GetWindowThreadProcessId", [HANDLE, ctypes.POINTER(DWORD)], DWORD),
            (self.user, "SendInput", [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int], ctypes.c_uint),
            (self.user, "OpenClipboard", [HANDLE], BOOL),
            (self.user, "CloseClipboard", [], BOOL),
            (self.user, "EmptyClipboard", [], BOOL),
            (self.user, "IsClipboardFormatAvailable", [ctypes.c_uint], BOOL),
            (self.user, "GetClipboardData", [ctypes.c_uint], HANDLE),
            (self.user, "SetClipboardData", [ctypes.c_uint, HANDLE], HANDLE),
            (self.user, "CreateWindowExW", [DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            HANDLE, HANDLE, HANDLE, HANDLE], HANDLE),
            (self.user, "DestroyWindow", [HANDLE], BOOL),
            (self.kernel, "GetCurrentThreadId", [], DWORD),
            (self.kernel, "ProcessIdToSessionId", [DWORD, ctypes.POINTER(DWORD)], BOOL),
            (self.kernel, "SetThreadExecutionState", [DWORD], DWORD),
            (self.kernel, "GlobalAlloc", [ctypes.c_uint, ctypes.c_size_t], HANDLE),
            (self.kernel, "GlobalLock", [HANDLE], HANDLE),
            (self.kernel, "GlobalUnlock", [HANDLE], BOOL),
            (self.kernel, "GlobalSize", [HANDLE], ctypes.c_size_t),
            (self.kernel, "GlobalFree", [HANDLE], HANDLE),
            (self.wts, "WTSQuerySessionInformationW", [HANDLE, DWORD, ctypes.c_int,
                                                       ctypes.POINTER(HANDLE), ctypes.POINTER(DWORD)], BOOL),
            (self.wts, "WTSFreeMemory", [HANDLE], None),
        ]
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result

    def _desktop_name(self, desktop):
        name = ctypes.create_unicode_buffer(256)
        size = DWORD()
        if not desktop or not self.user.GetUserObjectInformationW(
                desktop, 2, name, ctypes.sizeof(name), ctypes.byref(size)):
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        return name.value.casefold()

    def ensure_available(self):
        session = DWORD()
        if not self.kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        data, size = HANDLE(), DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, session.value, 8,
                                                   ctypes.byref(data), ctypes.byref(size)):
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        try:
            if not data or size.value < ctypes.sizeof(DWORD) or ctypes.cast(
                    data, ctypes.POINTER(DWORD)).contents.value != 0:  # WTSActive
                raise Refused("desktop_unavailable", _UNAVAILABLE)
        finally:
            if data:
                self.wts.WTSFreeMemory(data)
        desktop = self.user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        try:
            current = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
            if self._desktop_name(desktop) != "default" or self._desktop_name(current) != "default":
                raise Refused("desktop_unavailable", _UNAVAILABLE)
        finally:
            self.user.CloseDesktop(desktop)

    def wake_display(self):
        # Without ES_CONTINUOUS this only refreshes the display idle timer.
        # Active requests repeat it; closing the remote desktop restores normal idle behavior.
        if not self.kernel.SetThreadExecutionState(ES_DISPLAY_REQUIRED):
            raise Refused("desktop_unavailable", "Windows не удалось разбудить дисплей")

    @contextmanager
    def physical_pixels(self):
        previous = self.user.SetThreadDpiAwarenessContext(HANDLE(-4))
        if not previous:
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        try:
            yield
        finally:
            self.user.SetThreadDpiAwarenessContext(previous)

    def monitors(self):
        result, failed = [], []

        @self.monitor_callback
        def receive(handle, _dc, _rect, _extra):
            info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            if not self.user.GetMonitorInfoW(handle, ctypes.byref(info)):
                failed.append(True)
                return False
            rect = info.rcMonitor
            result.append(_Monitor(rect.left, rect.top, rect.right, rect.bottom, bool(info.dwFlags & 1)))
            return len(result) <= 16

        if not self.user.EnumDisplayMonitors(None, None, receive, 0) or failed or len(result) > 16:
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        return sorted(result, key=lambda m: (not m.primary, m.left, m.top, m.right, m.bottom))

    def cursor(self):
        point = POINT()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        return point.x, point.y

    def _guard_window(self, window):
        pid = DWORD()
        if not window or not self.user.GetWindowThreadProcessId(window, ctypes.byref(pid)):
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        if pid.value == os.getpid():
            raise Refused("outside", _OWN_WINDOW)

    def guard_pointer(self, x, y):
        self._guard_window(self.user.WindowFromPoint(POINT(x, y)))
        # При захвате мыши окно получает события даже за своими видимыми границами.
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if not self.user.GetGUIThreadInfo(0, ctypes.byref(info)):
            raise Refused("desktop_unavailable", _UNAVAILABLE)
        for window in (info.hwndCapture, info.hwndMenuOwner, info.hwndMoveSize):
            if window:
                self._guard_window(window)

    def guard_keyboard(self):
        self._guard_window(self.user.GetForegroundWindow())

    def send(self, events):
        array = (INPUT * len(events))(*events)
        inserted = self.user.SendInput(len(events), array, ctypes.sizeof(INPUT))
        if inserted != len(events):
            # При частичной вставке текста последний UTF-16 символ мог остаться нажатым.
            if inserted % 2 and events[inserted - 1].type == 1 and events[inserted - 1].ki.dwFlags == 4:
                up = _key(0, down=False, scan=events[inserted - 1].ki.wScan, unicode=True)
                self.user.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
            raise Refused("desktop_unavailable", "Windows не разрешила ввод в это окно")

    def clipboard(self, text):
        if not self.clipboard_window:
            self.clipboard_window = self.user.CreateWindowExW(
                0, "STATIC", "", 0, 0, 0, 0, 0, HANDLE(-3), None, None, None)  # HWND_MESSAGE
        if not self.clipboard_window or not self.user.OpenClipboard(self.clipboard_window):
            raise Refused("desktop_unavailable", "буфер обмена сейчас недоступен")
        try:
            if text is not None:
                encoded = _text_bytes(text, MAX_CLIPBOARD_UNITS) + b"\0\0"
                memory = self.kernel.GlobalAlloc(0x0002, len(encoded))  # GMEM_MOVEABLE
                if not memory:
                    raise Refused("desktop_unavailable", "буфер обмена сейчас недоступен")
                try:
                    pointer = self.kernel.GlobalLock(memory)
                    if not pointer:
                        raise Refused("desktop_unavailable", "буфер обмена сейчас недоступен")
                    try:
                        ctypes.memmove(pointer, encoded, len(encoded))
                    finally:
                        self.kernel.GlobalUnlock(memory)
                    if not self.user.EmptyClipboard() or not self.user.SetClipboardData(13, memory):
                        raise Refused("desktop_unavailable", "буфер обмена сейчас недоступен")
                    memory = None  # теперь память принадлежит Windows
                finally:
                    if memory:
                        self.kernel.GlobalFree(memory)
                return text
            if not self.user.IsClipboardFormatAvailable(13):
                return ""
            memory = self.user.GetClipboardData(13)
            length = self.kernel.GlobalSize(memory) if memory else 0
            if length > (MAX_CLIPBOARD_UNITS + 1) * 2:
                raise Refused("too_large", "текст в буфере обмена превышает предел")
            if not length or length % 2:
                raise Refused("invalid", "неверный текст в буфере обмена")
            pointer = self.kernel.GlobalLock(memory)
            if not pointer:
                raise Refused("desktop_unavailable", "буфер обмена сейчас недоступен")
            try:
                try:
                    raw = ctypes.string_at(pointer, length)
                    end = next((i for i in range(0, length, 2) if raw[i:i + 2] == b"\0\0"), None)
                    if end is None:
                        raise Refused("invalid", "неверный текст в буфере обмена")
                    result = raw[:end].decode("utf-16-le")
                except UnicodeError:
                    raise Refused("invalid", "неверный текст в буфере обмена") from None
                _text_bytes(result, MAX_CLIPBOARD_UNITS)
                return result
            finally:
                self.kernel.GlobalUnlock(memory)
        finally:
            self.user.CloseClipboard()

    def release(self):
        if self.clipboard_window:
            self.user.DestroyWindow(self.clipboard_window)
            self.clipboard_window = None


class NativeDesktop:
    """Ленивый доступ к WinAPI: создание экземпляра не читает экран или буфер."""

    def __init__(self, *, _api=None, _grab=None):
        self._api = _api
        self._grab = _grab
        self._keys = set()
        self._buttons = set()

    def _ready(self):
        if self._api is None:
            try:
                self._api = _Win32()
            except (AttributeError, OSError):
                raise Refused("unsupported", "рабочий стол недоступен в этой версии Windows") from None
        self._api.ensure_available()
        self._api.wake_display()
        return self._api

    def monitors(self):
        api = self._ready()
        with api.physical_pixels():
            monitors = api.monitors()
            _geometry(monitors)
            return [{"id": i, "width": m.width, "height": m.height, "primary": m.primary}
                    for i, m in enumerate(monitors)]

    def frame(self, monitor, *, max_edge=MAX_EDGE):
        if type(max_edge) is not int or not 640 <= max_edge <= 2560:
            raise Refused("invalid")
        api = self._ready()
        with api.physical_pixels():
            monitors = api.monitors()
            _geometry(monitors)
            selected = _monitor_at(monitors, monitor)
            from PIL import Image, ImageGrab
            try:
                with (self._grab or ImageGrab.grab)(
                        bbox=(selected.left, selected.top, selected.right, selected.bottom),
                        all_screens=True, include_layered_windows=True) as captured:
                    if captured.size != (selected.width, selected.height):
                        raise Refused("desktop_unavailable", _UNAVAILABLE)
                    image = captured.convert("RGB")
                    try:
                        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                        while True:
                            with io.BytesIO() as output:
                                image.save(output, format="JPEG", quality=65)
                                encoded = base64.b64encode(output.getvalue()).decode("ascii")
                            if len(encoded) <= MAX_IMAGE_BYTES:
                                break
                            smaller = image.resize((max(1, image.width * 3 // 4),
                                                    max(1, image.height * 3 // 4)), Image.Resampling.LANCZOS)
                            image.close()
                            image = smaller
                        api.ensure_available()  # блокировка во время захвата не отдаёт кадр наружу
                        return {"image": encoded, "width": image.width, "height": image.height,
                                "screen_width": selected.width, "screen_height": selected.height,
                                "monitor": monitor}
                    finally:
                        image.close()
            except (OSError, ValueError):
                raise Refused("desktop_unavailable", "Windows не удалось получить изображение экрана. Подключитесь снова.") from None

    def input(self, events):
        if not isinstance(events, list) or not 0 < len(events) <= MAX_EVENTS:
            raise Refused("invalid", "неверные события ввода")
        # Проверяем весь пакет до первого SendInput, включая общий предел UTF-16 текста.
        units = 0
        for event in events:
            if not isinstance(event, dict):
                raise Refused("invalid")
            kind = event.get("type")
            if kind == "move":
                if type(event.get("monitor")) is not int or not 0 <= event["monitor"] < 16:
                    raise Refused("invalid")
                if any(type(event.get(k)) not in (int, float) or not 0 <= event[k] <= 1
                       or not math.isfinite(event[k]) for k in ("x", "y")):
                    raise Refused("invalid")
            elif kind == "button":
                if (not isinstance(event.get("button"), str) or event["button"] not in _BUTTONS
                        or type(event.get("down")) is not bool):
                    raise Refused("invalid")
            elif kind == "wheel":
                if any(type(event.get(k)) is not int or abs(event[k]) > 2400 for k in ("dx", "dy")):
                    raise Refused("invalid")
            elif kind == "key":
                if not isinstance(event.get("code"), str) or event["code"] not in KEYS:
                    raise Refused("unsupported", "эта клавиша не поддерживается")
                if type(event.get("down")) is not bool:
                    raise Refused("invalid")
            elif kind == "text":
                units += len(_text_bytes(event.get("text"), MAX_TEXT_UNITS)) // 2
                if units > MAX_TEXT_UNITS:
                    raise Refused("too_large", "текст превышает предел")
            else:
                raise Refused("invalid")
        api = self._ready()
        with api.physical_pixels():
            for event in events:
                api.ensure_available()
                kind = event["type"]
                if kind == "move":
                    monitors = api.monitors()
                    bounds = _geometry(monitors)
                    selected = _monitor_at(monitors, event["monitor"])
                    x = selected.left + round(event["x"] * (selected.width - 1))
                    y = selected.top + round(event["y"] * (selected.height - 1))
                    api.guard_pointer(x, y)
                    api.send([_mouse(0x8000 | 0x4000 | 0x0001,
                                     x=round((x - bounds.left) * 65535 / max(1, bounds.width - 1)),
                                     y=round((y - bounds.top) * 65535 / max(1, bounds.height - 1)))])
                elif kind == "button":
                    button, down = event["button"], event["down"]
                    if down:
                        api.guard_pointer(*api.cursor())
                    api.send([_mouse(_BUTTONS[button][0 if down else 1])])
                    (self._buttons.add if down else self._buttons.discard)(button)
                elif kind == "wheel":
                    api.guard_pointer(*api.cursor())
                    for axis, flag, direction in (("dy", 0x0800, -1), ("dx", 0x1000, 1)):
                        if event[axis]:
                            api.send([_mouse(flag, data=event[axis] * direction)])
                elif kind == "key":
                    code, down = event["code"], event["down"]
                    if down:
                        api.guard_keyboard()
                    api.send([_key(*KEYS[code], down=down)])
                    (self._keys.add if down else self._keys.discard)(code)
                else:
                    data = _text_bytes(event["text"], MAX_TEXT_UNITS)
                    for offset in range(0, len(data), 64):
                        api.ensure_available()
                        api.guard_keyboard()
                        batch = []
                        for position in range(offset, min(offset + 64, len(data)), 2):
                            unit = int.from_bytes(data[position:position + 2], "little")
                            batch.extend((_key(0, scan=unit, unicode=True),
                                          _key(0, scan=unit, unicode=True, down=False)))
                        api.send(batch)

    def clipboard(self, text=None):
        if text is not None:
            _text_bytes(text, MAX_CLIPBOARD_UNITS)
        api = self._ready()
        result = api.clipboard(text)
        api.ensure_available()
        return result

    def release(self):
        """Отпустить только наш ввод; отказ защиты окна не мешает отпустить клавиши."""
        api = self._api
        if api is None:
            return
        for code in tuple(self._keys):
            try:
                api.send([_key(*KEYS[code], down=False)])
            except (Refused, OSError):
                pass
        for button in tuple(self._buttons):
            try:
                api.send([_mouse(_BUTTONS[button][1])])
            except (Refused, OSError):
                pass
        self._keys.clear()
        self._buttons.clear()
        api.release()
