"""Значок в трее (pystray, свой поток): круг цвета состояния, подсказка и меню; команды меню — в очередь Tk."""

import threading
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw

from .app import TITLE, WORDS

GREEN, GREY, RED = (46, 160, 67), (140, 140, 140), (200, 40, 40)
COLORS = {"online": GREEN, "connecting": GREY, "offline": GREY, "setup": GREY, "revoked": RED, "replaced": RED}
SIZE = 64


def image(state: str) -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((4, 4, SIZE - 5, SIZE - 5), fill=COLORS.get(state, GREY) + (255,))
    return img


def title(state: str) -> str:
    return f"{TITLE} — {WORDS.get(state, state)}"


class Tray:
    """`post(команда)` — в очередь главного потока: `setup`, `reset_pin`, `log`, `quit`."""

    def __init__(self, state: str, post: Callable[[str], None], pin_locked: Callable[[], bool]):
        menu = pystray.Menu(
            pystray.MenuItem("Настройка…", lambda: post("setup"), default=True),
            pystray.MenuItem("Снять блок PIN", lambda: post("reset_pin"), enabled=lambda item: pin_locked()),
            pystray.MenuItem("Открыть журнал", lambda: post("log")),
            pystray.MenuItem("Выход", lambda: post("quit")))
        self.icon = pystray.Icon("studio-agent", image(state), title(state), menu)

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="studio-agent-tray", daemon=True).start()

    def show(self, state: str) -> None:
        self.icon.icon = image(state)
        self.icon.title = title(state)

    def stop(self) -> None:
        self.icon.stop()
