# DeepSeek Harness — четвёртый помощник «Агентов» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** студия управляет установленным у человека DeepSeek Harness (npm-CLI, Python-CLI или Desktop) на Windows,
macOS и Linux как четвёртым помощником `deepseek`: прежние и новые чаты, история, ход с карточками, права, модели,
MCP, версия, баланс DeepSeek — с моделями, провайдерами, MCP и памятью из его профиля Web/Desktop.

**Architecture:** мост получает общую основу ACP `agent/studio_agent/acp.py`, выделенную из OpenCode; поверх неё —
`deepseek.py` (`DeepSeekRunner`: `dsh --profile acp`, `session/resume`), `deepseek_rpc.py` (поиск установок и запуск),
`deepseek_settings.py` (наложение профиля `--patch`), `deepseek_history.py` (история из файлов сессий),
`deepseek_usage.py` (баланс). Сервер и страница добавляют движок `deepseek` везде, где перечислены три, и новый шаг
схемы с расширенным CHECK `code_chats.engine`. Первый этап — проба настоящего DSH; её итоги фиксируют форматы.

**Tech Stack:** мост — Python 3.11+ (asyncio, ACP JSON-RPC по stdio); сервер — Python 3.13, FastAPI, SQLite;
страница — ES-модули без сборки; DSH — npm `@deepseek-ai/dsh` (Node), Desktop (Electron).

**Spec:** `docs/superpowers/specs/2026-11-22-studio-deepseek-harness-design.md`

## Global Constraints

