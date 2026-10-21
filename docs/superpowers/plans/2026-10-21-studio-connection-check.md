# Проверка подключения и паспорт возможностей — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** «Проверить» у подключения: пробы только чтением, паспорт по маршрутам с источниками возможностей
и профилем, проверка браузером для прямых, «Пробный ответ» у не-srvllm моделей чата.

**Architecture:** новый модуль `app/checks.py` (пробы, паспорт, API `check`/`passport`/`probe` под
`/api/connections`) и `app/capabilities.py` (профиль, перечень srvllm, метаданные, шаблоны имён). Паспорт —
столбец `connections.passport` (JSON); `connections.out` отдаёт его, панель администратора убирает.
`upstream.UpstreamRefused` получает `reason`, `upstream.request` — `keyless`. Страница — `settings.js`.

**Tech Stack:** FastAPI, httpx, SQLite, ES-модули без сборки; pytest + FakeGateway/FakeCloud, браузер CDP.

**Spec:** `docs/superpowers/specs/2026-10-21-studio-connection-check-design.md`

## Global Constraints

- Все пробы — `GET`, таймаут `CHECK_TIMEOUT_SEC = 10`; пробный ответ — `PROBE_TIMEOUT_SEC = 60`.
- Ни одна проба не генерирует, не делает `release`, не читает `GET /slot/videos`; пробного ответа у srvllm нет.
- Сервер ходит только через `upstream` (список, публичность имени, без перенаправлений).
- Токен не попадает в паспорт, ответ проверки и журнал; журнал — «connection <id>: проверка <итог>».
- Пределы `PUT …/passport`: 4 строки `routes`, 500 моделей, id ≤ 200 знаков, слова ≤ 300.
- Пробный ответ: `messages: [{"role":"user","content":"Ответь одним словом: ok"}]`, `max_tokens: 16`,
  `stream: false`; 400 со словом `max_tokens` — один повтор с `max_completion_tokens: 16`.
- Схема: `db.STEPS[10]`, `db.VERSION = 10` — одним блоком `PASSPORT_10`.
- Слова CORS: «нет ответа: сервис недоступен или не разрешает запросы из браузера (CORS)».

## Review Focus

- Подключение, убранное из списка (srvllm или `server_only`) — «Проверить» и «Обновить модели» говорят
  «убран из списка администратора», запроса нет (тест в Task 2 и Task 1).
- Openai-подключение с открытым `/models` — ключ `unverified`, а не `accepted` (Task 2).
- Браузер прислал паспорт для подключения через сервер — 409, подделать «серверную» проверку нельзя (Task 3).
- Смена токена/адреса/вида обнуляет паспорт, смена имени — нет (Task 1).
- Пробный ответ модели, не отмеченной в «Чате», или у srvllm — отказ без запроса (Task 3).

---

### Task 1: схема, `upstream` (причина отказа, без ключа), паспорт в подключении, «Обновить модели»

**Files:** Modify `app/db.py`, `app/upstream.py`, `app/connections.py`, `app/sections.py`, `app/admin.py`;
Test `tests/test_foundation.py`, `tests/test_connections.py`.

**Interfaces — Produces:**
- `db.PASSPORT_10 = "ALTER TABLE connections ADD COLUMN passport TEXT;\n"`; `db.VERSION = 10`.
- `upstream.UpstreamRefused(message, reason="unlisted")`, `.reason ∈ {"unlisted","internal","changed"}`;
  `refused_words` говорит `internal` своими словами («…разрешается во внутренний адрес…»).
- `upstream.request(conn, method, url, *, keyless=False, **kw)` — без `Authorization`.
- `connections.dropped(conn) -> bool` — не через сервер, но токен браузеру не выдаётся
  (`server_only`, srvllm или адрес без https).
- `connections.passport_of(conn_id) -> dict | None`, `connections.save_passport(conn_id, passport)`.
- `connections.out(conn, passport=True)` — поля `passport`, `dropped`; `sections.section_out/all_out(…, passport=True)`.
- `connections.fetch_models`: `dropped` и `UpstreamRefused` — 409 `refused` словами `refused_words`.

- [ ] Тесты: `test_migrate_adds_the_passport_to_a_version_9_base` (DROP_V10); `test_dropped_connection_models_say_so`
  (srvllm, список очищен → 409 `refused`, «убран из списка администратора», запросов 0; `found` раздела фото — то же);
  `test_internal_name_is_told_apart_from_the_list` (reason `internal`, слова разные);
  `test_keyless_request_carries_no_key`; `test_patch_drops_the_passport_on_address_kind_or_token`
  (имя — паспорт остаётся); `test_admin_mirrors_carry_no_passport`; правка `test_connection_out_carries_only_a_hint`.
