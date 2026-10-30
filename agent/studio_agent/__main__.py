"""Команды моста: `python -m studio_agent [run] [--console] | enroll | set-pin | clear-pin | reset-pin | status`.

Пароль и PIN — только `getpass` (не argv). `run` без `--console` — трей и окно (`app.main`)."""

import argparse
import getpass
import os
import sys
import threading

from . import enroll, journal, setup, single, store
from .pin import PinStore

STOP = ("setup", "revoked", "replaced")  # консоль на них выходит: канал сам не поднимется


def _ask(prompt: str, default: str = "") -> str:
    value = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return value or default


def _run_console(h) -> int:
    from . import app

    mutex = single.acquire()
    if mutex is None:
        print(app.ALREADY)
        return 1
    log = journal.setup(h, console=True)
    log.info("мост: старт в консоли")
    app.prepare_env()
    ctrl = app.Controller(h)
    finished = threading.Event()

    def show(state: str) -> None:
        print(f"{app.TITLE} — {app.WORDS.get(state, state)}", flush=True)
        if state in STOP:
            finished.set()

    show(ctrl.state)
    if ctrl.state == "setup":
        print("Настройте мост: python -m studio_agent enroll")
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


def _enroll(h) -> int:
    cfg = store.load_config(h)
    form = {"url": _ask("Адрес студии", cfg.url), "login": _ask("Логин"),
            "name": _ask("Имя компьютера", cfg.name or os.environ.get("COMPUTERNAME", "")),
            "projects_root": _ask("Папка проектов", cfg.projects_root), "pin": "", "pin2": "", "no_pin": False,
            "autostart": cfg.autostart}
    form["password"] = getpass.getpass("Пароль: ")
    errors = setup.validate(form, enrolled=False)
    if errors:
        print("\n".join(errors))
        return 1
    try:
        cfg = setup.apply(h, form)
    except enroll.EnrollError as e:
        print(e.words)
        return 1
    print(f"Привязан: компьютер {cfg.agent_id}. Работающий мост подхватит ключ после перезапуска.")
    return 0


def _set_pin(h) -> int:
    pin = getpass.getpass("PIN (4–8 цифр): ")
    errors = setup.pin_errors(pin, getpass.getpass("PIN ещё раз: ")) if pin else [setup.PIN_WORDS]
    if errors:
        print("\n".join(errors))
        return 1
    PinStore(h).set_pin(pin)
    print("PIN задан.")
    return 0


def _status(h) -> int:
    cfg = store.load_config(h)
    pins = PinStore(h)
    try:
        pin = ("заблокирован" if pins.locked else "задан") if pins.has_pin else "не задан"
    except OSError:
        pin = "не читается"
    print(f"Адрес студии: {cfg.url}\nКомпьютер: {cfg.agent_id if cfg.agent_id is not None else '—'}\n"
          f"Имя компьютера: {cfg.name or '—'}\nПапка проектов: {cfg.projects_root or '—'}\n"
          f"Ключ устройства: {'есть' if store.load_key(h) is not None else 'нет'}\nPIN: {pin}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m studio_agent", description="Мост studio-agent")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="канал к студии (по умолчанию — с треем)")
    run.add_argument("--console", action="store_true", help="без трея, журнал ещё и в консоль")
    sub.add_parser("enroll", help="привязать к студии")
    sub.add_parser("set-pin", help="задать PIN")
    sub.add_parser("clear-pin", help="убрать PIN")
    sub.add_parser("reset-pin", help="снять блок PIN")
    sub.add_parser("status", help="настройки (без ключа)")
    args = parser.parse_args(argv)
    h = store.home()
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
        print("PIN убран.")
        return 0
    if args.cmd == "reset-pin":
        PinStore(h).reset()
        print("Блок PIN снят.")
        return 0
    return _status(h)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
