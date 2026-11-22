# Studio: внешние модели через шлюз Cleaner — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** У внешнего подключения пользователя есть переключатель «Через Cleaner», а у администратора — флажок
«Cleaner для всех внешних моделей»; ход такого подключения со всеми инструментами идёт через шлюз Cleaner, ключ
провайдера остаётся в Studio.

**Architecture:** Новый модуль `app/cleaner_route.py` держит настройки администратора, правило «внутреннее или
внешнее», решение «через Cleaner» и все запросы к шлюзу Cleaner (контекст, ход, модели, «что ушло»). Подключение через
Cleaner считается серверным (`Connection.proxied`), поэтому инструменты, «Интернет», знания и «Перевод» работают
прежними путями; места, где запрос уходил провайдеру, вместо этого зовут `cleaner_route`. Предохранитель в
`upstream._prepare` не пускает такое подключение ни на какой адрес, кроме Cleaner.

**Tech Stack:** Python 3.13, FastAPI, httpx (async), SQLite, ES-модули без сборки, pytest + xdist, Playwright (integration).

**Spec:** `docs/superpowers/specs/2026-11-22-studio-cleaner-gateway-design.md`. Контракт шлюза — план Cleaner
`cleaner/docs/superpowers/plans/2026-11-22-cleaner-client-key.md`, раздел «Studio contract» (выполняется первым).

## Global Constraints

- Текст пользователя попадает на внешнее подключение только через Cleaner; недоступный Cleaner — отказ словами, без обхода.
- Журнал — только события с id, кодами, числами: без адресов провайдеров, ключей, текста (сторожат тесты журналов).
- Служебный ключ Cleaner и ключ провайдера в браузер не уходят; ключи — только заголовками.
- Шаги схемы — только добавление; строка 63 в таблице отката `CLAUDE.md` (сверяет `tests/test_docs.py`).
- Новый модуль `app/` — в «Карте модулей» `CLAUDE.md` (сверяет `tests/test_docs.py::test_module_map_lists_every_app_module`).
- Подписи интерфейса — через `tr` и `static/en.js`; слова сервера, показываемые странице, — с английским переводом (`tests/test_i18n*.py`).
- Значки — только `icons.js`; `:hover` — только в `@media (hover: hover)`; цвета — токены тем.
- Время — `clock.NOW`/`clock.SLEEP`; сеть в тестах — `httpx.MockTransport` (`tests/conftest.py::studio`).
- Публичный Git: в отслеживаемых файлах — только примеры адресов (`cleaner.example.com`, `10.77.1.x`, `cloud.test`).
- Версия Studio — `release.VERSION = "0.45.0"` (minor).
- Команды: точечно `.venv/Scripts/python -m pytest -q -n0 <файл>`; быстрый набор `.venv/Scripts/python -m pytest -q`.

## Review Focus

- Флажок администратора включён, а страница держит старое «прямое» состояние подключения: новый ход внешнего прямого подключения идёт через сервер и Cleaner, ответ хода не содержит `url` (тест в Task 3).
- Администратор сменил адрес или служебный ключ Cleaner при живых диалогах: следующий ход создаёт новый контекст, прежний уходит в очередь удаления со старым снимком (Task 3).
- Подключение через Cleaner на `localhost` или частный адрес: Cleaner отвечает 400 `upstream`, пользователь видит понятные слова, запроса к провайдеру нет (Task 3).
- Диалог с картинками в истории переключили на подключение через Cleaner: картинки не отправляются (пометка `chat.DROPPED`), ход идёт; новое вложение-картинка — отказ словами (Task 3).
- Общее подключение администратора с внешним адресом в принудительном режиме: у учётки переключатель заблокирован, ход идёт через Cleaner (Task 5).

---

### Task 1: Настройки Cleaner, шаг схемы 63 и откат

**Files:**
- Create: `app/cleaner_route.py`
- Modify: `app/db.py` (`VERSION = 63`, `CLEANER_ROUTE_63`, `STEPS[63]`)
- Modify: `app/admin.py` (маршруты `/api/admin/cleaner`), `app/main.py:110-112` (`cleaner_route.forward()` после `access.forward()`)
- Modify: `CLAUDE.md` (карта модулей: «Чат» или «Подключения» — `cleaner_route`; строка 63 таблицы отката)
- Test: `tests/test_cleaner_route.py` (новый), `tests/test_foundation.py` (34, 84, 196, 314, 350: 62 → 63, новый тест шага 63)