- [ ] Красные → код → зелёные: `pytest -q -n0 tests/test_connections.py tests/test_foundation.py tests/test_sections.py`.
- [ ] Коммит.

### Task 2: возможности и проверка через сервер

**Files:** Create `app/capabilities.py`, `app/checks.py`; Modify `app/main.py`, `tests/fakes.py`
(`GET /slot/models`, `slot_models`); Test `tests/test_checks.py`.

**Interfaces — Produces:**
- `capabilities.profile(conn) -> "srvllm"|"openai"|"openrouter"|"compatible"` (хост `api.openai.com`, `openrouter.ai`).
- `capabilities.CATALOG_CAPS` (короткое имя → кортеж возможностей), `capabilities.NAME_RULES`
  (кортеж `(шаблоны, возможности)`, первый совпавший; имя — после последнего `/`, без регистра),
  `capabilities.by_name(id)`, `capabilities.from_metadata(entry)`.
- `checks.check(conn) -> {"passport", "found": {section: [names]}}`; `POST /api/connections/{id}/check`.
- Паспорт: `{checked, source, profile, routes: [{path, ms, status, count, reach: {ok, words}, key: {state, code, words}|None}],
  models: [{id, context_length?, caps: [{name, source}], absent?}], probes: {}}`.

- [ ] Тесты (`test_checks.py`): srvllm — два запроса `/v1/models` и `/slot/models`, catalog-возможности,
  `absent` у модели, которой нет в `/slot/models`, «Описать»/«EN» у `gemma4-abl` при 200; никаких генераций,
  release, `/slot/videos`, чата; 401 → `refused`; 403 на `/slot/models` → `limited`; `down` → «не отвечает»;
  список очищен → паспорт «убран…» без запроса; имя во внутренний адрес → отказ без запроса; openai — `metadata`
  из `architecture`, `name`-догадки (тест на каждую строку `NAME_RULES`); открытый список → `unverified`;
  OpenRouter — `GET …/key`; прямое — 409 `direct`; чужое — 404; журнал без адреса и моделей; паспорт виден в
  `GET /api/connections` и `/api/sections`.
- [ ] Красные → код → зелёные: `pytest -q -n0 tests/test_checks.py`.
- [ ] Коммит.

### Task 3: паспорт браузера и пробный ответ

**Files:** Modify `app/checks.py`; Test `tests/test_checks.py`.

**Interfaces — Produces:** `PUT /api/connections/{id}/passport` `{routes?, models?, probes?}` → `{passport, found}`;
`POST /api/connections/{id}/probe` `{model}` → `{passport, probe}`.

- [ ] Тесты: форма и пределы (5 строк, 501 модель, id 201 знак, слова 301 — 422); `source: browser`, время
  сервера, возможности выводит сервер; не прямое — 409; probes-only сливается с прежним; пробный ответ — тело
  (текст, `max_tokens: 16`, `stream: false`), повтор с `max_completion_tokens` на 400 `max_tokens`, итог и
  возможность `chat`/`probe` в паспорте; srvllm — 409 без запроса; неотмеченная модель — 422; чужое — 404;
  прямое — 409 `direct`; новая проверка сохраняет прежние пробы.
- [ ] Красные → код → зелёные; коммит.

### Task 4: страница

**Files:** Modify `app/static/js/settings.js`, `app/static/css/app.css`, `tests/fakes.py` (FakeCloud: `open_list`);
Test `tests/test_ui_browser.py`.

- [ ] «Проверить» (`.link-check`) рядом с «Обновить модели», блок `<details class="passport">` (строка
  «Проверено … · адрес … · ключ …», раскрытый — адрес, ключ, профиль, модели, возможности моделей раздела с
  источником); ошибки — в блоке; прямое — проверка браузером и `PUT …/passport`; «Пробный ответ»
  (`.model-probe`) у отмеченных моделей чата не-srvllm с окном-предупреждением; в панели администратора — ничего.
  `refresh` прямого, но `dropped`, идёт через сервер.
- [ ] Браузерные тесты: `test_settings_check_shows_the_gateway_passport`, `test_settings_check_of_a_cloud_runs_in_the_browser`
  (облако + пробный ответ + закрытый порт → CORS).
- [ ] `pytest -q -n0 -m integration tests/test_ui_browser.py -k "settings or connection or cloud or cors or section"`; коммит.

### Task 5: документы и итог

- [ ] README «Подключения и разделы», CLAUDE.md «Устройство» — кратко.
- [ ] Быстрый набор `.venv/Scripts/python -m pytest -q`; коммит.
