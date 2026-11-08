"""WinAPI подставлены: быстрые тесты не читают настоящий экран/буфер и не вводят события."""

import base64
import ctypes
import io
import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PIL import Image

from studio_agent import desktop_native as desktop
from studio_agent.errors import Refused


class FakeWindows:
    def __init__(self):
        self.screens = [desktop._Monitor(0, 0, 1920, 1080, True),
                        desktop._Monitor(-1280, -100, 0, 924)]
        self.sent = []
        self.pointer_checks = []
        self.keyboard_checks = 0
        self.checks = 0
        self.available = True
        self.own = False
        self.closed = 0
        self.clipboard_calls = []
        self.display_awake = True
        self.wakes = 0

    def ensure_available(self):
        self.checks += 1
        if not self.available:
            raise Refused("desktop_unavailable", "нужен открытый сеанс")

    def physical_pixels(self):
        return nullcontext()

    def wake_display(self):
        self.wakes += 1
        self.display_awake = True

    def monitors(self):
        if not self.display_awake:
            raise Refused('desktop_unavailable', 'display is asleep')
        return self.screens

    def cursor(self):
        return 200, 300

    def guard_pointer(self, x, y):
        self.pointer_checks.append((x, y))
        if self.own:
            raise Refused("outside", "окно агента")

    def guard_keyboard(self):
        self.keyboard_checks += 1
        if self.own:
            raise Refused("outside", "окно агента")

    def send(self, events):
        self.sent.extend(events)

    def clipboard(self, text):
        self.clipboard_calls.append(text)
        return "текст" if text is None else text

    def release(self):
        self.closed += 1


def test_win32_input_structures_keep_pointer_alignment():
    is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(desktop.INPUT) == (40 if is_64 else 28)
    assert ctypes.sizeof(desktop.MOUSEINPUT) == (32 if is_64 else 24)
    assert ctypes.sizeof(desktop.KEYBDINPUT) == (24 if is_64 else 16)
    assert desktop.INPUT.data.offset == (8 if is_64 else 4)
    assert ctypes.sizeof(desktop.MONITORINFO) == 40
    assert ctypes.sizeof(desktop.GUITHREADINFO) == (72 if is_64 else 48)
    assert ctypes.sizeof(desktop.DWORD) == ctypes.sizeof(desktop.LONG) == 4
    assert ctypes.sizeof(desktop.ULONG_PTR) == ctypes.sizeof(ctypes.c_void_p)


def test_constructor_and_release_without_use_do_not_load_winapi(monkeypatch):
    monkeypatch.setattr(desktop, "_Win32", lambda: pytest.fail("WinAPI загружен"))
    native = desktop.NativeDesktop()
    native.release()


def test_monitors_hide_absolute_coordinates_and_never_capture():
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api, _grab=lambda **_kw: pytest.fail("захват экрана"))
    assert native.monitors() == [
        {"id": 0, "width": 1920, "height": 1080, "primary": True},
        {"id": 1, "width": 1280, "height": 1024, "primary": False},
    ]
    assert api.sent == [] and api.clipboard_calls == []


def test_sleeping_display_is_woken_before_start_and_each_frame():
    api = FakeWindows()
    api.display_awake = False
    native = desktop.NativeDesktop(_api=api, _grab=lambda **_: Image.new('RGB', (1920, 1080)))
    assert native.monitors()[0]['primary'] is True
    assert api.wakes == 1
    api.display_awake = False
    assert native.frame(0)['width'] == 1600
    assert api.wakes == 2
    assert api.sent == [] and api.clipboard_calls == []


def test_locked_session_is_not_woken():
    api = FakeWindows()
    api.available = False
    api.display_awake = False
    with pytest.raises(Refused, match='desktop_unavailable'):
        desktop.NativeDesktop(_api=api).monitors()
    assert api.wakes == 0 and not api.display_awake


@pytest.mark.parametrize('result', [0, 0x80000000])
def test_win32_display_request_only_resets_display_idle_timer(result):
    api = object.__new__(desktop._Win32)
    flags = []
    api.kernel = SimpleNamespace(SetThreadExecutionState=lambda value: flags.append(value) or result)
    if result:
        api.wake_display()
    else:
        with pytest.raises(Refused, match='desktop_unavailable'):
            api.wake_display()
    assert flags == [0x00000002]  # No persistent, system-sleep or away-mode override.


