# Studio 0.41.0 — всё оставшееся одним планом (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** в одной сессии выпустить всё, что утверждено владельцем и ещё не сделано: пункты 2, 5, 6, 8, 9, 11–24,
26–28, 30–39 спеки возможностей источников, единый вид разделов «как Агенты», 10 вложений в сообщении, доработки
обёртки Spark и панелей серверов — без деления на выпуски и без «потом».

**Architecture:** Studio остаётся универсальной: источник (API моделей или MCP) приносит требования стандартными
средствами MCP (`instructions`, `prompts`, `resources`, `annotations`, `capabilities.experimental`, `_meta`) или
профилем подключения. Новое в Studio — общие механизмы (пауза хода, корзина, контракты файлов и состояния, знания,
речь частями); Spark — только образец, его особенности живут в обёртке aitools вне репозитория. Один шаг схемы 58.

**Tech Stack:** FastAPI, SQLite (FTS5), httpx, Fernet (`STUDIO_DATA_KEY`), ES-модули без сборки, ffmpeg, yt-dlp;
обёртка — MCP SDK 2.3 lowlevel + Starlette; панель s06 — FastAPI + `ai-ctl` (проект vLLM).

**Spec:** [2026-11-21-studio-source-capabilities-design.md](../specs/2026-11-21-studio-source-capabilities-design.md)
(номера пунктов ниже — оттуда); опора — [техдизайн основы 0.40](../specs/2026-11-21-studio-source-foundation-design.md),
[группы доступа](../specs/2026-11-20-studio-access-groups-design.md); правила — CLAUDE.md, `agent/CLAUDE.md`, CLAUDE.md
проекта vLLM. Решения владельца в спеке и в этом плане не пересматривать.

## Как выполнять

- Одна сессия, все задачи. Исполнители — агенты `implementer` в worktree, не больше 4 одновременно, волнами ниже.
  Первое действие исполнителя — `git merge --ff-only main` (worktree создаётся от коммита начала сессии). На одного
  исполнителя — одна задача (не больше 5–6 пунктов), коммит после каждого пункта.
- Слияние в `main` — cherry-pick; конфликты дописывания (`admin.py`, `en.js`, `app.css`, `chat.js`, `chat_tools.py`,
  `trash.KINDS`, карта модулей CLAUDE.md) — оставлять обе стороны. После каждого слияния — быстрый набор.
- Ход не завершать, пока работают исполнители (запускать их на переднем плане, по 4 в одном сообщении).
- Push, выкладка Studio и включение живых ключей — не делать (⛔ ниже); выкладку Studio делает Codex одним SHA на оба
  экземпляра. Вопросы владельцу — одним сообщением в конце (раздел «Решения владельца в конце»).
- Находки вне задач — в `FINDINGS.md`; выпущенное из `IDEAS.md` — удалить запись, заголовок — в сообщение коммита.

| Волна | Задачи (параллельно) | Ждёт |
|---|---|---|
| A | 1 Схема 58 · 2 Выбор-кнопка и сторож дизайна · 6 Обёртка: контракты · 16 Панель s06 | — |
| B | 3 Дизайн разделов · 4 Пауза хода · 5 Корзина · 13 Обёртка: голоса, МТС, письмо | A (13 — после 6) |
| C | 7 Поле ввода чата · 8 Озвучка · 9 Контракты MCP в Studio · 10 Расшифровка | B (9 — после 6) |
| D | 11 Защищённый источник, план, память · 12 Знания · 14 «Записать» и «Разговор» · 15 ZIP, deno, «Скачать ролик» | C |
| E | 17 srvllm (⛔ если нет checkout `llm_routers`) · 18 Завершение | D |

Команды: быстрый набор `.venv/Scripts/python -m pytest -q`; файл — `.venv/Scripts/python -m pytest -q -n0 <файл>`;
браузерный файл — `.venv/Scripts/python -m pytest -q -n0 -m integration <файл>`; обёртка — в `D:\vsc\_work\aitools-studio`
командой из её README; vLLM — `python -m pytest` в `D:\vsc\vLLM` (CLAUDE.md проекта).

## Global Constraints

- Версия Studio — `app/release.py` `VERSION = "0.41.0"`; схема — `db.VERSION = 58`, **один** шаг `db.STEPS[58]`,
  только добавление; новых столбцов `*media_id` нет (иначе их ждёт `media.REFERENCES`, а чистка сирот прежнего кода
  останавливается).
- Приватность: в журнал — только id, коды и числа; тексты, имена файлов, пути, аргументы инструментов — никогда.
  Пользовательские тексты и секреты в новых таблицах — шифром `STUDIO_DATA_KEY` там, где указано.
- Администратор чужого не видит: корзина, «Мои файлы», ссылки, память, личные базы знаний — только владелец.
- Анонимные входы — только перечисленные в CLAUDE.md; новый (`/f`) добавляется туда с его защитой (задача 9).
- Сервер ходит только по списку адресов (`upstream.is_allowed`); MCP — через `upstream.stream(..., keyless=True)`.
- Подписи — через `tr`/`data-i18n` и `static/en.js`; значки — `icons.js`; меню — `menu.js`; цвета — `light-dark()`;
  `:hover` — только в `@media (hover: hover)`; имена разделов и страниц — `sections-meta.js` + `sections.NAMES`.
- Тесты: без реального времени (`clock.NOW`, `clock.SLEEP`), шлюз — `tests/fakes.py`, дольше 1 с — `integration`;
  браузер — 320, 390, 1280 px (Chromium) и 390 px WebKit для мобильного вида. Тесты, ожидающие запрос модели без
  `tools`, обновляются под встроенные инструменты (задача 4).
- Числа плана (копировать как есть): вложений в сообщении `chat.ATTACH_MAX = 10`; картинок модели по умолчанию
  `profiles.DEFAULT_MAX_IMAGES = 4` (1–32); файлов на MCP за ход `mcp_files.PER_TURN = 10`; кругов инструментов по
  умолчанию `chat.TOOL_ROUNDS = 15` (1–30); ожидание ответа на паузе `chat_pending.PENDING_TTL_SEC = 86400`; корзина
  `trash.TRASH_SEC = 14 * 86400`; ссылка на файл `my_files.LINK_SEC = 86400`; кусок загрузки `my_files.CHUNK = 32 MiB`;
  текст «Озвучки» `speeches.LONG_MAX = 200_000`; запись «Расшифровки» `transcripts.TOTAL_MAX = 4 * 3600`; знания —
  `knowledge.CHUNK_CHARS = 1500`, `OVERLAP = 200`, `INLINE_TOKENS = 30_000`, `TOP_K = 6`; память — `memory.ITEM_MAX = 500`,
  `MAX_ITEMS = 100`, `BLOCK_MAX = 4000`; состояние источника `mcp_contracts.STATUS_TTL_SEC = 15`; архив чата
  `chat_exports.ARCHIVE_MAX = 512 MiB`.

## Review Focus

1. **Ответ на паузу хода дважды или поздно** (два устройства, после перезапуска службы, после нового сообщения) —
   выигрывает первый ответ, остальные получают 409 `pending_gone`; карточка переживает перезапуск; новое сообщение
   снимает ожидание. Тесты — задача 4.
2. **Запись в корзине, на которую ссылаются** (ссылка «Поделиться», продолжение озвучки, скан перевода, поиск,
   «В работе», выгрузка всей истории) — нигде не видна, ссылка «недоступна»; после «Вернуть» всё на месте; уборка
   удаляет файлы один раз. Тесты — задача 5.
3. **10 разных вложений и модель с пределом 4 картинки** — без 422: модель получает 4 последние картинки, остальные
   помечены `chat.DROPPED`, MCP с согласием получает все. Тесты — задача 7.
4. **Анонимная ссылка на файл** — истёкшая, отозванная, чужой `Origin`, перебор, файл удалён на сервере MCP, владелец
   потерял доступ к серверу — одинаковый 404 (или 429 при переборе), не 500, имя файла не раскрывается. Тесты — задача 9.
5. **Телефон 320 px на английском** (длинные слова) — шапка в одну строку, без горизонтальной прокрутки, без
   увеличения страницы при фокусе поля. Тесты — задачи 2 и 3.

---

## Волна A

### Task 1: Шаг схемы 58, версия 0.41.0, строка отката

**Files:**
- Modify: `app/db.py` (константа `REMAINING_58`, `SCHEMA += REMAINING_58`, `STEPS[58]`, `VERSION = 58`)
- Modify: `app/release.py` (`VERSION = "0.41.0"`)
- Modify: `CLAUDE.md` (строка 58 таблицы «Откат по шагам схемы»)
- Create: `app/trash.py` (только `TABLES` и `ROLLBACK_58` — остальное в задаче 5)
- Test: `tests/test_foundation.py` (`== db.VERSION == 58`), новый `test_step_58_...`