- Движок — ровно `deepseek`, имя в интерфейсе — «DeepSeek Harness».
- Возможности моста: `hello.deepseek` (каталог `{version, models, efforts, defaults, settings}`), `hello.deepseek_models: true`, `hello.deepseek_usage: true`. Без `hello.deepseek` сервер отвечает 409 `unsupported`, страница помощника не показывает.
- Права чата DSH — ровно `("default", "read-only", "workspace-write", "danger-full-access")`; `default` не трогает окружение, остальные задают `DSH_PERMISSION_MODE` процессу хода.
- Параметр усилия ACP DSH — `reasoning_effort` (у OpenCode — `effort`).
- Новый внешний адрес моста — только `limits.HOSTS["deepseek"] = "api.deepseek.com"`; ключ DeepSeek уходит только туда и только при провайдере `deepseek-official` и `DEEPSEEK_BASE_URL` пустом или `https://api.deepseek.com`.
- Файлы DSH человека (`$DSH_HOME/profiles/*`, `cordis.patch.yml`, `.credentials.yaml`, `sessions/`) мост только читает; пробная сессия без prompt не оставляет следа в `session/list` и на диске.
- Журнал моста и сервера — только id, коды, числа, имена типов исключений: ни текстов, ни путей, ни имён проектов, ни ключей, ни вывода DSH.
- Схема — только добавление: шаг = следующий свободный номер на начало Task 9 (сейчас `db.VERSION` 62; планы зеркал и Cleaner-шлюза от 2026-11-22 претендуют на 63 — ниже «шаг N»); SQL — в `db.STEPS` и `db.SCHEMA`; строка N — в таблице отката `CLAUDE.md`.
- Версии: Studio `app/release.py::VERSION` и мост `agent/studio_agent/__init__.py::VERSION` — следующий minor на начало Task 11 (сейчас 0.44.0 и 0.44.2).
- Подписи страницы — через `tr` с переводом в `app/static/en.js`; отказы моста и сервера — тоже в `en.js` (`tests/test_i18n.py`, `tests/test_i18n_server.py`); подписи окна/трея/консоли моста — `i18n.tr` и `i18n.EN`; значки — только `icons.js`; `:hover` — только в `@media (hover: hover)`; браузер — 320 и 1280 px.
- Быстрые тесты без сети и реального времени (часы `clock.NOW` / параметр `now`, паузы `clock.SLEEP` / параметр `sleep`); дольше 1 с — `@pytest.mark.integration`; настоящий DSH — только `manual`.
- Команды: сервер — `.venv/Scripts/python -m pytest -q -n0 <файл>`; мост — из `agent/`: `.venv/Scripts/python -m pytest -q <файл>`; быстрые наборы обоих — один раз в Task 11.
- `.py` — UTF-8 без BOM, LF; `.ps1` — UTF-8 с BOM, CRLF. Коммит — через файл (`git commit -F`), в конце `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. В `main` работают другие сессии: `git add` только своих файлов, без `git add -A`; не пушить; worktree исполнителя — сначала `git merge --ff-only main`.
- Работающий Windows-мост не перезапускать и не завершать из задания.

## Review Focus

1. DSH нет, Node старше требования пакета или на Linux в PATH чужой `dsh` (distributed shell) — мост работает без `hello.deepseek`, карточка компьютера называет причину, ничего не падает — Task 3 (`test_foreign_dsh_is_rejected`, `test_old_node_is_reported_not_ready`), Task 8 (`test_hello_without_deepseek_when_not_installed`).
2. Чат открыт в DSH Web/Desktop (блокировка писателя) — отказ `writer_active` со словами чата, в файлы ничего не пишется, очередь уточнений возвращается в `done.unsent` — Task 6 (`test_resume_of_busy_session_is_writer_active_and_returns_queue`).
3. DSH обновился и пишет незнакомую версию формата сессий — история даёт `notice: "unsupported_format"`, ход в том же чате работает — Task 5 (`test_unknown_format_version_is_unsupported`), Task 6 (`test_history_unsupported_format_gives_notice_and_turn_still_runs`).
4. Патч профиля Web битый или с незнакомыми строками — ход идёт без них, сводка «не применено» называет только имена строк и причину — Task 4 (`test_unreadable_profile_yields_no_overlay_and_reason`, `test_known_rows_applied_unknown_skipped_by_name_only`).
5. Мост упал посреди хода — временный файл наложения с настройками провайдеров не остаётся: стирается при следующем старте ядра до первого хода — Task 4 (`test_overlay_written_privately_and_swept`), Task 8 (`test_core_start_sweeps_deepseek_overlays`).

---

### Task 1: Проба настоящего DSH (ворота плана)

**Files:**
- Create: `docs/reports/2026-11-22-deepseek-harness-probe.md`
- Create: `agent/tests/fixtures/deepseek/` — `initialize.json`, `session_new.json`, `session_list.json`, `turn_updates.jsonl`, `permission_request.json`, `resume_busy_error.json`, `prompt_result.json`, `session.v3.jsonl`, `session.v3.jsonl.zstd` (имена версий — по найденному формату), `web_patch.yml`, `acp_patch.yml`, `credentials.example.yml`
- Черновики пробы — только в `.tmp/dsh-probe/` (не коммитятся)

**Interfaces:**
- Produces: фикстуры выше без ключей, личных путей и текстов владельца (пути — `<PROJECT>`, `<HOME>`; ключи — `sk-test`); отчёт с разделами «D1 Наложение», «D2 Чтение истории», «D3 Права на возобновлённой сессии», «D4 След пробной сессии», «D5 Занятая сессия», «D6 Desktop», «Строки наложения», «Формат сессий», «Идентификатор сессии», «Зависимости моста». Каждый следующий Task берёт значения отсюда.

- [ ] **Step 1: Поставить npm-DSH локально, без глобальной установки**

Run: `npm install --prefix .tmp/dsh-probe/npm @deepseek-ai/dsh@latest` затем `node .tmp/dsh-probe/npm/node_modules/@deepseek-ai/dsh/lib/bin.js --version`
Expected: версия (на 2026-11-22 — `0.2.2-rc.5`); Node машины 26.7.0 удовлетворяет `>=24.9.0`. Не подходит — записать в отчёт и взять последний выпуск с подходящим `engines`.

- [ ] **Step 2: Отдельный `DSH_HOME` и сторонний провайдер**

`DSH_HOME=.tmp/dsh-probe/home`. Провайдер — шлюз srvllm как OpenAI-совместимый (адрес — из `deploy/local-profile.sh`/vault, ключ — из `local-config/secrets/vault.env` потоком в переменную окружения процесса, не в argv и не в вывод), модель с вызовом инструментов. Настроить его в профиле `web` (через `dsh --profile web --no-open` или правкой `profiles/web/cordis.patch.yml` по `--dump-config-schema`) и добавить туда один stdio MCP-сервер (любой из `apps/cli/config/examples/`). Модель без инструментов или шлюз не принимается — остановиться и вернуться к владельцу.

- [ ] **Step 3: Снять кадры ACP скриптом из `.tmp/dsh-probe/`**

Скрипт на `asyncio` запускает `node bin.js --profile acp [--patch <файл>]` во временном проекте и пишет кадры в JSONL: `initialize`; `session/new` + `session/close` без prompt; `session/list`; prompt «Ответь одним словом: готово»; prompt, вызывающий инструмент записи (карточка `session/request_permission`, ответ allow); `session/cancel` идущего prompt; `session/resume` после перезапуска процесса. Обезличить и сохранить фикстуры.

- [ ] **Step 4: Проверки D1–D6 и форматы — каждую записать в отчёт с итогом**

- D1: `--patch` со строками провайдера и MCP из патча `web` поверх `acp` — модель провайдера есть в `configOptions`, инструмент MCP вызывается; какие строки патча `web` относятся к web-app и ломают запуск; формат `--dump-config` (YAML/JSON).
- D2: есть ли официальный способ прочитать переписку (`dsh --help`, подкоманды, методы `--profile sdk`); если нет — формат файла: имя и версия, сжатие, поля записей пользователя, ответа, мыслей, шага инструмента и его итога, картинок; правило каталога `--<cwd>--/<id>/`.
- D3: сессия создана при `workspace-write`, возобновлена с `DSH_PERMISSION_MODE=read-only` — запись отказана или спрошена (да/нет).
- D4: после `session/new` + `session/close` без prompt — строки в `session/list` и каталога на диске нет (да/нет).
- D5: `session/resume` сессии, открытой другим процессом (второй `acp` или `dsh --profile web` с открытым чатом), — код и текст ошибки; держит ли Web блокировку до выхода из приложения.
- D6: Desktop для Windows — скачать официальный установщик в пустую `.tmp/dsh-probe/desktop-dl`, установить, записать: каталог установки, exe Electron, путь CLI в ресурсах, точную команду RunAsNode, `DSH_HOME` Desktop, что пишет «Manage dsh Command… → Install» в PATH; для macOS и Linux — пути по конфигурации сборки `apps/desktop` репозитория.
- Также: путь `DSH_HOME` по умолчанию на Windows; форма `usage_update` и `usage` итога prompt; отказ маршрута без картинок на prompt с картинкой; шаблон id сессии; формат ключа в `.credentials.yaml`; префикс провайдера в id моделей (`deepseek-official/...`?).

- [ ] **Step 5: Ворота**

Если D1 не работает целиком (строки Web зависят от web-app, память привязана к профилю Web, `--patch` ведёт себя иначе) — **остановиться** и вернуться к владельцу с отчётом (спека §6.1). Иначе — в отчёте раздел «Решения» с выбором по каждой ветви спеки: путь истории (официальный или файлы), зависимости моста (PyYAML и/или `zstandard` — только если без них нельзя), D3 → ветвь прав, D5 → слова отказа.

- [ ] **Step 6: Commit**

```bash
git add docs/reports/2026-11-22-deepseek-harness-probe.md agent/tests/fixtures/deepseek
git commit -F .tmp/commit-msg.txt   # «Probe DeepSeek Harness ACP, sessions and Desktop runtime»
```

---

### Task 2: Общая основа ACP, выделенная из OpenCode

**Files:**
- Create: `agent/studio_agent/acp.py`
- Modify: `agent/studio_agent/opencode.py` (остаются только OpenCode-специфичные части), `agent/studio_agent/opencode_rpc.py` (`Client` — тонкий подкласс), `agent/studio_agent/opencode_events.py` (общие `context`, `usage` переезжают в `acp.py`)
- Test: `agent/tests/test_acp.py`; без правки ожиданий — `test_opencode.py`, `test_opencode_features.py`, `test_opencode_rpc.py`, `test_opencode_native.py`, `test_opencode_usage.py`, `test_metadata.py`, `test_attachments.py`, `test_followups.py`, `test_history_images.py`, `test_turn_events.py`, `test_diffs.py`, `test_agent.py`, `test_app.py`

**Interfaces:**
- Produces:
  - `acp.RpcError` (переезжает; `opencode_rpc.RpcError` — тот же класс), `acp.RPC_SEC = 60`.
  - `acp.Client(command: Sequence[str], cwd: Path, *, env: Mapping[str, str] | None = None, spawn=asyncio.create_subprocess_exec)` — прежний `opencode_rpc.Client` с командой целиком; `opencode_rpc.Client(path, cwd, *, spawn=...)` передаёт `[str(path), "acp"]`.
  - `acp.Aliases(home: Path, filename: str, session_id: re.Pattern)`; `acp.options`, `acp.values`, `acp.settings(data, access=None, *, effort_key="effort")`, `acp.text_of`, `acp.step_of`, `acp.normalize(updates, *, image_blocks=None)`, `acp.timestamp`, `acp.context`, `acp.usage`, `acp.TurnClient`. Имена остаются доступны и из `opencode` (импорт), чтобы тесты OpenCode не менялись.
  - `class acp.AcpRunner(turns.Runner)` — всё общее из `OpenCodeRunner`: `probe`, `chats`, `close`, `_row`, `_revision`, `history`, `history_image`, `_configure`, `state`, `_run`, `_notification`, `_status`, `_request`, `_request_card`, `answer`, `_deny_all`, `_steer_input` (очередь до конца ответа). Атрибуты класса: `ENGINE: str`, `LABEL: str`, `SESSION_ID: re.Pattern`, `EFFORT_KEY = "effort"`, `ALIASES_FILE: str`, `CHOICES_DIR: str`, `NAMESPACE: uuid.UUID`, `FAILED: str` (слова сбоя хода). `__init__(self, emit, *, home: Path, choices=None, find_cli=None, factory=None)`.
  - Точки расширения `AcpRunner`: `async _connect(self, project: Path, *, access: str | None = None) -> acp.Client`; `async _mcp(self, rpc, project) -> list[dict]`; `async _new(self, rpc, project, directories=None) -> dict` (прежний `_temporary`); `async _open(self, rpc, project, target, directories=None) -> dict` (прежний `_load`); `async _discard(self, rpc, target) -> None` (прежний `_delete`); `async _history_items(self, project, target, row, *, image_blocks=None) -> list[dict]` (OpenCode — replay `_open` + `normalize`); `_child_of(self, update, target, known)` → `dict | False | None` (база — всегда `None`); `_progress(self, t, update) -> None` (база — ничего; OpenCode — `progress.opencode`).
  - `agent_mode`, `additional_directories`, `commands` остаются в базе необязательными (пусто, если ACP их не сообщает). Только в `OpenCodeRunner`: `start` с `agent_mode`/`additional_directories`, `fork_chat`, `subagents`, `subagent_history`, `_adopt`, `_child`, нативный API, Go.

- [ ] **Step 1: Зафиксировать наборы до правки**

Run: `.venv/Scripts/python -m pytest -q tests/test_opencode.py tests/test_opencode_features.py tests/test_opencode_rpc.py tests/test_opencode_native.py tests/test_opencode_usage.py tests/test_metadata.py tests/test_attachments.py tests/test_followups.py tests/test_history_images.py tests/test_turn_events.py tests/test_diffs.py tests/test_agent.py tests/test_app.py`
Expected: PASS; записать число тестов в сообщение коммита.

- [ ] **Step 2: Write the failing test** (`agent/tests/test_acp.py`)

```python
def test_runner_reports_its_own_engine_and_effort_option(tmp_path):
    # Probe(AcpRunner): ENGINE="probe", EFFORT_KEY="reasoning_effort", FakeRpc с configOptions model/reasoning_effort
    # chats() → items[0]["engine"] == "probe"; history() → ["engine"] == "probe"
    # state() → settings["effort"] == currentValue опции "reasoning_effort"
    # catalog["efforts"] собраны из "reasoning_effort"

