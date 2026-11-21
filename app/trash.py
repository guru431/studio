"""Корзина (шаг схемы 58): удалённая запись истории сначала получает `deleted` — время переноса — и уходит из
лент и поиска (триггеры `search_<таблица>_hide`); окончательно её удаляет очистка или владелец.

- Таблицы с корзиной — `TABLES` (тот же набор, что `db._TRASH_58` шага 58).
- Прежний код (до шага 58) столбец `deleted` не читает и показывает записи корзины как обычные: перед откатом
  `ROLLBACK_58` окончательно удаляет корзину (с каскадами), файлы потом убирает чистка сирот прежнего кода.
"""

TABLES = ("chats", "images", "videos", "speeches", "transcripts", "scans", "translations", "voices",
          "chat_media", "knowledge_bases", "kb_documents")

ROLLBACK_58 = "BEGIN;\n" + "".join(f"DELETE FROM {t} WHERE deleted IS NOT NULL;\n" for t in TABLES) + "COMMIT;\n"