**Interfaces:**
- Produces (все задачи опираются на эти имена):
  - `chat_pending(id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE, message_id TEXT NOT
    NULL REFERENCES messages ON DELETE CASCADE, kind TEXT NOT NULL CHECK (kind IN ('confirm','ask')), payload TEXT NOT
    NULL, created REAL NOT NULL, resolved REAL, outcome TEXT CHECK (outcome IN ('run','refuse','answer','superseded',
    'expired')))`, индекс `chat_pending_open ON chat_pending(chat_id) WHERE resolved IS NULL`.
  - `messages.todo TEXT` (JSON); `users.memory_on INTEGER NOT NULL DEFAULT 0`; `user_memory(id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, text_enc TEXT NOT NULL, created REAL NOT NULL)`.
  - `deleted REAL` у `chats`, `images`, `videos`, `speeches`, `transcripts`, `scans`, `translations`, `voices`,
    `chat_media`, `knowledge_bases`, `kb_documents`; частичные индексы `<t>_trash ... WHERE deleted IS NOT NULL`.
  - Триггеры поиска `search_<t>_hide AFTER UPDATE OF deleted ON <t> WHEN NEW.deleted IS NOT NULL` — удаляют строки
    `search_docs` записи (у `chats` — и строки его сообщений и вложений) для `chats`, `images`, `videos`, `speeches`,
    `transcripts`, `scans`, `translations`.
  - `file_links(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL REFERENCES users ON
    DELETE CASCADE, server_id INTEGER NOT NULL REFERENCES mcp_servers ON DELETE CASCADE, stamp TEXT NOT NULL, path_enc
    TEXT NOT NULL, name_enc TEXT NOT NULL, size INTEGER, created REAL NOT NULL, expires REAL NOT NULL, revoked REAL)`,
    индекс по `user_id`.
  - `mcp_servers.protected INTEGER NOT NULL DEFAULT 0`; `chats.protected_used INTEGER NOT NULL DEFAULT 0`.
  - `knowledge_bases(id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users ON DELETE CASCADE, name TEXT NOT NULL,
    created REAL NOT NULL, deleted REAL)` (`user_id NULL` — общая); `kb_documents(id TEXT PRIMARY KEY, kb_id TEXT
    REFERENCES knowledge_bases ON DELETE CASCADE, attachment_id TEXT REFERENCES attachments ON DELETE CASCADE, user_id
    INTEGER REFERENCES users ON DELETE CASCADE, name TEXT NOT NULL, chars INTEGER NOT NULL DEFAULT 0, state TEXT NOT
    NULL CHECK (state IN ('queued','ready','error')), created REAL NOT NULL, deleted REAL, CHECK ((kb_id IS NULL) <>
    (attachment_id IS NULL)))`; `kb_chunks(id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES kb_documents ON
    DELETE CASCADE, n INTEGER NOT NULL, text TEXT NOT NULL, vector BLOB)`; `kb_fts` — FTS5 `content='kb_chunks'`,
    `content_rowid='id'`, `tokenize='unicode61 remove_diacritics 2'` с триггерами `kb_chunks_ai`/`kb_chunks_ad`;
    `kb_rules(kb_id TEXT NOT NULL REFERENCES knowledge_bases ON DELETE CASCADE, group_id INTEGER NOT NULL REFERENCES
    access_groups ON DELETE CASCADE, PRIMARY KEY (kb_id, group_id))`; `chat_kbs(chat_id TEXT NOT NULL REFERENCES chats
    ON DELETE CASCADE, kb_id TEXT NOT NULL REFERENCES knowledge_bases ON DELETE CASCADE, PRIMARY KEY (chat_id, kb_id))`.
  - `transcripts.language TEXT`, `speeches.language TEXT` (NULL — авто/как раньше).
  - `trash.TABLES = ("chats", "images", "videos", "speeches", "transcripts", "scans", "translations", "voices",
    "chat_media", "knowledge_bases", "kb_documents")`; `trash.ROLLBACK_58: str` — `DELETE FROM <t> WHERE deleted IS NOT
    NULL;` по всем `TABLES` (с каскадами; файлы потом убирает чистка сирот прежнего кода).

- [ ] **Step 1: Write the failing test** `tests/test_foundation.py::test_step_58_adds_pause_trash_links_knowledge_and_previous_writer_works`:

```python
conn = ...  # база 0.40.1: historical_schema.install_schema(conn, 57), миграция — как в test_step_57_...
assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 58
for t in trash.TABLES: assert "deleted" in cols(conn, t)
assert {"chat_pending", "file_links", "user_memory", "knowledge_bases", "kb_documents",
        "kb_chunks", "kb_rules", "chat_kbs"} <= tables(conn)
# INSERT прежним кодом (без новых столбцов) в chats/images — работает, deleted IS NULL
# UPDATE images SET deleted=1 → строки search_docs этой записи нет
# INSERT в kb_chunks → kb_fts MATCH находит; DELETE — не находит
# conn.executescript(trash.ROLLBACK_58) удаляет только строки с deleted
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_foundation.py` — Expected: FAIL (версия 57).
- [ ] **Step 3: Implement** `REMAINING_58` в `app/db.py` по образцу `CAPABILITIES_57` (строка SQL, `SCHEMA +=`,
  `STEPS[58]` с комментарием); `release.py` → `0.41.0`; `app/trash.py` с `TABLES`, `ROLLBACK_58`. Строка 58 в CLAUDE.md:
  «`chat_pending`, корзина (`deleted`), `file_links`, `mcp_servers.protected`, память, знания, язык речи | новые таблицы
  не читает: ждущие подтверждения ответы выглядят законченными, ответить на них нельзя; записи из корзины снова видны
  как обычные; ссылок на файлы, памяти и знаний нет | перед откатом — `trash.ROLLBACK_58` через `sql` (окончательно
  удаляет корзину)». Обновить `test_migrate_creates_schema_once...` и `test_step_57_...` (`== 58`).
- [ ] **Step 4: Run** `tests/test_foundation.py tests/test_docs.py tests/test_backup_check.py tests/test_media_orphans.py` — PASS.
- [ ] **Step 5: Commit** `Add schema step 58 for pause, trash, file links, memory and knowledge`.

### Task 2: Выбор-кнопка «как у Агентов», токены управления, сторож дизайна

Причина «скачущих» размеров (замер 2026-11-21): правило `@media (pointer: coarse)` в `app.css` (~2051) делает каждый
видимый `<select>` не меньше 16 px, а `button.chip` остаётся 13 px; «Агенты» прячут настоящий `<select>` (16 px —
против увеличения iOS) и показывают кнопку `.code-pick-trigger` 12 px (`code-picker.js`). Решение — тот же приём везде.

**Files:**
- Create: `app/static/js/picker.js` (перенос `code-picker.js`: `export function picker(host, label, select)`);
  `code-picker.js` удалить, импорт в `code-chat.js` заменить.
- Modify: `app/static/css/app.css` — токены и общие классы; `.code-pick*` → `.pick*` (одно место).
- Create: `tests/test_ui_design_consistency.py` (integration).
- Modify: `tests/test_css_tokens.py`, `docs/architecture/shell.md` (раздел «Размеры управления»).

**Interfaces:**
- Produces: `picker(host: HTMLElement, label: string, select: HTMLSelectElement) -> {wrap, trigger, sync()}`
  (поведение `codePicker` без изменений; `sync()` перерисовывает подпись после программной смены значения или
  опций); токены в `:root`: `--ctl-font: var(--fs-xs)`, `--ctl-font-tight: var(--fs-2xs)`, `--ctl-h: 30px`,
  `--ctl-pad-y: 6px`, `--ctl-pad-x: 8px`, `--ctl-radius: 9px`, `--head-h: 48px`, `--title-font: var(--fs-lg)`,
  `--empty-font` (замер пустой панели «Агентов»); классы `.pick`, `.pick-trigger` (бывшие `.code-pick*`), `.ctl-chip`
  (кнопка-чип ряда управления), `.ctl-primary` (главная кнопка действия в ряду: высота `--ctl-h`, шрифт `--ctl-font`,
  акцент); у `.composer` — `container: composer / inline-size`, правила `@container composer (max-width: 550px)` от
  «Агентов» для всех полей ввода (шрифт выбора `--ctl-font-tight`, главная кнопка — только значок, зазор 4 px).
  `.code-composer` больше не задаёт свои `--code-*-font` — берёт `--ctl-*`.

- [ ] **Step 1: Write the failing test** `tests/test_ui_design_consistency.py` (помощники `engine`, `shell`, `cookie_for`
  из `tests/test_ui_design_tokens.py`, фикстура `iphone` из `tests/test_ui_webkit.py`; данные — как в
  `tests/test_ui_section_limits.py` (MCP с инструментами разделов) плюс два подключения с OCR и переводом, чтобы
  появились выборы модели):

```python
SECTIONS = ["code", "chat", "photo", "video", "speech", "transcribe", "ocr", "translate"]
WIDTHS, LANGS = [320, 390, 1280], ["ru", "en"]
# параметризация section × width × lang; разделы кроме "code" — xfail(strict=True) до задачи 3

def test_head_is_one_line_and_title_is_whole(...):
    assert head.height <= 56 and (h1 is None or h1.scrollWidth <= h1.clientWidth)

def test_composer_controls_match_agents(...):
    ref = measure("code", ".pick-trigger")                     # эталон на той же ширине
    for el in visible(".composer :is(.pick-trigger,.ctl-chip,.chip), "
                      ".split-toolbar :is(.pick-trigger,.ctl-chip,.ctl-primary)"):
        assert el.font_size == ref.font_size and abs(el.height - ref.height) <= 1
    assert count_visible("select:not(.pick select)") == 0     # видимых системных выборов нет

def test_no_horizontal_scroll(...):
    assert document.scrollWidth <= innerWidth

def test_fields_do_not_zoom_on_iphone(iphone, ...):            # WebKit 390
    assert all(fs >= 16 for fs in font_sizes("input, textarea, .pick select"))
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_design_consistency.py` —
  Expected: FAIL для `code` (нет `.pick-trigger`), xfail для остальных.
- [ ] **Step 3: Implement** `picker.js`, токены, `.pick*`, `.ctl-chip`, `.ctl-primary`, контейнер `composer`; «Агенты»
  — на `picker` без видимых изменений. `test_css_tokens.py`: в блоках `.pick-trigger`, `.ctl-chip`, `.ctl-primary`,
  `.composer .chip`, `.head h1` свойство `font-size` — только через `var(--…)`.
- [ ] **Step 4: Run** новый файл + `tests/test_ui_webkit.py tests/test_ui_shared_composer.py tests/test_css_tokens.py` —
  PASS (`code` проходит; остальные xfail).
- [ ] **Step 5: Commit** `Share the Agents picker and control tokens across sections`.

### Task 6: Обёртка aitools — контракты файлов, файлов пользователя, состояния, руководства, пределов, протокола

Вне репозитория: `D:\vsc\_work\aitools-studio` (локальный git). Open WebUI, `aitools_mcp.py` и панель Spark не трогать.
Имена контракта общие для обеих сторон; описание для любых серверов — `docs/architecture/mcp-contracts.md` (задача 9).

**Files:**
- Modify: `aitools_studio.py`; Create: `files_api.py` (маршруты файлов и корзины), `README.md` (контракт и env).
- Modify: `hints.example.json` (`"limits"`); Test: `tests/test_files_api.py`, `tests/test_aitools_studio.py`.

