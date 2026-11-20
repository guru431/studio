# Основа возможностей источников — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** системное сообщение из кусков источников, профили подключений, роли чата, общие MCP в диалоге по
умолчанию с выбором инструментов, файлы между чатом и MCP, формы разделов по схеме инструмента, сигналы MCP для
подсказок, подтверждений и веб-инструментов.

**Architecture:** новые модули `chat_system`, `profiles`, `roles`, `mcp_sources`, `mcp_files`; шаг схемы 57;
`chat.py` меняют только задачи B по очереди; обёртка aitools (вне репозитория) публикует правила, подсказки и пометки.

**Tech Stack:** Python 3.13 (FastAPI, SQLite), ES-модули без сборки; обёртка — Python 3.12, MCP SDK 2.3.

**Spec:** `docs/superpowers/specs/2026-11-21-studio-source-foundation-design.md` — все сигнатуры, пределы, имена
тестов и разметка берутся оттуда; план не повторяет их, а задаёт порядок, владение файлами и проверки.

## Global Constraints

- Схема — только добавление: `db.VERSION` 57, `db.STEPS[57]` = `db.CAPABILITIES_57`, тот же SQL в `db.SCHEMA`.
- Журнал — только id, коды, числа: ни текстов профилей и ролей, ни правил MCP, ни имён файлов и инструментов.
- Подписи — `tr` и `app/static/en.js`; 320 и 1280 px; значки — `icons.js`; `:hover` — в `@media (hover: hover)`.
- Отслеживаемые файлы — только примеры адресов (`studio.example.com`, `10.77.1.x`).
- Часы — `clock.NOW`; тест дольше 1 с — `integration`.
- Тесты: точечно `/d/vsc/studio/.venv/Scripts/python -m pytest -q -n0 <файлы>` из worktree; быстрый набор
  `/d/vsc/studio/.venv/Scripts/python -m pytest -q` — один раз в конце задачи; браузер — `-m integration`.
- Worktree исполнителя начинается со старого коммита: первым делом `git merge --ff-only main`.
- Коммит после каждой задачи; сообщение + пустая строка + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Общий MCP включён по умолчанию, но недоступен — ход не ждёт дольше `mcp_client.DISCOVER_SEC` и идёт без него
   (B2: `test_unreachable_default_server_does_not_block_turn`).
2. Включённых общих серверов больше `mcp_client.MAX_SERVERS` — действующий набор обрезается, ход не падает
   (B2: `test_default_servers_capped`).
3. Вложение больше `max_bytes` сервера — понятный отказ до загрузки, без шага-ошибки посреди хода
   (B3: `test_attachment_over_server_limit_refused`).
4. Схема инструмента с бессмысленными пределами (`min > max`, огромный `enum`, `default` вне пределов) — форма
   берёт встроенные пределы, страница не ломается (A5: `test_bad_schema_limits_ignored` в браузерном тесте).
5. Профиль с параметрами, которые провайдер не принимает, при прямом подключении — отказ словами, а не тихий сбой
   (A3/B1: `test_direct_turn_returns_params` дополнить проверкой, что `top_k` у `openai` не попадает в `params`).

---

## Партия 1 (параллельно, 4 исполнителя)

### Task A1: Шаг схемы 57

**Files:** Modify `app/db.py`, `CLAUDE.md` (строка 57 таблицы отката — текст из спеки §1),
`docs/architecture/deploy-host.md` (подробности отката, если там есть раздел по шагам); Test `tests/test_foundation.py`.
**Interfaces:** Produces — таблицы и столбцы спеки §1 с теми же именами; триггеры `connection_profiles_delete`,
`chat_roles_delete`, `mcp_uploads_server_delete`.

- [ ] Тест `test_step_57_adds_profiles_roles_dialog_mcp_state_and_previous_writer_works`: база шага 56 → `migrate`
  → таблицы и столбцы есть; вставка в `chats` без новых столбцов (прежний писатель) работает; удаление
  подключения уносит профиль, удаление роли обнуляет `chats.role_id`, удаление сервера уносит `mcp_uploads`.
- [ ] FAIL → реализация `CAPABILITIES_57` → PASS (`tests/test_foundation.py tests/test_docs.py`).
- [ ] Commit «Add schema step 57 for source capabilities».

### Task A2: Каталог MCP и знакомство — `mcp_sources`

**Files:** Create `app/mcp_sources.py`; Modify `app/mcp_client.py` (`Session.initialize` сохраняет `instructions`,
`capabilities`; `Session.ensure`, `Session.prompts`, `Session.prompt`; `forget(sid)` из `update_for`, `delete_for`,
`check_for`); Test `tests/test_mcp_sources.py`. Не трогать `mcp_sections.py` и `chat.py` (их переводит B2).
**Interfaces:** Produces — `mcp_sources.Catalog`, `async catalog(session, timeout) -> Catalog`, `forget(sid)`,
`hints(tool) -> dict` (`open_world`, `confirm` — с разбором `when`, `chat_files` — `max_bytes | None`), константы
спеки §4 и §7; `Session.instructions: str | None`, `Session.capabilities: dict`.