**Interfaces:**
- Produces (`app/cleaner_route.py`):
  - `SETTINGS_KEY = "cleaner_gateway"`
  - `class SettingsIn(BaseModel): base_url: str; key: str | None = None; forced: bool = False`
  - `def settings() -> dict` → `{"base_url": str, "key": str | None, "forced": bool, "reenter": bool}` (ключ расшифрован; `InvalidToken` → ключ None, `reenter` True, `forced` False)
  - `def public_settings() -> dict` → `{"base_url", "key_set", "forced", "reenter"}`
  - `def save_settings(body: SettingsIn) -> dict` (возвращает `public_settings()`)
  - `def configured() -> bool`, `def forced() -> bool`
  - `async def check() -> dict` → ответ самопроверки Cleaner `{ok, checks[]}`
  - `ROLLBACK_63: str`, `FORWARD_63: str`, `def forward() -> None`
- Схема: `connections.via_cleaner INTEGER NOT NULL DEFAULT 0`, `messages.cleaner_request TEXT`, таблица отката по образцу `access_rollback`.

- [ ] **Step 1: Write the failing tests**

```python
def test_admin_saves_cleaner_settings_and_key_never_returns(studio):
    admin = signin(studio, make_user(studio, "boss", is_admin=True) and "boss")
    allow(studio, "10.77.1.20:5464")
    r = studio.c.put("/api/admin/cleaner", json={"base_url": "https://10.77.1.20:5464", "key": "ck_" + "a" * 43, "forced": False})
    assert r.status_code == 200 and r.json() == {"base_url": "https://10.77.1.20:5464", "key_set": True, "forced": False, "reenter": False}
    assert "ck_" not in studio.c.get("/api/admin/cleaner").text

def test_unlisted_cleaner_address_is_refused(studio): ...          # 422 `server`, слова «адрес Cleaner должен быть в списке»
def test_forced_needs_address_and_key(studio): ...                 # 422 `cleaner`
def test_empty_key_keeps_the_previous(studio): ...
def test_new_data_key_asks_to_reenter(studio): ...                 # смена STUDIO_DATA_KEY → reenter True, forced False
def test_settings_are_admin_only(studio): ...                      # учётка — 403
def test_settings_change_is_logged_without_values(studio, caplog): ...

def test_step_63_adds_columns_and_previous_writer_works(tmp_path):  # по образцу test_step_62_…
    conn = historical_db(tmp_path, 62); db.migrate(conn)
    assert column(conn, "connections", "via_cleaner") and column(conn, "messages", "cleaner_request")
    conn.execute("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, cleaner, s06) VALUES (…)")  # прежний код

def test_rollback_63_spoils_routed_tokens_and_forward_restores(studio): ...
    # ROLLBACK_63: ключ подключения via_cleaner=1 сохранён и испорчен (security.decrypt → InvalidToken);
    # FORWARD_63 при старте возвращает его, если ключ не вводили заново
```

Помощники `allow` (запись списка адресов), `historical_db`, `column` — локальные в файле теста, по образцу
`tests/test_foundation.py` и `tests/historical_schema.py::install_schema`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route.py tests/test_foundation.py`
Expected: FAIL (`ModuleNotFoundError: cleaner_route`, `db.VERSION == 62`)

- [ ] **Step 3: Implement**
  - Настройки — JSON в `settings[SETTINGS_KEY]`, ключ — `security.encrypt`; по образцу `account_mail.settings`/`save_settings`.
    Адрес — без `/`, только из списка (`upstream.is_allowed`); пустой ключ сохраняет прежний; `forced` — только при
    заданных адресе и ключе. Смена адреса или ключа ставит контексты нового вида (`cleaner_contexts` с
    `base_url` прежнего адреса и снимком прежнего ключа) в `cleaner_deletions`.
  - Маршруты `admin.py`: `GET /api/admin/cleaner`, `PUT /api/admin/cleaner`, `POST /api/admin/cleaner/check`.
  - `ROLLBACK_63`/`FORWARD_63`/`forward()` — по образцу `access.ROLLBACK_56`/`FORWARD_56`/`access.forward`.
  - `CLAUDE.md`: строка 63 — «`connections.via_cleaner`, `messages.cleaner_request` — подключения через Cleaner |
    прежний код не знает столбца: ход такого подключения ушёл бы провайдеру напрямую; принудительный режим не
    соблюдается | перед откатом выполнить `cleaner_route.ROLLBACK_63` через `sql`, как строку 56; администратор решает,
    отключать ли внешние подключения; вперёд — `FORWARD_63` при старте».

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route.py tests/test_foundation.py tests/test_docs.py`
Expected: PASS