**Interfaces:**
- Produces (HTTP за тем же `Guard`: Bearer, `X-Studio-User`, папка `<ROOT>/<X-Studio-User>`; скрытые `.studio`,
  `.jobs`, `.secrets`, `.out-names`, `.trash` недоступны):
  - `initialize.capabilities.experimental`: `{"studio/files": {"version": 1, "max_upload": <AITOOLS_STUDIO_FILES_MAX,
    по умолчанию 2 GiB>, "chunk": 33554432}, "studio/user-files": [{"key": "youtube-cookies", "title": "Cookies
    YouTube", "accept": ".txt", "max_bytes": 1048576}], "studio/status": {"version": 1}}`; `resources` — если задан
    `AITOOLS_STUDIO_GUIDE`.
  - `POST /files/list {path}` → `{path, total_bytes, entries: [{name, dir, size, mtime}]}` (папки первыми);
    `GET /files/content` с `X-Studio-Path: <base64url(UTF-8 пути)>` → файл, Range; `PUT /files/upload` с
    `X-Studio-Path` и `Content-Range: bytes a-b/total` (кусок ≤ 32 MiB, докачка в `.studio/partial/`, на последнем
    куске — перенос на место) → `{done, size}`; `POST /files/mkdir {path}`; `POST /files/delete {path}` → перенос в
    `.trash/<id>/` с `meta.json {path, deleted}`; `POST /files/trash` → `[{id, path, deleted, size}]`;
    `POST /files/restore {id}` (имя занято — суффикс ` (2)`); `POST /files/zip {path}` → `{id, size}` (ZIP_STORED в
    `.studio/zips/`, срок 1 сутки); `GET /files/zip/{id}` — Range. Корзина старше 14 дней — в часовом `sweep()`.
  - `GET /user-files` → `[{key, title, present, size, mtime}]`; `PUT /user-files/{key}` (тело ≤ `max_bytes`);
    `DELETE /user-files/{key}`. Файл кладётся туда, откуда его читают инструменты YouTube aitools в `AITOOLS_WORKSPACE`
    (первый шаг — прочитать на Spark, только чтение, куда пишет закрытый `youtube_cookies`).
  - `GET /status` → `{state: "ok"|"busy"|"down", queue: <идущих заданий учётки>, message}`.
  - `resources/list` → `[{uri: "studio-guide://aitools", name, mimeType: "text/markdown", annotations: {audience:
    ["user"]}}]`; `resources/read` — текст файла `AITOOLS_STUDIO_GUIDE` (≤ 1 MiB).
  - `hints.json` `"limits": {"audio_transcribe": {"max_seconds": 3600}}` → `_meta["studio/limits"]` у инструмента.
  - `audio_transcribe`: поле `protocol: boolean = false` → aitools `transcribe` с `protocol=true`; файлы `.md`, `.docx`,
    `.txt`, `.srt` результата — артефакты (`register()`).

- [ ] **Step 1: Write the failing tests** (`make()` и `Studio(http, user)` из тестов обёртки):

```python
def test_capabilities_announce_files_user_files_status(): assert {"studio/files", "studio/user-files", "studio/status"} <= set(exp)
def test_list_hides_service_folders_and_other_users(): ...
def test_upload_in_chunks_resumes_and_lands_once(): ...   # 2 куска, обрыв, повтор куска, sha256 итога
def test_path_escape_is_refused(): assert put("../x").status_code == 400
def test_delete_moves_to_trash_and_restore_returns(): ...
def test_trash_older_than_14_days_is_swept(): ...         # now= подменён
def test_zip_supports_range(): assert r.status_code == 206
def test_user_file_lands_where_youtube_tools_read_it_and_is_not_listed(): ...
def test_status_counts_own_jobs_only(): ...
def test_guide_resource_listed_for_users(): ...
def test_transcribe_protocol_returns_document_artifacts(): ...
def test_limits_hint_becomes_meta(): assert tool["_meta"]["studio/limits"] == {"max_seconds": 3600}
```

- [ ] **Step 2: Run** тесты обёртки — FAIL.
- [ ] **Step 3: Implement** маршруты (`custom_starlette_routes`), `experimental` при создании опций инициализации,
  обработчики `resources`, `register()` для документов протокола.
- [ ] **Step 4: Run** — PASS (весь набор обёртки).
- [ ] **Step 5: Commit** (в репозитории обёртки) `Add files, user files, status, guide and limits contracts`.

### Task 16: Панель s06 и шлюз речи s06 (проект `D:\vsc\vLLM`)

Правила — CLAUDE.md проекта vLLM: привилегированные действия только через `ai-ctl`, проверка дублируется в
`app/configs.py` и `ai-ctl`, LF, ключи не копировать в файлы и вывод. Панель Spark не меняется; srvllm — задача 17.

**Files (проект vLLM):** `dashboard/app/{api.py, settings.py, sampler.py, jobs.py, db.py}`, `dashboard/deploy/ai-ctl`,
`dashboard/static/{app.js, index.html, style.css}`, шлюз речи s06 (найти по `speech/transcriptions`), тесты
`tests/python/`.

**Interfaces:**
- Produces:
  1. **Ночная сквозная проверка** — `sampler` в 03:30 времени узла: по каждой службе `settings.SERVICES` — малый живой
     вызов (1 токен чата, эмбеддинг строки, переранжирование 2 документов, ASR секунды тишины, TTS слова, OCR
     маленькой картинки, проба ffmpeg); итог — таблица `e2e_runs(id, started, finished, results JSON)`; `GET /api/e2e`;
     значок в шапке «Ночная проверка: n/m» с подробностями.
  2. **Отмена и журнал конвертации** — «Отменить» у заданий `/api/transcode/jobs`; команда
     `ai-ctl transcode-cancel <job_id>` (вызов `DELETE /v1/jobs/{id}` службы её ключом из файла); вид операции в
     `api.STEP_UP_KINDS`; запись `db.audit(...)`.
  3. **Все службы s06 в панели** — `qwen3-tts-cpu`, `ai-mcp-services`, `cleaner` в `settings.SERVICES` и `ai-ctl UNITS`
     (рестарт с повторным паролем, журнал в «События и логи»).
  4. **«Локальные правки: n из m»** — `ai-ctl manifest-drift`: sha256 управляемых файлов против манифеста
     (`dashboard/deploy/manifest.ps1`); строка в шапке и список файлов.
  5. **«Руководство»** — пункт навигации, Markdown раздела для операторов из `dashboard/README.md`, безопасный рендер.
  6. **Шлюз речи s06** — `GET /speech/limits` → `{max_seconds, max_bytes}`; поле `language` (`auto|ru|en|…`) у
     `/speech/transcriptions` пробрасывается в распознавание.
- [ ] **Step 1: Write the failing tests** — по одному на пункт (`ai-ctl` — через `ai_ctl_harness.py`, службы —
  `service_harness.py`; полный прогон падает на пропусках — без `skip`).
- [ ] **Step 2: Run** `python -m pytest` — FAIL.
- [ ] **Step 3: Implement** пункты 1–6.
- [ ] **Step 4: Run** `python -m pytest`, `python dashboard/deploy/facts.py --check`, `python tests/index.py --check`,
  `python -m py_compile` изменённых — PASS.
- [ ] **Step 5: Commit** (в проекте vLLM, по пункту). Выкладка панели и шлюза — ⛔ после слова владельца.

---

## Волна B

### Task 3: Дизайн всех разделов по образцу «Агентов»

**Files:** `app/static/js/{chat.js, roles.js, photo.js, video.js, speech.js, transcribe.js, ocr.js, translate.js,
mcp-sections.js}`, `app/static/css/app.css`, `tests/test_ui_design_consistency.py` (снять xfail), затронутые
браузерные тесты разделов.

**Interfaces:**
- Consumes: `picker()` и классы задачи 2.
- Produces (раскладка, которую проверяет сторож):
  - Шапка `header.head`: `min-height: var(--head-h)`, `h1 { flex: none; font: 600 var(--title-font) }`; в шапке только
    заголовок и кнопки-значки шапки — выборы подключения и модели из шапки уходят.
  - «Сканы»: `#ocr-model` → `picker` в ряду `.split-toolbar` (рядом с «Студия» и «Загрузить»).
  - «Перевод»: `.tl-head` без переноса; `#tl-model` → `picker` в `.composer-bar`; языки `#tl-source`/`#tl-target` →
    `picker` в компактном ряду `[Язык ▾] ⇄ [Язык ▾]` над полем; `#tl-new` — значок с подсказкой на ≤ 760 px.
  - `mcp-sections.js`: `control` — `picker` поверх `select#section-mcp-tool` (id остаётся у скрытого `select`).
  - «Озвучка»: `#chip-model`, `#chip-mcp-model`, `#chip-voice` → `picker`; «Видео»: `#video-model` → `picker`.
  - Чат: `#role-chip`, `#web-toggle`, `#mcp-toggle`, `#think-toggle` — `.ctl-chip` (значок + слово; слово скрыто
    ≤ 420 px, как сейчас).
  - Кнопки действий в рядах управления («Загрузить запись», «Загрузить», «Новый перевод») — `.ctl-primary`.
  - Пустые состояния — `--empty-font`.
- [ ] **Step 1:** снять `xfail` у разделов в `test_ui_design_consistency.py`. **Step 2: Run** — FAIL по разделам.
- [ ] **Step 3: Implement** раскладку раздел за разделом (коммит на раздел).
- [ ] **Step 4: Run** `test_ui_design_consistency.py` и `tests/test_ui_{browser,section_limits,shared_composer,webkit,
  mobile_navigation,status_layout,i18n,menus}.py` — PASS; ожидания старых размеров в тестах — под новый вид.
- [ ] **Step 5: Commit** по разделу: `Align <section> header and composer with Agents`.

### Task 4: Пауза и продолжение хода; подтверждение (п. 5); вопросы кнопками (п. 17); круги и повторы (п. 6)

**Files:**
- Create: `app/chat_pending.py` (пауза, маршрут ответа), `app/chat_tools.py` (встроенные инструменты не из `web.py`),
  `app/static/js/chat-pending.js`.
