"""Значок в трее (pystray, свой поток): рисунок значка студии с точкой состояния, подсказка с логином, меню и
уведомления Windows; команды меню — в очередь Tk.

Рисунок — `icons/icon.svg` студии (viewBox 512): скруглённый квадрат с диагональным градиентом, белое облачко,
четырёхлучевая звезда. Рисуется Pillow-ом в 512 px (кривые — многоугольником) и уменьшается: края гладкие."""

import contextlib
import functools
import math
import threading
from collections.abc import Callable

from PIL import Image, ImageChops, ImageDraw, ImageOps

from .app import TITLE, WORDS

GREEN, GREY, YELLOW, RED = (47, 158, 111), (143, 143, 143), (232, 168, 26), (220, 38, 38)
COLORS = {"online": GREEN, "connecting": GREY, "setup": GREY, "offline": YELLOW, "revoked": RED, "replaced": RED}
SIZE = 64
CANVAS = 512  # рисунок — в единицах viewBox студии
DOT_R, DOT_RIM = 104, 26  # точка состояния и её белая обводка — в единицах CANVAS
DOT_C = CANVAS - DOT_R - DOT_RIM - 4  # центр точки — правый нижний угол
TITLE_MAX = 127  # подсказка значка Windows (szTip — 128 с нулём)
TOP, BOTTOM = (0xE2, 0x82, 0x5C), (0xB2, 0x4F, 0x2B)  # градиент значка студии
STAR = (0xC9, 0x64, 0x42)
ONLINE_FIRST = "Компьютер подключён к студии"
NOTICES = {"revoked": "Компьютер отозван в студии", "replaced": "Ключ используется другим мостом"}


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 24) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
             cy + r * math.sin(math.radians(start + (end - start) * i / steps))) for i in range(steps + 1)]


def _cubic(p0, p1, p2, p3, steps: int = 24) -> list[tuple[float, float]]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _bubble() -> list[tuple[float, float]]:
    """`M168 108 H344 A84… 428 192 V300 A84… 344 384 H236 L152 446 L168 384 A84… 84 300 V192 A84… 168 108Z`."""
    return [(168, 108), *_arc(344, 192, 84, -90, 0), *_arc(344, 300, 84, 0, 90), (236, 384), (152, 446), (168, 384),
            *_arc(168, 300, 84, 90, 180), *_arc(168, 192, 84, 180, 270)]


def _star() -> list[tuple[float, float]]:
    """`M256 158 C266 222 286 236 340 246 C286 256 266 270 256 334 C246 270 226 256 172 246 C226 236 246 222 256 158Z`."""
    points = [(256, 158)]
    for c1, c2, end in (((266, 222), (286, 236), (340, 246)), ((286, 256), (266, 270), (256, 334)),
                        ((246, 270), (226, 256), (172, 246)), ((226, 236), (246, 222), (256, 158))):
        points += _cubic(points[-1], c1, c2, end)
    return points


@functools.lru_cache(maxsize=None)
def _canvas() -> Image.Image:
    """Значок студии в 512 px (RGBA)."""
    v = Image.linear_gradient("L").resize((CANVAS, CANVAS))
    diagonal = ImageChops.add(v, v.transpose(Image.Transpose.TRANSPOSE), scale=2)  # (x + y) / 2: x1=y1=0 → x2=y2=1
    img = ImageOps.colorize(diagonal, TOP, BOTTOM).convert("RGBA")
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, CANVAS - 1, CANVAS - 1), radius=112, fill=255)
    img.putalpha(mask)
    draw = ImageDraw.Draw(img)
    draw.polygon(_bubble(), fill=(255, 255, 255, 255))
    draw.polygon(_star(), fill=STAR + (255,))
    return img


@functools.lru_cache(maxsize=None)
def icon(size: int = SIZE) -> Image.Image:
    """Значок студии без точки (окно: `iconphoto`, шапка). Общий — не менять."""
    return _canvas().resize((size, size), Image.Resampling.LANCZOS)


@functools.lru_cache(maxsize=None)
def _image(state: str) -> Image.Image:
    img = _canvas().copy()
    draw = ImageDraw.Draw(img)
    rim = DOT_R + DOT_RIM
    draw.ellipse((DOT_C - rim, DOT_C - rim, DOT_C + rim, DOT_C + rim), fill=(255, 255, 255, 255))
    draw.ellipse((DOT_C - DOT_R, DOT_C - DOT_R, DOT_C + DOT_R, DOT_C + DOT_R), fill=COLORS.get(state, GREY) + (255,))
    return img.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def image(state: str) -> Image.Image:
    """Значок трея: рисунок студии и точка цвета состояния в правом нижнем углу."""
    return _image(state).copy()


def title(state: str, login: str = "") -> str:
    """«Studio Agent — на связи · ivanov»; без логина — без хвоста; не длиннее `TITLE_MAX`."""
    text = f"{TITLE} — {WORDS.get(state, state)}" + (f" · {login}" if login else "")
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


def status_line(state: str, login: str = "") -> str:
    """Первая строка меню: «● На связи — ivanov»."""
    words = WORDS.get(state, state)
    words = words[:1].upper() + words[1:]
    return f"● {words} — {login}" if login else f"● {words}"


def notice(state: str, fresh: bool) -> str | None:
    """Уведомление Windows на переход в `state`: первый `online` после привязки, отзыв, замена; обрывы — без."""
    if state == "online":
        return ONLINE_FIRST if fresh else None
    return NOTICES.get(state)


class Tray:
    """`post(команда)` — в очередь главного потока: `open`, `reset_pin`, `log`, `quit`."""

    def __init__(self, state: str, post: Callable[[str], None], pin_locked: Callable[[], bool], login: str = ""):
        import pystray

        self.state, self.login = state, login
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: status_line(self.state, self.login), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть Studio Agent", lambda: post("open"), default=True),
            pystray.MenuItem("Снять блок PIN", lambda: post("reset_pin"), enabled=lambda item: pin_locked()),
            pystray.MenuItem("Открыть журнал", lambda: post("log")),
            pystray.MenuItem("Выход", lambda: post("quit")))
        self.icon = pystray.Icon("studio-agent", image(state), title(state, login), menu)

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="studio-agent-tray", daemon=True).start()

    def show(self, state: str, login: str) -> None:
        self.state, self.login = state, login
        self.icon.icon = image(state)
        self.icon.title = title(state, login)
        self.refresh()

    def refresh(self) -> None:
        """Меню заново: строка состояния и «Снять блок PIN» (pystray на Windows строит меню заранее); в своём
        потоке — `pin_locked` читает `pin.bin` (DPAPI)."""
        def run() -> None:
            with contextlib.suppress(Exception):
                self.icon.update_menu()

        threading.Thread(target=run, name="studio-agent-tray-menu", daemon=True).start()

    def notify(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.icon.notify(text, TITLE)

    def stop(self) -> None:
        self.icon.stop()
