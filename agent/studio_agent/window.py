"""Окно «Studio Agent» (`Toplevel` скрытого корня Tk): вид «Вход» до привязки и «Подключение» после; окна PIN и
подтверждений — `Toplevel` той же темы (`theme`).

Окно одно: повторный `open_main` поднимает открытое, состояние ядра приходит `update(state)` из очереди Tk
(`app.main`): окно перестраивает только смена вида, карточка состояния меняется на месте, а пока идёт операция или
открыт диалог — ничего (`state_action`). Всё блокирующее (сеть, DPAPI, scrypt, реестр) — `Controller` в своём
потоке, окно ждёт итог `root.after`. Пароль и PIN стираются из полей сразу после нажатия. Вид — чистые функции
(`view_of`, `STATUS`, `status_of`, `card_of`, `state_action`, `pin_row`, `pin_dialog_errors`, `host_of`): их
проверяют тесты без Tk."""

import logging
import os
import threading
from urllib.parse import urlsplit

from . import setup, theme

log = logging.getLogger(__name__)

TITLE = "Studio Agent"
NOT_LINKED = "Не подключён"
NOTE_PASSWORD = "Пароль нужен один раз и на компьютере не хранится"
CONFIRM_OFF = ("Отключить компьютер от студии? Студия забудет этот компьютер, идущие ходы прервутся. "
               "Чаты останутся на компьютере.")
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
REPLACED_HINT = "Подключить снова — этот компьютер заберёт канал у другого моста"
# Кнопка карточки «Подключение» → (слова, стиль): `off` — отвязка с подтверждением, `relogin` — после отзыва стереть
# ключ без запроса к студии, `reconnect` — заменённый забирает канал (`restart_link`; ничего не стирается).
MAIN_BUTTONS = {"off": ("Отключиться", "Danger.TButton"), "relogin": ("Войти заново", "Accent.TButton"),
                "reconnect": ("Подключить снова", "Accent.TButton")}

_window = None


def view_of(enrolled: bool) -> str:
    """`link` — «Подключение» (есть ключ), `login` — «Вход»."""
    return "link" if enrolled else "login"


def status_of(state: str) -> tuple[str, str]:
    """Слова и цвет карточки «Подключение»; `setup` здесь — только что привязан, канал ещё не поднят."""
    return STATUS["connecting"] if state == "setup" else STATUS.get(state, STATUS["connecting"])


def card_of(state: str) -> tuple[str, str, str, str]:
    """Карточка состояния: слова, токен цвета точки, приглушённая строка под словами (или пусто), кнопка
    (`MAIN_BUTTONS`). Заменённый (`replaced`) ключ не стирает: второй мост той же учётки Windows в другом сеансе
    живёт с тем же `key.bin` и `pin.bin` — стёртый PIN открыл бы ему любую сессию без PIN."""
    words, token = status_of(state)
    if state == "replaced":
        return words, token, REPLACED_HINT, "reconnect"
    return words, token, "", "relogin" if state == "revoked" else "off"


def state_action(view: str, state: str, busy: bool) -> str:
    """Что окну делать со сменой состояния ядра: `wait` — идёт операция или открыто подтверждение (применить после:
    её обработчики держат виджеты окна), `render` — сменился вид (Вход ↔ Подключение), `card` — карточку
    состояния на месте, `none` — форму входа с введённым не трогать."""
    if busy:
        return "wait"
    if view_of(state != "setup") != view:
        return "render"
    return "card" if view == "link" else "none"


def pin_row(status: str | None) -> tuple[str, tuple[str, ...]]:
    """Строка PIN и кнопки (`set`, `change`, `clear`, `unlock`); `None` — ещё не прочитан."""
    if status not in PIN_WORDS:
        return "", ()
    return PIN_WORDS[status], PIN_BUTTONS[status]


def backend_row(status: dict | None) -> tuple[str, str]:
    """Результат собственной проверки помощника; связь со студией этого статуса не определяет."""
    if not status or status.get("status") == "checking":
        return "Проверка…", "muted"
    if status.get("status") == "ready":
        return f"Доступен · моделей: {status['models']}", "ok"
    return "Недоступен", "warn"


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
    """Состояние ядра (из очереди Tk): открытое окно — по `state_action`."""
    if _window is not None and _window.alive():
        _window.on_state(state)