- [ ] **Step 5: Commit** — `git commit -m "Add Cleaner gateway settings and schema step 63"`

---

### Task 2: Подключения — внешнее, переключатель, действующий маршрут

**Files:**
- Modify: `app/connections.py` (`Connection` 57-73, `ConnIn` 84, `ConnPatch` 95, `_from_row` 110, `_COLS` 120, `create` 329, `patch` 373, `out` 241)
- Modify: `app/sections.py:191-206` (`accepts`, `check_link`)
- Modify: `app/upstream.py:153-163` (`_prepare`: предохранитель)
- Modify: `app/cleaner_route.py`
- Test: `tests/test_cleaner_route.py`, `tests/test_connections.py`

**Interfaces:**
- Consumes: Task 1 (`forced()`).
- Produces:
  - `Connection.via_cleaner: bool = False`, `Connection.routed: bool = False`; `proxied = upstream.is_allowed(base_url) or routed`
  - `def is_external(conn: Connection) -> bool`, `def routes(conn: Connection) -> bool` в `cleaner_route`
  - `connections.out(conn)` дополнительно: `via_cleaner`, `routed`, `external`, `cleaner_locked` (= `forced()` и внешнее)

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("kind, base_url, listed, flags, external", [
    ("openai", "https://10.77.1.30:8000/v1", True, {}, False),      # частный IP из списка
    ("openai", "https://10.77.1.30:8000/v1", False, {}, True),      # частный IP не из списка (прямое)
    ("openai", "http://localhost:1234/v1", False, {}, True),
    ("openai", "https://api.cloud.test/v1", True, {}, True),        # имя — внешнее и из списка
    ("openai", "https://8.8.8.8/v1", True, {}, True),
    ("openai", "https://10.77.1.15:4000", True, {"s06": True}, False),
    ("openai", "https://10.77.1.20:5464", True, {"cleaner": True}, False),
    ("srvllm", "https://10.77.1.15:4000", True, {}, False),
])
def test_internal_and_external(studio, kind, base_url, listed, flags, external): ...

def test_via_cleaner_makes_a_direct_connection_server_side(studio):
    cid = add_connection(studio, base_url="https://api.cloud.test/v1", sections=["chat"])   # не в списке → прямое
    studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True})
    item = conn_of(studio, cid)
    assert item["routed"] and item["proxied"] and studio.c.get(f"/api/connections/{cid}/token").status_code == 403

def test_via_cleaner_only_for_external_openai(studio): ...          # srvllm, s06, Cleaner, частный из списка — 422 `via_cleaner`
def test_via_cleaner_links_only_chat_and_translate(studio): ...     # «Фото» — отказ check_link; accepts == ["chat", "translate"]
def test_toggle_keeps_the_token_and_resets_the_passport(studio): ...
def test_forced_routes_external_and_locks_the_toggle(studio): ...   # forced: routed True, cleaner_locked True, PATCH via_cleaner False — 422
def test_routed_connection_never_reaches_the_provider(studio):
    # прямой вызов upstream.request(conn, "GET", "https://api.cloud.test/v1/models") → UpstreamRefused(reason="cleaner")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route.py tests/test_connections.py`
Expected: FAIL

- [ ] **Step 3: Implement**
  - `is_external`: внутреннее — `s06`; `kind == "srvllm"`; `cleaner`; `kind == "openai"`, чей адрес есть в списке
    (`upstream.is_allowed`) и чей узел — IP-литерал, не `ipaddress.is_global`. Всё прочее, включая имена и `localhost`, — внешнее.
  - `routes`: `kind == "openai"` и не `cleaner`, не `s06`, `is_external` и (`via_cleaner` или `forced()`).
  - `_from_row` строит `Connection`, затем `routed`/`proxied` через `dataclasses.replace`.
  - `create`/`patch`: поле `via_cleaner`; недопустимое — 422 `via_cleaner`; смена флага обнуляет паспорт, ключ не требуется.
  - `sections.accepts`: `routed` → `list(sections.CLEANER)`.
  - `upstream._prepare`: `getattr(conn, "routed", False)` и адрес не адрес Cleaner из настроек → `UpstreamRefused(…, "cleaner")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route.py tests/test_connections.py tests/test_sections.py tests/test_checks.py`
