"""Окно «Studio Agent» (`Toplevel` скрытого корня Tk): вид «Вход» до привязки и «Подключение» после; окна PIN и
подтверждений — `Toplevel` той же темы (`theme`).

Окно одно: повторный `open_main` поднимает открытое, состояние ядра приходит `update(state)` из очереди Tk
(`app.main`). Всё блокирующее (сеть, DPAPI, scrypt, реестр) — `Controller` в своём потоке, окно ждёт итог
`root.after`. Пароль и PIN стираются из полей сразу после нажатия. Вид — чистые функции (`view_of`, `STATUS`,
`status_of`, `pin_row`, `pin_dialog_errors`, `host_of`): их проверяют тесты без Tk."""

import logging
import os
import threading
from urllib.parse import urlsplit

from . import setup, theme

log = logging.getLogger(__name__)

TITLE = "Studio Agent"
NOT_LINKED = "Не подключён"
NOTE_PASSWORD = "Пароль нужен один раз и на компьютере не хранится"
CONFIRM_OFF = ("Отключить компьютер от студии? Студия забудет этот компьютер, идущие ходы прервутся. Чаты Claude Code "
               "останутся на компьютере.")
CONFIRM_PIN_OFF = "Убрать PIN? Компьютер откроется в студии без PIN."
FAILED = "ошибка моста"
DASH = "—"
# Состояние ядра → (слова, токен цвета палитры).
STATUS = {"online": ("На связи", "ok"), "connecting": ("Подключается…", "muted"),
          "offline": ("Нет связи — переподключаюсь", "warn"), "revoked": ("Компьютер отозван в студии", "danger"),
          "replaced": ("Ключ используется другим мостом", "danger"), "setup": (NOT_LINKED, "muted")}
PIN_WORDS = {"none": "PIN не задан", "set": "PIN задан", "locked": "PIN заблокирован после 5 ошибок"}
PIN_BUTTONS = {"none": ("set",), "set": ("change", "clear"), "locked": ("unlock", "change")}
BUTTONS = {"set": "Задать PIN", "change": "Изменить PIN", "clear": "Убрать PIN", "unlock": "Снять блок"}
DOWN = ("revoked", "replaced")  # канал погашен студией — «Войти заново» вместо «Отключиться»

_window = None


def view_of(enrolled: bool) -> str:
    """`link` — «Подключение» (есть ключ), `login` — «Вход»."""
    return "link" if enrolled else "login"


def status_of(state: str) -> tuple[str, str]:
    """Слова и цвет карточки «Подключение»; `setup` здесь — только что привязан, канал ещё не поднят."""
    return STATUS["connecting"] if state == "setup" else STATUS.get(state, STATUS["connecting"])


def pin_row(status: str | None) -> tuple[str, tuple[str, ...]]:
    """Строка PIN и кнопки (`set`, `change`, `clear`, `unlock`); `None` — ещё не прочитан."""
    if status not in PIN_WORDS:
        return "", ()
    return PIN_WORDS[status], PIN_BUTTONS[status]


def pin_dialog_errors(pin: str, pin2: str) -> list[str]:
    """Окно «Задать / Изменить PIN»: пустой — не PIN."""
    return [setup.PIN_WORDS] if not pin else setup.pin_errors(pin, pin2)


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or url or DASH
    except ValueError:
        return url or DASH


def open_main(root, controller):
    """Окно моста; открыто — поднять."""
    global _window
    if _window is not None and _window.alive():
        _window.lift()
        return _window
    _window = MainWindow(root, controller)
    return _window


open_setup = open_main  # прежнее имя


def update(state: str) -> None:
    """Состояние ядра (из очереди Tk): открытое окно перерисовывается."""
    if _window is not None and _window.alive():
        _window.on_state(state)


def pin_changed() -> None:
    """PIN сменили мимо окна (трей «Снять блок PIN»): открытое окно перечитывает строку PIN."""
    if _window is not None and _window.alive() and _window.enrolled:
        _window.refresh_pin()


def prepare(root, dark: bool | None = None) -> dict:
    """Тема корня и значок окон — один раз; итог — палитра."""
    c = getattr(root, "_studio_palette", None)
    if c is None or (dark is not None and c["dark"] != dark):
        from PIL import ImageTk

        from . import tray

        c = root._studio_palette = theme.apply(root, dark)
        root._studio_icons = [ImageTk.PhotoImage(tray.icon(n), master=root) for n in (64, 32, 16)]
        root.iconphoto(True, *root._studio_icons)
    return c