- [ ] Тесты: кэш по ключу (sid, stamp, пользователь) с `CATALOG_TTL_SEC`; неудача не кэшируется; `forget` сбрасывает;
  `prompts/list` с листанием и пределами; `hints` — `openWorldHint` только явное `true`, `studio/confirm` с `when`
  (`seconds > 30`, `playlist_items` не пусто), `studio/chat-files`; общий сервер — ключ по `access.studio_user(uid, sid)`.
- [ ] FAIL → реализация → PASS (`tests/test_mcp_sources.py tests/test_mcp.py tests/test_access_groups.py`).
- [ ] Commit «Cache MCP catalogs with instructions, prompts and tool hints».

### Task A5: Пределы форм из схемы

**Files:** Modify `app/static/js/mcp-sections.js` (`limits(key)`), `app/static/js/video.js`, `photo.js`,
`series.js`, `app/mcp_sections.py` (только `check_bounds` и его вызов в `run`), `app/static/en.js`;
Test `tests/test_mcp_sections.py`, `tests/test_ui_section_limits.py` (integration).
**Interfaces:** Produces — `mcp_sections.check_bounds(tool: dict, args: dict) -> None` (422 словами спеки §6).

- [ ] Тесты: `test_run_refuses_values_outside_schema`; браузерный — ролик до 600 с по схеме, размеры фото по `enum`,
  `test_bad_schema_limits_ignored` (Review Focus 4), 320 и 1280.
- [ ] FAIL → реализация → PASS (быстрые + `-m integration tests/test_ui_section_limits.py tests/test_ui_mcp_bridge.py`).
- [ ] Commit «Take section form limits from the MCP tool schema».

### Task A6: Обёртка aitools — правила, подсказки, пометки (вне репозитория)

**Files:** `D:/vsc/_work/aitools-studio/aitools_studio.py`, `README.md`, `tests/test_aitools_studio.py`, примеры
`rules.example.md`, `prompts.example.json`, `hints.example.json`.
**Interfaces:** Produces — `initialize.instructions` из `AITOOLS_STUDIO_RULES`; `prompts/list|get` из
`AITOOLS_STUDIO_PROMPTS`; `annotations.openWorldHint` и `_meta["studio/confirm"]` из `AITOOLS_STUDIO_HINTS`
(остальным — явное `openWorldHint: false`); `_meta["studio/chat-files"] = {"max_bytes": …}` у `artifact_upload`,
`path` в его `structuredContent`.

- [ ] Тесты спеки §7 (`test_initialize_carries_rules_file`, `test_prompts_list_and_get`,
  `test_annotations_and_confirm_meta`, `test_artifact_upload_marks_chat_files_and_returns_path`); без файлов
  настроек обёртка работает как раньше.
- [ ] FAIL → реализация → PASS (`.venv/Scripts/python -m pytest -q` в каталоге обёртки).
- [ ] Commit в локальный git обёртки. Выкладку на Spark делает координатор.

## Партия 2 (параллельно, после слияния A1)

### Task A3: Профиль подключения — `profiles`

**Files:** Create `app/profiles.py`; Modify `app/connections.py` (`out`: `suggestions`, `profile_set`),
`app/admin.py` (маршруты общего профиля — дописать в конец файла), `app/checks.py` (параметры профиля в пробе),
маршруты своего профиля — рядом с маршрутами подключений; Test `tests/test_connection_profiles.py` (без хода чата).
**Interfaces:** Produces — `profiles.ProfileIn`, `get(owner_id, conn_id) -> dict`, `put(owner_id, conn_id, body)`,
`request_params(conn) -> dict` (уже с `max_completion_tokens` для `openai` и без `top_k` там), `suggestions_of(conn)
-> list`, `profiles.REFUSED`.

- [ ] Тесты спеки §2 кроме требующих хода (те — в B1).
- [ ] FAIL → реализация → PASS → Commit «Add connection profiles».

### Task A4: Роли — `roles`

**Files:** Create `app/roles.py`; Modify `app/main.py` (`roles.seed_defaults()` в lifespan), `app/admin.py`
(маршруты умолчаний — дописать в конец файла), маршруты `/api/roles`, `app/takeout.py` (выгрузка ролей);
Test `tests/test_chat_roles.py` (без хода чата).
**Interfaces:** Produces — `roles.BUILTIN`, `seed_defaults()`, `list_for(uid, lang) -> list[dict]`, `get(uid, role_id)
-> dict | None`, `create`, `update`, `delete`, `add_defaults(uid, lang)`, `roles.MAX_ROLES`.