def test_move_maps_negative_monitor_coordinates_to_virtual_desktop():
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api)
    native.input([{"type": "move", "monitor": 1, "x": 0, "y": 0},
                  {"type": "move", "monitor": 0, "x": 1, "y": 1}])
    assert api.pointer_checks == [(-1280, -100), (1919, 1079)]
    assert (api.sent[0].mi.dx, api.sent[0].mi.dy) == (0, 0)
    assert (api.sent[1].mi.dx, api.sent[1].mi.dy) == (65535, 65535)
    assert all(item.mi.dwFlags == 0xC001 for item in api.sent)


def test_unicode_text_sends_utf16_surrogates_and_wheel_matches_browser_direction():
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api)
    native.input([{"type": "text", "text": "Я🙂"},
                  {"type": "wheel", "dx": 120, "dy": 240}])
    assert [(item.ki.wVk, item.ki.wScan, item.ki.dwFlags) for item in api.sent[:6]] == [
        (0, 0x042F, 4), (0, 0x042F, 6),
        (0, 0xD83D, 4), (0, 0xD83D, 6), (0, 0xDE42, 4), (0, 0xDE42, 6)]
    assert api.sent[6].mi.dwFlags == 0x0800
    assert ctypes.c_int32(api.sent[6].mi.mouseData).value == -240
    assert api.sent[7].mi.dwFlags == 0x1000 and api.sent[7].mi.mouseData == 120


def test_extended_keys_are_tracked_and_release_skips_window_and_desktop_guards():
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api)
    native.input([{"type": "key", "code": "ControlRight", "down": True},
                  {"type": "button", "button": "left", "down": True}])
    assert api.sent[0].ki.wVk == 0xA3 and api.sent[0].ki.dwFlags == 1
    assert api.sent[1].mi.dwFlags == 2
    api.available = False
    api.own = True
    checks = api.checks
    native.release()
    assert api.checks == checks and api.closed == 1
    assert api.sent[2].ki.wVk == 0xA3 and api.sent[2].ki.dwFlags == 3
    assert api.sent[3].mi.dwFlags == 4
    native.release()
    assert len(api.sent) == 4


@pytest.mark.parametrize("event", [
    {"type": "move", "monitor": 0, "x": 0.5, "y": 0.5},
    {"type": "button", "button": "left", "down": True},
    {"type": "wheel", "dx": 0, "dy": 120},
    {"type": "key", "code": "KeyA", "down": True},
    {"type": "text", "text": "text"},
])
def test_remote_input_cannot_operate_agent_window(event):
    api = FakeWindows()
    api.own = True
    with pytest.raises(Refused) as caught:
        desktop.NativeDesktop(_api=api).input([event])
    assert caught.value.code == "outside"
    assert api.sent == []


@pytest.mark.parametrize("event", [
    {"type": "move", "monitor": 0, "x": float("nan"), "y": 0},
    {"type": "move", "monitor": 0, "x": 10 ** 1000, "y": 0},
    {"type": "move", "monitor": True, "x": 0, "y": 0},
    {"type": "move", "monitor": 0, "x": True, "y": 0},
    {"type": "button", "button": [], "down": True},
    {"type": "button", "button": "left", "down": 1},
    {"type": "wheel", "dx": 0, "dy": 10000},
    {"type": "key", "code": "VK_255", "down": True},
    {"type": "key", "code": "KeyA", "down": 1},
    {"type": "text", "text": "\ud800"},
    {"type": "text", "text": "\0"},
    {"type": "other"},
])
def test_bad_packet_is_rejected_before_any_input(event):
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api)
    with pytest.raises(Refused):
        native.input([{"type": "key", "code": "KeyA", "down": True}, event])
    assert api.sent == [] and api.checks == 0


def test_text_budget_covers_entire_packet_and_counts_utf16_units():
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api)
    with pytest.raises(Refused) as caught:
        native.input([{"type": "text", "text": "🙂" * 1200}] * 2)
    assert caught.value.code == "too_large" and api.sent == []


@pytest.mark.parametrize('edge', [640, 1600, 2560])
def test_frame_is_rgb_jpeg_and_bounded_without_saving_files(edge):
    api = FakeWindows()
    calls = []

    def grab(**kwargs):
        calls.append(kwargs)
        return Image.new("RGBA", (1920, 1080), (0, 100, 200, 255))

    result = desktop.NativeDesktop(_api=api, _grab=grab).frame(0, max_edge=edge)
    assert calls == [{"bbox": (0, 0, 1920, 1080), "all_screens": True,
                      "include_layered_windows": True}]
    assert result["screen_width"] == 1920 and result["screen_height"] == 1080
    expected = (min(edge, 1920), min(edge, 1920) * 9 // 16)
    assert (result["width"], result["height"]) == expected and result["monitor"] == 0
    assert len(result["image"]) <= desktop.MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(base64.b64decode(result["image"], validate=True))) as image:
        assert image.format == "JPEG" and image.mode == "RGB" and image.size == expected
    assert api.checks == 2


