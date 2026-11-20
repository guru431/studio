# Группы доступа к общим API и MCP — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** администратор Studio выдаёт общие API моделей, общие MCP и отдельные инструменты MCP группам учёток;
aitools хоста Spark подключается к Studio общим MCP с файлами разделов «Фото», «Видео», «Сканы».

**Architecture:** новый модуль `app/access.py` (группы, правила, проверки) и шаг схемы 56; фильтр доступа — в
узких местах, где общие строки (`user_id IS NULL`) попадают к учётке; панель — блок «Группы» и диалоги «Доступ».
На Spark — отдельная обёртка `aitools_studio.py` (Streamable HTTP, Bearer, личные папки по `X-Studio-User`,
контракт MCP-сервисов), вызывающая aitools через `aitools_cli.py`, как Open WebUI.

**Tech Stack:** Python 3.13 (FastAPI, SQLite), ES-модули без сборки; на Spark — Python 3.12, MCP SDK 2.3.0
(`mcp.server.lowlevel.Server`), Starlette 1.10, uvicorn 0.54.

**Spec:** `docs/superpowers/specs/2026-11-20-studio-access-groups-design.md`

## Global Constraints

- Схема — только добавление: `db.VERSION` 56, шаг `db.STEPS[56]`, тот же SQL в `db.SCHEMA`; строка 56 в таблице отката CLAUDE.md.
- Журнал — только id, коды и числа; имена групп, инструментов, логины, адреса — никогда.
- Подписи страницы — через `tr` и `app/static/en.js`; 320 и 1280 px; `:hover` — только в `@media (hover: hover)`; значки — только `icons.js`.
- Отслеживаемые файлы — только примеры адресов (`studio.example.com`, `10.77.1.x`); адреса Spark, s06 и токены — никогда.
- Часы — `clock.NOW`; без реального времени в тестах.
- Версия Studio — `app/release.py::VERSION` = `0.39.0`.
- Тесты Studio: точечно `.venv/Scripts/python -m pytest -q -n0 <файл>`; быстрый набор `.venv/Scripts/python -m pytest -q` (60 с) — один раз в конце пакета.
- `aitools_mcp.py` на Spark не меняется; ZeroClaw и Open WebUI не трогаются.

## Review Focus

1. Администратор, не состоящий ни в одной группе, видит и вызывает закрытое — тест в Task 1 (`test_admin_bypasses_rules`).
2. Удаление группы из правила оставляет правило «только администраторам», а не открывает всем — Task 1 (`test_deleted_group_keeps_rule_closed`).
3. Диалог с выбранной общей моделью, которую закрыли, — `GET` диалога без 500, ход — отказ «недоступно» — Task 2 (`test_closed_shared_model_in_existing_chat`).
4. Правило закрывает `job_status`/`artifact_upload` для группы, а раздел с разрешённым инструментом всё равно загружает исходник и забирает результат — Task 3 (`test_service_calls_ignore_tool_rules`).
5. Общий MCP выбран в диалоге (`chats.mcp_ids`), потом закрыт для учётки — ход идёт без него, без 500 — Task 3 (`test_closed_mcp_dropped_from_chat_selection`).

---

### Task 1: Шаг схемы 56 и модуль `access`

**Files:**
- Modify: `app/db.py` (константа `ACCESS_56`, `VERSION = 56`, `SCHEMA += ACCESS_56`, `STEPS[56]`)
- Create: `app/access.py`
- Test: `tests/test_access_groups.py`