- [ ] Тесты спеки §3 кроме «роль в системном сообщении» (B1).
- [ ] FAIL → реализация → PASS → Commit «Add chat roles with admin-managed defaults».

## Партия 3 (последовательно; все правят `chat.py`)

### Task B1: Системное сообщение — `chat_system`

**Files:** Create `app/chat_system.py`; Modify `app/chat.py` (замена `with_date`, `MCP_NOTE`, `StreamTask.params`,
`_cleaner_messages`, прямой ход — `params`, `role_id` в PATCH и `_chat_view`, оценка токенов в `create_turn`),
`app/static/js/chat.js` (подмешать `params` в прямой ход); Test `tests/test_chat_system.py`.
**Interfaces:** Consumes — `profiles.request_params`, `roles.get`; Produces — `chat_system.base`, `wrap`, `mcp_block`,
`clean`, константы спеки §0.

- [ ] Тесты: порядок кусков; профиль только своему подключению; роль в сообщении и у Cleaner; прямой ход получает
  куски и `params` (Review Focus 5); `clean` режет управляющие символы и `<mcp-rules`; токены учитывают куски.
- [ ] FAIL → реализация → PASS (`test_chat*.py test_connection_profiles.py test_chat_roles.py`) → Commit.

### Task B2: MCP в диалоге

**Files:** Modify `app/chat.py` (PATCH `mcp_off`, `mcp_tools_off`; `_chat_view`; `_needs_web_consent`),
`app/mcp_client.py` (`Tools.discover` из кэша, `Tools.call` перепроверка, `Tools.instructions`),
`app/mcp_sources.py` (`dialog_servers`), `app/mcp_sections.py` (`catalog` на кэш), маршруты `GET /api/mcp/catalog`,
`POST /api/mcp/{sid}/prompt`; Test `tests/test_mcp_dialog.py`, правка `tests/test_mcp.py`, `tests/test_access_groups.py`.
**Interfaces:** Consumes — A2; Produces — `mcp_sources.dialog_servers(uid, row, conn) -> list`.

- [ ] Тесты спеки §4 + `test_unreachable_default_server_does_not_block_turn`, `test_default_servers_capped`.
- [ ] FAIL → реализация → PASS → Commit.

### Task B3: Файлы между чатом и MCP — `mcp_files`

**Files:** Create `app/mcp_files.py`; Modify `app/chat.py` (`sync` в `StreamTask._read`, шаг с `files`, приём звука
и видео при сервере с `studio/chat-files`), `app/mcp_sections.py` (`upload_artifact(..., name)` → `{id, path}`,
`relay`), `app/web.py` (`public` — `{name, mime}`), маршрут скачивания; Test `tests/test_mcp_chat_files.py`.
**Interfaces:** Consumes — A2, B2; Produces — `mcp_files.sync`, `result_files`, `mcp_sections.relay`.

- [ ] Тесты спеки §5 + `test_attachment_over_server_limit_refused`.
- [ ] FAIL → реализация → PASS → Commit.

## Партия 4 (интерфейс параллельно, затем документы)

### Task C1–C4: страницы

C1 `connection-profile.js` (`#conn-profile-dialog`, кнопки в `settings.js` и `admin.js`); C2 `roles.js`
(`#role-chip`, `#role-menu`, `#chat-roles-settings`, `#admin-role-defaults`); C3 `chat-mcp.js` (`#chat-mcp-dialog`,
`.chat-mcp-server`, `.chat-mcp-tool`, метки, «Обновить список», чип `MCP (n)`); C4 `chat-files.js` (`.msg-files`,
имена файлов в ссылке и экспорте). Каждая — свой браузерный тест (`tests/test_ui_connection_profile.py`,
`test_ui_chat_roles.py`, `test_ui_chat_mcp.py`, `test_ui_chat_files.py`), 320 и 1280, подписи — `tr`/`en.js`.

### Task C5: документация и версия

`docs/architecture/chat.md`, `connections.md`, `media.md`, `docs/glossary.md`, карта модулей CLAUDE.md
(`chat_system`, `profiles`, `roles`, `mcp_sources`, `mcp_files`), `docs/guide.ru.md`, `app/release.py` → 0.40.0;
`tests/test_docs.py` и быстрый набор — зелёные.

## Координатор

После каждой партии: слияние веток в `main` (конфликты `admin.py` — дописывание маршрутов), быстрый набор,
браузерные тесты затронутого, одно ревью всей пачки после партии 4 (находки — в `FINDINGS.md`), выкладка обёртки
на Spark после A6. Выкладку Studio делает Codex; push — только по слову владельца.