Expected: PASS

- [ ] **Step 5: Commit** — `git commit -m "Route external connections through Cleaner by toggle or admin flag"`

---

### Task 3: Ход чата через Cleaner

**Files:**
- Modify: `app/cleaner_route.py` (запросы к шлюзу)
- Modify: `app/chat.py` (`StreamTask` 788-873, `_round` 1030-1105, `run` 1487, `_finish` 483, `create_turn` 2431-2626, `_chat_view` 330, `_message_out` 411)
- Modify: `tests/fakes.py` (`FakeCleanerGateway`)
- Test: `tests/test_cleaner_route_chat.py` (новый)

**Interfaces:**
- Consumes: Task 1, Task 2; контракт Cleaner.
- Produces (`app/cleaner_route.py`):
  - `REQUEST_HEADER = "x-cleaner-request"`
  - `async def context(conn: Connection, resource: str, item_id: str) -> str` — id чата Cleaner (`POST /api/v1/chats`
    служебным ключом; строка `cleaner_contexts` со снимком адреса Cleaner и служебного ключа; повторно — та же, пока
    снимок совпадает)
  - `def headers(conn: Connection, remote: str, hold: Iterable[str] = ()) -> dict[str, str]`
  - `@asynccontextmanager async def stream(conn: Connection, path: str, *, remote: str, hold: Iterable[str] = (), **kw)`
  - `async def request(conn: Connection, method: str, path: str, *, remote: str | None = None, hold: Iterable[str] = (), **kw) -> httpx.Response`
  - `def words(response: httpx.Response) -> str` — слова отказа Cleaner по коду и `error.code`
  - `StreamTask.cleaner_chat: str | None`; `messages.cleaner_request` = `"<id чата Cleaner>/<id запроса>"` последнего запроса хода
  - в `_message_out`: `cleaner_sent: bool`; в `_chat_view`: `routed: bool`

- [ ] **Step 1: Write the failing tests**

`FakeCleanerGateway` в `tests/fakes.py`: обработчик путей `/api/v1/chats` (POST/DELETE) и
`/agent/openai/v1/chat/completions` на адресе Cleaner; записывает запросы; по сценарию отдаёт SSE Chat Completions
(текст, `tool_calls`) и заголовок `x-cleaner-request`; любой запрос на узел провайдера — `AssertionError`.

```python
def test_turn_goes_to_cleaner_with_provider_key_and_context(cleaner_gw):
    s, gw = cleaner_gw                                    # Cleaner настроен, подключение https://api.cloud.test/v1 с via_cleaner
    chat_id = new_chat(s, connection_id=gw.conn_id, model="gpt-x")["id"]
    events = sse(turn(s, chat_id, "Привет"))
    sent = gw.turns[0]
    assert sent.url.path == "/agent/openai/v1/chat/completions"
    assert sent.headers["x-cleaner-key"] == gw.service_key and sent.headers["authorization"] == "Bearer cloud-key"
    assert sent.headers["x-cleaner-upstream"] == "https://api.cloud.test/v1" and sent.headers["x-cleaner-chat"] == gw.chats[0]
    assert messages(s, chat_id)[-1]["via"] == "server" and messages(s, chat_id)[-1]["cleaner_sent"]

def test_tools_web_and_knowledge_stay_on(cleaner_gw): ...           # тело содержит tools MCP и web_search; системная часть — выдержки знаний
def test_web_and_open_world_tools_are_held(cleaner_gw):
    # «Интернет» включён, MCP-инструмент с openWorldHint → x-cleaner-hold-tools == "web_search,read_page,youtube_subtitles,mcp_<id>_<n>"
def test_tool_round_reuses_the_context_and_stores_the_last_request(cleaner_gw): ...
def test_cleaner_down_refuses_without_touching_the_provider(cleaner_gw): ...      # 502 «Cleaner недоступен: данные не отправлены»
@pytest.mark.parametrize("status, code, words", [(400, "upstream", "публичные https"), (429, None, "Cleaner занят"),
                                                 (401, "client_login", "ключ"), (503, None, "очистк")])
def test_cleaner_refusals_in_words(cleaner_gw, status, code, words): ...
def test_new_image_attachment_is_refused(cleaner_gw): ...           # 409 `cleaner_images`
def test_history_images_are_dropped(cleaner_gw): ...               # в теле нет image_url, есть пометка chat.DROPPED
def test_forced_direct_connection_turn_is_server_side(cleaner_gw): ...   # ответ create_turn без `url`
def test_new_cleaner_address_makes_a_new_context(cleaner_gw): ...   # прежний id — в cleaner_deletions
def test_dialog_delete_queues_the_context(cleaner_gw): ...
def test_cleaner_turn_never_reaches_the_log(cleaner_gw, caplog): ...      # ни текста, ни api.cloud.test, ни ключей
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route_chat.py`
Expected: FAIL

