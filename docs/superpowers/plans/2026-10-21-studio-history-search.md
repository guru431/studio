# Поиск по истории и фильтры — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** поиск своей истории (чат, фото, видео, расшифровка, озвучка) по словам и фильтрам — экран «Поиск» со значком-лупой.

**Architecture:** документы поиска — обычная таблица `search_docs` (по строке на промпт, сообщение, название диалога,
документ, расшифровку, озвучку), её держат в лад триггеры таблиц истории (шаг схемы 13 — верны и при откате
кода). Над ней — FTS5 `search` с внешним содержимым (`content='search_docs'`): удаление и фильтры идут по
индексам `search_docs`, а не полным просмотром FTS. «ё»→«е» — только в тексте, что уходит в FTS (триггеры
`search_docs`), содержимое — исходное: `snippet()` выделяет по позициям токенов, а их число замена не меняет.
`POST /api/search` (тело) собирает запрос в `app/search.py`; страница — `app/static/js/search.js`.

**Tech Stack:** Python 3.13, FastAPI, SQLite FTS5 (3.46 прод), ES-модули без сборки.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-history-search-design.md](../specs/2026-10-21-studio-history-search-design.md)

## Global Constraints

- Слова поиска и фильтры — не в журнал службы и не в строку запроса; журнал — `search: N`.
- Только `user_id` сессии; чужое не находится ни словами, ни фильтрами, ни моделями.
- Шаг схемы только добавляет; прежний код (откат `install.sh`) пишет в таблицы истории, и индекс остаётся верным.
- База — только с цикла событий; файлы итога расшифровок — в потоке.
- `q` до 200 знаков, до 8 слов, слово — `"слово"*`, AND; `limit` до 50 (по умолчанию 30); `section` —
  `chat|photo|video|transcribe|speech`; `since`/`until` — `ГГГГ-ММ-ДД` по часам хоста, `until` включительно.
- Маркеры выделения `\u0002…\u0003`; страница строит выделение узлами, не HTML.
- Поиск 20 000 строк — быстрее 0,2 с.

## Review Focus

1. Ответ чата во время потока (`streaming`, текст пишется каждые `SAVE_SEC`) — не находится; после «готово» — находится (тест в задаче 1).
2. Удаление диалога каскадом и стирание истории — строки сообщений, названий и документов уходят (задача 1).
3. Запрос из одних знаков (`"*()`, `NEAR`, `OR`) — без 500: слова — только буквы и цифры (задача 3).
4. Курсор страниц на строках одного времени — без пропусков и повторов (задача 3).
5. Расшифровка, сделанная старым кодом после отката (без `text`), находится по имени сразу и по тексту после фона (задача 2).

---

### Task 1: Схема — шаг 13, индекс и триггеры

**Files:** Modify `app/db.py`; Test `tests/test_foundation.py`, `tests/test_search.py` (новый).

**Interfaces — Produces:** `db.VERSION = 13`; `db.SEARCH_13` (SQL шага); таблица `search_docs(id INTEGER PRIMARY KEY,
section, item, part, user_id, model, created, body)` — `part` = `''` у фото, видео, расшифровки, озвучки; у чата —
`'title'`, id сообщения или id вложения; `item` — id записи (у чата — id диалога). FTS5 `search(body)`.
`transcripts.text TEXT`.

- [ ] Тесты: `test_migrate_adds_the_search_index_to_a_version_12_base` (строки всех разделов до шага → после шага
  находятся `MATCH`; `streaming`-сообщение и неотправленный документ — нет); `DROP_V13` первой во всех цепочках;
  `test_migrate_creates_schema_once…` — версия 13. В `test_search.py`: каскады — удаление диалога, учётки;
  смена промпта ролика; ответ чата во время потока не найден, после — найден.
- [ ] Реализация: `SEARCH_13` = таблицы, индексы `search_docs_key UNIQUE(section, item, part)`,
  `search_docs_user(user_id, created)`, `search_docs_part(part)`, триггеры (см. спеку 5) и заполнение
  `INSERT … SELECT`; в `SCHEMA` и `STEPS[13]`.
- [ ] Прогон `tests/test_foundation.py tests/test_search.py`, коммит.

### Task 2: `transcripts.text` и фон `backfill_text`

**Files:** Modify `app/transcripts.py`, `app/main.py`; Test `tests/test_search.py`.

**Interfaces — Produces:** `transcripts.text_of(row, result) -> str` (реплики `view` через пробел);
`async transcripts.backfill_text() -> int` (сколько заполнено); `transcripts.start_backfill() -> asyncio.Task`
(в `loops` lifespan под `state.WATCHER`).

- [ ] Тесты: готовая расшифровка находится по слову реплики; правка меняет найденное, сброс — возвращает;
  `backfill_text` — строка без `text` с файлом заполняется, файл недоступен (OSError) — остаётся NULL и фон
  кончается, отсутствующий файл — пропуск.
- [ ] Реализация: `text` в итоге `run`; в `patch_transcript` и `reset_edits` — после чтения файла, без await до UPDATE.
- [ ] Прогон `tests/test_transcripts.py tests/test_search.py`, коммит.

### Task 3: `POST /api/search`, `GET /api/search/models`

**Files:** Create `app/search.py`; Modify `app/main.py`; Test `tests/test_search.py`.

**Interfaces — Produces:** ответ `{items: [{section, id, part, title, snippet, model, created, href}], more}`;
`before` — `{created, item, part}` последней строки; `{models: {раздел: [имена]}}`.

- [ ] Тесты: каждый раздел по началу слова, регистр и «ё»; чужое; фильтры раздела, модели, дат; курсор;
  диалог — один раз; удаление записи и «Удалить всю историю»; спецсимволы; 400 пустого; 401 и CSRF; журнал
  без слов; модели; 20 000 строк < 0,2 с.
- [ ] Реализация: слова — `[^\W_]+`, «ё»→«е»; выборка — `row_number() OVER (PARTITION BY section, item ORDER BY
  created DESC, part DESC)` = 1, курсор row value `(created, item, part) < (?, ?, ?)`, `limit + 1`;
  `snippet()` — отдельным запросом на строку страницы (`MATCH` и `rowid`); заголовки — из таблиц разделов.
- [ ] Прогон, коммит.

### Task 4: Экран «Поиск»

**Files:** Create `app/static/js/search.js`; Modify `app/static/js/app.js`, `app/static/index.html`,
`app/static/css/app.css`; Test `tests/test_ui_browser.py`.

- [ ] Браузерный тест: лупа `#search-open` → `#/search`; слово → строка `.search-row` с `mark`; нажатие →
  раздел с записью; чип раздела сужает выдачу.
- [ ] Реализация: раздел `search` в `SECTIONS`, ссылка-лупа в полосе над «В работе» (телефон — `right: 138px`,
  шапка — отступ под четыре кнопки); поле с паузой 300 мс и Enter, `attachMic`; чипы раздела, выбор модели,
  две даты и «сбросить»; дни как в лентах (`dayKey`); «Показать ещё»; «Ничего не нашлось»; состояние — в
  памяти модуля.
- [ ] Прогон браузерных `-k "search or jobs or login_shell or rail or layout_fits or phone_avatar or wipe"`, коммит.

### Task 5: Документы

**Files:** README.md (раздел «Поиск»), CLAUDE.md (индекс `search`, триггеры, `transcripts.text`, POST).

- [ ] Быстрый набор, коммит.