def test_runner_without_children_ignores_child_metadata(tmp_path):
    # update с _meta["opencode/child-session"] у Probe попадает в текст родителя, progress["agents"] нет
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest -q tests/test_acp.py`
Expected: FAIL — `ModuleNotFoundError: studio_agent.acp`

- [ ] **Step 4: Выделить `acp.py` и перевести `OpenCodeRunner` на `AcpRunner`**

Переносом кода, без изменения поведения OpenCode; `opencode_events.child/status/commands` остаются OpenCode-специфичными.

- [ ] **Step 5: Run tests**

Run: команда Step 1 + `tests/test_acp.py`
Expected: PASS, число тестов OpenCode то же, строки ожиданий в них не менялись (`git diff --stat agent/tests` показывает только `test_acp.py`).

- [ ] **Step 6: Commit** — `agent/studio_agent/acp.py opencode.py opencode_rpc.py opencode_events.py agent/tests/test_acp.py`, сообщение «Extract the shared ACP runner from OpenCode».

---

### Task 3: Поиск установок DSH и запуск (`deepseek_rpc.py`)

**Files:**
- Create: `agent/studio_agent/deepseek_rpc.py`
- Modify: `agent/studio_agent/host.py` (каталоги поиска Desktop, если нужны `host.find_tool`)
- Test: `agent/tests/test_deepseek_rpc.py`

**Interfaces:**
- Consumes: `acp.Client`, `host.find_tool`, `host.MACOS`, `host.LINUX`, `host.NO_WINDOW`.
- Produces:
  - `@dataclass(frozen=True) class Install: kind: Literal["npm", "python", "desktop"]; command: tuple[str, ...]; env: Mapping[str, str]; profile: Literal["web", "desktop"]; package_dir: Path | None; reason: str | None` — `command` без `--profile acp`; `reason="node"`, если Node не удовлетворяет `engines`; npm/python → `profile="web"`, desktop → `"desktop"`.
  - `find_installs(*, environ: Mapping[str, str] | None = None, home: Path | None = None) -> list[Install]`.
  - `pick(installs: list[Install], source: str | None) -> Install | None` — пригодная (`reason is None`) установка, чей `profile == source`; без источника — npm, затем python, затем desktop.
  - `node_ok(version: str, engines: str) -> bool` — диапазоны вида `>=24.9.0` и `^24.22.3 || >=26.13.1`.
  - `version(install: Install) -> str` — `--version`, до 64 знаков, иначе `"DeepSeek Harness"`.
  - `class Client(acp.Client)`: `__init__(self, install: Install, cwd: Path, *, access: str | None = None, overlay: Path | None = None, spawn=...)` — команда `[*install.command, "--profile", "acp", *(("--patch", str(overlay)) if overlay else ())]`, окружение — `os.environ` + `install.env` + `DSH_PERMISSION_MODE=<access>` при `access` из трёх пресетов.

- [ ] **Step 1: Write the failing tests**

```python
def test_windows_npm_install_runs_node_with_bin_js_not_cmd_shim(tmp_path, monkeypatch): ...
    # APPDATA/npm/node_modules/@deepseek-ai/dsh/{package.json name+engines, lib/bin.js}; node.exe в PATH; dsh.cmd рядом
    # → Install(kind="npm", command=(node.exe, …/lib/bin.js), profile="web", reason=None)