**Interfaces:**
- Produces:
  - таблицы `access_groups`, `access_group_members`, `access_rules`, `access_rule_groups` — как в спеке; триггеры
    `access_rules_connection_delete` (AFTER DELETE ON connections) и `access_rules_mcp_delete` (AFTER DELETE ON mcp_servers);
  - `access.is_admin(uid: int) -> bool`
  - `access.allowed(uid: int | None, kind: str, item_id: int) -> bool` — `uid None` (управление общим) → True
  - `access.tool_allowed(uid: int | None, sid: int, tool: str) -> bool` — сервер И правило инструмента
  - `access.shared_filter(uid: int | None, kind: str, column: str) -> tuple[str, tuple]` — SQL-условие
    «строка личная или общая с доступом» для `WHERE`; `column` — выражение id строки (`connections.id`, `mcp_servers.id`, `connection_id`)
  - `access.studio_user(uid: int) -> str` — 32 hex: `hmac.new(sha256(STUDIO_DATA_KEY).digest(), f"studio-user:{uid}", sha256).hexdigest()[:32]`
  - CRUD: `groups() -> list[dict]` (`{id, name, members: [uid]}`), `create_group(name) -> dict`, `rename_group(gid, name) -> dict`,
    `delete_group(gid)`, `set_members(gid, user_ids: list[int])`, `rule(kind, item_id, tool="") -> list[int] | None`
    (None — правила нет), `set_rule(kind, item_id, groups: list[int] | None, tool="")`, `tool_rules(sid) -> dict[str, list[int]]`,
    `set_tool_rules(sid, rules: dict[str, list[int]])` — полная замена. Ошибки — `HTTPException` 404/409/422 в стиле проекта.

- [ ] **Step 1: Тесты** в `tests/test_access_groups.py` (фикстура `studio`, `make_user`, `signin` из conftest):
  `test_schema_56_migrates_from_55` (база шага 55 → `db.migrate` → таблицы есть, `PRAGMA user_version` = 56);
  `test_rule_semantics` (нет правила → `allowed` True для любого; правило [g] → только участник g; правило [] → только админ);
  `test_admin_bypasses_rules`; `test_tool_rule_narrows_server` (сервер [g1], инструмент [g2] → нужен g1 и g2; инструмент без правила → как сервер);
  `test_deleted_group_keeps_rule_closed` (правило [g], `delete_group(g)` → `rule()` == [], не None, участник g больше не видит);
  `test_item_delete_drops_rules` (DELETE строки connections/mcp_servers → её `access_rules` нет);
  `test_group_name_unique_case_insensitive` (409); `test_studio_user_is_stable_and_opaque` (одинаков для uid, разный для разных, без uid и логина в строке, `re.fullmatch("[0-9a-f]{32}")`).
- [ ] **Step 2:** `.venv/Scripts/python -m pytest -q -n0 tests/test_access_groups.py` — FAIL (нет `access`).
- [ ] **Step 3:** реализовать `ACCESS_56` и `app/access.py` (без кэша в памяти; `shared_filter` — `NOT EXISTS`/`EXISTS` по `access_rules` и членству).
- [ ] **Step 4:** тот же запуск — PASS; `tests/test_foundation.py` (сверка схемы) — PASS.
- [ ] **Step 5:** commit «Add access groups and rules (schema 56)».

### Task 2: Доступ к общим API моделей

**Files:**
- Modify: `app/connections.py` (`list_for`, `get` — параметр `gate: bool = True`), `app/sections.py` (`_rows` — фильтр общих строк),
  `app/videos.py:_connection`, `app/ocr.py` (`lease_lost`, `resume_queued`), `app/speeches.py` и `app/transcripts.py` (раннер очереди),
  `app/translate.py` (возобновление очереди при старте) — `gate=False`
- Test: `tests/test_access_groups.py`

**Interfaces:**
- Consumes: `access.shared_filter(uid, "connection", ...)`.
- Produces: `connections.list_for(user_id, *, gate=True)`, `connections.get(user_id, conn_id, *, own=False, gate=True)`;
  общая строка без доступа при `gate=True` — как отсутствующая (тот же `_gone()`).

