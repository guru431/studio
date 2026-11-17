"""Регрессии macOS: системные API подставлены, экран/ввод владельца не затрагиваются."""
import base64
import io
import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PIL import Image

from studio_agent import desktop, desktop_macos as mac, host
from studio_agent.errors import Refused


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setattr(host, 'MACOS', True)
    monkeypatch.setitem(sys.modules, 'objc', SimpleNamespace(autorelease_pool=nullcontext))
    rect = lambda x, y, w, h: SimpleNamespace(origin=SimpleNamespace(x=x, y=y),
                                             size=SimpleNamespace(width=w, height=h))
    state = SimpleNamespace(locked=False, console=True, login=True, screen=True, input=True,
                            pid=os.getpid() + 1, windows=[], events=[], clipboard='текст', captures=[])
    q = SimpleNamespace(
        kCGSessionOnConsoleKey='console', kCGSessionLoginDoneKey='login', kCGHIDEventTap=0,
        kCGWindowListOptionOnScreenOnly=1, kCGNullWindowID=0, kCGWindowOwnerPID='pid', kCGWindowBounds='bounds',
        kCGEventFlagMaskShift=1, kCGEventFlagMaskControl=2, kCGEventFlagMaskAlternate=4, kCGEventFlagMaskCommand=8,
        kCGMouseButtonLeft=0, kCGMouseButtonRight=1, kCGMouseButtonCenter=2,
        kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2, kCGEventRightMouseDown=3, kCGEventRightMouseUp=4,
        kCGEventOtherMouseDown=5, kCGEventOtherMouseUp=6,
        kCGEventLeftMouseDragged=7, kCGEventRightMouseDragged=8, kCGEventOtherMouseDragged=9, kCGEventMouseMoved=10,
        kCGScrollEventUnitPixel=0,
        kCGMouseEventClickState=1,
        CGSessionCopyCurrentDictionary=lambda: {'console': state.console, 'login': state.login,
                                               'CGSSessionScreenIsLocked': state.locked},
        CGPreflightScreenCaptureAccess=lambda: state.screen, CGPreflightPostEventAccess=lambda: state.input,
        CGGetActiveDisplayList=lambda *args: (0, [2, 1], 2), CGMainDisplayID=lambda: 1,
        CGDisplayBounds=lambda display: rect(0, 0, 1440, 900) if display == 1 else rect(-1280, -100, 1280, 1024),
        CGDisplayPixelsWide=lambda display: 2880 if display == 1 else 1280,
        CGDisplayPixelsHigh=lambda display: 1800 if display == 1 else 1024,
        CGWindowListCopyWindowInfo=lambda *args: state.windows,
        CGEventCreate=lambda source: {}, CGEventGetLocation=lambda event: (100, 100),
        CGEventCreateMouseEvent=lambda source, kind, point, button: {'type': kind, 'point': point, 'button': button},
        CGEventCreateKeyboardEvent=lambda source, key, down: {'key': key, 'down': down},
        CGEventCreateScrollWheelEvent=lambda source, unit, axes, dy, dx: {'wheel': (dy, dx)},
        CGEventKeyboardSetUnicodeString=lambda event, count, units: event.update(
            text=b''.join(ord(unit).to_bytes(2, 'little') for unit in units).decode('utf-16-le'), units=count),
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventSetIntegerValueField=lambda event, field, value: event.update(clicks=value),
        CGEventPost=lambda tap, event: state.events.append(event.copy()))
    board = SimpleNamespace(stringForType_=lambda kind: state.clipboard,
                            clearContents=lambda: setattr(state, 'clipboard', ''),
                            setString_forType_=lambda text, kind: setattr(state, 'clipboard', text) or True)

    def bitmap(image):
        def png(*args):
            with io.BytesIO() as out:
                image.save(out, format='PNG')
                return out.getvalue()
        return SimpleNamespace(representationUsingType_properties_=png)

    appkit = SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(
            frontmostApplication=lambda: SimpleNamespace(processIdentifier=lambda: state.pid))),
        NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board), NSPasteboardTypeString='string',
        NSEvent=SimpleNamespace(doubleClickInterval=lambda: 0.5),
        NSBitmapImageFileTypePNG=1,
        NSBitmapImageRep=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithCGImage_=bitmap)))
    return mac.NativeDesktop(_quartz=q, _appkit=appkit), state