def test_posix_npm_link_is_accepted(tmp_path): ...       # ссылка dsh → …/@deepseek-ai/dsh/lib/bin.js
def test_foreign_dsh_is_rejected(tmp_path): ...          # dsh вне пакета DSH → []
def test_old_node_is_reported_not_ready(tmp_path): ...   # engines ">=24.9.0", node v22.1.0 → reason == "node"
def test_node_ok_ranges():
    assert node_ok("v26.7.0", ">=24.9.0") and not node_ok("v26.7.0", "^24.22.3 || >=26.13.1")
    assert node_ok("v24.22.3", "^24.22.3 || >=26.13.1") and not node_ok("v25.0.0", "^24.22.3 || >=26.13.1")
def test_desktop_install_found_per_platform(tmp_path, platform): ...  # пути и команда — отчёт Task 1 D6; env содержит ELECTRON_RUN_AS_NODE
def test_pick_follows_source_profile():
    # source "desktop" → desktop даже при более новом npm; "web" → npm; None → npm, затем desktop
def test_client_command_and_env(tmp_path):
    # access="read-only" → DSH_PERMISSION_MODE=read-only; access="default" и None → окружение родителя без изменений
    # overlay → "--patch", str(path) в конце команды; в команде нет значений ключей
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_deepseek_rpc.py` — Expected: FAIL (`ModuleNotFoundError`)
- [ ] **Step 3: Implement** по Interfaces; Windows — настоящий `node.exe` из PATH, обёртки `.cmd`/`.ps1` не исполняются; POSIX — путь с разрешёнными ссылками внутри пакета с `package.json` `name == "@deepseek-ai/dsh"` или пакета Python из отчёта Task 1; Desktop — по D6.
- [ ] **Step 4: Run** — Expected: PASS
- [ ] **Step 5: Commit** — «Find DeepSeek Harness installs, including Desktop's runtime».

---

### Task 4: Наложение настроек профиля (`deepseek_settings.py`)

**Files:**
- Create: `agent/studio_agent/deepseek_settings.py`
- Modify: `agent/requirements.txt` и `deploy/build-agent-macos.py` (колёса, включая macOS Intel) — только если Task 1 «Зависимости моста» требует PyYAML
- Test: `agent/tests/test_deepseek_settings.py`

**Interfaces:**
- Consumes: `host.private_dir`, `store.write_atomic`; фикстуры `web_patch.yml`, `acp_patch.yml`, `credentials.example.yml`; перечень «Строки наложения» отчёта Task 1.
- Produces:
  - `dsh_home(environ: Mapping[str, str] | None = None) -> Path` — `DSH_HOME` или умолчание платформы из отчёта.
  - `source(home: Path) -> Literal["web", "desktop"] | None` — из `profiles/web/cordis.patch.yml` и `profiles/desktop/cordis.patch.yml` тот, что менялся последним.
  - `KINDS: frozenset[str]` — имена строк/плагинов провайдеров и каталога LLM, MCP, памяти, пресета прав (из отчёта).
  - `REASONS = ("unknown", "acp_own", "env_key", "unreadable")`.
  - `build(home: Path, environ: Mapping[str, str], private: Path) -> tuple[Path | None, dict]` — файл `private/deepseek/overlay-<16 hex>.yml` и сводка `{"profile": str | None, "applied": [имя], "skipped": [{"name": имя, "reason": REASONS[i]}]}`; имена ≤ 100 знаков, списки ≤ 100; значения, пути и имена переменных в сводку не попадают.
  - `sweep(bridge_home: Path) -> int` — удаляет `deepseek/overlay-*` (как `mcp.sweep`).

- [ ] **Step 1: Write the failing tests**

```python
def test_newest_profile_is_the_source(tmp_path): ...            # desktop новее web → "desktop"; нет обоих → None
def test_known_rows_applied_unknown_skipped_by_name_only(tmp_path):
    # web_patch.yml: провайдер, MCP, строка ui web-app → applied содержит провайдера и MCP,
    # skipped == [{"name": <имя строки ui>, "reason": "unknown"}]; в json.dumps(summary) нет baseURL, "sk-test", путей
def test_rows_defined_in_users_acp_profile_are_not_overridden(tmp_path):  # такая строка → reason "acp_own", в файле её нет
def test_provider_key_from_missing_env_var_is_skipped(tmp_path):         # apiKeyEnv без переменной → "env_key", имени переменной нет
def test_unreadable_profile_yields_no_overlay_and_reason(tmp_path):      # битый YAML → (None, {"profile": "web", "applied": [], "skipped": [{"name": "web", "reason": "unreadable"}]})
def test_overlay_written_privately_and_swept(tmp_path):                  # файл в private/deepseek/, sweep → 1 и файла нет
def test_dsh_files_are_not_modified(tmp_path):                           # sha256 и mtime всех файлов DSH_HOME до и после build равны
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_deepseek_settings.py` — Expected: FAIL
- [ ] **Step 3: Implement** по Interfaces; разбор — путём, выбранным в отчёте Task 1 (вывод DSH или `yaml.safe_load`/`safe_dump`).
- [ ] **Step 4: Run** — Expected: PASS
- [ ] **Step 5: Commit** — «Overlay the user's DeepSeek Harness profile onto the ACP profile».

---

### Task 5: История из файлов сессий DSH (`deepseek_history.py`)

**Files:**
- Create: `agent/studio_agent/deepseek_history.py`
- Modify: `agent/requirements.txt` (`zstandard==<текущая>; python_version < "3.14"`) и `deploy/build-agent-macos.py` — если отчёт выбрал файлы со сжатием zstd
- Test: `agent/tests/test_deepseek_history.py`

Если отчёт Task 1 нашёл официальный способ прочитать переписку (D2), модуль вызывает его с теми же Interfaces, а тесты используют его фикстуру вместо файлов.

**Interfaces:**
- Consumes: фикстуры `session.v3.jsonl`, `session.v3.jsonl.zstd`, «Формат сессий» и «Идентификатор сессии» отчёта; `history_images.reference`, `timeline.append_text`, `timeline.append_step`, `timeline.compact`, `acp.step_of`.
- Produces:
  - `FORMATS: tuple[str, ...]` — поддерживаемые имена файлов (по отчёту).
  - `READ_MAX = 64 * 1024 * 1024` — больше — `Unsupported("too_large")`.
  - `class Unsupported(Exception)` с `code: Literal["format", "corrupt", "too_large"]`.
  - `locate(home: Path, project: Path, session_id: str) -> Path | None` — только внутри `home / "sessions"`, id по шаблону отчёта.
  - `read(path: Path, *, image_blocks: dict | None = None) -> list[dict]` — сообщения той же формы, что `acp.normalize` (`id`, `role`, `text`, `steps`, `time`, `images?`).
  - `revision(path: Path) -> tuple[int, int]` — `(st_mtime_ns, st_size)` для `metadata.HistoryIndex`.

- [ ] **Step 1: Write the failing tests**

```python
def test_raw_and_compressed_fixture_read_the_same_messages(): ...  # read(raw) == read(zstd)
def test_user_text_assistant_text_thought_and_tool_step():
    # из фикстуры: [0] role user, text "Ответь одним словом: готово"; [1] role assistant, text содержит "готово";
    # шаг инструмента записи с input и итогом; шаг "Рассуждения" при наличии мыслей