- [ ] **Step 1: Тесты:** `test_closed_shared_model_hidden_from_sections` (общая модель с правилом [g]: участник видит в `/api/sections`,
  чужой — нет; `PUT /api/sections/chat/default` на неё — 404); `test_closed_shared_model_in_existing_chat` (диалог на общей модели,
  потом правило без учётки: `GET /api/chats/{id}` — 200, ход — тот же отказ, что у удалённого подключения, умолчание `/api/me` уходит на личное);
  `test_background_paths_ignore_rules` (`videos._connection(row)` и `ocr.lease_lost` находят закрытое подключение);
  `test_admin_views_user_with_users_access` (`GET /api/admin/users/{uid}/sections` не показывает закрытое этой учётке).
- [ ] **Step 2:** запуск — FAIL.
- [ ] **Step 3:** фильтр в `list_for`/`get` (только когда `user_id is not None and gate`), в `sections._rows`; `gate=False` в перечисленных фоновых путях.
- [ ] **Step 4:** `tests/test_access_groups.py tests/test_shared_integrations.py tests/test_sections.py tests/test_connections.py` — PASS.
- [ ] **Step 5:** commit «Restrict shared model APIs by access groups».

### Task 3: Доступ к общим MCP и их инструментам; `X-Studio-User`

**Files:**
- Modify: `app/mcp_client.py` (`row`, `servers`, `present`, `Session.headers`, `Tools.discover`, `Tools.call`, `check_for`, `FORBIDDEN`),
  `app/mcp_sections.py` (`supported` получает `uid`, `sid`; `catalog`, `run`), `app/admin.py` (`check_shared_mcp` передаёт администратора)
- Test: `tests/test_access_groups.py`

**Interfaces:**
- Consumes: `access.allowed`, `access.tool_allowed`, `access.shared_filter(uid, "mcp", "mcp_servers.id")`, `access.studio_user`.
- Produces: `mcp_client.check_for(owner: int | None, sid: int, *, actor: int | None = None)` — `actor` — учётка для заголовка;
  `mcp_sections.supported(section, tools, *, uid=None, sid=None)` — отбрасывает инструменты, которые `tool_allowed` запрещает;
  `Session.headers()` у общего сервера (`owner_id is None`) всегда добавляет `X-Studio-User: access.studio_user(self.uid)`;
  `"x-studio-user"` — в `mcp_client.FORBIDDEN`.

- [ ] **Step 1: Тесты** (мок MCP из `tests/test_mcp.py::mock`, сервис разделов `tests/test_mcp_sections.py::service`):
  `test_closed_mcp_hidden` (`/api/mcp`, `/api/mcp-tools/{раздел}` без закрытого сервера; `PATCH` диалога с его id — 422/404 как у удалённого);
  `test_tool_rule_hides_tool_in_chat_and_sections` (инструмент [g]: у чужого нет в `Tools.discover` и в каталоге раздела, `run` — 422 «недоступен»);
  `test_tool_rule_rechecked_on_call` (правило поставлено между discover и call → шаг-ошибка, ход завершается `done`);
  `test_service_calls_ignore_tool_rules` (правила [] на `artifact_upload`, `job_status`, `job_result` → `run` с файлом и `status` по билету работают);
  `test_closed_mcp_dropped_from_chat_selection` (`chats.mcp_ids` содержит закрытый → ход `done`, без вызовов к нему);
  `test_ticket_rechecks_server_access` (билет выдан, сервер закрыли → `status` и файл — 404);
  `test_studio_user_header_only_for_shared` (заголовок у общего = `access.studio_user(uid)`, у личного — нет; «Проверить» администратора — его uid;
  заголовок `X-Studio-User` в настройках личного MCP — 422).
- [ ] **Step 2:** запуск — FAIL.
- [ ] **Step 3:** реализовать; служебные вызовы `upload_artifact`/`status` идут через `call` без проверки правил инструментов.
- [ ] **Step 4:** `tests/test_access_groups.py tests/test_mcp.py tests/test_mcp_sections.py tests/test_shared_integrations.py` — PASS.
- [ ] **Step 5:** commit «Restrict shared MCP servers and tools by access groups».