def login_changed(login: str) -> None:
    """Логин из `welcome` записан (событие ядра `login`): строка «Вход» открытого окна."""
    if _window is not None and _window.alive():
        _window.show_login(login)


def pin_changed() -> None:
    """PIN сменили мимо окна (трей «Снять блок PIN»): открытое окно перечитывает строку PIN."""
    if _window is not None and _window.alive() and _window.enrolled:
        _window.refresh_pin()


def backends_changed() -> None:
    """Проверка Claude Code или Codex закончилась: обновить только их строки."""
    if _window is not None and _window.alive():
        _window.show_backends()


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
        self.c = prepare(root, theme.dark_for(controller.config().theme))
        self.scale = root.winfo_fpixels("1i") / 96
        self.state = controller.state
        self.enrolled = controller.state != "setup"
        self.notice = ""  # над формой входа: студия не забыла компьютер при отвязке
        self.pin: str | None = None
        self.busy = 0  # идут операции окна и открыты его диалоги: окно не перестраивается, кнопки неактивны
        self.pending = False  # состояние пришло, пока окно занято, — применить после
        self.buttons: list = []  # кнопки вида «Подключение», что гаснут на время операции
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

    def hold(self) -> None:
        """Операция или диалог начались: перестроения ждут, кнопки гаснут."""
        self.busy += 1
        self.sync_buttons()

    def release(self) -> None:
        """Операция или диалог кончились; последнее — пришедшее тем временем состояние применяется."""
        self.busy = max(self.busy - 1, 0)
        self.sync_buttons()
        if not self.busy and self.pending and self.alive():
            self.pending = False
            self.on_state(self.state)

    def sync_buttons(self) -> None:
        self.buttons = [b for b in self.buttons if b.winfo_exists()]
        for b in self.buttons:
            b.state(["disabled"] if self.busy else ["!disabled"])

    def work(self, fn, done, *, hold: bool = True) -> None:
        """`fn` — в своём потоке (сеть, DPAPI, scrypt); `done(итог, сбой)` — в потоке Tk, если окно открыто. `hold` —
        на это время окно занято (`hold`/`release`)."""
        result: dict = {}
        if hold:
            self.hold()

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
            try:
                done(result.get("value"), bool(result.get("failed")))
            finally:
                if hold:
                    self.release()

        self.root.after(50, check)

    def on_state(self, state: str) -> None:
        self.state = state
        action = state_action(view_of(self.enrolled), state, self.busy > 0)
        if action == "wait":
            self.pending = True
        elif action == "render":
            self.enrolled = state != "setup"
            if self.enrolled:
                self.notice = ""
                self.pin = None
            self.render()
            if self.enrolled:
                self.refresh_pin()
        elif action == "card":
            self.show_card()

    def refresh_pin(self) -> None:
        """Строка PIN заново (DPAPI — в потоке); прочее окна не трогает: ошибки под ней остаются."""
        def done(status, failed):
            self.pin = None if failed else status
            self.show_pin()

        self.work(self.ctrl.pin_status, done, hold=False)

    def render(self) -> None:
        ttk = self.ttk
        self.buttons = []
        self.card_widgets = self.pin_line = self.login_value = None
        self.backend_widgets = {}
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
            self.theme_controls(top, row=1, card=False, span=3)

    def theme_controls(self, host, *, row: int, card: bool, span: int = 2) -> None:
        """Два явных варианта темы; старый config до выбора следует Windows."""
        ttk = self.ttk
        prefix = "Card." if card else ""
        box = ttk.Frame(host, style=prefix + "TFrame")
        box.grid(row=row, column=0, columnspan=span, sticky="ew", pady=(self.px(8), self.px(10)))
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="Тема", style=prefix + "Muted.TLabel").grid(row=0, column=0, sticky="w")
        selected = self.tk.StringVar(value="dark" if self.c["dark"] else "light")
        problem = self.tk.StringVar()

        def change() -> None:
            previous = "dark" if self.c["dark"] else "light"
            choice = selected.get()
            if self.busy:
                selected.set(previous)
                return
            problem.set("")

            def done(_, failed):
                if failed:
                    selected.set(previous)
                    problem.set(FAILED)
                else:
                    self.apply_theme(choice)

            self.work(lambda: self.ctrl.set_theme(choice), done)

        for column, (value, label) in enumerate((("light", "Светлая"), ("dark", "Тёмная")), start=1):
            button = ttk.Radiobutton(box, text=label, value=value, variable=selected, command=change,
                                    style=prefix + "TRadiobutton")
            button.grid(row=0, column=column, sticky="e", padx=(self.px(12), 0))
            self.buttons.append(button)
        ttk.Label(box, textvariable=problem, style=prefix + "Error.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w")

    def apply_theme(self, choice: str) -> None:
        """Перекрасить на месте: введённые поля, сообщения об ошибках и PIN не теряются."""
        previous = self.c
        self.c = prepare(self.root, theme.dark_for(choice))

        def recolour(widget):
            if isinstance(widget, (self.tk.Frame, self.tk.Canvas, self.tk.Toplevel)):
                old = widget.cget("background")
                token = "panel" if old == previous["panel"] else "bg"
                widget.configure(background=self.c[token])
                if isinstance(widget, self.tk.Frame) and int(widget.cget("highlightthickness")):
                    widget.configure(highlightbackground=self.c["border"], highlightcolor=self.c["border"])
            for child in widget.winfo_children():
                recolour(child)

        recolour(self.win)
        theme.dark_title(self.win, self.c["dark"])
        self.show_card()
        self.show_backends()

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
        folders = list(cfg.projects_roots)
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
        self.folders_control(box, row=r, folders=folders, problem=errors)
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
                    "projects_roots": list(folders), "pin": pin.get(), "pin2": pin2.get(), "no_pin": False,
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
        box = self.card(1)
        box.columnconfigure(0, weight=0)
        box.columnconfigure(1, weight=1)
        head = ttk.Frame(box, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, self.px(12)))
        d = self.px(14)
        dot = self.tk.Canvas(head, width=d, height=d, highlightthickness=0, background=self.c["panel"])
        oval = dot.create_oval(1, 1, d - 1, d - 1)
        dot.grid(row=0, column=0, padx=(0, self.px(10)))
        words = ttk.Label(head, style="Card.Status.TLabel")
        words.grid(row=0, column=1, sticky="w")
        hint = ttk.Label(box, style="Card.Muted.TLabel", wraplength=self.px(380), justify="left")
        hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, self.px(12)))
        helpers = ttk.Frame(box, style="Card.TFrame")
        helpers.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, self.px(12)))
        helpers.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate((("claude", "Claude Code"), ("codex", "Codex"), ("opencode", "OpenCode"))):
            ttk.Label(helpers, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=self.px(4))
            line = ttk.Frame(helpers, style="Card.TFrame")
            line.grid(row=row, column=1, sticky="e", padx=(self.px(20), 0))
            size = self.px(8)
            light = self.tk.Canvas(line, width=size, height=size, highlightthickness=0, background=self.c["panel"])
            point = light.create_oval(0, 0, size - 1, size - 1)
            light.grid(row=0, column=0, padx=(0, self.px(8)))
            value = ttk.Label(line, style="Card.TLabel")
            value.grid(row=0, column=1, sticky="e")
            self.backend_widgets[key] = (light, point, value)
        self.show_backends()
        login = self.ctrl.login
        for i, (label, value) in enumerate((("Студия", host_of(cfg.url)), ("Вход", login or DASH),
                                            ("Компьютер", cfg.name or DASH)), start=3):
            ttk.Label(box, text=label, style="Card.Muted.TLabel").grid(row=i, column=0, sticky="w",
                                                                       padx=(0, self.px(16)), pady=self.px(2))
            shown = ttk.Label(box, text=value, style="Card.TLabel")
            shown.grid(row=i, column=1, sticky="w", pady=self.px(2))
            if label == "Вход":
                self.login_value = shown
        bar = ttk.Frame(box, style="Card.TFrame")
        bar.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(self.px(14), 0))
        bar.columnconfigure(0, weight=1)
        self.card_problem = self.tk.StringVar()
        ttk.Label(bar, textvariable=self.card_problem, style="Card.Error.TLabel", wraplength=self.px(240)).grid(
            row=0, column=0, sticky="w")
        main = ttk.Button(bar)
        main.grid(row=0, column=1, sticky="e")
        self.buttons.append(main)
        self.card_widgets = (dot, oval, words, hint, main)
        self.show_card()
        self.settings_card(cfg)
        self.sync_buttons()
        self.win.bind("<Return>", lambda e: None)

    def show_card(self) -> None:
        """Карточка состояния на месте (`card_of`): точка, слова, строка под ними, кнопка; прочее окна не трогается."""
        if self.card_widgets is None:
            return
        dot, oval, words, hint, main = self.card_widgets
        text, token, under, kind = card_of(self.state)
        dot.itemconfigure(oval, fill=self.c[token], outline=self.c[token])
        words.configure(text=text)
        hint.configure(text=under)
        if under:
            hint.grid()
        else:
            hint.grid_remove()
        label, style = MAIN_BUTTONS[kind]
        main.configure(text=label, style=style, command=lambda: self.main_action(kind))

    def show_login(self, login: str) -> None:
        if self.login_value is not None and self.login_value.winfo_exists():
            self.login_value.configure(text=login or DASH)

    def show_backends(self) -> None:
        status = self.ctrl.backend_status()
        for key, (dot, oval, label) in self.backend_widgets.items():
            words, token = backend_row(status.get(key))
            dot.itemconfigure(oval, fill=self.c[token], outline=self.c[token])
            label.configure(text=words, foreground=self.c[token])

    def main_action(self, kind: str) -> None:
        if self.busy:
            return
        self.card_problem.set("")
        if kind == "reconnect":  # канал заберёт этот мост, другой получит 4409; ключ и PIN не трогаются
            self.ctrl.restart_link()
        elif kind == "relogin":
            self.unenroll(ask_studio=False)
        else:
            confirm(self, CONFIRM_OFF, "Отключить", lambda: self.unenroll(ask_studio=True))

    def unenroll(self, *, ask_studio: bool) -> None:
        def done(result, failed) -> None:
            if failed:
                self.card_problem.set(FAILED)
                return
            erased, words = result
            self.notice = words  # не стёрто, а ключа уже нет — ядро даст `setup`: слова над формой входа
            if not erased:
                self.card_problem.set(words)
                return
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
        self.theme_controls(box, row=1, card=True)
        problem = tk.StringVar()
        self.folders_control(box, row=2, folders=list(cfg.projects_roots), problem=problem, save=True)
        auto = tk.BooleanVar(value=self.ctrl.autostart_enabled())  # реестр, а не config.json

        def toggle() -> None:
            on = auto.get()
            problem.set("")

            def done(_, failed) -> None:
                if failed:
                    auto.set(not on)
                    problem.set(FAILED)

            self.work(lambda: self.ctrl.set_autostart(on), done)

        box_auto = ttk.Checkbutton(box, text="Запускать при входе в Windows", variable=auto, command=toggle,
                                   style="Card.TCheckbutton")
        box_auto.grid(row=4, column=0, columnspan=2, sticky="w", pady=(self.px(12), self.px(12)))
        self.buttons.append(box_auto)
        self.pin_line = ttk.Frame(box, style="Card.TFrame")
        self.pin_line.grid(row=5, column=0, columnspan=2, sticky="ew")
        self.pin_line.columnconfigure(0, weight=1)
        ttk.Label(box, textvariable=problem, style="Card.Error.TLabel", wraplength=self.px(380)).grid(
            row=6, column=0, columnspan=2, sticky="w")
        self.settings_problem = problem
        self.show_pin()

    def folders_control(self, box, *, row: int, folders: list[str], problem, save: bool = False) -> None:
        """Список разрешённых папок и добавление/удаление; при привязке сохраняется сразу."""
        ttk = self.ttk
        ttk.Label(box, text="Папки проектов", style="Card.Muted.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w")
        area = ttk.Frame(box, style="Card.TFrame")
        area.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(self.px(3), self.px(10)))
        area.columnconfigure(0, weight=1)
        viewport = ttk.Frame(area, width=self.px(285), height=self.px(76), style="Card.TFrame")
        viewport.grid(row=0, column=0, sticky="ew")
        viewport.grid_propagate(False)
        viewport.rowconfigure(0, weight=1)
        viewport.columnconfigure(0, weight=1)
        listing = ttk.Treeview(viewport, show="tree", selectmode="browse", height=3, style="Folders.Treeview")
        listing.column("#0", minwidth=self.px(180), width=self.px(285), stretch=False)
        listing.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(area, orient="vertical", command=listing.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        listing.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=listing.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        listing.configure(xscrollcommand=horizontal.set)
        bar = ttk.Frame(area, style="Card.TFrame")
        bar.grid(row=2, column=0, columnspan=2, sticky="e", pady=(self.px(6), 0))

        def render() -> None:
            import tkinter.font as tkfont

            listing.delete(*listing.get_children())
            font = tkfont.nametofont("TkDefaultFont", self.root)
            listing.column("#0", width=max(self.px(285), max((font.measure(p) for p in folders), default=0) + self.px(24)))
            for index, path in enumerate(folders):
                listing.insert("", "end", iid=str(index), text=path)
            if folders:
                listing.selection_set(str(len(folders) - 1))
                listing.see(str(len(folders) - 1))

        def apply(proposed: list[str]) -> None:
            problem.set("")
            if not save:
                folders[:] = proposed
                render()
                return

            def done(found, failed) -> None:
                if failed or found:
                    problem.set("\n".join(found) if found else FAILED)
                    return
                folders[:] = self.ctrl.config().projects_roots
                render()

            self.work(lambda: self.ctrl.set_folders(proposed), done)

        def add() -> None:
            from tkinter import filedialog

            if self.busy:
                return
            chosen = filedialog.askdirectory(parent=self.win, initialdir=folders[-1] if folders else None, mustexist=True)
            if chosen:
                path = os.path.normpath(chosen)
                if os.path.normcase(path) not in {os.path.normcase(p) for p in folders}:
                    apply([*folders, path])

        def remove() -> None:
            if self.busy or not listing.selection():
                return
            if len(folders) <= 1:
                problem.set("Нужна хотя бы одна папка проектов")
                return
            index = int(listing.selection()[0])
            apply(folders[:index] + folders[index + 1:])

        for column, (label, action) in enumerate((("Добавить…", add), ("Убрать", remove))):
            button = ttk.Button(bar, text=label, command=action)
            button.grid(row=0, column=column, padx=(self.px(8), 0))
            self.buttons.append(button)
        render()

    def show_pin(self) -> None:
        """Строка PIN (`pin_row`) на месте: слова и кнопки."""
        line, ttk = self.pin_line, self.ttk
        if line is None or not line.winfo_exists():
            return
        for child in line.winfo_children():
            child.destroy()
        words, buttons = pin_row(self.pin)
        ttk.Label(line, text=words or "PIN…", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        actions = {"set": lambda: PinDialog(self, BUTTONS["set"]), "change": lambda: PinDialog(self, BUTTONS["change"]),
                   "clear": lambda: confirm(self, CONFIRM_PIN_OFF, "Убрать", self.clear_pin),
                   "unlock": self.unlock_pin}
        for i, key in enumerate(buttons):
            b = ttk.Button(line, text=BUTTONS[key], command=actions[key])
            b.grid(row=0, column=1 + i, padx=(self.px(8), 0))
            self.buttons.append(b)
        self.sync_buttons()

    def clear_pin(self) -> None:
        self.settings_problem.set("")
        self.work(lambda: self.ctrl.set_pin(None), lambda _, failed: self.after_pin(failed))

    def unlock_pin(self) -> None:
        self.settings_problem.set("")
        self.work(self.ctrl.reset_pin, lambda _, failed: self.after_pin(failed))  # меню трея — `on_pin_changed` ядра

    def after_pin(self, failed: bool) -> None:
        self.refresh_pin()  # строку PIN — заново, ошибку под ней — после: перечитывание её не стирает
        if failed:
            self.settings_problem.set(FAILED)


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
        main.hold()  # пока открыт — главное окно не перестраивается: `on_ok` держит его виджеты

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
            self.main.release()


def confirm(main: MainWindow, text: str, ok: str, on_ok) -> None:
    """Подтверждение: опасная кнопка `ok` и «Отмена»."""
    dialog = _Dialog(main, TITLE)
    main.ttk.Label(dialog.body, text=text, wraplength=main.px(340), justify="left").grid(row=0, column=0, sticky="w")

    def go() -> None:
        on_ok()  # операция занимает окно раньше, чем диалог его отпустит: между ними перестроения нет
        dialog.close()

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