def test_unknown_format_version_is_unsupported(tmp_path):  # только session.v9.jsonl → Unsupported, code == "format"
def test_corrupt_file_is_unsupported_not_crash(tmp_path):  # обрезанный zstd / мусорная строка → code == "corrupt"
def test_images_are_references_and_blocks_collected():     # images == [{"index": 0, "name": ...}], image_blocks[id] — блок
def test_locate_rejects_paths_outside_sessions_root(tmp_path):  # id с "..", "/" или ":" → None
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_deepseek_history.py` — Expected: FAIL
- [ ] **Step 3: Implement** по Interfaces; распаковка — `compression.zstd` (3.14+), иначе `zstandard`; чтение потоковое с пределом `READ_MAX`.
- [ ] **Step 4: Run** — Expected: PASS
- [ ] **Step 5: Commit** — «Read DeepSeek Harness chat history from its session files».

---

### Task 6: `DeepSeekRunner` (`deepseek.py`), права, MCP

**Files:**
- Create: `agent/studio_agent/deepseek.py`
- Modify: `agent/studio_agent/permissions.py` (`DEEPSEEK`, `valid()`), `agent/studio_agent/mcp.py` (`ENGINES`, `validate` — SSE отказ для `deepseek`, `deepseek(config, info)` по образцу `opencode`), `agent/studio_agent/history_images.py:66-72` (ветка `deepseek`, если блок картинки того же вида)
- Test: `agent/tests/test_deepseek.py`, `agent/tests/test_mcp.py`

**Interfaces:**
- Consumes: `acp.AcpRunner` и точки расширения (Task 2), `deepseek_rpc.Install/find_installs/pick/Client/version` (Task 3), `deepseek_settings.dsh_home/source/build` (Task 4), `deepseek_history.locate/read/revision/Unsupported` (Task 5).
- Produces:
  - `permissions.DEEPSEEK = ("default", "read-only", "workspace-write", "danger-full-access")`.
  - `mcp.deepseek(config: dict, info: dict) -> list[dict]` — stdio и HTTP; `mcp.validate(config, "deepseek")` отказывает SSE.
  - `class DeepSeekRunner(acp.AcpRunner)`: `ENGINE = "deepseek"`, `LABEL = "DeepSeek Harness"`, `EFFORT_KEY = "reasoning_effort"`, `ALIASES_FILE = "deepseek-chats.json"`, `CHOICES_DIR = "deepseek"`, `SESSION_ID` — шаблон отчёта, `FAILED = "DeepSeek Harness не ответил — проверьте настройки провайдера в DeepSeek Harness на компьютере"`.
  - `__init__(self, emit, *, home, choices=None, find_installs=deepseek_rpc.find_installs, factory=deepseek_rpc.Client, environ=None)`.
  - `catalog` = `{"version", "models", "efforts", "defaults", "settings": <сводка Task 4>}`; `probe` создаёт пробную сессию и закрывает её `session/close` (D4 — следа нет; иначе каталог по способу из отчёта). `state()` и событие `settings` хода несут ту же сводку в `settings["overlay"]`.
  - `_connect(project, *, access=None)`: `pick(find_installs(), source(dsh_home()))`; нет — `Refused("unsupported", "DeepSeek Harness не найден на компьютере")`; `reason == "node"` — `Refused("unsupported", "Для DeepSeek Harness нужен Node.js новее установленного")`; наложение `build(...)` на процесс, удаляется после `disconnect`.
  - `_open` — `session/resume` с `mcpServers`; ошибка занятой сессии (код из D5) → `Refused("writer_active", <слова D5; по умолчанию «Чат открыт в DeepSeek Harness на компьютере — закройте его там и повторите»>)`.
  - `_discard` — `session/close`. `_history_items` — `deepseek_history`; ревизия `HistoryIndex` — `revision(path)`.
  - `history(...)` при `Unsupported` с `code="format"`/`"corrupt"` → `{"items": [], "start": 0, "total": 0, "engine": "deepseek", "notice": "unsupported_format"}`; `"too_large"` → `Refused("too_large")`.
  - Права: при D3 = да — `access` передаётся в `_connect` каждого хода; при D3 = нет — у чата с сессией `state()` возвращает `settings["access_fixed"] = True`, а ход с иным `access` у такого чата — `Refused("invalid", "Права этого чата DeepSeek Harness заданы при создании")`.
  - Картинки — блоками ACP; отказ маршрута (вид из отчёта) → `FAILED` заменяется словами «Выбранная модель DeepSeek Harness не принимает картинки».
  - Нет `fork_chat`, `subagents`, `subagent_history`.

- [ ] **Step 1: Write the failing tests** (`FakeRpc` по образцу `agent/tests/test_opencode.py`, ответы — из фикстур Task 1)

```python
async def test_probe_builds_catalog_from_config_options_and_leaves_no_session(home): ...
    # calls == [("session/new", …), ("session/set_config_option", …)*, ("session/close", …)], без "session/prompt"
    # catalog["efforts"] — из "reasoning_effort"; catalog["settings"]["profile"] == "web"
async def test_chats_list_external_sessions_as_other_origin(home): ...   # tag None, engine "deepseek"
async def test_turn_streams_text_step_and_permission_card_then_done(home): ...
async def test_resume_of_busy_session_is_writer_active_and_returns_queue(home): ...
    # _open → RpcError из resume_busy_error.json → done с message D5, done.unsent содержит уточнение, prompt не вызывался