### Task 4: API администратора

**Files:**
- Modify: `app/admin.py`
- Test: `tests/test_access_groups.py`

**Interfaces:**
- Consumes: CRUD `access.*` из Task 1.
- Produces (все под `auth.require_admin`): `GET/POST /api/admin/groups`, `PATCH/DELETE /api/admin/groups/{gid}`,
  `PUT /api/admin/groups/{gid}/members` `{user_ids}`; `GET/PUT /api/admin/shared/connections/{cid}/access` `{groups: null|[gid]}`;
  `GET/PUT /api/admin/shared/mcp/{sid}/access` `{groups: null|[gid], tools: {имя: [gid]}}`; `GET /api/admin/users` — поле `groups: [gid]`.
  Модели тела — pydantic в `admin.py`: `GroupIn{name: 1..60}`, `MembersIn{user_ids: list[int] ≤ 1000}`, `AccessIn{groups: list[int]|None}`,
  `McpAccessIn{groups, tools: dict[str, list[int]] ≤ 256}`; имя инструмента — `mcp_client.NAME`; неизвестная группа или учётка — 422;
  чужой (личный) id подключения или сервера — 404.

- [ ] **Step 1: Тесты:** `test_groups_api_roundtrip`, `test_access_api_roundtrip_connection_and_mcp`, `test_access_api_rejects_unknown_ids`,
  `test_access_api_admin_only` (обычная учётка — 403 на каждый новый путь), `test_admin_access_log_has_no_names` (`caplog`: нет имени группы и инструмента).
- [ ] **Step 2–4:** FAIL → реализация → PASS (`tests/test_access_groups.py tests/test_admin*.py`).
- [ ] **Step 5:** commit «Add admin API for access groups and shared access».

### Task 5: Панель администратора

**Files:**
- Modify: `app/static/js/admin.js` (блок «Группы», «Доступ» у общих API), `app/static/js/mcp-settings.js` (в режиме `admin` — «Доступ» и правила инструментов),
  `app/static/js/settings.js` (кнопка «Доступ» у общего подключения в `settingsView` с `shared: true`), `app/static/en.js`, стили — в существующем CSS панели
- Test: `tests/test_ui_access_groups.py` (новый, `pytestmark = pytest.mark.integration`)

**Interfaces:**
- Consumes: API Task 4; список инструментов — `POST /api/admin/shared/mcp/{sid}/check` (`{tools}`).
- Produces: id для тестов — `#admin-groups`, `#access-dialog`, `#mcp-access-dialog`; диалоги — через существующий `openDialog`.
  Список инструментов в `#mcp-access-dialog` — живой список «Проверить» плюс имена из правил, которых сейчас нет у сервера, с пометкой
  «нет у сервера»; отказ «Проверить» — правила всё равно показываются и сохраняются. Правило без групп — подпись «Только администраторам».

- [ ] **Step 1: Тест** `test_admin_sets_groups_and_tool_access` (320 и 1280): создать группу, добавить участника, у общего MCP поставить
  инструменту «Группам: …», у общего API — «Выбранные группы»; перезагрузка — значения сохранились; обычная учётка не видит блока «Группы».
- [ ] **Step 2–4:** FAIL → реализация → PASS (`-m integration tests/test_ui_access_groups.py`; без браузера — пропуск, это норма машины разработки).
  Плюс быстрые `tests/test_css_tokens.py tests/test_sections_meta.py` — PASS.
- [ ] **Step 5:** commit «Manage access groups in the admin panel».

### Task 6: Документация и версия

**Files:**
- Modify: `CLAUDE.md` (строка 56 таблицы отката — SQL-обход из спеки; `access` в карте модулей, строка «Подключения»),
  `docs/architecture/connections.md`, `docs/architecture/chat.md` (MCP: группы, `X-Studio-User`), `docs/guide.ru.md` (раздел про группы),
  `app/release.py` (`0.39.0`)
