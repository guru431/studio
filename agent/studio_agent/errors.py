"""Отказ просьбы студии: код протокола и слова для страницы."""

# Слова по умолчанию (план, «Тексты»). У not_found и too_large слов несколько — точные передаёт
# вызывающий; здесь — первое у not_found и общее «предел канала» у too_large.
WORDS = {
    "desktop_disabled": "Рабочий стол выключен. Включите его в агенте на компьютере",
    "desktop_unavailable": "Рабочий стол недоступен — войдите в сеанс на компьютере",
    "closed": "компьютер закрыт",
    "pin": "неверный PIN",
    "pin_locked": "PIN заблокирован — снимите блок на самом компьютере",
    "pin_needed": "нужен PIN",
    "not_found": "нет такого проекта",
    "outside": "путь вне проекта",
    "too_large": "ответ больше предела канала",
    "binary": "двоичный файл",
    "conflict": "файл изменился",
    "running": "в чате уже идёт ход",
    "busy": "достигнут лимит одновременных заданий — дождитесь завершения",
    "idle": "хода нет",
    "gone": "запроса уже нет",
    "not_git": "не репозиторий git",
    "git_unavailable": "git не смог прочитать репозиторий",
    "invalid": "неверные поля",
    "unsupported": "помощник не установлен на компьютере",
    "error": "ошибка моста",
}


class Refused(Exception):
    """Отказ просьбы: `code`, `message` (без слов — из WORDS), доп. поля ответа (`left`, `version`)."""

    def __init__(self, code: str, message: str = "", **extra):
        self.code = code
        self.message = message or WORDS[code]
        self.extra = extra
        super().__init__(code)