- [ ] **Step 3: Implement**
  - Запросы к Cleaner — через синтетическое `Connection(cleaner=True, base_url=<адрес Cleaner>)` и
    `upstream.stream/request(…, keyless=True, headers=headers(...))`: так работают список адресов, `STUDIO_CLEANER_CA`
    (`state.CLEANER_HTTP`) и проверка имени. Ключ провайдера — `connections.token(conn)` снимка задания.
  - `create_turn`: для `conn.routed` — `remote = await cleaner_route.context(conn, "chat", chat_id)` до `StreamTask`;
    `free` учитывает `conn.routed` так же, как `conn.cleaner`; новое вложение-картинка — 409 `cleaner_images`;
    картинки истории не уходят (`chat.DROPPED`).
  - `StreamTask._round`: `conn.routed` → `cleaner_route.stream(self.conn, "/chat/completions", remote=self.cleaner_chat,
    hold=self._held(), json=payload, timeout=…)`; `_held()` — имена `web.NAMES` включённых видов + `self.mcp.open_world`.
    Заголовок `x-cleaner-request` каждого круга перезаписывает `messages.cleaner_request`.
  - Отказы Cleaner — `cleaner_route.words(response)`; разбор SSE — прежний.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route_chat.py tests/test_chat.py tests/test_chat_web.py tests/test_cleaner.py tests/test_chat_mcp.py`
Expected: PASS

- [ ] **Step 5: Commit** — `git commit -m "Send chat turns of routed connections through the Cleaner gateway"`

---

### Task 4: Перевод, модели, проверка и «Пробный ответ» через Cleaner

**Files:**
- Modify: `app/translate_text.py:316-345` (`detect`, `chunk`: аргумент `remote: str | None = None`)
- Modify: `app/translate.py:284-318, 350, 522` (`_attempt`, `_languages`, `_context`)
- Modify: `app/connections.py:474` (`fetch_models`), `app/checks.py:163, 238` (`check`, `probe`)
- Modify: `app/cleaner_route.py` (`async def models(conn: Connection) -> list[dict]`)
- Test: `tests/test_cleaner_route_tasks.py` (новый)

**Interfaces:**
- Consumes: Task 3 (`context`, `request`, `stream`, `words`).
- Produces: `cleaner_route.models(conn) -> list[dict]` (как `connections.fetch_models`); контексты `("translate", job.id)` и `("probe", str(conn.id))`.

- [ ] **Step 1: Write the failing tests**

```python
def test_translation_chunks_go_through_cleaner_in_one_context(cleaner_gw):
    job = start_translation(s, text="Иван Петров приехал. " * 50, connection_id=gw.conn_id)
    assert {r.headers["x-cleaner-chat"] for r in gw.turns} == {gw.chats[0]} and len(gw.turns) >= 2
    assert "tools" not in json.loads(gw.turns[0].content)

def test_detect_goes_through_cleaner(cleaner_gw): ...
def test_models_refresh_goes_through_cleaner(cleaner_gw): ...       # GET /agent/openai/v1/models с x-cleaner-upstream
def test_check_and_probe_go_through_cleaner(cleaner_gw): ...        # паспорт с маршрутом «через Cleaner»; пробный ответ — свой контекст
def test_translation_with_cleaner_down_stops_without_provider(cleaner_gw): ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route_tasks.py`
Expected: FAIL

- [ ] **Step 3: Implement** — каждый путь: `conn.routed` → `cleaner_route` с контекстом; прочее — как было.
      `_languages` и `_context` для `routed` ведут себя как для обычного подключения (не как прежний Cleaner).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route_tasks.py tests/test_translate.py tests/test_checks.py tests/test_connections.py`