- Test: `tests/test_docs.py`

- [ ] **Step 1:** правки; символы — в виде `` `модуль.символ` `` только существующие.
- [ ] **Step 2:** `tests/test_docs.py` — PASS; быстрый набор `.venv/Scripts/python -m pytest -q` — PASS, без новых тестов дольше 1 с.
- [ ] **Step 3:** commit «Document access groups and release Studio 0.39.0».

### Task 7: Обёртка aitools — ядро (вне репозитория Studio)

Каталог разработки — `<projects>/_work/aitools-studio` (свой локальный git, venv с mcp 2.3.0, starlette 1.10.0, uvicorn 0.54.0, httpx, pytest).

**Files:**
- Create: `aitools_studio.py`, `tests/fake_cli.py` (подмена `aitools_cli.py`: `list` — 3 инструмента, `call` — пишет файлы в `AITOOLS_WORKSPACE/out`,
  `generate_image`/`animate_image` — «Запущено фоновое задание 1120-120000-abcd» и `.jobs/<id>/job.json`), `tests/test_aitools_studio.py`, `README.md`

**Interfaces:**
- Produces: `create_app(config: Config, *, run=None) -> Starlette`; `Config(token: str, hosts: tuple[str, ...], root: Path, cli: list[str],
  deny: frozenset[str] = {"run_python", "youtube_cookies"}, call_wait: float = 25, ttl_upload: int = 86400, ttl_out: int = 3 * 86400)`;
  окружение службы: `AITOOLS_STUDIO_TOKEN`, `AITOOLS_STUDIO_HOSTS`, `AITOOLS_STUDIO_ROOT`, `AITOOLS_STUDIO_PORT`.
- Поведение: middleware — `Host` из `hosts` или loopback, иначе 403; `Authorization: Bearer` = токен (`hmac.compare_digest`), иначе 401;
  `X-Studio-User` — `[0-9a-f]{32}`, иначе 400; папка учётки — `root/<значение>`. MCP — `mcp.server.lowlevel.Server` с `on_list_tools`
  (схемы из `aitools_cli.py list` как есть, без `deny`; список читается при старте) и `on_call_tool`; `streamable_http_app(json_response=True,
  stateless_http=True, max_request_body_size=32 MiB)`. Вызов — `aitools_cli.py call <имя> <json>` с `AITOOLS_WORKSPACE` учётки; не уложился в
  `call_wait` — процесс работает дальше, ответ — текст и `structuredContent {id: job_<32 hex>, status: "running"}`.

- [ ] **Step 1: Тесты:** `test_rejects_bad_token_host_and_user` (401/403/400; `X-Studio-User` = `../x` — 400); `test_lists_tools_without_denied`;
  `test_call_runs_cli_in_users_folder` (два пользователя — разные папки, файл одного не виден `list_files` другого);
  `test_slow_call_becomes_job` (`call_wait=0.2`, медленный фейк → `job_…`, потом `job_status` completed с текстом).
- [ ] **Step 2–4:** FAIL → реализация → PASS (`.venv/Scripts/python -m pytest -q`).
- [ ] **Step 5:** commit в локальный git.

### Task 8: Обёртка aitools — контракт разделов Studio

**Files:**
- Modify: `aitools_studio.py`; Test: `tests/test_aitools_studio.py`