async def test_access_sets_permission_env_only_for_presets(home): ...     # ветвь D3
async def test_history_unsupported_format_gives_notice_and_turn_still_runs(home): ...
async def test_followup_during_answer_is_queued_until_end(home): ...
async def test_mcp_servers_passed_on_new_and_resume(home): ...
async def test_overlay_removed_after_disconnect(home): ...
async def test_journal_has_no_texts_or_paths(home, caplog): ...           # ни текста prompt, ни пути проекта, ни "sk-test"
def test_deepseek_refuses_sse():  # test_mcp.py: validate({"type": "sse", …}, "deepseek") → Refused
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_deepseek.py tests/test_mcp.py` — Expected: FAIL
- [ ] **Step 3: Implement** по Interfaces.
- [ ] **Step 4: Run** — Expected: PASS
- [ ] **Step 5: Commit** — «Drive DeepSeek Harness over ACP as the fourth runner».

---

### Task 7: Баланс DeepSeek (`deepseek_usage.py`) и новый адрес моста

**Files:**
- Create: `agent/studio_agent/deepseek_usage.py`
- Modify: `agent/studio_agent/limits.py` (`HOSTS["deepseek"]`), `agent/studio_agent/subscription.py:24-28` (`LINKS`), `:277` (ключ кэша с моделью и для `deepseek`), `:295-303` (ветка `deepseek` в `_load`); `CLAUDE.md` и `agent/CLAUDE.md` — перечни внешних адресов моста (`api.deepseek.com` — баланс DeepSeek, ключ DeepSeek)
- Test: `agent/tests/test_deepseek_usage.py`, `agent/tests/test_subscription.py`, `tests/test_docs.py` (без правки — должен пройти)

**Interfaces:**
- Consumes: `deepseek_settings.dsh_home`; формат ключа `.credentials.yaml` и префикс провайдера моделей из отчёта Task 1.
- Produces:
  - `URL = f"https://{limits.HOSTS['deepseek']}/user/balance"`, `OFFICIAL_BASE = "https://api.deepseek.com"`.
  - `key(home: Path, environ: Mapping[str, str]) -> str | None` — `DEEPSEEK_API_KEY`, иначе `.credentials.yaml`.
  - `eligible(model: str | None, environ: Mapping[str, str]) -> str | None` — `None`, если можно спрашивать; иначе причина `"provider"` или `"base_url"`.
  - `read(*, model=None, home=None, environ=None, opener=None, now=time.time) -> dict` — ответ `usage`: `{"engine": "deepseek", "available": bool, "source": "api.deepseek.com", "checked_at", "cache_ttl": 60, "plan": None, "windows": [five_hour, seven_day, monthly — available False, проценты None], "statistics": {"available": False, "source": "", "summary": {}, "daily": []}, "balance": {"available": bool, "items": [{"currency": str, "total": float, "granted": float, "topped_up": float}], "reason"?: str}}`; без перенаправлений, тело ≤ 64 КиБ, таймаут 12 с.
  - Слова причин (`balance.reason`): `provider` — «Баланс показывается только для официального провайдера DeepSeek»; `base_url` — «Задан свой адрес DeepSeek — баланс не запрашивается»; нет ключа — «На компьютере нет ключа DeepSeek»; сбой — «DeepSeek не ответил на запрос баланса».

- [ ] **Step 1: Write the failing tests**

```python
def test_balance_parsed_from_official_response():
    # opener → {"is_available": true, "balance_infos": [{"currency": "CNY", "total_balance": "0.00",
    #           "granted_balance": "0.00", "topped_up_balance": "0.00"}]}
    # → balance == {"available": True, "items": [{"currency": "CNY", "total": 0.0, "granted": 0.0, "topped_up": 0.0}]}
    # запрос: URL == "https://api.deepseek.com/user/balance", заголовок "Authorization: Bearer sk-test"
