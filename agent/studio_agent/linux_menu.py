"""Local terminal settings for a running or stopped Linux bridge."""
import contextlib
import getpass
import hashlib
import json
import os
import socket
import stat
import struct
import subprocess
from pathlib import Path

from . import VERSION, app, enroll, host, linux_autostart, setup, single
from .errors import Refused
from .i18n import EN, tr

LIMIT = 64 * 1024
UNIX_PATH_MAX = 108  # sun_path с завершающим NUL


def _address(h):
    """Сокет меню — в каталоге только владельца: имя абстрактного сокета занял бы любой локальный пользователь,
    и мост не стартовал бы. Длинный путь каталога моста — в `$XDG_RUNTIME_DIR` (тоже только владельца)."""
    path = h.resolve() / "run" / "menu.sock"
    if len(os.fsencode(path)) < UNIX_PATH_MAX:
        return path
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        digest = hashlib.sha256(os.fsencode(h.resolve())).hexdigest()[:32]
        path = Path(runtime) / f"studio-agent-{digest}" / "menu.sock"
        if len(os.fsencode(path)) < UNIX_PATH_MAX:
            return path
    raise OSError(tr("Путь каталога моста слишком длинный для канала меню"))


def _private(folder):
    """Каталог сокета: свой, не ссылка, режим 0700; чужой или подменённый — отказ."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise PermissionError(tr("Каталог канала меню принадлежит другой учётной записи"))
    os.chmod(folder, 0o700)


def _check_peer(sock):
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    if struct.unpack("3i", raw)[1] != os.geteuid():
        raise PermissionError(tr("Другая учётная запись"))


@contextlib.contextmanager
def listener(h):
    """Слушает один мост — `serve` под мьютексом надзирателя, поэтому прежний файл сокета (после падения) убирается."""
    path = _address(h)
    _private(path.parent)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.bind(str(path))
        try:
            sock.listen(4)
            sock.settimeout(0.25)
            yield sock
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)


def _receive(sock):
    raw = sock.recv(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError(tr("Слишком большой запрос"))
    return json.loads(raw)


def handle_one(server, ctrl):
    try:
        conn, _ = server.accept()
    except TimeoutError:
        return
    with conn:
        try:
            _check_peer(conn)
            conn.settimeout(2)
            body = _receive(conn)
            result = dispatch(ctrl, body["op"], body.get("value"))
            reply = {"result": result}
        except Refused as error:
            reply = {"error": error.message}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError,
                RuntimeError, subprocess.SubprocessError):
            reply = {"error": tr("Не удалось выполнить действие. Проверьте состояние агента.")}
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps(reply).encode())


def invoke(h, op, value=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        try:
            sock.connect(str(_address(h)))
        except (ConnectionRefusedError, FileNotFoundError):  # мост не запущен: сокета нет или он остался от упавшего
            mutex = single.acquire()
            if mutex is None:
                raise RuntimeError(tr("Мост запускается или обновляется. Повторите действие.")) from None
            try:
                return dispatch(app.Controller(h), op, value)
            finally:
                mutex.close()
        _check_peer(sock)
        sock.settimeout(150)
        raw = json.dumps({"op": op, "value": value}).encode()
        if len(raw) > LIMIT:
            raise ValueError(tr("Слишком большой запрос"))
        sock.sendall(raw)
        reply = _receive(sock)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]


def dispatch(ctrl, op, value):
    if op == "status":
        cfg = ctrl.config()
        from .desktop_linux import diagnostic
        return {"version": VERSION, "studio": ctrl.studio_version,
                "state": tr(app.WORDS.get(ctrl.state, ctrl.state) if ctrl.agent else "остановлен"),
                "url": cfg.url, "name": cfg.name, "login": cfg.login, "id": cfg.agent_id,
                "enrolled": ctrl.enrolled, "roots": cfg.projects_roots, "pin": ctrl.pin_status(),
                "autostart": ctrl.autostart_enabled(), "max_turns": cfg.max_turns,
                "desktop": cfg.desktop_enabled, "display": diagnostic(), "activity": ctrl.activity_status(),
                "backends": ctrl.backend_status(), "cli": {name: bool(host.find_tool(name))
                    for name in ("claude", "codex", "opencode")}}
    if op == "enroll":
        return ctrl.apply_setup(value)
    if op == "folders":
        return ctrl.set_folders(value)
    if op == "pin":
        if value is not None and (not value or setup.pin_errors(value, value)):
            raise ValueError(tr(setup.PIN_WORDS))
        ctrl.set_pin(value)
    elif op == "reset":
        ctrl.reset_pin()
    elif op == "autostart" and type(value) is bool:
        ctrl.set_autostart(value)
    elif op == "limit":
        ctrl.set_max_turns(value)
    elif op == "desktop" and type(value) is bool:
        if value:
            if ctrl.agent is None:
                return [tr("Сначала запустите мост в графическом сеансе.")]
            ctrl.agent.desktop.authorize_local()
        ctrl.set_desktop_enabled(value)
    elif op == "stop-desktop":
        ctrl.stop_desktop_session()
    elif op == "open-menu":
        if ctrl.activity_status()["desktop_active"]:
            ctrl.stop_desktop_session()
    elif op == "idle":
        ctrl.set_quit_when_idle(not ctrl.activity_status()["quit_when_idle"])
    elif op == "quit":
        ctrl.request_quit()
    elif op == "reconnect":
        ctrl.restart_link()
    elif op == "terminal":
        from . import linux_terminal
        if value is None:
            return linux_terminal.load(ctrl.h)
        linux_terminal.save(ctrl.h, value)
    elif op == "cancel-update":
        ctrl.cancel_update()
    elif op == "forget":
        ok, message = ctrl.unenroll()
        return [message] if message else ([] if ok else [tr("Ошибка отключения")])
    else:
        raise ValueError(tr("Неизвестное действие"))
    return []


def _yes(value):
    return tr("да" if value else "нет")


def status(h):
    s = invoke(h, "status")
    activity = s["activity"]
    print(f"Studio Agent {s['version']} — {s['state']}\n" + tr(
        "Студия: {0} (версия {1})\nУчётная запись: {2}\nКомпьютер: {3} (ID {4})\nПапки проектов: {5}\n"
        "Ключ устройства: {6}\nPIN: {7}\nАвтозапуск: {8}\nЗадания: {9} из {10}, ждут ответа: {11}\n"
        "Выход после заданий: {12}\nРабочий стол: {13}, сеанс активен: {14}.",
        s['url'], s['studio'] or '—', s['login'] or '—', s['name'] or '—', s['id'] or '—',
        ', '.join(s['roots']) or '—', _yes(s['enrolled']),
        tr(dict(none='не задан', set='задан', locked='заблокирован')[s['pin']]), _yes(s['autostart']),
        activity['tasks'], s['max_turns'], activity['waiting'], _yes(activity['quit_when_idle']), _yes(s['desktop']),
        _yes(activity['desktop_active'])) + f" {s['display']}")
    for name, found in s["cli"].items():
        backend = s["backends"][name]
        result = tr({"ready": "доступен", "checking": "проверка"}.get(backend["status"], "недоступен"))
        print(tr("{0}: CLI {1}, {2}, моделей {3}", name, _yes(found), result, backend['models']))
    return 0


def enrollment(h):
    from .__main__ import _ask, _ask_folders, rebind_confirmed
    s = invoke(h, "status")
    if not rebind_confirmed(s["enrolled"], s["login"]):
        return 1
    form = {"url": _ask(tr("Адрес студии"), s["url"]), "login": _ask(tr("Логин")),
            "name": _ask(tr("Имя компьютера"), s["name"] or socket.gethostname()),
            "projects_roots": _ask_folders(s["roots"]), "password": getpass.getpass(tr("Пароль: ")),
            "autostart": s["autostart"]}
    errors = invoke(h, "enroll", form)
    # Слова считает служба на своём языке: код из письма узнаётся на обоих.
    if len(errors) == 1 and errors[0] in (enroll.EMAIL_CODE_REQUIRED, EN[enroll.EMAIL_CODE_REQUIRED]):
        form["email_code"] = input(tr("Код из письма: ")).strip()
        errors = invoke(h, "enroll", form)
    print("\n".join(errors) if errors else tr("Компьютер привязан."))
    return int(bool(errors))


def set_pin(h):
    pin = getpass.getpass(tr("Новый PIN (4–8 цифр): "))
    errors = setup.pin_errors(pin, getpass.getpass(tr("Повторите PIN: "))) if pin else [tr(setup.PIN_WORDS)]
    if errors:
        print("\n".join(errors))
        return 1
    invoke(h, "pin", pin)
    print(tr("PIN задан."))
    return 0


def command(h, cmd):
    """Консольная команда Linux: отказ моста («Мост запускается…», слова студии) — строкой и кодом 1, как в меню,
    а не трассировкой Python."""
    try:
        if cmd == "status":
            return status(h)
        if cmd == "enroll":
            return enrollment(h)
        if cmd == "set-pin":
            return set_pin(h)
        if cmd == "clear-pin":
            invoke(h, "pin", None)
            print(tr("PIN убран."))
        else:  # reset-pin
            invoke(h, "reset")
            print(tr("Блок PIN снят."))
        return 0
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    except Refused as error:
        print(error.message)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error))
    return 1


def _number(prompt):
    value = input(prompt).strip()
    if not value.isascii() or not value.isdigit():
        raise ValueError(tr("Введите число"))
    return int(value)


def _action(h, choice):
    from .__main__ import _ask_folders, yes
    if choice == "1":
        status(h)
    elif choice == "2":
        enrollment(h)
    elif choice == "3":
        return invoke(h, "folders", _ask_folders(invoke(h, "status")["roots"]))
    elif choice == "4":
        set_pin(h)
    elif choice == "5":
        if yes(input(tr("Убрать PIN? [да/нет] "))):
            return invoke(h, "pin", None)
    elif choice == "6":
        return invoke(h, "reset")
    elif choice == "7":
        return invoke(h, "limit", _number(tr("Одновременных заданий: ")))
    elif choice == "8":
        return invoke(h, "autostart", not invoke(h, "status")["autostart"])
    elif choice == "9":
        linux_autostart.install_unit()
        linux_autostart.systemctl("start", linux_autostart.UNIT)
    elif choice == "10":
        return invoke(h, "idle")
    elif choice == "11":
        return invoke(h, "desktop", True)
    elif choice == "12":
        return invoke(h, "desktop", False)
    elif choice == "13":
        return invoke(h, "stop-desktop")
    elif choice == "14":
        return invoke(h, "cancel-update")
    elif choice == "15":
        if yes(input(tr("Отвязать компьютер от Студии? [да/нет] "))):
            return invoke(h, "forget")
    elif choice == "16":
        return invoke(h, "reconnect")
    elif choice == "17":
        if yes(input(tr("Выйти и прервать выполняемые задания? [да/нет] "))):
            return invoke(h, "quit")
    elif choice == "18":
        with (h / "agent.log").open("rb") as log:
            log.seek(max(0, os.fstat(log.fileno()).st_size - 16 * 1024))
            print(log.read(16 * 1024).decode("utf-8", "replace"))
    elif choice == "19":
        from . import linux_terminal
        try:
            current = invoke(h, "terminal")
        except (ValueError, RuntimeError):
            current = linux_terminal.DEFAULT
            print(tr("Проверьте сохранённые параметры. Можно ввести их заново."))
        print(tr("Enter оставляет значение, минус очищает поле."))
        def ask(label, key):
            entry = input(f"{label} [{current[key]}]: ").strip()
            return "" if entry == "-" else entry or current[key]
        endpoint = ask(tr("Адрес SSH (имя или IP)"), "host")
        user = ask(tr("Пользователь SSH"), "user") if endpoint else ""
        port = str(ask(tr("Порт SSH"), "port") or 22) if endpoint else "22"
        if not port.isascii() or not port.isdigit():
            raise ValueError(tr("Введите число"))
        port = int(port)
        web = ask(tr("HTTPS-адрес веб-консоли"), "web_url")
        data = dict(host=endpoint, user=user, port=port, web_url=web)
        linux_terminal.addresses(data)
        invoke(h, "terminal", data)
        print(tr("Адреса терминала сохранены."))
    else:
        print(tr("Введите номер пункта."))


MENU = ((1, "Состояние и проверка CLI"), (2, "Привязка к Студии"), (3, "Папки проектов"), (4, "Задать PIN"),
        (5, "Убрать PIN"), (6, "Сбросить блокировку PIN"), (7, "Лимит заданий"), (8, "Переключить автозапуск"),
        (9, "Запустить службу"), (10, "Запросить или отменить выход после заданий"),
        (11, "Разрешить рабочий стол / выбрать экраны"), (12, "Выключить рабочий стол"),
        (13, "Завершить текущий сеанс экрана"), (14, "Отменить подготовку обновления"), (15, "Отвязать компьютер"),
        (16, "Переподключиться к Студии"), (17, "Выйти сейчас"), (18, "Журнал"), (19, "SSH и веб-консоль"),
        (0, "Закрыть меню"))


def menu(h):
    # Do not display local configuration or prompt for passwords on a remotely controlled screen.
    try:
        invoke(h, "open-menu")
    except (OSError, RuntimeError) as error:
        print(str(error))
        return 1
    while True:
        print("\nStudio Agent\n" + "\n".join(f"{number:>2}  {tr(label)}" for number, label in MENU))
        try:
            choice = input("> ").strip()
            if choice == "0":
                break
            messages = _action(h, choice)
            if messages:
                print("\n".join(messages))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        except Refused as error:
            print(error.message)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(str(error))
    return 0