- Modify: `app/chat.py` (`StreamTask._read_model`, `_tools`, `create_turn`, выдача сообщений), `app/mcp_client.py`
  (`Tools._offered`, `Tools.confirm_hint`), `app/admin.py` (`/api/admin/chat`), `app/static/js/{chat.js, admin.js}`,
  `app/static/en.js`.
- Test: `tests/test_chat_pending.py`, `tests/test_chat_tools_rounds.py`, правка `tests/test_mcp_dialog.py` (подтверждение
  ~446), `tests/test_ui_chat_pending.py` (integration).

**Interfaces:**
- Consumes: таблица `chat_pending` (задача 1); `mcp_sources.confirm_needed(hint, args) -> bool`,
  `mcp_sources.effective(tool, rule)`; `StreamTask.put(kind, data)`, `_sse`, `_EventStream`, `_finish`.
- Produces:
  - `chat_pending.PENDING_TTL_SEC = 86400`; `async def pause(task: StreamTask, kind: str, call: ToolCall, view: dict)
    -> str` — пишет `payload` = Fernet JSON `{messages: task.messages, call: {id, name, arguments}, rounds, seen,
    steps}`, шлёт SSE `pending {id, kind, ...view}`, завершает ход статусом `done` (текст до паузы сохранён).
  - `POST /api/chats/{chat_id}/pending/{pending_id}` тело `PendingIn{request_id: str (шаблон как у TurnIn), action:
    Literal["run","refuse","answer"], answer: str | None (≤ 4000)}` → атомарно `UPDATE ... SET resolved, outcome WHERE
    id=? AND resolved IS NULL` (0 строк — 409 `pending_gone`; старше TTL — `outcome='expired'` и 409 `pending_gone`);
    то же сообщение снова `streaming`, `StreamTask(resume=payload)` продолжает цикл; ответ — поток SSE, как у
    `create_turn`. Перед продолжением — заново подключение (`ConnectionChanged` → 409 `connection_changed`), доступ,
    политика и «Интернет» (существующая перепроверка в `Tools.call`).
  - Выдача сообщений: у сообщения с открытой паузой — `pending: {id, kind, server, tool, arguments: [[ключ, значение
    ≤ 200 знаков]] ≤ 8, question, options, allow_text}`.
  - `create_turn`: открытая пауза чата → `outcome='superseded'` до нового хода.
  - Подтверждение: `Tools._offered` больше не исключает инструменты с подтверждением (константа `NEEDS_CONFIRM`
    уходит); `Tools.confirm_hint(name) -> dict | None`; в `StreamTask._tools` при `confirm_needed(...)` для вызова, не
    подтверждённого этой паузой, — `pause(..., "confirm", ...)`; `refuse` → результат «Пользователь отказался запускать
    этот инструмент.»
  - `chat_tools.ASK_USER` — `ask_user(question: string ≤ 500, options: string[] ≤ 6 (каждая ≤ 80), allow_text: boolean =
    true)`; вызов → `pause(..., "ask", ...)`; ответ → `{"answer": "<текст>"}`, отказ → `{"answer": null, "note":
    "пользователь не ответил"}`. `chat_tools.specs(task) -> list[dict]`, `chat_tools.NAMES: set[str]`,
    `async chat_tools.call(task, call) -> dict` (шаг).
  - Встроенные инструменты (`ask_user`, далее задачи 11–12) предлагаются в каждом ходе через сервер (не Cleaner). Отказ
    шлюза от `tools` (400 с маркером из `profiles.REJECTED` или словом `tool`) — один повтор без инструментов и метка
    `chat.NO_TOOLS[(connection_id, model)]` на `chat.NO_TOOLS_SEC = 3600`.
  - Круги: `chat.TOOL_ROUNDS = 15`; `chat.tool_rounds() -> int` читает `settings.chat_tool_rounds` (1–30);
    `GET/PUT /api/admin/chat` `ChatSettingsIn{tool_rounds: int = Field(ge=1, le=30)}`; блок в `admin.js`.
  - Повторы: `StreamTask.seen: set[str]`, ключ — sha256 `name + "\0" + json.dumps(args, sort_keys=True,
    ensure_ascii=False, separators=(",", ":"))`; повтор не выполняется, шаг `{kind: "duplicate", tool}`, модели —
    «Повторный вызов с теми же аргументами пропущен: результат уже есть выше.»
  - Страница: `chat-pending.js` `paintPending(bubble, pending, onAnswer)` — карточка подтверждения (сервер, инструмент,
    аргументы, «Запустить»/«Отмена») или вопроса (кнопки вариантов, поле «Свой ответ», «Пропустить»); ответ продолжает
    тот же пузырь через `readServer`.
- [ ] **Step 1: Write the failing tests:**

```python
def test_confirm_tool_pauses_and_refuse_never_calls_mcp(): ...   # server() из test_mcp_dialog: вызовов 0
def test_run_calls_once_and_answer_continues_same_message(): ...  # один вызов, тот же assistant id, итог done
def test_second_answer_and_late_answer_get_pending_gone(): ...    # 409; clock.NOW + 86401 → 409
def test_new_message_supersedes_pending(): ...
def test_pending_survives_restart(): ...                          # новое приложение на той же базе — карточка в выдаче
def test_web_off_or_access_removed_before_answer_refuses(): ...
def test_ask_user_option_and_own_text_reach_model(): ...          # FakeGateway.chat_script с tool_calls ask_user
def test_rounds_follow_admin_setting(): ...                       # PUT /api/admin/chat {tool_rounds: 2} → 3-й круг без tools
def test_duplicate_call_skipped(): ...
def test_gateway_refusing_tools_retried_without_them(): ...
```

- [ ] **Step 2: Run** `tests/test_chat_pending.py tests/test_chat_tools_rounds.py tests/test_mcp_dialog.py` — FAIL.
- [ ] **Step 3: Implement** по Interfaces. **Step 4: Run** — PASS, плюс `tests/test_chat*.py tests/test_mcp*.py` и
  `tests/test_ui_chat_pending.py` (карточка на 320 и 1280, ответ кнопкой продолжает пузырь).
- [ ] **Step 5: Commit** по пункту: пауза, подтверждение, `ask_user`, круги и повторы.

### Task 5: Корзина на 14 дней (п. 9), пароль окончательного удаления (п. 34), «Хранилище» (п. 35)

**Files:**
- Modify: `app/trash.py` (API, уборка); модули удаления: `chat.py` (`delete_chat`, `list_chats`, `_chat_row`,
  `stop_user`), `images.py`, `videos.py`, `speeches.py`, `transcripts.py`, `ocr.py`, `translate.py`, `voices.py`,
  `mcp_files.py` (`delete_media`, `media_of`); прочие читатели истории: `jobs.py` (`_rows`, `_chat_done`, `_chat`),
  `search.py` (`_title`, `reindex`), `chains.py`, `lock_policy.py`, `push.py`, `mcp_client.py` (~524),
  `chat_shares.py` (снимок обычного чата), `takeout.py` (`_pages`), `account_profile.py` (`delete_history`,
  хранилище), `main.py` (фоновая задача).
- Create: `app/static/js/trash.js` (страница `#/trash`); Modify: `index.html` (меню аватара), `app.js` (`LOADERS`),
  `sections-meta.js` + `sections.NAMES` + `auth.PAGE_SECTIONS` (как у `settings`), `settings.js` (`storageBlock`),
  `en.js`, `tests/test_ui_browser.py` (~224, ~338 — списки меню).
- Test: `tests/test_trash.py`, `tests/test_ui_trash.py` (integration).

**Interfaces:**
- Consumes: столбцы `deleted`, триггеры `search_<t>_hide`, `trash.TABLES` (задача 1).
- Produces:
  - Каждый модуль раздела: `async def purge(user_id: int, item_id: str) -> None` — прежнее окончательное удаление
    (файлы, каскады, шлюз), вынесенное из обработчика `DELETE`; обработчик `DELETE` ставит `deleted = clock.NOW()`
    (прежние условия: 409 для незавершённых картинок и озвучки; ролик — сначала `_drop` у шлюза; чат — `_cancel`
    потоков). Все выборки списков и записей по id — `AND deleted IS NULL` (`db.page(..., extra=...)`); запись в
    корзине по id — 404.
  - `trash.KINDS: dict[str, Kind]` (`table`, `section`, `purge`, `title(row) -> str`) — разделы этой задачи; базы
    знаний добавит задача 12. `GET /api/trash` → `[{kind, id, title, deleted, bytes}]` своих;
    `POST /api/trash/{kind}/{id}/restore`; `DELETE /api/trash/{kind}/{id}` (окончательно одну запись);
    `POST /api/trash/empty {password}` (окончательно всё, по паролю); `trash.start_purger()` — раз в сутки (метка в
    `settings`, как `media.orphans_due`) `purge` записей с `deleted < NOW - TRASH_SEC`. Восстановление вызывает
    `search.reindex_item(section, item_id)` (вставка строки поиска одной записи тем же SQL, что `reindex`).
  - Связи: снимок «Поделиться» чата в корзине — «недоступна»; «В работе» и push его не видят; `takeout` не выгружает
    корзину; озвучка — `delete_chat` ставит `deleted` всем записям чата озвучки; удаление перевода больше не трогает
    скан (это делает `purge`).
  - `DELETE /api/history` требует `{password}` (та же проверка и паузы, что у выключения блокировки) — окончательно.
  - `GET /api/me/storage` дополняется: `heaviest: [{section, id, title, bytes}]` (50, по сумме `media.size` ссылок
    записи), `trash: {section: {count, bytes}}`, `failed: {section: count}`; `POST /api/me/storage/failed {sections}`
    переносит неудачные и отменённые записи в корзину → `{moved}`. Строку файлов MCP добавит задача 9.
  - Меню аватара: «Корзина» (`#/trash`); страница — по разделам, «Вернуть», «Удалить навсегда», «Очистить корзину»
    (поле пароля в окне подтверждения).
- [ ] **Step 1: Write the failing tests** (`tests/test_trash.py`, параметризация по `trash.KINDS`):