def test_frame_reduces_resolution_when_encoded_image_exceeds_budget(monkeypatch):
    api = FakeWindows()
    api.screens = [desktop._Monitor(0, 0, 400, 400, True)]
    monkeypatch.setattr(desktop, "MAX_IMAGE_BYTES", 2500)
    result = desktop.NativeDesktop(_api=api, _grab=lambda **_: Image.new("RGB", (400, 400))).frame(0)
    assert result["width"] < 400 and len(result["image"]) <= 2500


def test_large_virtual_geometry_is_refused_before_capture():
    api = FakeWindows()
    api.screens = [desktop._Monitor(0, 0, 8000, 8000, True)]
    with pytest.raises(Refused) as caught:
        desktop.NativeDesktop(_api=api, _grab=lambda **_: pytest.fail("capture")).frame(0)
    assert caught.value.code == "too_large"


def test_lock_during_capture_discards_frame():
    api = FakeWindows()

    def grab(**_):
        api.available = False
        return Image.new("RGB", (1920, 1080))

    with pytest.raises(Refused) as caught:
        desktop.NativeDesktop(_api=api, _grab=grab).frame(0)
    assert caught.value.code == "desktop_unavailable"


@pytest.mark.parametrize("operation", ["monitors", "frame", "input", "clipboard"])
def test_unavailable_desktop_prevents_every_remote_operation(operation):
    api = FakeWindows()
    api.available = False
    native = desktop.NativeDesktop(_api=api, _grab=lambda **_: pytest.fail("capture"))
    arguments = {"monitors": (), "frame": (0,), "input": ([{"type": "text", "text": "x"}],),
                 "clipboard": ()}
    with pytest.raises(Refused) as caught:
        getattr(native, operation)(*arguments[operation])
    assert caught.value.code == "desktop_unavailable"
    assert api.sent == [] and api.clipboard_calls == []


def test_clipboard_is_explicit_and_validated_before_native_access():
    api = FakeWindows()
    native = desktop.NativeDesktop(_api=api)
    assert native.clipboard() == "текст"
    assert native.clipboard("") == ""
    assert api.clipboard_calls == [None, ""]
    with pytest.raises(Refused) as caught:
        native.clipboard("🙂" * 40000)
    assert caught.value.code == "too_large" and api.clipboard_calls == [None, ""]


def raw_windows(*, state=0, input_name="Default", thread_name="Default", can_open=True):
    """Подставленные дескрипторы проверяют настоящую ветку OpenInputDesktop/WTS."""
    api = object.__new__(desktop._Win32)
    calls = []
    state_data = desktop.DWORD(state)

    def session(_pid, output):
        output._obj.value = 7
        return True

    def query(_server, session_id, kind, output, size):
        assert session_id == 7 and kind == 8
        output._obj.value = ctypes.addressof(state_data)
        size._obj.value = ctypes.sizeof(state_data)
        return True

    def name(handle, kind, output, size, _needed):
        assert kind == 2 and size >= 2
        output.value = input_name if handle == 10 else thread_name
        return True

    api.kernel = SimpleNamespace(ProcessIdToSessionId=session, GetCurrentThreadId=lambda: 99)
    api.wts = SimpleNamespace(WTSQuerySessionInformationW=query,
                             WTSFreeMemory=lambda _: calls.append("free"))
    api.user = SimpleNamespace(
        OpenInputDesktop=lambda flags, inherit, access: 10 if can_open else None,
        GetThreadDesktop=lambda thread: 11,
        GetUserObjectInformationW=name,
        CloseDesktop=lambda handle: calls.append(("close", handle)),
    )
    return api, calls


@pytest.mark.parametrize("options", [
    {"state": 4}, {"state": 1}, {"input_name": "Winlogon"},
    {"thread_name": "other"}, {"can_open": False},
])
def test_win32_refuses_disconnected_secure_and_nondefault_desktops(options):
    api, calls = raw_windows(**options)
    with pytest.raises(Refused) as caught:
        api.ensure_available()
    assert caught.value.code == "desktop_unavailable"
    assert "free" in calls
    if not options.get("state") and options.get("can_open", True):
        assert ("close", 10) in calls


def test_win32_accepts_only_current_active_default_desktop_and_closes_handle():
    api, calls = raw_windows()
    api.ensure_available()
    assert calls == ["free", ("close", 10)]