@pytest.mark.parametrize('field', ['locked', 'console', 'login', 'screen', 'input'])
def test_lock_inactive_session_and_missing_permissions_refuse_without_native_input(system, field):
    native, state = system
    setattr(state, field, field == 'locked')
    with pytest.raises(Refused, match='desktop_unavailable'):
        native.monitors()
    assert state.events == []


def test_retina_monitor_coordinates_and_dragging_use_logical_display_bounds(system):
    native, state = system
    assert native.monitors() == [{'id': 0, 'width': 1440, 'height': 900, 'primary': True},
                                  {'id': 1, 'width': 1280, 'height': 1024, 'primary': False}]
    native.input([{'type': 'button', 'button': 'left', 'down': True},
                  {'type': 'move', 'monitor': 1, 'x': 1, 'y': 0},
                  {'type': 'button', 'button': 'left', 'down': False}])
    assert state.events[1]['point'] == (-1, -100)
    assert state.events[1]['type'] == native.q.kCGEventLeftMouseDragged
    native.release()
    assert len(state.events) == 3


def test_double_click_counts_and_release_attempts_all_held_keys_after_one_failure(system):
    native, state = system
    native._now = lambda: 10
    for _ in range(2):
        native.input([{'type': 'button', 'button': 'left', 'down': True},
                      {'type': 'button', 'button': 'left', 'down': False}])
    assert [item['clicks'] for item in state.events] == [1, 1, 2, 2]
    native.input([{'type': 'key', 'code': 'ShiftLeft', 'down': True},
                  {'type': 'key', 'code': 'MetaLeft', 'down': True}])
    attempts = []
    def post(event):
        attempts.append(event)
        if len(attempts) == 1:
            raise Refused('desktop_unavailable')
    native._post = post
    native.release()
    assert len(attempts) == 2 and native._keys == set()


def test_command_shortcut_unicode_scroll_and_release_without_screen_permission(system):
    native, state = system
    native.input([{'type': 'key', 'code': 'MetaLeft', 'down': True},
                  {'type': 'key', 'code': 'KeyC', 'down': True},
                  {'type': 'key', 'code': 'KeyC', 'down': False},
                  {'type': 'key', 'code': 'MetaLeft', 'down': False},
                  {'type': 'text', 'text': 'Я🙂'}, {'type': 'wheel', 'dx': 120, 'dy': 240},
                  {'type': 'key', 'code': 'ShiftRight', 'down': True}])
    assert [item['flags'] for item in state.events[:4]] == [8, 8, 8, 0]
    assert [(item['text'], item['units']) for item in state.events[4:8]] == [('Я', 1), ('Я', 1), ('🙂', 2), ('🙂', 2)]
    assert state.events[8]['wheel'] == (-240, -120)
    state.locked, state.input = True, False
    native.release()
    assert state.events[-1] == {'key': mac.KEYS['ShiftRight'], 'down': False, 'flags': 0}


def test_entire_invalid_batch_is_refused_before_any_event(system):
    native, state = system
    with pytest.raises(Refused, match='unsupported'):
        native.input([{'type': 'key', 'code': 'KeyA', 'down': True},
                      {'type': 'key', 'code': 'Insert', 'down': True}])
    assert state.events == []


def test_multiline_text_uses_return_and_tab_keys_without_losing_unicode(system):
    native, state = system
    native.input([{'type': 'text', 'text': 'Я\r\n\t🙂'}])
    assert [event['key'] for event in state.events] == [mac.KEYS[code] for code in
        ('Space', 'Space', 'Enter', 'Enter', 'Tab', 'Tab', 'Space', 'Space')]
    assert [event.get('text') for event in state.events] == ['Я', 'Я', None, None, None, None, '🙂', '🙂']