```python
def test_delete_hides_everywhere_and_restore_returns(kind): ...  # список, по id 404, поиск, «В работе»; restore — обратно
def test_purge_after_14_days_removes_files_once(kind): ...       # clock.NOW + TRASH_SEC + 1; повтор уборки — без ошибок
def test_share_link_of_trashed_chat_is_unavailable_and_returns_after_restore(): ...
def test_speech_chat_delete_trashes_all_parts(): ...
def test_translation_delete_keeps_scan_until_purge(): ...
def test_takeout_skips_trash(): ...
def test_history_erase_and_empty_trash_need_password_and_count_failures(): ...  # 403 без пароля; неудачи — в счёт пауз
def test_admin_sees_no_foreign_trash(): ...
def test_storage_heaviest_and_failed_move_to_trash(): ...
```

- [ ] **Step 2: Run** `tests/test_trash.py` — FAIL.
- [ ] **Step 3: Implement** раздел за разделом (коммит на раздел), затем API `trash.py`, уборка, страница, хранилище.
- [ ] **Step 4: Run** `tests/test_trash.py`, тесты разделов (`tests/test_{chat,images,videos,speech*,transcri*,ocr*,
  translate*,voices,search,jobs,takeout,chat_shares}*.py`), `tests/test_ui_trash.py` — PASS.
- [ ] **Step 5: Commit** по шагам; последний — `Add 14-day trash, password-only erase and storage hints`.

### Task 13: Обёртка — голоса Spark (п. 19), фасад бизнес-API (п. 14), стартовые файлы, письмо (п. 21)

**Files (обёртка):** `aitools_studio.py`, Create: `mts_facade.py`, `tests/test_speech.py`, `tests/test_mts_facade.py`,
стартовые `studio-rules.md`, `studio-prompts.json`, `studio-hints.json`, `studio-guide.md`; `letter-spark-admin.md`;
`studio-s06-postdeploy.py` (новые общие MCP).

**Interfaces:**
- Produces:
  1. **Голоса.** Первый шаг — на Spark (только чтение) найти источник озвучки: инструмент в `aitools_cli.py list`
     (tts/speak/voice) или TTS-служба из настроек звука Open WebUI. Есть — инструмент раздела `speech_synthesize(text,
     voice: enum голосов (обновление при старте и раз в час), model?: enum, language?: enum)` → mp3-артефакт;
     `voice_delete(name)` — только если у службы есть удаление (правило «только администраторам» ставит Studio). Нет
     программного источника — запись в `FINDINGS.md` («ждёт TTS на Spark»); сторона Studio (задача 8) работает с любым
     MCP, у которого `voice` — enum (s06-llm уже такой).
  2. **Фасад бизнес-API.** Найти на Spark (только чтение) инструмент Open WebUI для бизнес-API (функции Tools в базе
     Open WebUI или файл); перенести белый список функций и маскирование в `mts_facade.py`: пути `/mts/mcp` (обычный
     набор, номера SIM — `•••• 1234`) и `/mts-admin/mcp` (полный), свой токен `AITOOLS_STUDIO_MTS_TOKEN`, ключ API — из
     файла службы, ограничение частоты — как у обёртки Open WebUI. `studio-s06-postdeploy.py`: общие MCP «МТС» (группа
     «МТС») и «МТС (админ)» (правило без групп — только администраторам), оба `protected = 1` и **выключены**.
  3. **Стартовые файлы** правил, подсказок, пометок (`open_world`, `confirm`, `limits`) и руководства для Studio — из
     правил модели Open WebUI берётся только то, что про инструменты; кладутся в `~/.config/ai-tools/studio-*` при
     выкладке (⛔); дальше их ведёт администратор Spark.
  4. **Письмо** администратору Spark — дополнить: новые файлы, контракты 0.41, правила скачивания из video (спека,
     п. 21), фасад и его ключ. Отправляет владелец (⛔).
- [ ] **Step 1: Write the failing tests** — голоса из enum и их обновление, синтез → артефакт; фасад: обычный набор
  маскирует SIM, администраторский — нет, функция вне белого списка — отказ, чужой токен — 401.
- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5: Commit** по пункту (в репозитории обёртки). Выкладка на Spark — ⛔ (порядок — память проекта
  `s06-shared-mcp-spark-bridge`).

---

## Волна C

### Task 7: Поле ввода чата — 10 вложений, текст и код (п. 30), паспорт (п. 31), подсказки (п. 2), контекст (п. 33)

**Files:**
- Modify: `app/chat.py` (`ATTACH_MAX`, `TurnIn`, `_free_attachments`, `_cap_images`, `compose`, `_content`, оценка),
  `app/profiles.py` (`Params.max_images`, `max_images()`), `app/documents.py` (`SUFFIXES`, `LANG`), `app/mcp_files.py`
  (`PER_TURN`), `app/static/js/{chat.js, connection-profile.js}`; Create: `app/static/js/chat-suggest.js`,
  `app/static/js/chat-context.js`.
- Test: `tests/test_chat.py` (~399–412, ~709–732 — под новый предел), `tests/test_chat_attachments_limits.py`,
  `tests/test_chat_suggest.py`, `tests/test_ui_chat_composer.py` (integration).

