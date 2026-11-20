"""Команды моста: `python -m studio_agent [run] [--console] | enroll | set-pin | clear-pin | reset-pin | status`.

Пароль и PIN — только `getpass` (не argv). `run` без `--console` — трей и окно (`app.main`)."""

import argparse
import getpass
import os
import socket
import sys
import threading

from . import enroll, host, journal, setup, single, store
from .i18n import tr
from .pin import PinStore

STOP = ("setup", "revoked", "replaced")  # консоль на них выходит: канал сам не поднимется


def _ask(prompt: str, default: str = "") -> str:
    value = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return value or default


def _run_console(h) -> int:
    from . import app

    mutex = single.acquire()
    if mutex is None:
        print(tr(app.ALREADY))
        return 1
    log = journal.setup(h, console=True)
    log.info("мост: старт в консоли")
    app.prepare_env()
    ctrl = app.Controller(h)
    finished = threading.Event()

    def show(state: str) -> None:
        print(f"{app.TITLE} — {tr(app.WORDS.get(state, state))}", flush=True)
        if state in STOP:
            finished.set()

    show(ctrl.state)
    if ctrl.state == "setup":
        print(tr("Настройте мост: python -m studio_agent enroll"))
        return 1
    ctrl.on_state(show)
    ctrl.start()
    try:
        while not finished.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    ctrl.quit()
    mutex.close()
    return 0 if ctrl.state not in STOP else 1


def yes(answer: str) -> bool:
    """Ответ «да» консоли — на языке моста; русское «да» понимается всегда."""
    return answer.strip().lower() in {"да", tr("да")}


def rebind_confirmed(enrolled: bool, login: str) -> bool:
    """Компьютер уже привязан — новая привязка только с согласия: прежний компьютер будет отозван в студии."""
    if not enrolled:
        return True
    answer = input(tr("Компьютер уже привязан (логин {0}). Привязать заново? "
                      "Прежний компьютер будет отозван в Студии. [да/нет] ", login or "—"))
    return yes(answer)


def _enroll(h) -> int:
    cfg = store.load_config(h)
    if not rebind_confirmed(store.load_key(h) is not None, cfg.login):
        return 1
    form = {"url": _ask(tr("Адрес студии"), cfg.url), "login": _ask(tr("Логин")),
            "name": _ask(tr("Имя компьютера"), cfg.name or socket.gethostname()),
            "projects_roots": _ask_folders(cfg.projects_roots), "pin": "", "pin2": "", "no_pin": False,
            "autostart": cfg.autostart}
    form["password"] = getpass.getpass(tr("Пароль: "))
    errors = setup.validate(form, enrolled=False)
    if errors:
        print("\n".join(errors))
        return 1
    old = setup.previous(h, form)
    try:
        cfg = setup.apply(h, form)
    except enroll.EnrollError as e:
        if e.code == "email_code":
            form["email_code"] = input(tr("Код из письма: ")).strip()
            try:
                cfg = setup.apply(h, form)
            except enroll.EnrollError as retry:
                print(retry.words)
                return 1
        else:
            print(e.words)
            return 1
    if old is not None and not setup.revoke(*old):
        print(tr(enroll.KEPT))
    print(tr("Привязан: компьютер {0}. Работающий мост подхватит ключ после перезапуска.", cfg.agent_id))
    return 0


def _ask_folders(current: list[str]) -> list[str]:
    """Пустой первый ввод сохраняет список; затем можно добавить сколько угодно папок."""
    if current:
        print(tr("Папки проектов:") + "\n" + "\n".join(f"  {path}" for path in current))
    first = input(tr("Папка проектов (Enter — оставить прежние): " if current else "Папка проектов: ")).strip()
    roots = [first] if first else list(current)
    while path := input(tr("Ещё папка проектов (Enter — закончить): ")).strip():
        roots.append(path)
    return roots


def _set_pin(h) -> int:
    pin = getpass.getpass(tr("PIN (4–8 цифр): "))
    errors = setup.pin_errors(pin, getpass.getpass(tr("PIN ещё раз: "))) if pin else [tr(setup.PIN_WORDS)]
    if errors:
        print("\n".join(errors))
        return 1
    PinStore(h).set_pin(pin)
    print(tr("PIN задан."))
    return 0


def _status(h) -> int:
    cfg = store.load_config(h)
    pins = PinStore(h)
    try:
        pin = ("заблокирован" if pins.locked else "задан") if pins.has_pin else "не задан"
    except OSError:
        pin = "не читается"
    print(tr("Адрес студии: {0}\nКомпьютер: {1}\nИмя компьютера: {2}\nПапки проектов: {3}\nКлюч устройства: {4}\n"
             "PIN: {5}", cfg.url, cfg.agent_id if cfg.agent_id is not None else "—", cfg.name or "—",
             ", ".join(cfg.projects_roots) or "—", tr("есть" if store.load_key(h) is not None else "нет"), tr(pin)))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m studio_agent", description=tr("Мост studio-agent"))
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help=tr("канал к студии (по умолчанию — с треем)"))
    run.add_argument("--console", action="store_true", help=tr("без трея, журнал ещё и в консоль"))
    sub.add_parser("enroll", help=tr("привязать к студии"))
    sub.add_parser("set-pin", help=tr("задать PIN"))
    sub.add_parser("clear-pin", help=tr("убрать PIN"))
    sub.add_parser("reset-pin", help=tr("снять блок PIN"))
    sub.add_parser("status", help=tr("настройки (без ключа)"))
    if host.LINUX:
        sub.add_parser("menu", help=tr("интерактивные настройки и диагностика"))
        sub.add_parser("_service", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    h = store.home()
    if host.LINUX:
        from . import linux_menu
        if args.cmd in (None, "menu"):
            return linux_menu.menu(h)
        if args.cmd in ("run", "_service"):
            from . import linux_service
            return linux_service.supervise(h) if args.cmd == "run" else linux_service.serve(h)
        return linux_menu.command(h, args.cmd)
    if args.cmd in (None, "run"):
        if getattr(args, "console", False):
            return _run_console(h)
        from . import app
        return app.main([])
    if args.cmd == "enroll":
        return _enroll(h)
    if args.cmd == "set-pin":
        return _set_pin(h)
    if args.cmd == "clear-pin":
        PinStore(h).set_pin(None)
        print(tr("PIN убран."))
        return 0
    if args.cmd == "reset-pin":
        PinStore(h).reset()
        print(tr("Блок PIN снят."))
        return 0
    return _status(h)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