class MainWindow:
    def __init__(self, root, controller):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root, self.ctrl = root, controller
        self.c = prepare(root)
        self.scale = root.winfo_fpixels("1i") / 96
        self.state = controller.state
        self.enrolled = controller.state != "setup"
        self.notice = ""  # над формой входа: студия не забыла компьютер при отвязке
        self.pin: str | None = None
        self.win = tk.Toplevel(root, background=self.c["bg"])
        self.win.title(TITLE)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        theme.dark_title(self.win, self.c["dark"])
        self.body = None
        self.render()
        self.lift()
        if self.enrolled:
            self.refresh_pin()

    # --- общее ---

    def px(self, n: float) -> int:
        return round(n * self.scale)

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except self.tk.TclError:
            return False

    def close(self) -> None:
        if self.alive():
            self.win.destroy()

    def lift(self) -> None:
        """Поверх прочих окон: корень скрыт, а окно открывает трей или автозапуск."""
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.root.after(300, lambda: self.alive() and self.win.attributes("-topmost", False))
        self.win.focus_force()

    def work(self, fn, done) -> None:
        """`fn` — в своём потоке (сеть, DPAPI, scrypt); `done(итог, сбой)` — в потоке Tk, если окно открыто."""
        result: dict = {}

        def run() -> None:
            try:
                result["value"] = fn()
            except Exception as e:
                log.warning("окно: %s", type(e).__name__)
                result["failed"] = True

        threading.Thread(target=run, name="studio-agent-window", daemon=True).start()

        def check() -> None:
            if not self.alive():
                return
            if not result:
                self.root.after(50, check)
                return
            done(result.get("value"), bool(result.get("failed")))

        self.root.after(50, check)

    def on_state(self, state: str) -> None:
        self.state = state
        enrolled = state != "setup"
        changed = enrolled != self.enrolled
        self.enrolled = enrolled
        if changed and enrolled:
            self.notice = ""
            self.pin = None
            self.refresh_pin()
        if changed or enrolled:  # форму входа с введённым не трогать
            self.render()

    def refresh_pin(self) -> None:
        def done(status, failed):
            self.pin = None if failed else status
            if self.enrolled:
                self.render()

        self.work(self.ctrl.pin_status, done)

    def render(self) -> None:
        ttk = self.ttk
        if self.body is not None:
            self.body.destroy()
        self.body = ttk.Frame(self.win, padding=self.px(20))
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1, minsize=self.px(400))  # ширина не скачет со сменой состояния
        self.header()
        if view_of(self.enrolled) == "login":
            self.login_view()
        else:
            self.link_view()

    def header(self) -> None:
        from PIL import ImageTk

        from . import tray

        ttk = self.ttk
        top = ttk.Frame(self.body)
        top.grid(row=0, column=0, sticky="ew", pady=(0, self.px(16)))
        top.columnconfigure(1, weight=1)
        self._logo = ImageTk.PhotoImage(tray.icon(self.px(32)), master=self.win)
        ttk.Label(top, image=self._logo).grid(row=0, column=0, padx=(0, self.px(10)))
        ttk.Label(top, text=TITLE, style="Title.TLabel").grid(row=0, column=1, sticky="w")
        if view_of(self.enrolled) == "login":
            ttk.Label(top, text=NOT_LINKED, style="Badge.TLabel").grid(row=0, column=2, sticky="e")

    def card(self, row: int):
        """Карточка: панель с границей."""
        outer = self.tk.Frame(self.body, background=self.c["panel"], highlightthickness=1,
                              highlightbackground=self.c["border"], highlightcolor=self.c["border"])
        outer.grid(row=row, column=0, sticky="ew", pady=(0, self.px(12)))
        inner = self.ttk.Frame(outer, style="Card.TFrame", padding=self.px(16))
        inner.pack(fill="both", expand=True)
        inner.columnconfigure(0, weight=1)
        return inner

    # --- «Вход» ---

    def login_view(self) -> None:
        tk, ttk = self.tk, self.ttk
        cfg = self.ctrl.config()
        row = 1
        if self.notice:
            ttk.Label(self.body, text=self.notice, style="Error.TLabel", wraplength=self.px(380)).grid(
                row=row, column=0, sticky="w", pady=(0, self.px(12)))
            row += 1
        box = self.card(row)
        box.columnconfigure(1, weight=1)
        url = tk.StringVar(value=cfg.url)
        login, password = tk.StringVar(), tk.StringVar()
        name = tk.StringVar(value=cfg.name or os.environ.get("COMPUTERNAME", ""))
        folder = tk.StringVar(value=cfg.projects_root)
        pin, pin2 = tk.StringVar(), tk.StringVar()
        auto = tk.BooleanVar(value=cfg.autostart)
        errors = tk.StringVar()
        gap = self.px(10)
        r = 0

        def field(label: str, var, *, secret: bool = False, column: int = 0, span: int = 2):
            ttk.Label(box, text=label, style="Card.TLabel").grid(row=r, column=column, columnspan=span, sticky="w",
                                                                 padx=(0 if column == 0 else gap, 0))
            entry = ttk.Entry(box, textvariable=var, width=22 if span == 1 else 46, show="•" if secret else "")
            entry.grid(row=r + 1, column=column, columnspan=span, sticky="ew", pady=(self.px(3), gap),
                       padx=(0 if column == 0 else gap, 0))
            return entry

        field("Адрес студии", url)
        r += 2
        first = field("Логин", login)
        r += 2
        secret = field("Пароль", password, secret=True)
        secret.grid_configure(pady=(self.px(3), self.px(2)))
        ttk.Label(box, text=NOTE_PASSWORD, style="Card.Muted.TLabel").grid(row=r + 2, column=0, columnspan=2,
                                                                          sticky="w", pady=(0, gap))
        r += 3
        field("Имя компьютера", name)
        r += 2
        ttk.Label(box, text="Папка проектов", style="Card.TLabel").grid(row=r, column=0, columnspan=2, sticky="w")
        line = ttk.Frame(box, style="Card.TFrame")
        line.grid(row=r + 1, column=0, columnspan=2, sticky="ew", pady=(self.px(3), gap))
        line.columnconfigure(0, weight=1)
        ttk.Entry(line, textvariable=folder).grid(row=0, column=0, sticky="ew")

        def browse() -> None:
            from tkinter import filedialog

            chosen = filedialog.askdirectory(parent=self.win, initialdir=folder.get() or None, mustexist=True)
            if chosen:
                folder.set(os.path.normpath(chosen))

        ttk.Button(line, text="Обзор…", command=browse).grid(row=0, column=1, padx=(self.px(8), 0))
        r += 2
        box.columnconfigure(0, weight=1, uniform="pin")
        box.columnconfigure(1, weight=1, uniform="pin")
        field("PIN (по желанию, 4–8 цифр)", pin, secret=True, span=1)
        field("PIN ещё раз", pin2, secret=True, column=1, span=1)
        r += 2
        ttk.Checkbutton(box, text="Запускать при входе в Windows", variable=auto, style="Card.TCheckbutton").grid(
            row=r, column=0, columnspan=2, sticky="w")
        foot = ttk.Frame(self.body)
        foot.grid(row=row + 1, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        ttk.Label(foot, textvariable=errors, style="Error.TLabel", wraplength=self.px(260), justify="left").grid(
            row=0, column=0, sticky="w")
        go = ttk.Button(foot, text="Подключить", style="Accent.TButton")
        go.grid(row=0, column=1, sticky="ne", padx=(self.px(12), 0))

        def submit() -> None:
            if not go.instate(["!disabled"]):
                return
            form = {"url": url.get(), "login": login.get(), "password": password.get(), "name": name.get(),
                    "projects_root": folder.get(), "pin": pin.get(), "pin2": pin2.get(), "no_pin": False,
                    "autostart": auto.get()}
            password.set("")
            pin.set("")
            pin2.set("")
            errors.set("")
            go.state(["disabled"])

            def done(found, failed) -> None:
                if failed or found:
                    if go.winfo_exists():
                        go.state(["!disabled"])
                        errors.set("\n".join(found) if found else FAILED)
                    return
                self.notice = ""
                self.enrolled = True
                self.render()
                self.refresh_pin()

            self.work(lambda: self.ctrl.apply_setup(form), done)

        go.configure(command=submit)
        self.win.bind("<Return>", lambda e: submit() if view_of(self.enrolled) == "login" else None)
        (first if not login.get() else secret).focus_set()

    # --- «Подключение» ---

    def link_view(self) -> None:
        ttk = self.ttk
        cfg = self.ctrl.config()
        words, token = status_of(self.state)
        box = self.card(1)
        box.columnconfigure(0, weight=0)
        box.columnconfigure(1, weight=1)
        head = ttk.Frame(box, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, self.px(12)))
        d = self.px(14)
        dot = self.tk.Canvas(head, width=d, height=d, highlightthickness=0, background=self.c["panel"])
        dot.create_oval(1, 1, d - 1, d - 1, fill=self.c[token], outline=self.c[token])
        dot.grid(row=0, column=0, padx=(0, self.px(10)))
        ttk.Label(head, text=words, style="Card.Status.TLabel").grid(row=0, column=1, sticky="w")
        login = self.ctrl.login
        for i, (label, value) in enumerate((("Студия", host_of(cfg.url)), ("Вход", login or DASH),
                                            ("Компьютер", cfg.name or DASH)), start=1):
            ttk.Label(box, text=label, style="Card.Muted.TLabel").grid(row=i, column=0, sticky="w",
                                                                       padx=(0, self.px(16)), pady=self.px(2))
            ttk.Label(box, text=value, style="Card.TLabel").grid(row=i, column=1, sticky="w", pady=self.px(2))
        bar = ttk.Frame(box, style="Card.TFrame")
        bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(self.px(14), 0))
        bar.columnconfigure(0, weight=1)
        problem = self.tk.StringVar()
        ttk.Label(bar, textvariable=problem, style="Card.Error.TLabel", wraplength=self.px(240)).grid(
            row=0, column=0, sticky="w")
        if self.state in DOWN:
            off = ttk.Button(bar, text="Войти заново", style="Accent.TButton")
            off.configure(command=lambda: self.unenroll(off, problem, ask_studio=False))
        else:
            off = ttk.Button(bar, text="Отключиться", style="Danger.TButton")
            off.configure(command=lambda: confirm(self, CONFIRM_OFF, "Отключить",
                                                  lambda: self.unenroll(off, problem, ask_studio=True)))
        off.grid(row=0, column=1, sticky="e")
        self.settings_card(cfg)
        self.win.bind("<Return>", lambda e: None)

    def unenroll(self, button, problem, *, ask_studio: bool) -> None:
        button.state(["disabled"])

        def done(result, failed) -> None:
            if failed:
                button.state(["!disabled"])
                problem.set(FAILED)
                return
            self.notice = result[1]
            self.enrolled = False
            self.pin = None
            self.render()

        self.work(lambda: self.ctrl.unenroll(ask_studio=ask_studio), done)

    def settings_card(self, cfg) -> None:
        tk, ttk = self.tk, self.ttk
        box = self.card(2)
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="Настройки", style="Card.Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                       pady=(0, self.px(10)))
        problem = tk.StringVar()
        ttk.Label(box, text="Папка проектов", style="Card.Muted.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(box, text=cfg.projects_root or DASH, style="Card.TLabel", wraplength=self.px(300)).grid(
            row=2, column=0, sticky="w")

        def change() -> None:
            from tkinter import filedialog

            chosen = filedialog.askdirectory(parent=self.win, initialdir=cfg.projects_root or None, mustexist=True)
            if not chosen:
                return

            def done(found, failed) -> None:
                if failed or found:
                    problem.set("\n".join(found) if found else FAILED)
                    return
                self.render()

            self.work(lambda: self.ctrl.set_folder(os.path.normpath(chosen)), done)

        ttk.Button(box, text="Изменить…", command=change).grid(row=1, column=1, rowspan=2, sticky="e",
                                                              padx=(self.px(12), 0))
        auto = tk.BooleanVar(value=cfg.autostart)

        def toggle() -> None:
            on = auto.get()

            def done(_, failed) -> None:
                if failed:
                    auto.set(not on)
                    problem.set(FAILED)

            self.work(lambda: self.ctrl.set_autostart(on), done)

        ttk.Checkbutton(box, text="Запускать при входе в Windows", variable=auto, command=toggle,
                        style="Card.TCheckbutton").grid(row=3, column=0, columnspan=2, sticky="w",
                                                        pady=(self.px(12), self.px(12)))
        words, buttons = pin_row(self.pin)
        line = ttk.Frame(box, style="Card.TFrame")
        line.grid(row=4, column=0, columnspan=2, sticky="ew")
        line.columnconfigure(0, weight=1)
        ttk.Label(line, text=words or "PIN…", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        actions = {"set": lambda: PinDialog(self, BUTTONS["set"]), "change": lambda: PinDialog(self, BUTTONS["change"]),
                   "clear": lambda: confirm(self, CONFIRM_PIN_OFF, "Убрать", self.clear_pin),
                   "unlock": self.unlock_pin}
        for i, key in enumerate(buttons):
            ttk.Button(line, text=BUTTONS[key], command=actions[key]).grid(row=0, column=1 + i,
                                                                           padx=(self.px(8), 0))
        ttk.Label(box, textvariable=problem, style="Card.Error.TLabel", wraplength=self.px(380)).grid(
            row=5, column=0, columnspan=2, sticky="w")
        self.settings_problem = problem

    def clear_pin(self) -> None:
        self.work(lambda: self.ctrl.set_pin(None), lambda _, failed: self.after_pin(failed))

    def unlock_pin(self) -> None:
        self.work(self.ctrl.reset_pin, lambda _, failed: self.after_pin(failed))

    def after_pin(self, failed: bool) -> None:
        if failed:
            self.settings_problem.set(FAILED)
        self.refresh_pin()


class _Dialog:
    """Окно поверх главного той же темы: модальное, Escape и крестик — «Отмена»."""

    def __init__(self, main: MainWindow, title: str):
        tk, ttk = main.tk, main.ttk
        self.main = main
        self.win = tk.Toplevel(main.win, background=main.c["bg"])
        self.win.title(title)
        self.win.resizable(False, False)
        self.win.transient(main.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        theme.dark_title(self.win, main.c["dark"])
        self.body = ttk.Frame(self.win, padding=main.px(20))
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)

    def buttons(self, row: int, ok: str, style: str, command):
        ttk = self.main.ttk
        bar = ttk.Frame(self.body)
        bar.grid(row=row, column=0, sticky="e", pady=(self.main.px(16), 0))
        go = ttk.Button(bar, text=ok, style=style, command=command)
        go.grid(row=0, column=0)
        ttk.Button(bar, text="Отмена", command=self.close).grid(row=0, column=1, padx=(self.main.px(8), 0))
        self.win.bind("<Return>", lambda e: command())
        return go

    def show(self) -> None:
        self.win.update_idletasks()
        m = self.main.win
        x = m.winfo_rootx() + (m.winfo_width() - self.win.winfo_reqwidth()) // 2
        y = m.winfo_rooty() + (m.winfo_height() - self.win.winfo_reqheight()) // 3
        self.win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.win.grab_set()
        self.win.focus_force()

    def close(self) -> None:
        if self.win.winfo_exists():
            self.win.grab_release()
            self.win.destroy()


def confirm(main: MainWindow, text: str, ok: str, on_ok) -> None:
    """Подтверждение: опасная кнопка `ok` и «Отмена»."""
    dialog = _Dialog(main, TITLE)
    main.ttk.Label(dialog.body, text=text, wraplength=main.px(340), justify="left").grid(row=0, column=0, sticky="w")

    def go() -> None:
        dialog.close()
        on_ok()

    dialog.buttons(1, ok, "Danger.TButton", go).focus_set()
    dialog.show()


class PinDialog(_Dialog):
    """«Задать PIN» / «Изменить PIN»: старый PIN не спрашивается (на машине — владелец учётки Windows)."""

    def __init__(self, main: MainWindow, title: str):
        super().__init__(main, title)
        tk, ttk = main.tk, main.ttk
        self.pin, self.pin2, self.errors = tk.StringVar(), tk.StringVar(), tk.StringVar()
        first = None
        for i, (label, var) in enumerate((("Новый PIN", self.pin), ("Ещё раз", self.pin2))):
            ttk.Label(self.body, text=label).grid(row=2 * i, column=0, sticky="w")
            entry = ttk.Entry(self.body, textvariable=var, show="•", width=24)
            entry.grid(row=2 * i + 1, column=0, sticky="ew", pady=(main.px(3), main.px(10)))
            first = first or entry
        ttk.Label(self.body, textvariable=self.errors, style="Error.TLabel").grid(row=4, column=0, sticky="w")
        self.go = self.buttons(5, "Сохранить", "Accent.TButton", self.save)
        first.focus_set()
        self.show()

    def save(self) -> None:
        if not self.go.instate(["!disabled"]):
            return
        pin, pin2 = self.pin.get(), self.pin2.get()
        self.pin.set("")
        self.pin2.set("")
        found = pin_dialog_errors(pin, pin2)
        if found:
            self.errors.set("\n".join(found))
            return
        self.go.state(["disabled"])
        self.errors.set("")

        def done(_, failed) -> None:
            if failed:
                if self.win.winfo_exists():
                    self.go.state(["!disabled"])
                    self.errors.set(FAILED)
                return
            self.close()
            self.main.refresh_pin()

        self.main.work(lambda: self.main.ctrl.set_pin(pin), done)