**Interfaces:**
- Produces:
  - `chat.ATTACH_MAX = 10`; `TurnIn.attachment_ids` — `max_length=ATTACH_MAX`; больше 10 — 422 `{"code": "attachments",
    "message": "не больше 10 вложений в сообщении"}`; прежний 422 `images` уходит.
  - `profiles.DEFAULT_MAX_IMAGES = 4`; `Params.max_images: int | None = Field(None, ge=1, le=32, strict=True)` — в запрос
    не уходит (`request_params` не меняется); `profiles.max_images(conn) -> int`. `compose(chat_id, upto, model=None,
    max_images=profiles.DEFAULT_MAX_IMAGES)`; `_cap_images(out, limit)` оставляет `limit` последних картинок запроса,
    прочие заменяет `chat.DROPPED` = «[фото убрано из контекста: модель принимает не больше {n} картинок на запрос]».
    Все три вызова `compose` передают предел подключения хода.
  - `mcp_files.PER_TURN = 10`.
  - `documents.SUFFIXES` += `.csv .tsv .json .jsonl .xml .yaml .yml .toml .ini .log .py .js .ts .html .css .sql .sh .ps1`;
    текст — UTF-8, иначе UTF-16 по BOM, иначе cp1251; `documents.LANG: dict[str, str]` (суффикс → язык); `chat._content`
    оборачивает код в блок ```` ```<язык> ```` внутри `<document>`. Страница: `ACCEPT` того же списка; `chat.DOC_MIME`
    — по суффиксам.
  - Паспорт: если среди вложений есть фото, а в паспорте выбранной модели нет `vision`, над полем — строка «Модель по
    паспорту не смотрит фото» со ссылками «Убрать фото» и «Сменить модель»; если в диалоге MCP с `studio/chat-files` и
    согласием — «Фото получит инструмент MCP «<сервер>»». Отправку не блокирует.
  - Подсказки: `GET /api/chats/suggestions?connection_id=&model=&chat_id=` → `[{title, text, source, prompt?: {sid,
    name, arguments: [{name, description, required}]}}]` ≤ 8: профиль подключения (`profiles.suggestions_of`), затем
    `prompts` MCP диалога (новый чат — MCP по умолчанию). `chat-suggest.js` `paintSuggestions(host, items, field)` — чипы
    под пустой заметкой (`emptyNote`); нажатие вставляет текст, не отправляет; подсказка с аргументами — форма в
    `popupPanel`, текст — `POST /api/mcp/{sid}/prompt`.
  - Контекст: `POST /api/chats/{chat_id}/estimate {text, attachment_ids}` → `{tokens, limit | null}` (`compose` +
    системное сообщение хода + `estimate_tokens`, предел — `_limit(conn, model)`); `chat-context.js` — кольцо 18 px в
    `.composer-opts`, обновление через 800 мс после ввода или смены вложений, `≥ 0.7` — `.ctx-warn`, `≥ 0.9` —
    `.ctx-full`, подсказка «Контекст ~62 % (81 000 из 131 072)»; предел неизвестен — кольца нет. «Продолжить в новом
    диалоге» не делать.
  - Профиль подключения: в `connection-profile.js` `PARAMS` — «Картинок в запросе» (`max_images`).
- [ ] **Step 1: Write the failing tests:**

```python
def test_ten_mixed_attachments_accepted_eleventh_refused(): ...    # 422 code="attachments"
def test_five_photos_with_limit_four_newest_four_reach_model(): ... # chat_requests: 4 image_url, DROPPED один раз
def test_profile_max_images_overrides_default_and_stays_out_of_request(): ...
def test_mcp_with_consent_gets_all_photos(): ...                    # server/consent/send из test_mcp_chat_files
def test_code_file_becomes_fenced_document(): ...                   # .py → ```python
def test_cp1251_text_file_decoded(): ...
def test_suggestions_merge_profile_and_mcp_prompts_capped_at_8(): ...
def test_estimate_counts_system_history_and_attachments(): ...
```

  Браузер: 10 вложений проходят, 11-е — сообщение; предупреждение паспорта; подсказка вставляет текст; кольцо меняет
  цвет (320 и 1280).
- [ ] **Step 2–4:** FAIL → реализация → PASS (`tests/test_chat*.py tests/test_mcp_chat_files.py tests/test_profiles*.py`
  и браузерный файл).
- [ ] **Step 5: Commit** по пункту.

### Task 8: «Озвучка» — длинный текст (п. 27), «Прочитать» (п. 12, 28), голоса MCP (п. 19), язык озвучки (п. 26)

**Files:** `app/speeches.py`, `app/speech.py`, `app/static/js/speech.js`; Create: `app/static/js/speech-read.js`;
Modify: `chat.js` (действие у ответа), `translate.js`, `ocr.js`, `en.js`; Test: `tests/test_speech_long.py`,
`tests/test_speech_read.py`, `tests/test_ui_speech_read.py` (integration).

**Interfaces:**
- Produces:
  - `speeches.LONG_MAX = 200_000`; `speeches.split_text(text: str, limit: int) -> list[str]` — по абзацам, затем по
    предложениям, затем по словам, каждая часть ≤ `limit` (`TEXT_MAX` для шлюза, `EXTERNAL_MAX` для внешнего);
    `create` с текстом длиннее предела ставит части записями **одного** чата озвучки по порядку; `QUEUE_MAX` считается
    по чатам (`_full_queue` — число разных `chat_id` в очереди и работе).
  - `GET /api/speeches/chats/{chat_id}/audio` → один mp3 готовых частей (`audio.mp3_of`), имя — по началу текста
    (`Content-Disposition` RFC 5987); страница: счётчик «n / 200 000 · будет N частей», у чата — «Слушать подряд» и
    «Скачать одним mp3».
  - `POST /api/speeches/read {text, origin: {kind: "chat"|"translate"|"ocr", id}}` → текст через `exports.md_plain`;
    источник — умолчание раздела «Озвучка» пользователя, голос — последний с этой моделью, иначе первый доступный →
    `{chat_id}`; умолчание — инструмент MCP → 409 `read_mcp`, и страница запускает его тем же путём, что раздел
    (`sectionMcp('speech').run`). `speech-read.js` `readAloud(text, origin, host)` — проигрыватель на месте (части
    играют по мере готовности, опрос `GET /api/speeches/chats/{id}`), «Скачать». Кнопка «Прочитать» — у ответа чата, у
    результата «Перевода», у текста «Сканов».
  - Голоса MCP: при выбранном инструменте MCP `speech.js` `paintVoice` показывает `argument('voice').enum` группой
    «<имя сервера>» рядом с личными голосами; выбор уходит полем `voice`.
  - Язык: выбор «Русский / English» у Qwen3-TTS и F5-TTS srvllm (`speech.say(conn, model, text, voice, language=None)`;
    по умолчанию — прежний `speech.TTS_LANGUAGE`), у MCP — по enum `language` схемы (общий рендер полей — задача 10);
    значение — в `speeches.language`.
- [ ] **Step 1: Write the failing tests:**

```python
def test_long_text_split_into_parts_of_one_chat_in_order(): ...
def test_split_never_exceeds_limit_and_keeps_all_words(): ...
def test_queue_counts_chats_not_parts(): ...
def test_chat_audio_concatenates_done_parts(): ...
def test_read_from_chat_answer_strips_markdown_and_uses_default_source(): ...
def test_read_with_mcp_default_returns_read_mcp(): ...
def test_tts_language_sent_to_gateway(): ...
```

- [ ] **Step 2–4:** FAIL → реализация → PASS (`tests/test_speech*.py`, `tests/test_voices*.py`, браузерный файл:
  «Прочитать» у ответа играет и скачивается, 320 и 1280).
- [ ] **Step 5: Commit** по пункту.

### Task 9: Контракты MCP в Studio — «Мои файлы» и ссылки (п. 8), файлы пользователя (п. 20), состояние (п. 39), «Справка» (п. 16)

**Files:**
- Create: `app/mcp_contracts.py`, `app/my_files.py`, `app/help.py`, `app/static/js/{my-files.js, help.js,
  file-link.js}`, `app/static/file.html` (оболочка `/f`), `docs/architecture/mcp-contracts.md`.
- Modify: `app/mcp_client.py` (`Session.resources`, `Session.read_resource`), `app/mcp_sections.py` (вынести
  `endpoint(session, path) -> httpx.URL` из `relay`), `app/main.py` (роутеры, заголовки `/f*`, `public_guard` для
  `POST /f/*`), `app/chat_shares.py` (переиспользовать `_same_origin`, `PUBLIC_HEADERS`), `app/account_profile.py`
  (строка файлов MCP в хранилище), `app/static/js/{mcp-settings.js, chat.js, mcp-sections.js, app.js, settings.js}`,
  `index.html` (меню: «Мои файлы», «Справка»), `deploy/apache-vhost.conf`, `deploy/deploy.sh`, `deploy/install.sh`,
  `deploy/docker/Dockerfile` (руководство в образ), `CLAUDE.md` (анонимный вход `/f`, карта модулей).
- Test: `tests/test_mcp_contracts.py`, `tests/test_my_files.py`, `tests/test_file_links.py`, `tests/test_help.py`,
  `tests/test_deploy.py`, `tests/test_ui_my_files.py` (integration).

**Interfaces:**
- Consumes: контракт обёртки (задача 6); `file_links` (задача 1); `mcp_sections.ticket/redeem`, `relay`;
  `access.allowed`, `access.studio_user`; `email_accounts.throttle(scope, value, maximum, seconds)`.
- Produces:
  - `mcp_contracts.features(catalog: Catalog) -> Features(files: dict | None, user_files: list[dict], status: bool)` из
    `capabilities.experimental`; `mcp_contracts.STATUS_TTL_SEC = 15`; `GET /api/mcp/status` → `{"<sid>": {state,
    queue, message}}` доступных серверов со `studio/status` (кэш по `(sid, studio_user)`); точка состояния у чипа MCP
    чата и у выбора «Студия/MCP» разделов.
  - Файлы пользователя: `GET /api/mcp/{sid}/user-files`; `PUT /api/mcp/{sid}/user-files/{key}` (тело ≤ `max_bytes`
    контракта и ≤ 1 MiB); `DELETE ...`; блок в «Настройки → MCP» у сервера («Загрузить», «Убрать», есть/нет). Содержимое
    — ни модели, ни в журнал.
  - «Мои файлы» (`/api/my-files`): `GET /servers` → `[{sid, name, max_upload, chunk}]`; `POST /{sid}/list {path}`;
    `POST /{sid}/ticket {path}` → `{url}` (`GET /api/my-files/{sid}/download/{ticket}`, Range, через `endpoint`
    `/files/content`); `PUT /{sid}/upload` (`X-Studio-Name` — путь, `Content-Range`; кусок ≤ `my_files.CHUNK`);
    `POST /{sid}/{mkdir|delete|zip|trash|restore}`; ZIP — по ticket `/files/zip/{id}`. Страница `#/myfiles` (меню аватара
    «Мои файлы», видна при хотя бы одном сервере): вкладка на сервер, папки, загрузка кусками с повтором, скачивание,
    ZIP, корзина сервера, «Ссылка на сутки», список своих ссылок с отзывом. `GET /api/me/storage` — `mcp_files: [{sid,
    name, bytes}]` по `total_bytes`.
  - Ссылки: `POST /api/my-files/{sid}/links {path}` → `{url: "https://<хост>/f#<token>", expires}` (token —
    `secrets.token_urlsafe(32)`, в базе `sha256`, путь и имя — шифром, `my_files.LINK_SEC = 86400`);
    `GET /api/my-files/links`; `DELETE /api/my-files/links/{id}`.
  - Анонимно (`auth.guard` защищает только `/api`, поэтому вход со своей защитой): `GET /f` — оболочка (`no-store`,
    `no-referrer`, `noindex`); `POST /f/check {token}` → `{name, size, expires}`; `POST /f/open {token}` → cookie
    `studio_file` (HttpOnly, Secure, SameSite=Strict, Path=/f/file, 600 с; значение — случайный id в памяти процесса);
    `GET /f/file` → поток с Range по cookie. Тело POST ≤ 4096 байт, чужой `Origin` — 403; неизвестный, истёкший,
    отозванный токен, сменившийся `stamp`, снятый доступ владельца к серверу, исчезнувший файл — одинаковый 404;
    неудачи — `throttle("file_link", digest(ip), 30, 900)` → 429 `file_locked`. Токен в строку запроса не попадает
    (фрагмент адреса).
  - `Session.resources() -> list[dict]` (`resources/list`, ≤ 64), `Session.read_resource(uri) -> str` (≤ 1 MiB).
  - «Справка» `#/help` (меню аватара): `GET /api/help` → `{studio: {sections: [{id, title}]}, servers: [{sid, name,
    resources: [{uri, name}]}]}` (ресурсы с `mimeType` `text/markdown` и `audience` с `user`); `GET /api/help/studio` —
    `docs/guide.ru.md` без вступления до первого `## `, относительные ссылки репозитория — простым текстом;
    `POST /api/help/server {sid, uri}` → Markdown. Рендер — тот же безопасный Markdown, что у ответов чата (без HTML).
    `docs/guide.ru.md` — в `PARTS` (`deploy.sh`, `install.sh`) и в образ.
  - Apache: `<LocationMatch "^/api/my-files/[^/]+/upload$">` `LimitRequestBody` = 33 MiB + 1 MiB.
  - `docs/architecture/mcp-contracts.md` — все расширения Studio для любых серверов: `studio/confirm`,
    `studio/chat-files`, `studio/limits`, `studio/files`, `studio/user-files`, `studio/status`, ресурсы руководства;
    ссылка в README архитектуры и в `connections.md`.
- [ ] **Step 1: Write the failing tests** (поддельный сервер MCP с маршрутами контракта — по образцу `server()` из
  `tests/test_mcp_chat_files.py`):

```python
def test_features_read_from_experimental_capabilities(): ...
def test_list_download_range_upload_chunks_delete_restore_zip_proxy_with_studio_user(): ...
def test_admin_cannot_open_foreign_files_or_links(): ...
def test_link_flow_check_open_file_range(): ...
def test_link_expired_revoked_stamp_changed_access_removed_file_gone_all_404(): ...  # тело без имени файла
def test_link_bruteforce_locks_ip_429(): ...
def test_link_post_foreign_origin_403_and_body_cap_413(): ...
def test_user_file_upload_never_reaches_model_or_log(): ...        # caplog без имени и содержимого
def test_status_cached_15s(): ...
def test_help_lists_studio_guide_and_user_resources_only(): ...
```

- [ ] **Step 2–4:** FAIL → реализация → PASS (новые файлы, `tests/test_mcp*.py`, `tests/test_deploy.py`,
  `tests/test_docs.py`, браузерный файл: «Мои файлы» на 320 и 1280, ссылка открывается в чистом контексте браузера).
- [ ] **Step 5: Commit** по пункту.

### Task 10: «Расшифровка» — поля по схеме (п. 11, 26), длинные записи (п. 23), проверка до загрузки (п. 36), собеседники (п. 37), язык

**Files:** `app/static/js/mcp-sections.js` (рендер полей), `photo.js`, `video.js`, `transcribe.js`, `app/transcripts.py`,
`app/speech.py`, `app/audio.py`, `app/dictation.py` (язык голосового ввода), `app/static/js/settings.js` (язык ввода),
`en.js`; Test: `tests/test_transcribe_long.py`, `tests/test_speech_segments.py`, `tests/test_ui_section_fields.py`
(integration).

**Interfaces:**
- Produces:
  - `mcp-sections.js`: `extraFields(tool, shown: string[]) -> HTMLElement` — свойства схемы вне `fields` раздела и не
    файловые: `boolean` → переключатель `.ctl-chip`, `enum` → `picker`, число с границами → поле `chip-num`, строка (без
    `format`, `maxLength ≤ 200`) → маленькое поле; значения уходят в `run(values)`. Даёт «Протокол», «Говорящих»,
    «Язык» у `audio_transcribe` и новые поля любого раздела. Файлы результата (`.txt`, `.srt`, `.md`, `.docx`) —
    списком скачивания под результатом.
  - `speech.limits(conn, model) -> Limits(max_seconds: int)`: srvllm и s06 — `GET <slot>/speech/limits` (кэш 600 с),
    нет маршрута — `transcripts.DURATION_MAX`; MCP — `_meta["studio/limits"].max_seconds`.
  - `transcripts.TOTAL_MAX = 4 * 3600`; запись длиннее `limits.max_seconds` у API-источника — части
    `audio.cut(src, start: float, length: float, dst) -> None` (mp3 16 кГц моно) длиной `max_seconds`, распознавание по
    очереди (`detail` «часть 2 из 4»), склейка со сдвигом времени; перед каждой частью после первой — служебная реплика
    «— Часть N (с ЧЧ:ММ:СС): собеседники пронумерованы заново —». У MCP — только проверка предела.
  - `GET /api/transcripts/limits?connection_id=&model=` → `{max_seconds, total_max}`; страница до загрузки читает
    длительность (`loadedmetadata` по blob URL) и говорит «не подходит: длиннее 4 ч» или «будет расшифрована N частями».
  - Собеседники внешних сервисов: модели `*-diarize*` — `response_format=diarized_json`, реплики по `speaker`;
    `speech.segments_from_srt(text) -> list[Segment]`, `speech.segments_from_labeled(text) -> list[Segment]` (строки
    «Говорящий N:», «Speaker N:», «Собеседник N:»); результат MCP с такими строками или `.srt` показывается видом
    расшифровки (реплики по собеседникам).
  - Язык: выбор «Авто / Русский / English» у загрузки, «Расшифровать ещё раз» и голосового ввода; `transcripts.language`;
    внешний — поле `language` (`ru`/`en`), s06 — поле `language`, srvllm — `language=` в запросе (шлюз, не знающий
    поля, его пропускает; контракт шлюзов — задачи 16 и 17); «Авто» — поле не шлётся.
- [ ] **Step 1: Write the failing tests:**

```python
def test_long_recording_split_by_gateway_limits_and_times_shifted(): ...  # FakeGateway: /speech/limits {max_seconds: 600}
def test_no_limits_route_falls_back_to_duration_max(): ...
def test_over_total_max_refused_before_work(): ...
def test_part_marker_between_parts(): ...
def test_diarized_json_and_labeled_text_and_srt_become_turns(): ...
def test_language_sent_per_connection_kind_and_omitted_for_auto(): ...
def test_mcp_extra_fields_pass_bounds_check(): ...                 # check_bounds на сервере
```

  Браузер: у инструмента с `protocol`/`speakers`/`language` появляются поля, значения уходят в вызов; проверка до
  загрузки говорит про части (320 и 1280).
- [ ] **Step 2–4:** FAIL → реализация → PASS (`tests/test_transcri*.py tests/test_speech*.py tests/test_mcp_sections.py`,
  браузерный файл).
- [ ] **Step 5: Commit** по пункту.

---

## Волна D

### Task 11: Защищённый источник (п. 14), план работы и память (п. 18)

**Files:** `app/mcp_client.py`, `app/chat.py`, `app/chat_tools.py`, `app/chat_system.py`; Create: `app/memory.py`;
Modify: `mcp_client.ServerIn`/`app/admin.py` (`protected`), `app/history.py` (стирание памяти), `app/takeout.py`
(память в выгрузке), `app/static/js/{mcp-settings.js, chat.js, settings.js}`, `en.js`; Test:
`tests/test_protected_source.py`, `tests/test_memory_todo.py`.

**Interfaces:**
- Consumes: `mcp_servers.protected`, `chats.protected_used`, `messages.todo`, `user_memory`, `users.memory_on`;
  `chat_tools.specs/call` (задача 4).
- Produces:
  - `ServerIn.protected: bool = False` (только общие; флажок «Защищённый источник» в редакторе администратора).
    Успешный вызов инструмента такого сервера → `UPDATE chats SET protected_used=1, web=0`. Дальше в диалоге: включение
    «Интернета» → 409 `protected`; веб-инструменты (`web.tools_for`, `open_world`) не предлагаются; вызов любого
    инструмента со строкой аргумента по `mcp_client.URL_RE = r"https?://|www\."` — отказ шагом «Ссылки в аргументах
    после защищённого источника запрещены». Чип «Интернет» — неактивен с подсказкой.
  - `chat_tools.TODO_WRITE` — `todo_write(items: [{text ≤ 200, status: "pending"|"in_progress"|"done"}] ≤ 20)` → пишет
    `messages.todo` текущего ответа, SSE `todo {items}`; на странице — блок плана в ответе.
  - `memory.py`: `ITEM_MAX = 500`, `MAX_ITEMS = 100`, `BLOCK_MAX = 4000`; `list_for(uid)`, `save(uid, text) -> str`
    (шифром), `forget(uid, item_id)`, `clear(uid)`; `chat_tools.MEMORY_SAVE`/`MEMORY_FORGET` — только при `memory_on`;
    `chat_system.memory_block(uid) -> str` — кусок «Что известно о пользователе» после роли, новые первыми, ≤ 4000.
    `GET/POST/DELETE /api/memory`, `PATCH /api/me {memory_on}`; «Настройки → Чат»: переключатель «Память» (по умолчанию
    выключено), список с удалением, «Очистить всё». Стирание истории уносит память; выгрузка истории — с памятью.
- [ ] **Step 1: Write the failing tests:**

```python
def test_protected_call_turns_web_off_and_locks_it(): ...
def test_after_protected_web_and_open_world_tools_not_offered(): ...
def test_url_in_arguments_refused_after_protected(): ...
def test_non_protected_dialog_unchanged(): ...
def test_todo_write_persists_and_streams(): ...
def test_memory_off_offers_no_memory_tools_and_no_block(): ...
def test_memory_saved_encrypted_and_in_system_block_capped(): ...
def test_history_erase_clears_memory(): ...
```

- [ ] **Step 2–4:** FAIL → реализация → PASS (`tests/test_chat*.py tests/test_mcp*.py tests/test_takeout*.py`).
- [ ] **Step 5: Commit** по пункту.

### Task 12: Знания — поиск по большим вложениям и базы знаний (п. 15)

**Files:** Create: `app/knowledge.py`, `app/static/js/knowledge.js`; Modify: `app/chat.py` (`_content`, загрузка
вложения, ход), `app/chat_tools.py`, `app/trash.py` (`KINDS` баз и документов), `app/admin.py`,
`app/static/js/{settings.js, admin.js, chat.js}`, `en.js`; Test: `tests/test_knowledge.py`, `tests/test_ui_knowledge.py`
(integration).

**Interfaces:**
- Consumes: `knowledge_bases`, `kb_documents`, `kb_chunks`, `kb_fts`, `kb_rules`, `chat_kbs` (задача 1);
  `documents.extract`; `upstream.request`; `access.is_admin`, членство в группах; `trash.KINDS` (задача 5).
- Produces:
  - Источник: `settings.knowledge_source` = `{connection_id, embed_model, rerank_model | null}` — только общее
    подключение через сервер; `GET/PUT /api/admin/knowledge`. Нет источника — только BM25 (FTS5), всё работает.
  - `knowledge.embed(texts: list[str]) -> list[bytes]` (`POST <api>/embeddings`, пачки по 64, float32 через
    `array('f')`, без numpy); `knowledge.rerank(query, docs) -> list[float]` (`POST <api>/rerank {model, query,
    documents}` → `results[{index, relevance_score}]`); `knowledge.index(doc_id, text)` — куски `CHUNK_CHARS` с
    перекрытием `OVERLAP`, фоновая задача, `state` queued → ready/error; `knowledge.search(uid, query, *, kb_ids=(),
    attachment_ids=(), k=TOP_K) -> list[Hit(doc_name, n, text, score)]` — BM25 топ-30 ∪ векторы топ-30 → RRF → rerank.
  - Большое вложение: текст длиннее `INLINE_TOKENS` по `estimate_tokens` или не помещается в контекст → `kb_documents`
    с `attachment_id`; `_content` для него — `<document name=… excerpts="true">` с найденными по последнему сообщению
    кусками (с номером куска) вместо всего текста.
  - Базы: `GET/POST /api/knowledge`, `DELETE /api/knowledge/{id}` (в корзину), `POST /api/knowledge/{id}/documents`
    (тело — файл, `X-Studio-Name`, ≤ `documents.DOC_MAX`), `DELETE .../documents/{doc}` (в корзину); общие — в панели
    администратора с группами (`kb_rules`: нет строк — всем; строки — членам групп; администратор — всегда).
    `PUT /api/chats/{chat_id}/knowledge {kb_ids}`; чип «Знания» (`picker` мультивыбора) в поле ввода; найденное по
    сообщению — блоком `<knowledge>` в сообщение пользователя хода; `chat_tools.KNOWLEDGE_SEARCH`
    `knowledge_search(query, kb_ids?)`.
- [ ] **Step 1: Write the failing tests:**

```python
def test_chunks_overlap_and_cover_text(): ...
def test_bm25_only_without_source(): ...
def test_embeddings_and_rerank_order_hits(): ...                  # FakeGateway.on["/v1/embeddings"], ["/v1/rerank"]
def test_big_attachment_sends_excerpts_not_whole_text(): ...
def test_shared_kb_visible_by_group_only_and_admin_always(): ...
def test_chat_kb_block_and_tool(): ...
def test_kb_delete_goes_to_trash_and_restores(): ...
def test_texts_never_logged(): ...
```

- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5: Commit** по пункту.

### Task 14: «Записать» в «Расшифровке» (п. 38) и «Разговор» в чате (п. 13)

**Files:** Create: `app/static/js/recorder.js`, `app/static/js/chat-voice.js`; Modify: `app/static/js/{mic.js,
transcribe.js, chat.js, app.js, icons.js}`, `app/speeches.py` (`POST /api/speech/say`), `en.js`;
Test: `tests/test_speech_say.py`, `tests/test_ui_recorder.py`, `tests/test_ui_conversation.py` (integration, звук —
поддельный поток `getUserMedia` браузера).

**Interfaces:**
- Consumes: загрузка `POST /api/transcripts`; голосовой ввод (`dictation.py`, тот же маршрут, что у микрофона поля);
  `speech.say(conn, model, text, voice, language=None)` (задача 8); чат `turn()` и `readServer`.
- Produces:
  - `recorder.js`: `startMeeting({onTick}) -> {id, pause(), resume(), stop(): Promise<Blob>, cancel()}` —
    `MediaRecorder` (`audio/webm;codecs=opus`, иначе `audio/mp4`), кусок каждые 10 с в IndexedDB `studio-rec`
    (`chunks` по `[id, n]`), Wake Lock; `pendingRecordings() -> [{id, started, seconds}]`, `restore(id) -> Blob`,
    `discard(id)`. На «Расшифровке» — «Записать» рядом с «Загрузить»: таймер, «Пауза», «Стоп и расшифровать»,
    «Отменить»; незаконченная запись после перезагрузки — «Восстановить запись (мм:сс)». `mic.js`: запись встречи —
    владелец микрофона (диктовка в это время не стартует); `cancelRecording()` для встречи ставит паузу, а не стирает;
    смена раздела запись не прерывает (точка «● Запись мм:сс» у рельса); на iPhone — честная строка «когда экран
    гаснет, запись встаёт».
  - `POST /api/speech/say {text ≤ 1000}` → звук (не сохраняется) умолчанием «Озвучки» через API-подключение; один
    запрос на учётку (429 `busy`); умолчание — MCP → 409 `say_mcp` (страница: «для «Разговора» нужен источник озвучки
    через API»).
  - `chat-voice.js` `startConversation(chat) -> stop()`: кнопка «Разговор» в поле ввода; конец фразы по энергии
    (`AnalyserNode`, тишина 1,2 с после ≥ 0,4 с речи, фраза ≤ 60 с) → распознавание → обычный ход → по мере `delta` —
    предложения в очередь `say` и воспроизведение по порядку → снова слушает; остановка — кнопкой, Escape, сменой
    раздела; iOS — звук разблокируется первым нажатием. Весь текст — в истории чата.
- [ ] **Step 1: Write the failing tests** — `say` (умолчание, 429, 409 `say_mcp`, ничего в историю и журнал);
  браузер: запись → пауза → стоп → загрузка; перезагрузка посреди записи → восстановление; «Разговор»: фраза → ход →
  звук ответа → снова слушает (320 и 1280).
- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5: Commit** по пункту.

### Task 15: Архив чата ZIP (п. 32), JS-рантайм yt-dlp (п. 22), «Скачать ролик» (п. 24)

**Files:** `app/chat_exports.py`, `app/static/js/chat.js` (меню экспорта), `deploy/docker/Dockerfile`,
`constraints.txt`, `docs/architecture/deploy-docker.md`, `app/code_actions.py`, `app/static/js/code-quick-actions.js`,
`en.js`; Test: `tests/test_chat_archive.py`, `tests/test_deploy.py`, `tests/test_code_actions*.py`.

**Interfaces:**
- Produces:
  - `chat_exports.ARCHIVE_MAX = 512 MiB`; `POST /api/chats/{chat_id}/archive` → `{token}`; `GET
    /api/chats/archive/{token}` — поток `media.ZipWriter`: `chat.md` (тот же `markdown(snapshot)`), `files/` —
    исходники вложений, медиа диалога, файлы результатов MCP (через `mcp_sections.relay`; недоступные — списком в
    `missing.txt`); больше предела — 413 `export_size`; срок токена и число одновременных — как у выгрузки истории.
  - Dockerfile: `yt-dlp[default]==<закреплённая>` и deno закреплённой версии (пакет PyPI `deno` через constraints,
    если ставится на linux/amd64 образа, иначе архив релиза с проверкой sha256); раздел «Поднять yt-dlp» в
    `deploy-docker.md` (когда YouTube ломается: новая версия, проверка `yt-dlp --version` и пробного ролика в образе,
    выкладка). `tests/test_deploy.py::test_dockerfile_pins_yt_dlp_default_and_deno`.
  - `code_actions.DEFAULTS` += `{"title": "Скачать ролик", "text": …}` — агент на компьютере пользователя скачивает
    ролик по ссылке yt-dlp: mp4 avc1+m4a, `--` перед адресом, cookies браузера пользователя при отказе, файл — в
    «Загрузки», в ответе — путь; перед запуском просит разрешение. В редакторе быстрых действий — «Добавить из набора»
    (умолчания, которых нет в списке пользователя).
- [ ] **Step 1–4:** тесты (архив: состав, `missing.txt`, 413, чужой токен 404; Dockerfile; набор действий) → FAIL →
  реализация → PASS.
- [ ] **Step 5: Commit** по пункту.

---

## Волна E

### Task 17: Панель srvllm и шлюз речи srvllm (проект `llm_routers`)

⛔ На этой машине нет checkout `llm_routers` (проверено 2026-11-21) и SSH к srvllm. Если сессия идёт здесь — запись в
`FINDINGS.md` «Панель srvllm: пункты 0.41 ждут checkout llm_routers» и пункт в итоговом отчёте. Если доступ есть —
выполнить: ночная сквозная проверка, отмена и журнал очередей видео и картинок (повторный пароль, аудит), карточка
ComfyUI через контроллер слотов, «кто держит карту» с освобождением через контроллер, «Локальные правки: n из m»,
«Руководство»; шлюз речи — `GET /speech/limits` и `language` у `/speech/asr`, `tts-qwen`, `tts-f5`. Тесты и коммиты — по
правилам проекта.

### Task 18: Завершение

- [ ] **Step 1: Документация** — `docs/architecture/{chat.md, connections.md, mcp-contracts.md, media.md, speech.md,
  sharing-export.md, shell.md, security-lock.md}`, `docs/glossary.md`, `docs/guide.ru.md` (корзина, «Мои файлы»,
  ссылки, «Справка», знания, память, «Разговор», «Записать», «Прочитать», пауза хода; «Настройки → …» по
  `test_docs.py`), карта модулей и инварианты CLAUDE.md (анонимный `/f`, корзина, защищённый источник). Run
  `tests/test_docs.py` — PASS.
- [ ] **Step 2: Одно независимое ревью** всей ветки от `8c753c73` (приватность и журнал, согласие на файлы, анонимный
  `/f`, корзина и уборка, пауза хода, защищённый источник, знания, откат 58); найденное — исправить до выпуска; в
  `FINDINGS.md` — только действительно постороннее.
- [ ] **Step 3: IDEAS.md** — удалить выпущенные: «Речь: Язык распознавания и озвучки», «Речь: Длинный текст одним
  заходом», «Речь: «Озвучить» из перевода, скана и ответа чата», «Чат и подключения: Текстовые файлы и код как
  вложения», «Чат и подключения: Предупреждение по паспорту перед отправкой», «Оболочка, поиск, ссылки: Экспорт чата
  одним архивом с вложениями», «Вход и учётки: Пароль для «Удалить всю историю»», «Фото и видео: «Хранилище»: самые
  тяжёлые записи и чистка неудачных», «Речь: Проверка записи до загрузки», «Речь: Собеседники у внешнего сервиса»,
  «Речь: Записать разговор прямо в «Расшифровке»» (заголовки — в сообщение коммита). «Чат и подключения: Заполнение
  контекста и «продолжить в новом диалоге с итогом»» — сначала в `IDEAS-archive.md` (`wontfix` части «продолжить»:
  решение владельца 2026-11-21; заполнение выпущено в 0.41.0), затем удалить из `IDEAS.md`.
- [ ] **Step 4: Проверка** — быстрый набор (оба: `main` и `agent`), браузерные файлы задач 2–15, WebKit; `git status`
  чистый; версия 0.41.0 в `/health` локального запуска.
- [ ] **Step 5: Commit** `Release Studio 0.41.0` (без push) и записка Codex на выкладку (SHA, шаг 58, Apache-блок
  `/api/my-files/[^/]+/upload`, руководство в образе, deno).

## Решения владельца в конце (⛔, одним сообщением)

1. Push и выкладка Studio 0.41.0 (Codex) и выкладка обёртки на Spark со стартовыми файлами правил, подсказок, руководства.
2. Включение «МТС» и «МТС (админ)» после проверки на живых ключах и состав группы «МТС».
3. Отправка письма администратору Spark.
4. Выкладка панели и шлюза речи s06 (проект vLLM).
5. Модель чата Spark (vLLM) как общий API моделей в Studio s06 — подключать ли (вопрос не решён).
6. Живая приёмка — вход владельца (`STUDIO_ADMIN_PASSWORD` не рабочий): шапки и поле ввода всех разделов на телефоне,
   10 вложений, подтверждение и вопрос кнопками, корзина, «Мои файлы» со ссылкой, «Прочитать», «Разговор», «Записать»,
   знания, память, «Справка».
7. Если Task 17 не выполнена здесь — когда и с какой машины.
