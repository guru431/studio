"""Язык окна, трея, меню и консоли моста: русский — при русском языке системы, иначе английский.

Ключи словаря `EN` — русские исходные строки, как `app/static/en.js` у страницы; параметры — `{0}`, `{1}`… и
подставляются после перевода (значение не становится шаблоном). Переопределение — `STUDIO_AGENT_LANG` (`ru`/`en`).
Отказы `Refused`, уходящие в студию, сюда не входят: их переводит страница по языку студии."""

import functools
import os
import re

from . import host

LANGUAGES = ("ru", "en")


def _system() -> str:
    """Язык интерфейса системы: Windows — `GetUserDefaultUILanguage`, macOS — первый язык из настроек, иначе —
    переменные локали POSIX и `locale`; не удалось — пусто (английский)."""
    if os.name == "nt":
        try:
            import ctypes
            return "ru" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x19 else "en"
        except Exception:
            return ""
    if host.MACOS:  # приложение из LaunchAgent переменных локали не получает
        try:
            from Foundation import NSLocale
            return str(NSLocale.preferredLanguages()[0])
        except Exception:
            pass
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        if value := os.environ.get(name):
            return value
    try:
        import locale
        return locale.getlocale()[0] or ""
    except Exception:
        return ""


@functools.lru_cache(maxsize=1)
def language() -> str:
    """`ru` или `en` на весь процесс; тесты меняют `STUDIO_AGENT_LANG` и зовут `language.cache_clear()`."""
    forced = os.environ.get("STUDIO_AGENT_LANG", "").strip().lower()
    if forced in LANGUAGES:
        return forced
    return "ru" if _system().strip().lower()[:2] == "ru" else "en"


def tr(text: str, *args) -> str:
    """Перевод русской строки и подстановка `{n}`; ключа нет в словаре — строка как есть."""
    if language() == "en":
        text = EN.get(text, text)
    if not args:
        return text
    return re.sub(r"\{(\d+)\}", lambda m: str(args[int(m[1])]) if int(m[1]) < len(args) else m[0], text)


