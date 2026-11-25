"""Пределы протокола v1 и моста (Global Constraints плана)."""

import re

MIB = 1024 * 1024

FRAME_MAX = 48 * MIB  # кадр канала с base64 вложений до 30 МиБ
WELCOME_SEC = 10  # ждать welcome после hello
PIN_TRIES = 5  # неверных PIN подряд до блока
OPEN_IDLE_SEC = 3 * 60 * 60  # открытая сессия без просьб: 3 часа
TEXT_MAX = 2 * MIB  # текст файла
VISUALIZATION_MAX = 10 * MIB  # HTML с вложенными данными, отдельно от редактора
RAW_MAX = 10 * MIB  # raw (PDF)
IMAGE_COUNT = 12  # картинок хода; мост объявляет в `hello.limits` (0.48.0)
IMAGE_MAX = 10 * MIB  # картинка или файл хода
IMAGES_MAX = 30 * MIB  # картинки и файлы одного хода вместе
ATTACHMENT_COUNT = 12  # файлы и картинки вместе
PATH_MAX = 1024  # знаков пути
SKILL_MAX = 64 * 1024  # файл навыка проекта `SKILL.md` (`skills`)
SKILLS_MAX = 100  # навыков проекта в списке; дальше — `truncated`
CLIP = 20_000  # строки событий и шагов, дальше — «…»
EVENT_MAX = 1 * MIB  # событие хода в ASCII-JSON; больше — урезка строже (CLIP_TIGHT), затем ввод пометкой
CLIP_TIGHT = 2000
MESSAGE_MAX = 300  # message отказа
HISTORY_BUDGET = 8 * MIB  # страница истории и снимок
FILES_MAX = 5000  # файлов в списке
CHATS_MAX = 200  # чатов проекта
GIT_FILES_MAX = 2000  # файлов git
MAX_TURNS = 12  # одновременных ходов по умолчанию; меняется в настройках моста
QUEUE_PAUSE_SEC = 30 * 60  # пауза очереди без правок; дольше — ход кончается, очередь — в done.unsent
# CLI сообщил idle, а ход ещё ждёт ответа на свой ввод (фоновой работы нет): дольше — ход кончается ошибкой.
# Штатно после такого idle CLI за миллисекунды шлёт `running` или ResultMessage; 2 минуты — запас на перегруз.
IDLE_INPUT_SEC = 120
COMPLETIONS_MAX = 50  # завершений ходов без канала в памяти, по одному на чат (протокол §18)
WATCHED_MAX = 64  # окон, что смотрят чат, в ответе на карточку (`answer.watched`, протокол §25)
SELF_CHECK_SEC = 5  # предел каждой проверки самопроверки моста (`selfcheck`)
LOW_DISK = 1024 * MIB  # свободного места на диске моста меньше — предупреждение самопроверки
LOG_TAIL = 200  # строк `agent.log` для Студии (`journal.tail`)
LOG_LINE_MAX = 2000  # знаков строки журнала для Студии
WAIT_NOTICE_SEC = 60  # задание ждёт ответа дольше — одно уведомление трея (`app.WaitNotice`)
BACKOFF = (1, 2, 4, 8, 16, 32, 60)  # паузы переподключения, с
PROTOCOL_PAUSE_SEC = 60  # пауза после закрытия 4400
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
CHAT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # UUID строчными
# Внешние HTTPS-адреса, к которым мост ходит сам, помимо студии из `config.json` и шлюза OpenCode из конфига
# пользователя. Перечень в CLAUDE.md и agent/CLAUDE.md и литералы адресов в коде моста сверяет tests/test_docs.py.
HOSTS = {
    "ipify": "api64.ipify.org",                    # внешний IP компьютера
    "npm": "registry.npmjs.org",                   # версии и обновление CLI
    "marketplace": "marketplace.visualstudio.com",  # версии расширений VS Code
    "homebrew": "formulae.brew.sh",                # версии Homebrew (macOS)
    "claude_releases": "downloads.claude.ai",      # версия Claude Code (macOS)
    "anthropic": "api.anthropic.com",              # лимиты подписки Claude, OAuth-токен Claude Code
    "opencode": "opencode.ai",                     # лимиты OpenCode Go, ключ OpenCode
    "deepseek": "api.deepseek.com",                # баланс DeepSeek, ключ DeepSeek
    "github": "github.com",                        # анонимная проверка публичной копии проекта
}