def test_no_request_for_third_party_provider():   # модель не deepseek-official → opener не вызван, reason «…официального провайдера…»
def test_custom_base_url_blocks_key():            # DEEPSEEK_BASE_URL="https://proxy.example" → opener не вызван
def test_no_key_gives_reason(tmp_path): ...
def test_redirect_refused_and_body_limited(): ...
def test_key_never_in_result_or_log(caplog): ...  # "sk-test" нет в json.dumps(result) и caplog.text
def test_deepseek_usage_is_cached_per_model():    # test_subscription.py: две модели — два ключа кэша
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_deepseek_usage.py tests/test_subscription.py` — Expected: FAIL
- [ ] **Step 3: Implement**; добавить адрес в `limits.HOSTS` и оба CLAUDE.md.
- [ ] **Step 4: Run** мост — команда Step 2; сервер — `.venv/Scripts/python -m pytest -q -n0 tests/test_docs.py` — Expected: PASS
- [ ] **Step 5: Commit** — «Show the DeepSeek balance in the usage panel».

---

### Task 8: Мост — четвёртый помощник в канале, окне и обновлении

**Files:**
- Modify: `agent/studio_agent/agent.py` (`:126,131` `deepseek=None`; `:170`, `:809`, `:839`, `:921` перечни бэкендов; `:192-200` hello; `:391` движки; `:397,479` имена — словарь `{"codex": "Codex", "opencode": "OpenCode", "deepseek": "DeepSeek Harness"}`; `:527` software; `:561` права; `:725` fork и `:758-770` подагенты — для `deepseek` `Refused("unsupported")`; `:788` поиск чатов; `:864` `_runner`)
- Modify: `agent/studio_agent/app.py` (`:17` импорт; `:69,80-83` `deepseek_factory=DeepSeekRunner`; `:92` `_backends`; `:520` `deepseek_settings.sweep`; `:531-539` создание и передача в `Agent`; `:581,585,602,658,696` перечни; `:749-758` проба)
- Modify: `agent/studio_agent/software.py` (`TOOLS["deepseek"] = "@deepseek-ai/dsh"`, `PACKAGES`, `inspect` — npm → `source "npm"`; python и Desktop → `source "native"`, `reason "manual"`; `latest` для `deepseek` допускает `INSTALLED_VERSION`; `:341,361` бэкенды)
- Modify: `agent/studio_agent/window.py:647`, `linux_menu.py:132`, `i18n.py` (`EN`), `jobs.py:3` и `__init__.py:1` (строки)
- Test: `agent/tests/test_agent.py`, `test_app.py`, `test_software.py`, `test_window_logic.py`, `test_linux_runtime.py`, `test_i18n.py`

**Interfaces:**
- Consumes: `DeepSeekRunner` (Task 6), `deepseek_settings.sweep` (Task 4), `deepseek_usage.read` через `subscription` (Task 7).
- Produces: hello `"deepseek": runner.catalog` (если не `None`), `"deepseek_models": True` (если бэкенд есть), `"deepseek_usage": True`; `Agent(..., deepseek=None)`; `Controller(..., deepseek_factory=DeepSeekRunner)`; `software_update` принимает `"deepseek"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_hello_declares_deepseek_catalog_and_flags(...): ...   # hello["deepseek"]["models"], deepseek_models, deepseek_usage
def test_hello_without_deepseek_when_not_installed(...): ...   # catalog None → нет ключа "deepseek", остальное как было
def test_fork_and_subagents_for_deepseek_are_unsupported(...): ...
def test_chats_include_deepseek_rows(...): ...
def test_models_refresh_for_deepseek_without_new_channel(...): ...   # просьба models с engine "deepseek" → новый каталог
def test_controller_builds_four_backends_and_probes_deepseek(...): ...   # test_app.py
def test_core_start_sweeps_deepseek_overlays(...): ...                   # test_app.py
def test_deepseek_npm_prerelease_is_offered_as_update(...): ...          # test_software.py: latest "0.2.2-rc.6" > "0.2.2-rc.5"
def test_deepseek_desktop_and_python_installs_are_manual(...): ...       # reason == "manual"
def test_four_cli_status_rows(...): ...                                  # test_window_logic.py, test_linux_runtime.py
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_agent.py tests/test_app.py tests/test_software.py tests/test_window_logic.py tests/test_linux_runtime.py tests/test_i18n.py` — Expected: FAIL
- [ ] **Step 3: Implement** по Files и Interfaces.
- [ ] **Step 4: Run** — Expected: PASS
- [ ] **Step 5: Commit** — «Wire DeepSeek Harness into the bridge hello, window and updates».

---

### Task 9: Сервер — движок `deepseek`, шаг схемы, лимиты

**Files:**
- Modify: `app/bridge.py:234-236` (флаги `deepseek_models`, `deepseek_usage`), `:259-277` (разбор `hello.deepseek` по правилам `opencode` + `settings`)
- Modify: `app/agents.py:190-192,236`; `app/claude_code.py:112-114` (`ACCESS["deepseek"]`), `:182-183`, `:465` (имя), `:624,633` (MCP `Literal`), `:687` (software), `:807-831` (fork/subagents для `deepseek` — 409 `unsupported`)
- Modify: `app/account_profile.py:47,50-51,72,96,100-102,197-199` (четыре движка; приоритеты 1–4)
- Modify: `app/code_usage.py:56` (`Literal`), новая модель `UsageBalance`, `Usage.balance`, `LINKS["deepseek"] = [{"label": "Баланс DeepSeek", "url": "https://platform.deepseek.com/usage"}]`, `:101,108` (`model` и для `deepseek` при `deepseek_usage`)
- Modify: `app/code_activity.py:47`, `app/computer_info.py:15` (`[:3]` → `[:4]`), `:16,28,31`
- Modify: `app/db.py` (шаг N: пересборка `code_chats` с `CHECK (engine IN ('claude', 'codex', 'opencode', 'deepseek'))`, все столбцы, включая `origin`, индекс `code_chats_user`; `db.STEPS[N]`, `db.SCHEMA += …`, `VERSION = N`)
- Modify: `CLAUDE.md` (строка N таблицы отката), `tests/fake_bridge.py:68,102-103,129,135,329,427` (`FakeMachine`: каталог `deepseek`, флаги, очередь как у `opencode`, без подагентов)
- Test: `tests/test_deepseek.py` (новый), `tests/test_foundation.py` (`db.VERSION == N`), `tests/test_code.py:331`, `tests/test_codex.py:18`, `tests/test_profile_admin.py:113-129`, `tests/test_code_usage.py`, `tests/test_computer_info.py`, `tests/test_i18n_server.py`

**Interfaces:**
- Consumes: протокол Task 8 (hello, `usage.balance`, `history.notice`, отказ `writer_active`).
- Produces: `bridge.parse_hello` → `result["deepseek"] = {"version", "models", "efforts", "defaults"?, "settings": {"profile": "web"|"desktop"|None, "applied": [str ≤ 100] ≤ 100, "skipped": [{"name": str ≤ 100, "reason": "unknown"|"acp_own"|"env_key"|"unreadable"}] ≤ 100}}`; `claude_code.ACCESS["deepseek"] == ("default", "read-only", "workspace-write", "danger-full-access")`; `code_usage.UsageBalance{available: bool, items: list[{currency: str ≤ 8, total: float ≥ 0 | None, granted, topped_up}] ≤ 10, reason: str ≤ 300 | None}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_deepseek.py`)

```python
def test_hello_deepseek_catalog_parsed_and_bad_rejected(): ...  # плохой id модели, reason вне списка → ProtocolError
def test_step_n_widens_engine_check_and_keeps_origin(tmp_path):
    # база шага N-1 с code_chats(origin='studio') → migrate → строка цела, INSERT engine='deepseek' проходит,
    # engine='other' → IntegrityError
