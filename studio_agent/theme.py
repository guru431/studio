"""Вид окон моста — палитра и шрифты студии (`app/static/css/app.css`): светлая «Бумага», тёмная «Графит».

Тема — по Windows (`AppsUseLightTheme`); ttk — на `clam` со своими стилями. Палитра и стили — только здесь: окна
берут их отсюда. Tk импортируется в `apply` — палитру читают и тесты без окна."""

import contextlib
import ctypes
import winreg

PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
DWMWA_USE_IMMERSIVE_DARK_MODE = 20

# Токены студии дословно (спека §5.3); `warn` («нет связи»), `field` (поле ввода) и `edge` (рамка полей и кнопок:
# `border` «Графита» на его фоне почти не видна) — свои, в тон палитре.
PAPER = {"bg": "#fbfaf7", "panel": "#ffffff", "text": "#1f1d1a", "muted": "#7b766d", "border": "#e3dfd6",
         "accent": "#c96442", "on_accent": "#ffffff", "ok": "#2f9e6f", "danger": "#b42318",
         "warn": "#b7791f", "field": "#ffffff", "edge": "#d6d1c6"}
GRAPHITE = {"bg": "#1b1b1b", "panel": "#262626", "text": "#ececec", "muted": "#8f8f8f", "border": "#2a2a2a",
            "accent": "#ececec", "on_accent": "#111111", "ok": "#3ecf8e", "danger": "#f87171",
            "warn": "#f5b94a", "field": "#1f1f1f", "edge": "#3d3d3d"}
FONTS = ("Segoe UI Variable Text", "Segoe UI")  # первый, что есть
SIZE = 10


def palette(dark: bool) -> dict[str, str]:
    return dict(GRAPHITE if dark else PAPER)


def is_dark() -> bool:
    """Тёмная тема приложений Windows (`AppsUseLightTheme` = 0); нет значения — светлая."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def dark_title(win, dark: bool) -> None:
    """Тёмный заголовок окна (Windows 10 20H1+, 11); не вышло — светлый, как есть."""
    with contextlib.suppress(Exception):
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value),
                                                   ctypes.sizeof(value))


def apply(root, dark: bool | None = None) -> dict:
    """Стили ttk корня (общие для всех его окон) → палитра; в ней же `font` — семейство шрифта и `dark`."""
    import tkinter.font as tkfont
    from tkinter import ttk

    dark = is_dark() if dark is None else dark
    c = palette(dark)
    families = set(tkfont.families(root))
    family = next((f for f in FONTS if f in families), "TkDefaultFont")
    c["font"], c["dark"] = family, dark
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        with contextlib.suppress(Exception):
            tkfont.nametofont(name, root).configure(family=family, size=SIZE)
    base, bold = (family, SIZE), (family, SIZE, "bold")
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=c["bg"], foreground=c["text"], bordercolor=c["border"], darkcolor=c["bg"],
                lightcolor=c["bg"], troughcolor=c["border"], focuscolor=c["accent"], selectbackground=c["accent"],
                selectforeground=c["on_accent"], insertcolor=c["text"], font=base)
    s.configure("TFrame", background=c["bg"])
    s.configure("Card.TFrame", background=c["panel"])
    s.configure("TLabel", background=c["bg"], foreground=c["text"])
    for area, bg in (("", c["bg"]), ("Card.", c["panel"])):
        s.configure(f"{area}TLabel", background=bg, foreground=c["text"])
        s.configure(f"{area}Muted.TLabel", background=bg, foreground=c["muted"])
        s.configure(f"{area}Title.TLabel", background=bg, foreground=c["text"], font=(family, SIZE + 4, "bold"))
        s.configure(f"{area}Head.TLabel", background=bg, foreground=c["text"], font=bold)
        s.configure(f"{area}Status.TLabel", background=bg, foreground=c["text"], font=(family, SIZE + 3, "bold"))
        s.configure(f"{area}Error.TLabel", background=bg, foreground=c["danger"])
        s.configure(f"{area}TCheckbutton", background=bg, foreground=c["text"], indicatorbackground=c["field"],
                    indicatorforeground=c["text"], indicatorrelief="flat", bordercolor=c["edge"])
        s.map(f"{area}TCheckbutton", background=[("active", bg)],
              indicatorbackground=[("pressed", c["border"]), ("selected", c["field"])])
    s.configure("Badge.TLabel", background=c["border"], foreground=c["muted"], padding=(8, 2))
    s.configure("TEntry", fieldbackground=c["field"], foreground=c["text"], bordercolor=c["edge"],
                lightcolor=c["field"], darkcolor=c["field"], insertcolor=c["text"], padding=(6, 4))
    s.map("TEntry", bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["field"])],
          fieldbackground=[("readonly", c["bg"]), ("disabled", c["bg"])],
          foreground=[("disabled", c["muted"])])
    button = {"padding": (14, 5), "relief": "solid", "borderwidth": 1, "focusthickness": 0, "font": base}
    s.configure("TButton", background=c["panel"], foreground=c["text"], bordercolor=c["edge"],
                lightcolor=c["panel"], darkcolor=c["panel"], **button)
    s.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["border"]), ("active", c["bg"])],
          lightcolor=[("pressed", c["border"]), ("active", c["bg"])],
          darkcolor=[("pressed", c["border"]), ("active", c["bg"])],
          foreground=[("disabled", c["muted"])], bordercolor=[("focus", c["muted"])])
    s.configure("Accent.TButton", background=c["accent"], foreground=c["on_accent"], bordercolor=c["accent"],
                lightcolor=c["accent"], darkcolor=c["accent"], **{**button, "font": bold})
    s.map("Accent.TButton", background=[("disabled", c["border"]), ("pressed", c["muted"]), ("active", c["muted"])],
          lightcolor=[("disabled", c["border"]), ("pressed", c["muted"]), ("active", c["muted"])],
          darkcolor=[("disabled", c["border"]), ("pressed", c["muted"]), ("active", c["muted"])],
          bordercolor=[("disabled", c["border"]), ("active", c["muted"])],
          foreground=[("disabled", c["muted"])])
    s.configure("Danger.TButton", background=c["panel"], foreground=c["danger"], bordercolor=c["danger"],
                lightcolor=c["panel"], darkcolor=c["panel"], **button)
    s.map("Danger.TButton", background=[("pressed", c["border"]), ("active", c["bg"])],
          lightcolor=[("pressed", c["border"]), ("active", c["bg"])],
          darkcolor=[("pressed", c["border"]), ("active", c["bg"])],
          foreground=[("disabled", c["muted"])], bordercolor=[("disabled", c["border"])])
    root.configure(background=c["bg"])
    root.option_add("*Toplevel.background", c["bg"])
    return c