Expected: PASS

- [ ] **Step 5: Commit** — `git commit -m "Translate, list models and probe routed connections through Cleaner"`

---

### Task 5: Принудительный режим — разделы, облачные роли srvllm, «Знания»

**Files:**
- Modify: `app/cleaner_route.py` (`def forced_refusal(conn: Connection, section: str, model: str) -> str | None`)
- Modify: `app/sections.py:150-206` (`resolve`, `check_link`), `app/chat.py` (`create_turn`)
- Modify: `app/knowledge.py:157-217, 232, 459-509` (`_source_conn`, `index`, `search`, `for_turn`), `app/chat_tools.py:205`
- Modify: `app/admin.py:461-467` (панель знаний: предупреждение о внешнем источнике в принудительном режиме)
- Test: `tests/test_cleaner_forced.py` (новый)

**Interfaces:**
- Consumes: Task 2 (`is_external`, `routes`, `forced`).
- Produces: отказ 409 `{"code": "cleaner_forced", "message": …}`; `knowledge.search(..., vectors: bool = True)`,
  `knowledge.for_turn(..., vectors: bool = True)`, `knowledge.index(doc_id, text, vectors: bool = True)`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("section", ["photo", "transcribe", "speech", "dictation"])
def test_forced_refuses_external_connections_outside_chat_and_translate(forced_studio, section):
    r = use_section(forced_studio, section, external_conn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cleaner_forced"

def test_forced_refuses_new_links_of_external_connections(forced_studio): ...
def test_forced_refuses_srvllm_cloud_roles_but_not_local(forced_studio): ...   # «gpt-x» — 409, «local-gemma-abl» — идёт
def test_forced_leaves_internal_connections_alone(forced_studio): ...         # s06, srvllm-карта, частный из списка
def test_shared_external_connection_is_routed_and_locked(forced_studio): ...
def test_external_knowledge_source_is_off_when_forced(forced_studio): ...     # нет /embeddings и /rerank, поиск — BM25
def test_routed_dialog_skips_an_external_knowledge_source(cleaner_gw): ...    # без forced: ход через Cleaner — без векторов
def test_big_attachment_of_a_routed_dialog_is_indexed_without_vectors(cleaner_gw): ...
def test_internal_knowledge_source_still_works_when_forced(forced_studio): ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_forced.py`
Expected: FAIL

- [ ] **Step 3: Implement**
  - `forced_refusal`: `forced()` и (внешнее и раздел не из `sections.CLEANER`) или (`kind == "srvllm"`, раздел из
    `sections.CLEANER`, не `sections.is_local`) → слова из спеки (6.3, «Принудительный режим»).
  - Вызовы — в `sections.resolve`, `sections.check_link`, `chat.create_turn`.
  - «Знания»: `_source_conn()` → None при `forced()` и внешнем источнике; `vectors=False` у хода и инструмента
    `knowledge_search` диалога с `conn.routed` при внешнем источнике; индексация больших вложений такого диалога — без векторов.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_forced.py tests/test_knowledge.py tests/test_sections.py`
Expected: PASS

- [ ] **Step 5: Commit** — `git commit -m "Enforce Cleaner for external models in every section when forced"`

---

### Task 6: Страница — переключатель, блок администратора, «Что получила модель»

**Files:**
- Modify: `app/chat.py` (маршрут `GET /api/chats/{chat_id}/messages/{message_id}/cleaner-sent`), `app/cleaner_route.py` (`async def sent(value: str) -> dict`)
- Modify: `app/static/js/settings.js:1528-1617` (`linkCard`: переключатель в меню «⋯», метка «через Cleaner», пояснение блокировки)
- Modify: `app/static/js/admin.js:711-731` (`cleanerBlock()` по образцу `chatSettingsBlock` 372)
- Modify: `app/static/js/chat.js:1734-1814` (`paint`: подпись и кнопка «Что получила модель», окно `<dialog>`), признак диалога «через Cleaner»
- Modify: `app/static/en.js`, `docs/glossary.md` (прежний вид — «Cleaner (своя учётка Cleaner)», новый — «Через Cleaner»)
- Test: `tests/test_cleaner_route_chat.py` (маршрут), `tests/test_ui_cleaner_route.py` (новый, `integration`)

**Interfaces:**
- Consumes: Task 3 (`messages.cleaner_request`, `cleaner_sent`), контракт Cleaner («Что ушло»).
- Produces: `GET /api/chats/{chat_id}/messages/{message_id}/cleaner-sent` → `{"messages": [...], "tools": N, "counts": {...}}`;
  журнал удалён — 410 `{"code": "journal_gone"}`; чужой диалог — 404.

- [ ] **Step 1: Write the failing tests**

```python
def test_sent_shows_the_cleaned_request_to_the_owner(cleaner_gw):
    body = s.c.get(f"/api/chats/{chat_id}/messages/{answer_id}/cleaner-sent").json()
    assert body["messages"][-1]["content"] == "Позвони <PERSON_1>"
def test_sent_of_another_users_dialog_is_404(cleaner_gw): ...
def test_sent_after_journal_gone_is_410(cleaner_gw): ...
def test_sent_is_not_in_share_or_export(cleaner_gw): ...
```

`tests/test_ui_cleaner_route.py` (`integration`, 320 и 1280 px): переключатель в меню подключения включается и
выключается; в принудительном режиме заблокирован с пояснением; блок администратора сохраняет адрес и флажок,
ключ не показывает; под ответом через Cleaner — подпись «через Cleaner» и кнопка, окно показывает запрос с заменами.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route_chat.py -k sent`
Expected: FAIL

- [ ] **Step 3: Implement** — маршрут проверяет владельца диалога и `cleaner_request`, зовёт `cleaner_route.sent`;
      страница — по существующим образцам (`chatSettingsBlock`, меню `linkCard`, `<dialog>` через `showModal()`).

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner_route_chat.py tests/test_i18n.py tests/test_i18n_server.py tests/test_css_tokens.py`
then `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_cleaner_route.py`
Expected: PASS (браузеры, которых нет на машине, — пропуск)

- [ ] **Step 5: Commit** — `git commit -m "Show the Cleaner toggle, admin block and what the model received"`

---

### Task 7: Документация, версия, сквозная проверка с настоящим Cleaner

**Files:**
- Modify: `docs/cleaner-integration.md` (новый раздел «Через Cleaner: свой провайдер» до прежнего вида; «Границы»: «Интернет» — с заменами у веб-инструментов)
- Modify: `docs/architecture/chat.md:589-608`, `docs/architecture/connections.md:55-60`, `docs/guide.ru.md`, `README.md`/`README.ru.md` (строка о Cleaner)
- Modify: `CLAUDE.md` (инвариант списка адресов: подключения через Cleaner и частные адреса списка; «Ловушки»: `routed` ⇒ `proxied`, запросы — только через `cleaner_route`)
- Modify: `app/release.py` (`VERSION = "0.45.0"`)
- Modify: `tests/test_cleaner_contract.py` (новый `integration`-тест с настоящим Cleaner и ключом `client`)

- [ ] **Step 1:** Документы; значения констант в тексте — только с модулем (`tests/test_docs.py`).
- [ ] **Step 2:** Сквозной тест: настоящий Cleaner на петле (`tests/studio_server.py` с ключом `client`), заглушка
      провайдера; ход с MCP-инструментом — модель видит замены, инструмент получает оригинал, результат уходит
      провайдеру очищенным; поиск — с заменами; «Что получила модель» — запрос с заменами; удаление диалога удаляет контекст.
      Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_cleaner_contract.py` → PASS
- [ ] **Step 3: Fast suite** — `.venv/Scripts/python -m pytest -q` и набор моста по `CLAUDE.md` → PASS ≤ 60 с.
- [ ] **Step 4: Commit** — `git commit -m "Release Studio 0.45.0: external models through the Cleaner gateway"`

---

## После плана (с владельцем)

Выкладка не входит в задачи плана. Порядок: Cleaner на s06-llm (выпустить ключ `client` администратором Cleaner,
сеть Studio s06 — в `CLIENTS` профиля хоста), затем Studio по `CLAUDE.md` «Хост и выкладка» на оба экземпляра
(в «Администрировании» второго экземпляра Cleaner не настраивается, пока нет адреса и ключа). Затем живая приёмка спеки
(раздел 10, п. 3) на s06.