def test_chat_with_deepseek_engine_routes_to_bridge(...): ...   # FakeMachine получает engine "deepseek"
def test_fork_and_subagents_refused_for_deepseek(...): ...      # 409 unsupported
def test_old_bridge_without_deepseek_gets_409(...): ...
def test_usage_balance_passthrough_and_validation(...): ...     # balance отдан странице; currency длиннее 8 → 502 bridge
def test_history_notice_passthrough(...): ...                   # notice "unsupported_format" доходит до страницы
def test_computer_info_keeps_four_tools(...): ...
def test_old_three_priorities_stay_valid_and_deepseek_goes_last(...): ...  # test_profile_admin.py
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_deepseek.py tests/test_foundation.py tests/test_code.py tests/test_codex.py tests/test_profile_admin.py tests/test_code_usage.py tests/test_computer_info.py tests/test_i18n_server.py` — Expected: FAIL
- [ ] **Step 3: Implement** по Files и Interfaces.
- [ ] **Step 4: Строка отката N**

Проверить чтением кода прежнего выпуска (`git show <тег/коммит>:app/claude_code.py`), что он делает со строкой `engine = 'deepseek'` (словарь имён `:465` даёт `KeyError` → 500 на её пути), и записать в таблицу `CLAUDE.md`: «N | `code_chats.engine` (deepseek) | <что делает прежний код> | `DELETE FROM code_chats WHERE engine = 'deepseek';` — сессии остаются на компьютерах».
- [ ] **Step 5: Run** — команда Step 2 — Expected: PASS
- [ ] **Step 6: Commit** — «Accept the deepseek engine on the server, step N».

---

### Task 10: Страница — DeepSeek Harness в «Агентах», настройках и лимитах

**Files:**
- Create: `app/static/js/code-deepseek.js`
- Modify: `app/static/js/code-options.js:3,11-16` (`ENGINE_NAMES`, `ACCESS`, новый `QUEUE_ONLY = new Set(['opencode', 'deepseek'])`), `code-sessions.js:27,100-101`, `code.js:658,864,867`, `code-chat.js:400,407,849` (ветвление и подагенты скрыты для `deepseek`), `:915,923` (`QUEUE_ONLY`), отказ `writer_active` и `notice` истории, `code-context.js:25-27` (текст без стоимости для `deepseek`), `code-usage.js` (баланс), `code-software.js:6`, `mcp-settings.js:294`, `settings.js:156,163,1000,1133,1144`, `ui.js:124`, `app/static/en.js`, `app/static/css/app.css` (`#code-deepseek-options`), `docs/glossary.md:91`
- Test: `tests/test_ui_deepseek.py` (новый, `integration`), `tests/test_ui_agent_priority.py`, `tests/test_ui_computer_info.py`, `tests/test_ui_code_usage.py`, `tests/test_i18n.py`, `tests/test_css_tokens.py`

**Interfaces:**
- Consumes: сводка наложения — `hello.deepseek.settings` и `state().settings.overlay` (Task 6, через сервер Task 9), `settings.access_fixed`, `usage.balance`, `history.notice`, отказ `writer_active`.
- Produces: `ENGINE_NAMES.deepseek = 'DeepSeek Harness'`; `ACCESS.deepseek = { default: tr("По настройкам DeepSeek Harness"), 'read-only': tr("Только чтение"), 'workspace-write': tr("Правка в папке проекта"), 'danger-full-access': tr("Полный доступ") }`; `export function deepseekView(host, settings) -> { update(settings) }` в `code-deepseek.js`.
- Тексты (дословно, переводы — в `en.js`): заголовок панели «Настройки DeepSeek Harness»; «Настройки взяты из профиля Web» / «…профиля Desktop» / «Профиль DeepSeek Harness не найден — действуют общие настройки»; «Не применено:» и причины `unknown` «неизвестный вид настройки», `acp_own` «задано в профиле редактора (acp)», `env_key` «ключ из переменной окружения недоступен мосту», `unreadable` «профиль не читается»; заметка истории «Историю этой версии DeepSeek Harness студия пока не читает»; заголовок баланса «Баланс DeepSeek»; при `access_fixed` — «Права заданы при создании чата»; строки с перечнем помощников — «Claude Code, Codex, OpenCode и DeepSeek Harness».

- [ ] **Step 1: Write the failing tests** (`tests/test_ui_deepseek.py`, 320 и 1280 px, `FakeMachine` с `deepseek`)

```python
def test_new_deepseek_chat_turn_with_permission_card(page, ...): ...
def test_settings_panel_lists_not_applied_items(page, ...): ...    # «Не применено:» и «ключ из переменной окружения недоступен мосту»
def test_balance_panel_shows_currencies(page, ...): ...            # «Баланс DeepSeek», «CNY»
def test_fork_subagents_and_steer_hidden_for_deepseek(page, ...): ...  # кнопка «Отправить после ответа»
def test_history_notice_for_unsupported_format(page, ...): ...
def test_writer_active_message_is_shown(page, ...): ...
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_deepseek.py` — Expected: FAIL
- [ ] **Step 3: Implement** по Files и Interfaces; перезапуск тестового процесса после правки статики.
- [ ] **Step 4: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_deepseek.py tests/test_ui_agent_priority.py tests/test_ui_computer_info.py tests/test_ui_code_usage.py` и `.venv/Scripts/python -m pytest -q -n0 tests/test_i18n.py tests/test_css_tokens.py tests/test_sections_meta.py` — Expected: PASS
- [ ] **Step 5: Commit** — «Show DeepSeek Harness in Agents, settings and usage».

---

### Task 11: Документация, версии, живая проверка, итоговые наборы

**Files:**
- Create: `agent/tests/test_deepseek_live.py` (`manual`)
- Modify: `README.md`, `README.ru.md`, `docs/guide.ru.md` (раздел DeepSeek Harness рядом с OpenCode), `agent/README.md`, `agent/CLAUDE.md` (карта пакета, живой тест, Job Object), `agent/AGENTS.md`, `AGENTS.md`, `CLAUDE.md` (карта модулей), `docs/architecture/agent-bridge.md` (строки таблицы возможностей, раздел DeepSeek Harness, обновление программ), `docs/architecture/agents-workspace.md` (приоритеты 1–4), `docs/architecture/testing.md`, `docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md` (добавка `hello.deepseek`, `usage.balance` в §9, `history.notice`, `software` в §21, §22), `deploy/build-agent-macos.py:349`, `app/release.py`, `agent/studio_agent/__init__.py`
- Test: `tests/test_docs.py`, быстрые наборы

**Interfaces:**
- Consumes: всё выше.

- [ ] **Step 1: Живой тест** `agent/tests/test_deepseek_live.py`: временный проект и `DSH_HOME`, провайдер — из окружения теста (по умолчанию шлюз srvllm, ключ потоком из vault), один ход «Ответь одним словом: готово», затем `chats`, `history` и `state`; созданная сессия закрывается.
Run: `.venv/Scripts/python -m pytest tests/test_deepseek_live.py -q -m manual --durations=5` (из `agent/`)
Expected: PASS
- [ ] **Step 2: Документация и версии** — по Files; следующие minor-версии Studio и моста.
- [ ] **Step 3: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_docs.py` — Expected: PASS
- [ ] **Step 4: Быстрые наборы**
Run: `.venv/Scripts/python -m pytest -q` (корень) и `.venv/Scripts/python -m pytest -q` (из `agent/`)
Expected: PASS, оба в бюджете 60 с; тесты дольше 1 с из вывода `--durations` — сделать быстрыми или пометить `integration`.
- [ ] **Step 5: Commit** — «Release Studio <версия> and Studio Agent <версия>: DeepSeek Harness».

Выкладка на экземпляры Studio — отдельным решением владельца после выпуска; Windows-мост после выкладки не перезапускать.
