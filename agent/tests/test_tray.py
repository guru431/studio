"""Значок трея, подсказка, строка меню и уведомления — без pystray и Tk."""

from studio_agent import tray


def test_tray_title_with_and_without_login():
    assert tray.title("online", "ivanov") == "Studio Agent — на связи · ivanov"
    assert tray.title("online", "") == "Studio Agent — на связи"
    assert tray.title("replaced") == "Studio Agent — ключ используется другим мостом"
    long = tray.title("replaced", "л" * 200)
    assert len(long) == 127 and long.endswith("…")


def test_status_line():
    assert tray.status_line("online", "ivanov") == "● На связи — ivanov"
    assert tray.status_line("offline", "") == "● Нет связи"
    assert tray.status_line("connecting", "owner") == "● Подключается… — owner"


def test_tray_image_has_status_dot():
    assert tray.COLORS["online"] != tray.COLORS["offline"] != tray.COLORS["revoked"] != tray.COLORS["connecting"]
    assert tray.COLORS["setup"] == tray.COLORS["connecting"] and tray.COLORS["replaced"] == tray.COLORS["revoked"]
    centre = round(tray.DOT_C * tray.SIZE / tray.CANVAS)
    for state, colour in tray.COLORS.items():
        img = tray.image(state)
        assert img.size == (64, 64) and img.mode == "RGBA"
        assert img.getpixel((centre, centre)) == colour + (255,), state
    img = tray.image("online")
    assert img.getpixel((0, 0))[3] < 64                                 # скруглённый угол — прозрачный
    r, g, b, a = img.getpixel((4, 32))                                  # левый край — градиент значка студии
    assert a == 255 and r > g > b and r > 180
    assert img.getpixel((24, 22))[:3] == (255, 255, 255)                # облачко
    assert img.getpixel((32, 30))[:3] == tray.STAR                      # звезда
    rim = round((tray.DOT_C - tray.DOT_R - tray.DOT_RIM / 2) * tray.SIZE / tray.CANVAS)
    assert img.getpixel((rim, centre))[:3] == (255, 255, 255)           # обводка точки
    assert tray.image("online") is not tray.image("online")             # копия: кэш не портится


def test_icon_without_dot_for_the_window():
    img = tray.icon(32)
    assert img.size == (32, 32)
    assert all(abs(a - b) <= 4 for a, b in zip(img.getpixel((16, 15))[:3], tray.STAR))


def test_notice_only_for_first_online_and_studio_closing():
    assert tray.notice("online", True) == "Компьютер подключён к студии"
    assert tray.notice("online", False) is None
    assert tray.notice("revoked", False) == "Компьютер отозван в студии"
    assert tray.notice("replaced", True) == "Ключ используется другим мостом"
    for state in ("offline", "connecting", "setup"):
        assert tray.notice(state, True) is None


def test_local_activity_menu_routes_desktop_and_exit_actions_without_private_details(monkeypatch):
    import sys
    from types import SimpleNamespace

    class Menu(tuple):
        SEPARATOR = object()

        def __new__(cls, *items):
            return super().__new__(cls, items)

    def item(text, action, **kwargs):
        return SimpleNamespace(text=text, action=action, enabled=kwargs.get('enabled', True),
                               visible=kwargs.get('visible', True))

    monkeypatch.setitem(sys.modules, 'pystray', SimpleNamespace(Menu=Menu, MenuItem=item,
        Icon=lambda name, image, title, menu: SimpleNamespace(title=title, menu=menu)))
    monkeypatch.setattr(tray.Tray, 'refresh', lambda self: None)
    commands = []
    icon = tray.Tray('online', commands.append, lambda: False, 'owner')
    icon.show_activity({'tasks': 3, 'waiting': 1, 'desktop_active': True, 'quit_when_idle': False})
    items = [entry for entry in icon.icon.menu if entry is not Menu.SEPARATOR]

    def text(entry):
        return entry.text(entry) if callable(entry.text) else entry.text

    assert 'заданий: 3' in icon.icon.title and 'ждут: 1' in icon.icon.title
    stop = next(entry for entry in items if text(entry) == 'Завершить этот сеанс')
    assert stop.enabled(stop)
    stop.action()
    next(entry for entry in items if text(entry).startswith('Выйти после')).action()
    next(entry for entry in items if text(entry).startswith('Выход сейчас')).action()
    assert commands == ['stop_desktop', 'quit_idle', 'quit']
    cancel = next(entry for entry in items if text(entry) == 'Отменить обновление')
    assert not cancel.visible(cancel) and not cancel.enabled(cancel)
    icon.show_activity({'tasks': 0, 'waiting': 0, 'desktop_active': False, 'quit_when_idle': True, 'updating': True})
    assert cancel.visible(cancel) and cancel.enabled(cancel)
    cancel.action()
    assert commands[-1] == 'cancel_update'
    assert not stop.enabled(stop)
    assert any(text(entry) == 'Отменить выход после завершения' for entry in items)
    icon.show('replaced', 'x' * 200)
    assert len(icon.icon.title) <= tray.TITLE_MAX