def test_native_window_guards_resolve_pid_from_pointer_and_foreground(monkeypatch):
    api = object.__new__(desktop._Win32)
    checked = []

    def pid(handle, output):
        checked.append(handle)
        output._obj.value = 12345
        return 88

    monkeypatch.setattr(os, "getpid", lambda: 12345)
    api.user = SimpleNamespace(WindowFromPoint=lambda point: 0x123456789,
                               GetForegroundWindow=lambda: 0x234567890,
                               GetWindowThreadProcessId=pid)
    for guard, arguments in ((api.guard_pointer, (10, 20)), (api.guard_keyboard, ())):
        with pytest.raises(Refused) as caught:
            guard(*arguments)
        assert caught.value.code == "outside"
    assert checked == [0x123456789, 0x234567890]


def test_pointer_guard_checks_agent_mouse_capture_outside_window(monkeypatch):
    api = object.__new__(desktop._Win32)
    checked = []
    monkeypatch.setattr(os, "getpid", lambda: 12345)

    def pid(handle, output):
        checked.append(handle)
        output._obj.value = 12345 if handle == 99 else 54321
        return 88

    def thread(_thread, output):
        output._obj.hwndCapture = 99
        return True

    api.user = SimpleNamespace(WindowFromPoint=lambda point: 98,
                               GetWindowThreadProcessId=pid, GetGUIThreadInfo=thread)
    with pytest.raises(Refused) as caught:
        api.guard_pointer(10, 20)
    assert caught.value.code == "outside" and checked == [98, 99]


def test_native_clipboard_write_has_owner_and_transfers_memory_only_after_success():
    api = object.__new__(desktop._Win32)
    api.clipboard_window = None
    calls, buffers = [], []

    def create(*args):
        assert args[1] == "STATIC" and args[8].value == ctypes.c_void_p(-3).value
        return 0x123456789

    def allocate(flags, length):
        assert flags == 2
        buffers.append(ctypes.create_string_buffer(length))
        return ctypes.addressof(buffers[-1])

    def opened(window):
        assert window == 0x123456789
        return True

    def store(kind, memory):
        assert kind == 13
        assert ctypes.string_at(memory, 10) == "Я🙂!\0".encode("utf-16-le")
        calls.append("store")
        return memory

    api.user = SimpleNamespace(CreateWindowExW=create, OpenClipboard=opened,
                               EmptyClipboard=lambda: True, SetClipboardData=store,
                               CloseClipboard=lambda: calls.append("close"),
                               DestroyWindow=lambda hwnd: calls.append(("destroy", hwnd)))
    api.kernel = SimpleNamespace(GlobalAlloc=allocate, GlobalLock=lambda memory: memory,
                                 GlobalUnlock=lambda _: None,
                                 GlobalFree=lambda _: pytest.fail("память уже принадлежит Windows"))
    assert api.clipboard("Я🙂!") == "Я🙂!"
    api.release()
    assert calls == ["store", "close", ("destroy", 0x123456789)]


def test_native_clipboard_read_stops_at_aligned_terminator_and_ignores_unused_memory():
    api = object.__new__(desktop._Win32)
    api.clipboard_window = 123
    calls = []
    raw = "A🙂".encode("utf-16-le") + b"\0\0\x00\xd8"
    memory = ctypes.create_string_buffer(raw, len(raw))
    api.user = SimpleNamespace(OpenClipboard=lambda window: True,
                               IsClipboardFormatAvailable=lambda kind: True,
                               GetClipboardData=lambda kind: 99,
                               CloseClipboard=lambda: calls.append("close"))
    api.kernel = SimpleNamespace(GlobalSize=lambda handle: len(raw),
                                 GlobalLock=lambda handle: ctypes.addressof(memory),
                                 GlobalUnlock=lambda handle: calls.append("unlock"))
    assert api.clipboard(None) == "A🙂"
    assert calls == ["unlock", "close"]


def test_partial_unicode_send_releases_last_inserted_code_unit():
    api = object.__new__(desktop._Win32)
    calls = []

    def send(number, data, size):
        assert size == ctypes.sizeof(desktop.INPUT)
        calls.append(number)
        if len(calls) == 2:
            up = data._obj
            assert up.ki.wScan == ord("Я") and up.ki.dwFlags == 6
        return 1

    api.user = SimpleNamespace(SendInput=send)
    with pytest.raises(Refused):
        api.send([desktop._key(0, scan=ord("Я"), unicode=True),
                  desktop._key(0, scan=ord("Я"), unicode=True, down=False)])
    assert calls == [2, 1]