**Interfaces:**
- Produces (поверх Task 7, только в обёртке): `artifact_upload(data_base64, mime_type, name="")` → `{id: art_<32 hex>, mime_type, name, size}`,
  файл — `<папка>/studio_uploads/`; `generate_image` (схема aitools + тот же смысл) и `video_create(artifact_id, request, seconds=3,
  orientation="auto", seed=-1, enhance_prompt=True)` → `{id: job_<32 hex>, status: "queued"}`; `read_document(path="", artifact_id="", …)` —
  картинка → `ocr`, иначе → `read_document` aitools, ответ `{text}`; `job_status(job_id)` → `{id, status: queued|running|completed|failed,
  error?: {message}}` (номера aitools тоже принимаются, `state` running/done/failed и мёртвый pid → failed); `job_result(job_id)` →
  `{result: {text, artifacts: [{id, mime_type, name, download_path: "/artifacts/<id>/content"}]}}`; `GET /artifacts/{id}/content` — тот же
  Bearer и `X-Studio-User`, чужой — 404, `FileResponse` с Range. Метаданные — `<папка>/.studio/` (скрыта от `list_files`); уборка — при старте
  и раз в час по `ttl_upload`/`ttl_out`.

- [ ] **Step 1: Тесты:** `test_studio_section_flow` (повторяет шаги `mcp_sections`: `artifact_upload` → `video_create` → `job_status` running →
  фейк завершает → completed → `job_result` → скачивание с `Range: bytes=0-9` — 206); `test_artifact_of_other_user_is_404`;
  `test_read_document_routes_image_to_ocr`; `test_cleanup_respects_ttl` (время подменено).
- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5:** commit в локальный git.

### Task 9: Установка на Spark

- [ ] **Step 1:** каталог `~/ai-tools-studio/` (владелец admin): `aitools_studio.py`, `README.md` (записка автору aitools: что обёртка берёт у aitools — `aitools_cli.py list|call`,
  строку «фоновое задание …», `.jobs/<id>/job.json`).
- [ ] **Step 2:** `~/.config/ai-tools/studio.env` 0600: `AITOOLS_STUDIO_TOKEN` (новый, `secrets.token_urlsafe(32)`), `AITOOLS_STUDIO_HOSTS`, `AITOOLS_STUDIO_ROOT=%h/ai-files-studio`, `AITOOLS_STUDIO_PORT=8766`.
- [ ] **Step 3:** `~/.config/systemd/user/aitools-studio.service` (как `mts-mcp.service`: `EnvironmentFile`, `Restart=on-failure`, python из `~/ai-tools/.venv`), `enable --now`.
- [ ] **Step 4:** UFW: `8766/tcp ALLOW IN` только с адреса хоста Studio s06 (комментарий `studio-mcp aitools`).
- [ ] **Step 5:** проверка с хоста Studio: без токена — 401; с токеном и `X-Studio-User` — `tools/list` без `run_python`.

### Task 10: Выкладка Studio (оба экземпляра, один SHA)

- [ ] **Step 1:** ревью ветки одним проходом (новые находки — в `FINDINGS.md`), слияние в `main`, быстрый набор — PASS;
  браузерный тест запуска `image_generate` и `video_create` со схемами обёртки в разделах «Фото» и «Видео» (ревью Codex).
- [ ] **Step 2:** выкладка 0.39.0 одного SHA на оба экземпляра (`bash deploy/deploy.sh` и `STUDIO_DEPLOY_PROFILE` второго);
  экземпляр, недоступный с этой машины, — отдельный шаг владельца; смоук `/health` — `0.39.0` у каждого.

### Task 11: Подключение Spark только к Studio s06

- [ ] **Step 1:** общий MCP «spark» уже заведён выключенным (адрес — в списке разрешённых, токен — потоком stdin).
- [ ] **Step 2:** группа «Медиа»; правила: `image_generate`, `video_create`, `generate_image`, `animate_image` — «Медиа»;
  у общего MCP s06-llm `job_list` — «только администраторам» (находка о заданиях одного клиента); правила сохранены — только потом `enabled`.
- [ ] **Step 3:** приёмка в браузере тремя учётками: администратор, обычный участник «Медиа», обычная учётка вне группы —
  каталоги «Фото»/«Видео»/«Сканы», запуск инструмента, файл результата; учётки и пароли — у владельца.