EN = {
    # app: состояние канала (трей, консоль, меню Linux), отвязка
    "Мост уже запущен": "The bridge is already running",
    "не настроен": "not set up",
    "подключается…": "connecting…",
    "на связи": "online",
    "нет связи": "offline",
    "компьютер отозван": "computer revoked",
    "ключ используется другим мостом": "key is used by another bridge",
    "учётка отключена в студии": "account disabled in Studio",
    "Не удалось стереть ключ компьютера — закройте окно моста и нажмите «Отключиться» снова":
        "Could not erase the computer key. Close the bridge window and press “Disconnect” again",
    "Компьютер отключён, но прежний PIN стереть не удалось — после новой привязки смените или уберите его в "
    "окне моста": "The computer is disconnected, but the old PIN could not be erased. After linking again, change or "
                  "remove it in the bridge window",
    # enroll, setup: проверка формы и отказы привязки
    "Неверный логин или пароль": "Incorrect login or password",
    "Вход на паузе после неудач — попробуйте позже": "Sign-in paused after failed attempts. Try again later",
    "Имя компьютера — от 1 до 64 знаков": "Computer name must be 1 to 64 characters",
    "Адрес студии — https://…": "Studio address must be https://…",
    "Студия недоступна ({0})": "Studio unavailable ({0})",
    "неверный ответ": "invalid response",
    "Студия недоступна — компьютер остался в её списке: отзовите его в «Настройки» → «Компьютеры»":
        "Studio unavailable. The computer is still in its list: revoke it in “Settings” → “Computers”",
    "Введите код из письма и повторите пароль": "Enter the code from the email and repeat the password",
    "Код неверен или истёк. Для нового письма оставьте поле кода пустым":
        "The code is incorrect or expired. Leave the code field empty to get a new email",
    "Сначала подтвердите почту в браузере Студии": "Confirm your email in the Studio browser first",
    "Папки нет": "Folder not found",
    "PIN — от 4 до 8 цифр": "PIN must be 4 to 8 digits",
    "PIN не совпадает": "PINs do not match",
    # tray: значок, меню, уведомления
    "Компьютер подключён к студии": "Computer connected to Studio",
    "Компьютер отозван в студии": "Computer revoked in Studio",
    "Ключ используется другим мостом": "Key is used by another bridge",
    "Заданий: {0} · ждут ответа: {1}": "Tasks: {0} · awaiting reply: {1}",
    "заданий: {0} · ждут: {1}": "tasks: {0} · waiting: {1}",
    "рабочий стол": "desktop",
    "Удалённый рабочий стол подключён": "Remote desktop connected",
    "Удалённый рабочий стол не подключён": "Remote desktop not connected",
    "Открыть Studio Agent": "Open Studio Agent",
    "Завершить этот сеанс": "End this session",
    "Снять блок PIN": "Unlock PIN",
    "Открыть журнал": "Open log",
    "Отменить обновление": "Cancel update",
    "Отменить выход после завершения": "Cancel quit after completion",
    "Выйти после завершения · заданий: {0}": "Quit after completion · tasks: {0}",
    "Выход сейчас · заданий: {0}": "Quit now · tasks: {0}",
    # window: состояние и карточка «Подключение»
    "Не подключён": "Not connected",
    "На связи": "Online",
    "Подключается…": "Connecting…",
    "Нет связи — переподключаюсь": "Offline. Reconnecting",
    "Учётка отключена в студии — подключусь после включения":
        "Account disabled in Studio. Will reconnect after it is enabled",
    "Подключить снова — этот компьютер заберёт канал у другого моста":
        "Connect again: this computer will take the channel from the other bridge",
    "Отключиться": "Disconnect",
    "Войти заново": "Sign in again",
    "Подключить снова": "Connect again",
    "Отключить компьютер от студии? Студия забудет этот компьютер, идущие ходы прервутся. "
    "Чаты останутся на компьютере.": "Disconnect the computer from Studio? Studio will forget this computer and "
                                     "running tasks will stop. Chats stay on the computer.",
    "Отключить": "Disconnect",
    "ошибка моста": "bridge error",
    "Подключение": "Connection",
    "Работа": "Activity",
    "Настройки": "Settings",
    "Доступ": "Access",
    "Студия": "Studio",
    "Вход": "Sign-in",
    "Компьютер": "Computer",
    "Версия Studio": "Studio version",
    "Оперативная память": "Memory",
    "Неизвестна": "Unknown",
    "Нет связи": "Offline",
    "Проверка…": "Checking…",
    "Доступен · моделей: {0}": "Available · models: {0}",
    "Доступен": "Available",
    "Недоступен": "Unavailable",
    "Недоступен · нужен Node.js новее": "Unavailable · needs a newer Node.js",
    "Недоступно": "Unavailable",
    "Доступно {0} из {1} ГБ": "{0} of {1} GB available",
    # window: самопроверка; трей: задание ждёт ответа
    "Самопроверка": "Self-check",
    "Проверить": "Check",
    "Запись в каталог моста": "Writing to the agent folder",
    "Канал со студией": "Studio connection",
    "Удалённый рабочий стол": "Remote desktop",
    "Свободное место": "Free space",
    "Не найден": "Not found",
    "Не ответил вовремя": "Timed out",
    "Ошибка проверки": "Check failed",
    "Нужен Node.js новее": "Needs a newer Node.js",
    "Нет входа": "Not signed in",
    "Не настроены": "Not set up",
    "Нет записи": "Not writable",
    "Выключен в окне моста": "Turned off in the agent window",
    "Не поддерживается": "Not supported",
    "Нет разрешения на запись экрана": "No screen recording permission",
    "Нет разрешения на управление вводом": "No input control permission",
    "Мало места": "Low disk space",
    "В порядке": "OK",
    "вход есть": "signed in",
    "вход не проверен": "sign-in not checked",
    "доступно {0} из {1}": "{0} of {1} available",
    "свободно {0} ГБ": "{0} GB free",
    "Задание ждёт ответа в Студии": "A task is waiting for your reply in Studio",
    # window: вход
    "Адрес студии": "Studio address",
    "Логин или email": "Login or email",
    "Пароль": "Password",
    "Пароль нужен один раз и на компьютере не хранится": "The password is needed once and is not stored on the computer",
    "Код из письма": "Code from email",
    "Имя компьютера": "Computer name",
    "PIN (по желанию)": "PIN (optional)",
    "Ещё раз": "Repeat",
    "Подключить": "Connect",
    "Запускать при входе в macOS": "Start when signing in to macOS",
    "Запускать при входе в Windows": "Start when signing in to Windows",
    # window: работа и настройки
    "Работа на компьютере": "Activity on this computer",
    "Отменить выход": "Cancel quit",
    "Выйти после завершения": "Quit after completion",
    "Выйти сейчас": "Quit now",
    "Идёт заданий: {0}. Выйти сейчас и прервать их?": "Tasks running: {0}. Quit now and stop them?",
    "Выйти": "Quit",
    "Тема": "Theme",
    "Светлая": "Light",
    "Тёмная": "Dark",
    "Папки проектов": "Project folders",
    "Добавить…": "Add…",
    "Убрать": "Remove",
    "Нужна хотя бы одна папка проектов": "At least one project folder is required",
    "Одновременных заданий": "Concurrent tasks",
    "Число заданий — целое число больше нуля": "The number of tasks must be a whole number greater than zero",
    "Сохранить": "Save",
    "Отмена": "Cancel",
    "Разрешить удалённый рабочий стол": "Allow remote desktop",
    "Экран, ввод и буфер. Выбор сохраняется; меняется только здесь.":
        "Screen, input and clipboard. The choice is saved and can only be changed here.",
    "PIN не задан": "PIN not set",
    "PIN задан": "PIN set",
    "PIN заблокирован после 5 ошибок": "PIN locked after 5 failed attempts",
    "Задать PIN": "Set PIN",
    "Изменить PIN": "Change PIN",
    "Убрать PIN": "Remove PIN",
    "Снять блок": "Unlock",
    "Убрать PIN? Компьютер откроется в студии без PIN.": "Remove the PIN? The computer will open in Studio without a PIN.",
    "Новый PIN": "New PIN",
    # window: разрешения macOS
    "Подтвердите запрос macOS для Studio Agent. После выдачи права на запись экрана macOS может попросить перезапуск.":
        "Confirm the macOS prompt for Studio Agent. After granting screen recording, macOS may ask for a restart.",
    "Проверка разрешений macOS…": "Checking macOS permissions…",
    "Не удалось проверить разрешения macOS": "Could not check macOS permissions",
    "Откройте «Конфиденциальность и безопасность» в настройках macOS":
        "Open “Privacy & Security” in macOS System Settings",
    "Запись экрана…": "Screen Recording…",
    "Универсальный доступ…": "Accessibility…",
    "Удалённый рабочий стол…": "Remote Desktop…",
    "Запись экрана: {0}": "Screen recording: {0}",
    "Управление вводом: {0}": "Input control: {0}",
    "Графический сеанс: {0}": "Graphical session: {0}",
    "Разрешено": "Allowed",
    "Нет доступа": "No access",
    "Не удалось проверить": "Could not check",
    "Экран заблокирован": "Screen locked",
    "Недоступен — откройте Studio Agent из «Приложений»": "Unavailable. Open Studio Agent from Applications",
    # __main__: консоль Windows и macOS, общие вопросы консоли
    "Настройте мост: python -m studio_agent enroll": "Set up the bridge: python -m studio_agent enroll",
    "Компьютер уже привязан (логин {0}). Привязать заново? Прежний компьютер будет отозван в Студии. [да/нет] ":
        "This computer is already linked (login {0}). Link again? The previous computer will be revoked in Studio. "
        "[yes/no] ",
    "да": "yes",
    "нет": "no",
    "Логин": "Login",
    "Пароль: ": "Password: ",
    "Код из письма: ": "Code from email: ",
    "Привязан: компьютер {0}. Работающий мост подхватит ключ после перезапуска.":
        "Linked: computer {0}. A running bridge will pick up the key after a restart.",
    "Папки проектов:": "Project folders:",
    "Папка проектов (Enter — оставить прежние): ": "Project folder (Enter to keep the current ones): ",
    "Папка проектов: ": "Project folder: ",
    "Ещё папка проектов (Enter — закончить): ": "Another project folder (Enter to finish): ",
    "PIN (4–8 цифр): ": "PIN (4–8 digits): ",
    "PIN ещё раз: ": "PIN again: ",
    "PIN задан.": "PIN set.",
    "PIN убран.": "PIN removed.",
    "Блок PIN снят.": "PIN unlocked.",
    "не задан": "not set",
    "задан": "set",
    "заблокирован": "locked",
    "не читается": "unreadable",
    "есть": "present",
    "Адрес студии: {0}\nКомпьютер: {1}\nИмя компьютера: {2}\nПапки проектов: {3}\nКлюч устройства: {4}\nPIN: {5}":
        "Studio address: {0}\nComputer: {1}\nComputer name: {2}\nProject folders: {3}\nDevice key: {4}\nPIN: {5}",
    "Мост studio-agent": "studio-agent bridge",
    "канал к студии (по умолчанию — с треем)": "channel to Studio (with the tray by default)",
    "без трея, журнал ещё и в консоль": "without the tray, also log to the console",
    "привязать к студии": "link to Studio",
    "задать PIN": "set PIN",
    "убрать PIN": "remove PIN",
    "снять блок PIN": "unlock PIN",
    "настройки (без ключа)": "settings (without the key)",
    "интерактивные настройки и диагностика": "interactive settings and diagnostics",
    # linux_menu: меню и консоль Linux
    "Состояние и проверка CLI": "Status and CLI check",
    "Привязка к Студии": "Link to Studio",
    "Сбросить блокировку PIN": "Reset PIN lock",
    "Лимит заданий": "Task limit",
    "Переключить автозапуск": "Toggle autostart",
    "Запустить службу": "Start the service",
    "Запросить или отменить выход после заданий": "Request or cancel quit after tasks",
    "Разрешить рабочий стол / выбрать экраны": "Allow desktop / choose screens",
    "Выключить рабочий стол": "Turn off desktop",
    "Завершить текущий сеанс экрана": "End the current screen session",
    "Отменить подготовку обновления": "Cancel update preparation",
    "Отвязать компьютер": "Unlink computer",
    "Переподключиться к Студии": "Reconnect to Studio",
    "Журнал": "Log",
    "SSH и веб-консоль": "SSH and web console",
    "Закрыть меню": "Close menu",
    "остановлен": "stopped",
    "Студия: {0} (версия {1})\nУчётная запись: {2}\nКомпьютер: {3} (ID {4})\nПапки проектов: {5}\n"
    "Ключ устройства: {6}\nPIN: {7}\nАвтозапуск: {8}\nЗадания: {9} из {10}, ждут ответа: {11}\n"
    "Выход после заданий: {12}\nРабочий стол: {13}, сеанс активен: {14}.":
        "Studio: {0} (version {1})\nAccount: {2}\nComputer: {3} (ID {4})\nProject folders: {5}\n"
        "Device key: {6}\nPIN: {7}\nAutostart: {8}\nTasks: {9} of {10}, awaiting reply: {11}\n"
        "Quit after tasks: {12}\nDesktop: {13}, session active: {14}.",
    # строка «Рабочий стол» статуса: `desktop_linux._REASONS` (в студию те же слова уходят отказом по-русски — их
    # переводит словарь страницы `app/static/en.js`)
    "Доступен системный выбор экранов и устройств ввода": "System screen and input device chooser is available",
    "Нужен активный разблокированный графический сеанс Linux с systemd-logind":
        "Requires an active unlocked Linux graphical session with systemd-logind",
    "Нужны системные Python GI, libportal, GStreamer и модуль PipeWire":
        "Requires system Python GI, libportal, GStreamer and the PipeWire module",
    "Графическая оболочка должна поддерживать порталы RemoteDesktop и ScreenCast":
        "The desktop environment must support the RemoteDesktop and ScreenCast portals",
    "доступен": "available",
    "проверка": "checking",
    "недоступен": "unavailable",
    "недоступен: нужен Node.js новее": "unavailable: needs a newer Node.js",
    "{0}: CLI {1}, {2}, моделей {3}": "{0}: CLI {1}, {2}, models {3}",
    "Компьютер привязан.": "Computer linked.",
    "Новый PIN (4–8 цифр): ": "New PIN (4–8 digits): ",
    "Повторите PIN: ": "Repeat PIN: ",
    "Убрать PIN? [да/нет] ": "Remove the PIN? [yes/no] ",
    "Одновременных заданий: ": "Concurrent tasks: ",
    "Отвязать компьютер от Студии? [да/нет] ": "Unlink the computer from Studio? [yes/no] ",
    "Выйти и прервать выполняемые задания? [да/нет] ": "Quit and stop running tasks? [yes/no] ",
    "Введите число": "Enter a number",
    "Введите номер пункта.": "Enter an item number.",
    "Проверьте сохранённые параметры. Можно ввести их заново.": "Check the saved settings. You can enter them again.",
    "Enter оставляет значение, минус очищает поле.": "Enter keeps the value, a minus clears the field.",
    "Адрес SSH (имя или IP)": "SSH address (name or IP)",
    "Пользователь SSH": "SSH user",
    "Порт SSH": "SSH port",
    "HTTPS-адрес веб-консоли": "Web console HTTPS address",
    "Адреса терминала сохранены.": "Terminal addresses saved.",
    "Сначала запустите мост в графическом сеансе.": "Start the bridge in a graphical session first.",
    "Ошибка отключения": "Unlink failed",
    "Неизвестное действие": "Unknown action",
    "Слишком большой запрос": "Request too large",
    "Не удалось выполнить действие. Проверьте состояние агента.": "Could not perform the action. Check the agent status.",
    "Мост запускается или обновляется. Повторите действие.": "The bridge is starting or updating. Try again.",
    "Путь каталога моста слишком длинный для канала меню": "The bridge folder path is too long for the menu channel",
    "Каталог канала меню принадлежит другой учётной записи": "The menu channel folder belongs to another user account",
    "Другая учётная запись": "Another user account",
}