@pytest.mark.parametrize('foreground', [True, False])
def test_remote_input_cannot_reach_agents_window_or_menu(system, foreground):
    native, state = system
    if foreground:
        state.pid = os.getpid()
    else:
        state.windows = [{'pid': os.getpid(), 'bounds': {'X': 0, 'Y': 0, 'Width': 200, 'Height': 200}}]
    with pytest.raises(Refused, match='outside'):
        native.input([{'type': 'button', 'button': 'left', 'down': True}])
    assert state.events == []


def test_clipboard_unicode_bounds_and_roundtrip(system):
    native, state = system
    assert native.clipboard() == 'текст'
    assert native.clipboard('Я🙂') == 'Я🙂'
    assert native.clipboard() == 'Я🙂'
    with pytest.raises(Refused, match='too_large'):
        native.clipboard('🙂' * 32769)
    assert state.clipboard == 'Я🙂'


def test_frames_stay_in_memory_resize_retina_and_recheck_lock(system):
    native, state = system
    with Image.new('RGB', (2880, 1800), 'red') as picture:
        def capture(display, width, height):
            state.captures.append((display, width, height))
            return picture
        native._capture = capture
        frame = native.frame(0, max_edge=800)
        assert state.captures == [(1, 800, 500)]
        assert (frame['width'], frame['height'], frame['screen_width'], frame['screen_height']) == (800, 500, 1440, 900)
        with Image.open(io.BytesIO(base64.b64decode(frame['image']))) as image:
            assert image.format == 'JPEG' and image.size == (800, 500)

        def lock(*args):
            state.locked = True
            return picture
        native._capture = lock
        with pytest.raises(Refused, match='desktop_unavailable'):
            native.frame(0)


def test_screencapturekit_selects_display_and_limits_capture_size(system, monkeypatch):
    native, state = system
    seen = {}
    config = SimpleNamespace(setWidth_=lambda value: seen.update(width=value),
                             setHeight_=lambda value: seen.update(height=value),
                             setShowsCursor_=lambda value: seen.update(cursor=value))
    def select(display, windows):
        seen['display'] = display.displayID()
        return 'filter'
    def capture(filter_, config, callback):
        assert filter_ == 'filter'
        callback('image-in-memory', None)
    sc = SimpleNamespace(
        SCShareableContent=SimpleNamespace(
            getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=lambda a, b, callback:
                callback(SimpleNamespace(displays=lambda: [SimpleNamespace(displayID=lambda: 2)]), None)),
        SCContentFilter=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithDisplay_excludingWindows_=select)),
        SCStreamConfiguration=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: config)),
        SCScreenshotManager=SimpleNamespace(captureImageWithFilter_configuration_completionHandler_=capture))
    monkeypatch.setitem(sys.modules, 'ScreenCaptureKit', sc)
    assert native._capture_image(2, 800, 640) == 'image-in-memory'
    assert seen == {'display': 2, 'width': 800, 'height': 640, 'cursor': True}
    sc.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_ = lambda a, b, callback: callback(None, 'denied')
    with pytest.raises(Refused, match='desktop_unavailable'):
        native._capture_image(2, 800, 640)
    del sc.SCScreenshotManager
    native.q.CGDisplayCreateImage = lambda display: ('legacy-in-memory', display)
    assert native._capture_image(2, 800, 640) == ('legacy-in-memory', 2)


@pytest.mark.asyncio
async def test_desktop_selects_mac_backend_and_revokes_on_permission_loss(system, monkeypatch):
    native, state = system
    monkeypatch.setattr(mac, 'NativeDesktop', lambda: native)
    service = desktop.Desktop(True)
    try:
        started = await service.start('a' * 32)
        assert started['monitors'][0]['width'] == 1440
        state.input = False
        with pytest.raises(Refused, match='desktop_unavailable'):
            await service.input('a' * 32, started['control'], [{'type': 'key', 'code': 'KeyA', 'down': True}])
        assert service.status()['active'] is False
    finally:
        await service.close()
