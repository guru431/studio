"""Значок трея, подсказка, строка меню и уведомления — без pystray и Tk."""

from studio_agent import tray


def test_tray_title_with_and_without_login():
    assert tray.title("online", "example-user") == "Studio Agent — на связи · example-user"
    assert tray.title("online", "") == "Studio Agent — на связи"
    assert tray.title("replaced") == "Studio Agent — ключ используется другим мостом"
    long = tray.title("replaced", "л" * 200)
    assert len(long) == 127 and long.endswith("…")


def test_status_line():
    assert tray.status_line("online", "example-user") == "● На связи — example-user"
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
