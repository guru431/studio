"""Окно «Studio Agent — настройка»: `Toplevel` скрытого корня Tk; привязка по сети — в своём потоке.

Пароль после отправки стирается из поля; окно одно — повторный вызов поднимает открытое."""

import logging
import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import autostart

log = logging.getLogger(__name__)

TITLE = "Studio Agent — настройка"
NOTE_PASSWORD = "Пароль студии нужен один раз для привязки и на компьютере не хранится."
NOTE_NEW = "Новая привязка заводит новый компьютер в студии — прежний отзовите в профиле студии."
FAILED = "ошибка моста"
WRAP = 360

_window: tk.Toplevel | None = None


def open_setup(root: tk.Tk, controller) -> tk.Toplevel:
    global _window
    if _window is not None and _window.winfo_exists():
        _raise(root, _window)
        return _window
    cfg = controller.config()
    enrolled = controller.enrolled
    win = _window = tk.Toplevel(root)
    win.title(TITLE)
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=12)
    frame.grid(sticky="nsew")
    frame.columnconfigure(1, weight=1)

    url = tk.StringVar(value=cfg.url)
    login, password = tk.StringVar(), tk.StringVar()
    name = tk.StringVar(value=cfg.name or os.environ.get("COMPUTERNAME", ""))
    folder = tk.StringVar(value=cfg.projects_root)
    pin, pin2 = tk.StringVar(), tk.StringVar()
    no_pin = tk.BooleanVar(value=False)
    try:
        auto = tk.BooleanVar(value=autostart.enabled() if enrolled else cfg.autostart)
    except OSError:
        auto = tk.BooleanVar(value=cfg.autostart)
    errors = tk.StringVar()

    row = 0

    def field(label: str, var: tk.StringVar, secret: bool = False) -> ttk.Entry:
        nonlocal row
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        entry = ttk.Entry(frame, textvariable=var, width=40, show="*" if secret else "")
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        row += 1
        return entry

    def note(text: str) -> None:
        nonlocal row
        ttk.Label(frame, text=text, wraplength=WRAP, foreground="#555").grid(row=row, column=0, columnspan=3,
                                                                             sticky="w", pady=(0, 6))
        row += 1

    field("Адрес студии", url)
    first = field("Логин", login)
    field("Пароль", password, secret=True)
    note(NOTE_PASSWORD)
    field("Имя компьютера", name)
    field("Папка проектов", folder)

    def browse() -> None:
        chosen = filedialog.askdirectory(parent=win, initialdir=folder.get() or None, mustexist=True)
        if chosen:
            folder.set(os.path.normpath(chosen))

    ttk.Button(frame, text="Обзор…", command=browse).grid(row=row - 1, column=2, padx=(6, 0))
    field("PIN (4–8 цифр, по желанию)", pin, secret=True)
    field("PIN ещё раз", pin2, secret=True)
    ttk.Checkbutton(frame, text="Убрать PIN", variable=no_pin).grid(row=row, column=1, sticky="w")
    row += 1
    ttk.Checkbutton(frame, text="Запускать при входе в Windows", variable=auto).grid(row=row, column=1, sticky="w")
    row += 1
    if enrolled:
        note(NOTE_NEW)
    ttk.Label(frame, textvariable=errors, foreground="#b00020", wraplength=WRAP).grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
    row += 1

    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(10, 0))
    main = ttk.Button(buttons, text="Сохранить" if enrolled else "Привязать")
    unlock = ttk.Button(buttons, text="Снять блок PIN")
    cancel = ttk.Button(buttons, text="Отмена", command=win.destroy)
    for b in (main, unlock, cancel):
        b.pack(side="left", padx=(6, 0))
    if not controller.pin_locked:
        unlock.state(["disabled"])

    def reset() -> None:
        controller.reset_pin()
        unlock.state(["disabled"])

    unlock.configure(command=reset)

    def submit() -> None:
        form = {"url": url.get(), "login": login.get(), "password": password.get(), "name": name.get(),
                "projects_root": folder.get(), "pin": pin.get(), "pin2": pin2.get(), "no_pin": no_pin.get(),
                "autostart": auto.get()}
        password.set("")
        errors.set("")
        main.state(["disabled"])
        done: dict = {}

        def work() -> None:  # привязка ходит в сеть до 30 с — не в потоке Tk
            try:
                done["errors"] = controller.apply_setup(form)
            except Exception as e:
                log.warning("настройка: %s", type(e).__name__)
                done["errors"] = [FAILED]

        threading.Thread(target=work, name="studio-agent-setup", daemon=True).start()

        def check() -> None:
            if not win.winfo_exists():
                return
            if "errors" not in done:
                root.after(100, check)
                return
            main.state(["!disabled"])
            if done["errors"]:
                errors.set("\n".join(done["errors"]))
            else:
                win.destroy()

        root.after(100, check)

    main.configure(command=submit)
    win.bind("<Return>", lambda e: submit() if main.instate(["!disabled"]) else None)
    win.bind("<Escape>", lambda e: win.destroy())
    _raise(root, win)
    (first if not enrolled else main).focus_set()
    return win


def _raise(root: tk.Tk, win: tk.Toplevel) -> None:
    """Поверх прочих окон: корень скрыт, а окно открывает трей или автозапуск."""
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    root.after(300, lambda: win.winfo_exists() and win.attributes("-topmost", False))
    win.focus_force()
