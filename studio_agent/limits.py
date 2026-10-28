"""Пределы протокола v1 и моста (Global Constraints плана)."""

import re

MIB = 1024 * 1024

FRAME_MAX = 16 * MIB  # кадр канала
WELCOME_SEC = 10  # ждать welcome после hello
PIN_TRIES = 5  # неверных PIN подряд до блока
OPEN_IDLE_SEC = 1800  # открытая сессия без просьб
TEXT_MAX = 2 * MIB  # текст файла
RAW_MAX = 10 * MIB  # raw (PDF)
PATH_MAX = 1024  # знаков пути
CLIP = 20_000  # строки событий и шагов, дальше — «…»
EVENT_MAX = 1 * MIB  # событие хода в ASCII-JSON; больше — урезка строже (CLIP_TIGHT), затем ввод пометкой
CLIP_TIGHT = 2000
MESSAGE_MAX = 300  # message отказа
HISTORY_BUDGET = 8 * MIB  # страница истории и снимок
FILES_MAX = 5000  # файлов в списке
CHATS_MAX = 200  # чатов проекта
GIT_FILES_MAX = 2000  # файлов git
MAX_TURNS = 2  # одновременных ходов
BACKOFF = (1, 2, 4, 8, 16, 32, 60)  # паузы переподключения, с
PROTOCOL_PAUSE_SEC = 60  # пауза после закрытия 4400
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
CHAT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # UUID строчными
