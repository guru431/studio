# Раздел «Open WebUI» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Раздел Студии, в котором сервер Студии от имени владельца работает с учётками Open WebUI 0.13: дерево
чатов, чтение, продолжение и создание чатов с выбором модели, живой поток, пауза ответа и push.

**Architecture:** Сервер Студии — посредник. `owui_tokens` хранит постоянную сессию Open WebUI (долгий токен
шифром, рабочий — в памяти), `owui` — маршруты `/api/owui/*` поверх REST Open WebUI через `upstream`, `owui_view` —
чистые функции, приводящие ответы и события Open WebUI к виду Студии, `owui_link` — свой клиент socket.io
(Engine.IO 4 поверх `websockets`) на учётку и пересылка событий на страницу по SSE. Страница — без сборки,
по образцу «Агентов».

**Tech Stack:** Python 3.13, FastAPI, httpx, websockets 17.4 (уже в `constraints.txt`), SQLite; ES-модули без сборки;
pytest (+ xdist), браузерные тесты через CDP (`integration`).

**Spec:** `docs/superpowers/specs/2026-11-22-studio-open-webui-design.md` — читать целиком перед первой задачей.

## Global Constraints

- Open WebUI не ниже 0.13 с `features.auth_sessions`; проверено на 0.13.4 и 0.13.3.
- В Open WebUI ходит только сервер Студии; токены в браузер не уходят; пароль не сохраняется нигде.
- Все запросы наружу (REST и WebSocket) — через `upstream`: список разрешённых, имя — только https и только публичные
  адреса, внутренние — IP-записью, без переадресаций.
- Переписка Open WebUI не пишется ни в базу, ни в журнал Студии. Журнал — только id учётки Студии и учётки Open WebUI,
  коды HTTP, коды отказов, числа; ни адреса сервера, ни email, ни текстов, ни имён файлов, ни id чатов.
- Чужой или несуществующий id учётки — одинаковый 404 `not_found`.
- Студия сама никогда не шлёт `tool_approval_mode: "full"`.
- Схема — только добавлять шаги (`db.STEPS`); шаг 63 или следующий свободный, если 63 занят к моменту реализации.
- Без реального времени в тестах: `clock.NOW`, `clock.SLEEP`; тест дольше 1 с — `integration`.
- В отслеживаемые файлы — только примеры адресов (`10.77.1.x`, `studio.example.com`, `owui.example.com`); реальные
  адреса и данные тестового пользователя — только в локальном `.env`.
- Подписи страницы — через `tr` и `en.js`; названия чатов и тексты пользователя не переводятся.
- Значения из спеки: `owui.LABEL_MAX` = 80, `owui.LOGIN_FAILS` = 5, `owui.LOGIN_WINDOW_SEC` = 900,
  `owui_tokens.REFRESH_AHEAD_SEC` = 120, `owui_link.IDLE_CLOSE_SEC` = 60, `owui_link.CONNECT_WAIT_SEC` = 10,
  имя сессии в Open WebUI — `"Studio"`, push: заголовок `"Open WebUI"`, слова паузы
  `{"ru": "нужно ваше решение", "en": "your decision is needed"}`.

## Review Focus

1. **Open WebUI недоступен или молчит** — раздел отвечает 503 `owui_offline` за ограниченное время, остальная Студия
   работает. Тест: `test_offline_server_answers_503_quickly_and_studio_still_works` (задача 4).
2. **Обновлённый Open WebUI отдал неожиданный JSON** (нет ключа, список вместо словаря, ветка без родителя или с
   циклом) — 502 `owui_shape` со словами про версию, без 500 и без зависания на обходе дерева. Тесты:
   `test_branch_survives_missing_parent_and_cycle`, `test_unexpected_shape_is_502_with_version` (задача 4).
3. **Сессию отозвали в Open WebUI, пока раздел открыт** — `state = relogin`, SSE шлёт `link {state: relogin}`,
   отправка — 409 `relogin`, соединение закрыто. Тест: `test_revoked_session_turns_link_into_relogin` (задача 5),
   `test_send_after_revoke_is_409_relogin` (задача 6).
4. **Одна учётка с двух устройств сразу** — одно обновление токена, а не два (иначе второе сожжёт ротацию).
   Тест: `test_parallel_calls_refresh_once` (задача 2).
5. **Сообщение в чат, где ещё идёт ответ** (из Open WebUI или с другого устройства) — Open WebUI ставит его в
   очередь, страница видит `queued`, сообщение не теряется. Тест: `test_send_while_answer_runs_reports_queued`
   (задача 6).

---

## Карта файлов

| Файл | Ответственность |
|---|---|
| `app/upstream.py` (правка) | явный `Authorization`, клиент доверенного сертификата, цель WebSocket, получение сертификата |
| `app/state.py` (правка) | `pinned_http(pem)` — кэш клиентов httpx по доверенному сертификату, закрытие на остановке |
| `app/db.py` (правка) | `OWUI_63`, `VERSION`, `STEPS` |
| `app/owui_tokens.py` | секреты учётки, рабочий токен, обновление с ротацией и восстановлением, `relogin` |
| `app/owui_view.py` | чистые функции: ветка чата, элементы дерева, нормализация событий |
| `app/owui.py` | `Account`, `call()` к Open WebUI, маршруты `/api/owui/*`, предел неудачных входов, `drop_user` |
| `app/owui_link.py` | клиент socket.io на учётку, наблюдатели SSE, идущие ответы, push по концу и паузе |
| `app/push.py` (правка) | `OWUI_TITLE`, `DECISION_NOTICE` |
| `app/main.py`, `app/account.py`, `app/admin.py`, `app/auth.py`, `app/lock_policy.py`, `app/sections.py` (правки) | подключение роутера и остановки, удаление/отключение учётки Студии, экраны, блокировка, имя раздела |
| `app/static/js/owui.js` | роутер раздела и раскладка |
| `app/static/js/owui-panel.js` | учётки, форма подключения, дерево |
| `app/static/js/owui-chat.js` | лента, поток, поле ввода, модель, вложения, «Остановить» |
| `app/static/js/owui-pending.js` | карточка паузы |
| `app/static/js/{sections-meta,icons,app,lock}.js`, `app/static/en.js`, `app/static/css/app.css`, `login.html`, `manifest*.webmanifest` (правки) | регистрация раздела, значок, подписи, стили |
| `tests/fakes.py` (правка), `tests/fake_owui.py` | поддельный Open WebUI: REST и сокет в памяти |
| `deploy/apache-vhost.conf` (правка) | `ProxyPass /api/owui/` |
| `docs/architecture/open-webui.md` и правки `CLAUDE.md`, `docs/glossary.md`, `docs/guide.ru.md`, `README*.md` | документация |

---

### Task 1: `upstream` для учёток Open WebUI

**Files:**
- Modify: `app/upstream.py` (`_prepare`, `request`, `stream`; новые `pinned_context`, `fetch_certificate`, `ws_target`, `WsTarget`)
- Modify: `app/state.py` (`pinned_http`, закрытие кэша там же, где закрываются `HTTP` и `CLEANER_HTTP`)
- Test: `tests/test_owui_upstream.py`

**Interfaces:**
- Produces:
  - `upstream.request(conn, method, url, *, keyless=False, auth: str | None = None, **kw) -> httpx.Response` и так же
    `upstream.stream(...)`: `auth` — готовое значение `Authorization`; при нём `connections.token` не зовётся.
    Клиент: `state.pinned_http(conn.cert_pem)`, если у `conn` есть непустой атрибут `cert_pem`; иначе прежний выбор.
  - `upstream.pinned_context(pem: str) -> ssl.SSLContext` — якорь доверия ровно этот сертификат, без проверки имени,
    с частичной цепочкой (ослабление `VERIFY_X509_STRICT` — по результату теста с самоподписанным сертификатом).
  - `upstream.fetch_certificate(conn, url: str) -> bytes` — DER сертификата сервера рукопожатием без проверки, после
    тех же проверок адреса (`UpstreamRefused` до всякого ввода-вывода).
  - `@dataclass(frozen=True) class WsTarget: host: str; port: int; path: str; ssl: ssl.SSLContext | None; server_hostname: str | None; host_header: str`
  - `async upstream.ws_target(conn, url: str) -> WsTarget` — для `ws://`/`wss://` из `http://`/`https://` адреса:
    те же правила списка и имени, что у `_prepare`; для имени — проверенный публичный IP в `host`, имя в
    `server_hostname` и `host_header`.

- [ ] **Step 1: Write the failing tests** — в `tests/test_owui_upstream.py`, с `allow(...)` как в `tests/test_checks.py`
  и подменой `upstream.RESOLVE`:
  - `test_explicit_auth_is_sent_and_connection_token_is_not_read` — `conn` без строки в `connections`; запрос уходит
    с `Authorization: Bearer s-token`; `connections.token` подменён на падающий и не вызван.
  - `test_pinned_certificate_selects_its_own_client` — `conn.cert_pem` задан → запрос идёт через
    `state.pinned_http(pem)` (тот же объект при повторе с тем же PEM).
  - `test_ws_target_refuses_unlisted_and_private_names` — адрес вне списка → `UpstreamRefused(reason="unlisted")`;
    имя из списка, разрешающееся в `10.0.0.5` → `reason="internal"`; `http://owui.example.com:443` (имя без https) —
    `unlisted`.
  - `test_ws_target_pins_public_name` — `https://owui.example.com/` при `RESOLVE → ["93.184.216.34"]` →
    `WsTarget(host="93.184.216.34", port=443, path="/ws/socket.io/?EIO=4&transport=websocket", server_hostname="owui.example.com", host_header="owui.example.com")`; для IP-записи `http://10.77.1.20:8080` —
    `ssl is None`, `server_hostname is None`.
  - `test_pinned_context_accepts_only_its_certificate` — локальный TLS-сервер на `127.0.0.1` с самоподписанным
    сертификатом (генерируется `cryptography` в тесте); рукопожатие с `pinned_context(его PEM)` проходит, с
    `pinned_context(другой PEM)` — `ssl.SSLCertVerificationError`; `fetch_certificate` возвращает его DER.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_owui_upstream.py` — Expected: FAIL (нет `auth`, `ws_target`…).

- [ ] **Step 3: Implement** интерфейсы выше. `_prepare(conn, url, kw, keyless, auth)`: `auth` → `headers["Authorization"] = auth`.
  `state.pinned_http(pem)`: `httpx.AsyncClient` из `state._http(upstream.pinned_context(pem))`, кэш по sha256 PEM
  (не больше 32 клиентов, лишний закрывается).

- [ ] **Step 4: Run** тот же файл и `tests/test_checks.py tests/test_connections.py tests/test_cleaner.py` — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "upstream: explicit auth, pinned certificates and WebSocket targets"`.

---

### Task 2: учётка в базе и токены

**Files:**
- Modify: `app/db.py` (`OWUI_63`, `SCHEMA += OWUI_63`, `VERSION = 63`, `STEPS[63] = OWUI_63`)
- Create: `app/owui_tokens.py`
- Create: `tests/fake_owui.py`; Modify: `tests/fakes.py` (`FakeGateway.hosts`)
- Test: `tests/test_owui_tokens.py`

**Interfaces:**
- Consumes: `upstream.request(..., auth=...)` (Task 1), `security.encrypt/decrypt`.
- Produces:
  - Таблица `owui_accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, label TEXT NOT NULL, base_url TEXT NOT NULL, who_enc BLOB NOT NULL, secret_enc BLOB NOT NULL, cert_pem TEXT, state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok','relogin')), version TEXT, created REAL NOT NULL, used REAL)` и индекс по `user_id`.
  - `owui_tokens.Secret` (dataclass): `session_id: str`, `refresh_token: str`, `pending_request_id: str | None`.
  - `owui_tokens.save(aid: int, secret: Secret) -> None`, `owui_tokens.load(aid: int) -> Secret`.
  - `async owui_tokens.access(acc) -> str` — рабочий токен из памяти; до конца меньше `REFRESH_AHEAD_SEC` — обновить.
  - `async owui_tokens.renew(acc, *, force: bool = False) -> str` — под `asyncio.Lock` учётки: записать
    `pending_request_id` (uuid4) → `POST /api/v1/auths/sessions/refresh {refresh_token, request_id}` → сохранить новую
    пару, очистить `pending_request_id`, `used = clock.NOW()`. Если `pending_request_id` уже есть — слать его же.
    401 → `state = 'relogin'`, забыть токен, `raise owui_tokens.Relogin`.
  - `owui_tokens.forget(aid: int) -> None` — убрать рабочий токен из памяти.
  - `owui_tokens.TOKENS: dict[int, tuple[str, float]]`, `owui_tokens.LOCKS: dict[int, asyncio.Lock]` (чистит фикстура `studio`).
  - `tests/fake_owui.py::FakeOwui` — `URL = "http://10.77.1.20:8080"`, `ENTRY = "10.77.1.20:8080"`, `EMAIL`, `PASSWORD`,
    `version = "0.13.4"`, `down = False` (сетевой отказ на любой запрос), `async handler(request) -> httpx.Response`
    (REST из таблицы протокола спеки), счётчики `refreshes`, `revoked`, `requests`; методы
    `issue_session() -> owui_tokens.Secret` (сессия без входа — для тестов), `revoke_all()`, `add_chat(...)`,
    `add_folder(...)`; сокет — в Task 5.
  - `FakeGateway.hosts: dict[str, handler]` — по `"host:port"` из `request.url`; проверяется до `reply` и `on`.

- [ ] **Step 1: Write the failing tests** (`tests/test_owui_tokens.py`; учётка вставляется прямо в базу с секретом
  от `FakeOwui.issue_session()`):
  - `test_schema_has_owui_accounts_and_step_63` — таблица есть в новой базе и после `db.STEPS[63]` на базе 62.
  - `test_access_refreshes_ahead_of_expiry` — токен на 900 с; часы +781 с → `access` даёт новый, `refreshes == 1`;
    +10 с от старта → тот же, без обновления.
  - `test_rotation_recovers_with_the_same_request_id` — Fake отвечает на refresh, но Студия «падает» до записи
    (исключение после ответа) → `secret.pending_request_id` записан; следующий `renew` шлёт тот же `request_id` с
    прежним `refresh_token` и получает выдачу (Fake возвращает последнюю выдачу по `request_id`).
  - `test_refused_refresh_turns_account_into_relogin` — Fake отвечает 401 → `Relogin`, в базе `state == 'relogin'`,
    `TOKENS` без учётки.
  - `test_parallel_calls_refresh_once` — `asyncio.gather` пяти `access` при истёкшем токене → `refreshes == 1`.
  - `test_secret_is_never_plain_in_the_database` — в `secret_enc` и `who_enc` нет строки refresh-токена и email.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_owui_tokens.py` — Expected: FAIL.

- [ ] **Step 3: Implement** схему, `owui_tokens` и `FakeOwui` (REST: `/api/config`, `/api/v1/auths/signin`,
  `/api/v1/auths/sessions`, `/refresh`, `DELETE /api/v1/auths/sessions/{id}`; остальное — в следующих задачах).
  `Account` для этих тестов — минимальная заготовка в `owui.py` (поля `id, user_id, label, base_url, cert_pem, state`,
  свойство `name = label`), полностью — в Task 3. Добавить очистку `TOKENS`/`LOCKS` в фикстуру `studio`.

- [ ] **Step 4: Run** тот же файл и `tests/test_db*.py` — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Open WebUI accounts: schema step 63 and session tokens"`.

---

### Task 3: подключение, переподключение, удаление учёток

**Files:**
- Modify/Create: `app/owui.py` (`Account`, `load`, `call`, маршруты учёток, `FAILS`, `drop_user`)
- Modify: `app/main.py` (роутер), `app/account.py::delete_user_data` (`await owui.drop_user(uid)` после `bridge.drop_user`),
  `app/admin.py` (рядом с `bridge.drop_user(uid, disabled=True)` — `await owui.drop_user(uid, revoke=False)`),
  `app/lock_policy.py` (`"owui"` в `SECTIONS`, `"owui": "owui"` в карте `blocks`), `app/sections.py::NAMES`
  (`"owui": "Open WebUI"`)
- Test: `tests/test_owui_accounts.py`, `tests/test_lock_policy.py` (`SECTION_HEADS["owui"] = "owui"` — иначе
  `test_every_api_route_is_classified_for_partial_protection` падает на новых маршрутах)

**Interfaces:**
- Consumes: Task 1, Task 2.
- Produces:
  - `owui.Account` (frozen dataclass): `id, user_id, label, base_url, cert_pem, state, version`; `name` → `label`.
  - `owui.load(uid: int, aid: int) -> Account` — 404 `not_found` для чужой и несуществующей.
  - `async owui.call(acc, method: str, path: str, *, timeout: float = owui.TIMEOUT_SEC, **kw) -> httpx.Response` — токен
    `owui_tokens.access`; 401 → `renew(force=True)` и один повтор; `Relogin` → 409 `relogin`; `UpstreamRefused` → 422
    `unlisted` со словами `upstream.refused_words`; сетевая ошибка или таймаут → 503 `owui_offline`. `TIMEOUT_SEC = 15`.
  - `owui.shape_error(acc) -> HTTPException` — 502 `owui_shape` «Open WebUI <версия> ответил не так, как ожидает Студия».
  - `owui.FAILS: dict[int, list[float]]` — неудачи входа по uid Студии (чистит фикстура `studio`).
  - `async owui.drop_user(uid: int, *, revoke: bool = True) -> None` — закрыть соединения (`owui_link.drop_account`,
    после Task 5), при `revoke` — отозвать сессии (срок 5 с на учётку, неудача не мешает).
  - Маршруты (все через `auth.guard`): `GET/POST /api/owui/accounts`, `PATCH/DELETE /api/owui/accounts/{aid}`,
    `POST /api/owui/accounts/{aid}/login`, `POST /api/owui/accounts/{aid}/certificate`. Тела и коды — спека,
    «Подключение», «Переподключение», «Удаление». Выдача учётки:
    `{id, label, url, who: {name, email}, state, version, cert: "<sha256 AA:BB:…>" | null}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_owui_accounts.py`; `allow(FakeOwui.ENTRY)`, `s.gw.hosts[...] = fake.handler`, `signin`):
  - `test_connect_creates_account_without_storing_password` — 201/200, выдача без токенов; в базе нет пароля ни в одном
    столбце; Fake видит одну сессию с именем `"Studio"`.
  - `test_connect_refuses_unlisted_address` — 422 `unlisted`, к Fake ни одного запроса.
  - `test_connect_maps_login_errors` — неверный пароль → 422 `owui_login`; роль `pending` → 422 `pending`; ответ входа с
    требованием MFA → 422 `mfa`; версия `0.12.9` или нет `auth_sessions` → 422 `unsupported`.
  - `test_login_failures_are_limited_per_studio_user` — 5 неверных паролей → шестой 429 `owui_locked` без запроса к
    Fake; через 900 с по часам — снова можно.
  - `test_self_signed_certificate_needs_confirmed_fingerprint` — https-адрес с несверяемым сертификатом
    (подмена `upstream.fetch_certificate` и ошибки TLS) → 409 `certificate` с `sha256`; повтор с этим `cert_sha256` —
    создано, `cert_pem` сохранён; с чужим отпечатком — снова 409.
  - `test_duplicate_account_is_409_exists` и `test_relogin_with_other_user_is_409_other_user`.
  - `test_foreign_account_is_404` — вторая учётка Студии получает 404 на `PATCH`, `DELETE`, `login`.
  - `test_delete_revokes_session_and_removes_row` — Fake `revoked == 1`; при недоступном Fake строка всё равно удалена.
  - `test_studio_account_deletion_revokes_owui_sessions` — `DELETE /api/account` → сессии отозваны, строк нет.
  - `test_accounts_log_has_no_address_email_or_secrets` (`caplog`) — в журнале нет `10.77.1.20`, email, пароля, токенов.
  - `test_locked_section_is_423` — учётка с областью блокировки `["owui"]` и истёкшей отметкой: `GET /api/owui/accounts`
    → 423 `screen_locked`, `GET /api/chats` — не 423.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_owui_accounts.py tests/test_lock_policy.py` — Expected: FAIL.

- [ ] **Step 3: Implement** по спеке. Тело входа — Pydantic-модель с ограничениями длины (`label ≤ LABEL_MAX`, `url ≤ 2048`,
  `email ≤ 320`, `password ≤ screenlock.PASSWORD_MAX`); `base_url` нормализуется (без `/` в конце, без
  userinfo/query/fragment). Ответ Open WebUI на вход с MFA — как его отдаёт 0.13 (см. `routers/auths.py`, `mfa.py`).

- [ ] **Step 4: Run** тот же файл и `tests/test_account.py tests/test_admin*.py` — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Open WebUI accounts: connect, re-login, certificate pinning, delete"`.

---

### Task 4: дерево, модели, чат, файлы (чтение)

**Files:**
- Create: `app/owui_view.py`
- Modify: `app/owui.py` (маршруты чтения)
- Test: `tests/test_owui_view.py` (чистые функции), `tests/test_owui_read.py` (маршруты)

**Interfaces:**
- Consumes: `owui.call`, `owui.load`, `owui.shape_error` (Task 3).
- Produces:
  - `owui_view.chat_item(raw: dict) -> dict` → `{id, title, updated, folder_id, active, pending}`.
  - `owui_view.branch(raw_chat: dict) -> dict` → `{id, title, models, folder_id, messages: [Message]}`; `Message` и
    `Pending` — как в спеке «Вид чата для страницы»: `{id, role, model, timestamp, text, reasoning: {text, seconds} | null, tools: [{call_id, name, status, result}], files: [{id, name, kind}], other: [type], done, error, pending}`.
    Обход от `currentId` по `parentId`, не больше числа сообщений шагов (цикл и пропавший родитель — конец ветки).
    `owui_view.RESULT_MAX = 2000`, `owui_view.ARGS_MAX = 2000` — обрезка текста результатов и аргументов.
  - `owui_view.Shape(Exception)` — неожиданная форма; маршрут превращает в `owui.shape_error`.
  - Маршруты: `GET …/tree` → `{pinned, folders: [{id, name, parent_id}], chats}`; `GET …/chats?page=N` → `{chats}`;
    `GET …/folders/{fid}/chats` → `{chats}`; `POST …/search {text}` → `{chats}`; `GET …/models` →
    `{models: [{id, name, vision, file_upload}], default}` без `agent:*`, `default` — модель последнего чата, иначе
    первая; `GET …/chats/{chat}` → `branch(...)` + `active`; `GET …/files/{fid}` — поток `/api/v1/files/{fid}/content`
    с её `Content-Type`, `Cache-Control: no-store`.

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_owui_view.py`: `test_branch_follows_current_id_not_siblings` (две ветки, отдаётся только текущая),
    `test_reasoning_and_text_split_from_output` (элементы `reasoning` и `message` как в пробе: `reasoning.text` склеен,
    `seconds` из `duration`/`ended_at − started_at`), `test_tools_and_results_are_paired_and_truncated`
    (`function_call` + `function_call_output`, результат длиннее 2000 обрезан), `test_pending_question_and_approval`
    (`ask_user` в статусе `pending` → `kind == "question"` с вопросами и вариантами; другой `function_call` в
    `requires_approval` → `kind == "approval"`), `test_unknown_output_item_goes_to_other`,
    `test_branch_survives_missing_parent_and_cycle`, `test_old_messages_without_output_use_content`.
  - `tests/test_owui_read.py`: `test_tree_lists_pinned_folders_and_root_chats`, `test_models_hide_agent_models_and_pick_last_chat_model`,
    `test_search_query_travels_in_body_only` (у запроса к Студии нет query string; к Fake уходит `?text=`),
    `test_chat_view_is_normalized`, `test_file_proxy_is_no_store`, `test_unexpected_shape_is_502_with_version`
    (Fake отдаёт список вместо объекта чата), `test_offline_server_answers_503_quickly_and_studio_still_works`
    (`FakeOwui.down = True` → 503 `owui_offline`; `GET /api/me` — 200).

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_owui_view.py tests/test_owui_read.py` — Expected: FAIL.

- [ ] **Step 3: Implement** `owui_view` и маршруты; Fake дополняется эндпоинтами дерева, моделей, чата, поиска, файлов
  (формы ответов — из таблицы протокола спеки и пробы).

- [ ] **Step 4: Run** те же файлы — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Open WebUI section: tree, models, chat view and file proxy"`.

---

### Task 5: соединение socket.io и поток

**Files:**
- Create: `app/owui_link.py`
- Modify: `app/owui_view.py` (`normalize`), `app/main.py` (`await owui_link.stop()` в остановке, до `push.stop()`),
  `app/owui.py` (`drop_user` зовёт `owui_link.drop_account`), `tests/fake_owui.py` (сокет в памяти)
- Test: `tests/test_owui_link.py`

**Interfaces:**
- Consumes: `upstream.ws_target` (Task 1), `owui_tokens.access/renew/Relogin` (Task 2), `owui.Account`.
- Produces:
  - `owui_view.normalize(payload: dict) -> list[dict]` — событие `events` Open WebUI → события Студии из спеки:
    `list`, `title`, `active`, `delta {kind: text|reasoning}`, `item`, `done`, `pending`, `queued`; неизвестное → `[]`.
  - `owui_link.Transport` (Protocol): `async send(text: str)`, `async recv() -> str`, `async close()`.
  - `owui_link.CONNECT: Callable[[upstream.WsTarget, dict[str, str]], Awaitable[Transport]]` — по умолчанию
    `websockets.connect` к `target.host:port` с `ssl`, `server_hostname`, заголовком `Host`; тесты подменяют.
  - `async owui_link.watch(acc) -> asyncio.Queue` / `owui_link.unwatch(aid: int, q: asyncio.Queue) -> None` — наблюдатель SSE.
  - `async owui_link.ensure(acc) -> str` — поднять соединение и вернуть `sid` (ждать не дольше `CONNECT_WAIT_SEC`,
    иначе 503 `owui_offline`).
  - `owui_link.track(acc, chat_id: str, message_id: str) -> None` — ответ из Студии идёт: держать соединение до `done`,
    ошибки или `pending`.
  - `async owui_link.drop_account(aid: int) -> None`, `async owui_link.stop() -> None`; `owui_link.LINKS: dict[int, Link]`
    (закрывает и чистит фикстура `studio`).
  - При каждом подключении сокета — `GET /api/config`: версия пишется в `owui_accounts.version`; ниже 0.13 или нет
    `features.auth_sessions` — соединение не поднимается, `ensure` отвечает 422 `unsupported`, наблюдателям — `link {state: down}`.
  - Протокол: `0{…}` → `40{"token": …}` → `40{"sid": …}`; `2` → `3`; `42["events", {...}]` → `normalize` → во все очереди
    наблюдателей учётки; `42<n>[…]` (обратный вызов) → немедленно `43<n>[{"error": "client does not support this"}]`;
    после каждого `renew` — `42["user-join", {"auth": {"token": …}}]` (не реже, чем за `REFRESH_AHEAD_SEC` до конца
    токена); обрыв → переподключение с паузой `clock.SLEEP` 1, 2, 4 … 30 с, наблюдателям `link {state: down|up}`;
    `Relogin` → `link {state: relogin}` и закрыть; нет причин дольше `IDLE_CLOSE_SEC` → закрыть.

- [ ] **Step 1: Write the failing tests** (`tests/test_owui_link.py`; `owui_link.CONNECT` → `fake.socket_connect`):
  - `test_normalize_maps_probe_events` — события из пробы (спека, «Протокол»): `response.output_text.delta` →
    `delta kind=text`, `response.reasoning_text.delta` → `delta kind=reasoning`, `chat:completion done` → `done`,
    `chat:active pending_approval` → `pending`, `chat:title` → `title`, `chat:list` → `list`; неизвестное → `[]`.
  - `test_watch_opens_link_and_relays_events` — наблюдатель получает `link up`, затем события Fake.
  - `test_callbacks_are_refused_at_once` — Fake шлёт `421["execute:python", {...}]` → сразу получает `431[{"error": ...}]`.
  - `test_token_refresh_rejoins_socket` — часы вперёд → `renew` → Fake получает `user-join` с новым токеном.
  - `test_reconnects_after_drop_and_tells_watchers` — Fake рвёт соединение → `link down`, затем `link up`.
  - `test_revoked_session_turns_link_into_relogin` — Fake отзывает сессию и рвёт сокет → refresh 401 → `link relogin`,
    соединение закрыто, `state == 'relogin'`.
  - `test_link_closes_after_idle` — последний наблюдатель ушёл, нет `track` → через 60 с по часам `LINKS` пуст.
  - `test_pause_releases_tracked_turn` — `track` + событие `pending` → причина снята, соединение закрывается по простою.
  - `test_link_records_version_and_refuses_old_server` — версия Fake пишется в базу; Fake `0.12.9` → `ensure` — 422 `unsupported`.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_owui_link.py` — Expected: FAIL.

- [ ] **Step 3: Implement** `owui_link` и сокет Fake (сервер Engine.IO в памяти: очереди кадров, методы `emit_event`,
  `call`, `drop`, `received`).

- [ ] **Step 4: Run** тот же файл — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Open WebUI section: socket.io link and event relay"`.

---

### Task 6: отправка, стоп, пауза, вложения, SSE и push

**Files:**
- Modify: `app/owui.py` (маршруты отправки и SSE), `app/owui_link.py` (push по концу хода), `app/push.py`
  (`OWUI_TITLE = "Open WebUI"`, `DECISION_NOTICE`), `app/static/en.js` («нужно ваше решение»)
- Test: `tests/test_owui_send.py`; Modify: `tests/test_sections_meta.py::test_english_notification_words_match_the_interface_catalog`
  (добавить `push.DECISION_NOTICE`)

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces:
  - `POST …/chats {model, text, files}` → `{chat_id, message_id}`; `POST …/chats/{chat}/messages {model, text, files, parent_id}` →
    `{message_id, queued}`; `POST …/chats/{chat}/stop` → 204; `POST …/chats/{chat}/resolve {message_id, call_id, action, answers}` →
    204 (уже решено — 409 `resolved`); `POST …/files` (сырое тело до `owui.FILE_MAX = 20 * 1024 * 1024`, имя —
    `X-Studio-Name`, как в `documents`) → `{id, name, kind}` после `process/status` `completed`;
    `GET …/events` — SSE (`claude_code.SSE_HEADERS`) из `owui_link.watch`, пинг-комментарий раз в 20 с.
  - `owui.TEXT_MAX = chat.TEXT_MAX`.
  - Тело `/api/chat/completions` (спека, «Отправка»): `model`, `stream: true`, `messages: [{role: "user", content}]`,
    `parent_id` (`null` для нового), `chat_id` (для продолжения), `user_message {id, role, content, parentId, childrenIds, timestamp, models, files}`,
    `id` (ответ), `session_id = await owui_link.ensure(acc)`, `background_tasks: {title_generation: true}` (новый),
    `tool_ids` = `info.meta.toolIds` модели, `features` — как у страницы Open WebUI 0.13, без `code_interpreter`, если
    `GET /api/config` (с токеном) даёт `code.interpreter_engine` пустой или `"pyodide"`;
    `params.tool_approval_mode` — настройка пользователя Open WebUI, только если она `"ask"` или `"auto"`.
  - `owui_link` по концу отслеживаемого хода: `push.notify_later(uid, push.CHAT_NOTICES[0 if ok else 1], f"owui/{aid}/{chat}", message_id, title=push.OWUI_TITLE, quiet=True)`;
    по паузе — то же с `push.DECISION_NOTICE`.

- [ ] **Step 1: Write the failing tests** (`tests/test_owui_send.py`):
  - `test_new_chat_sends_server_side_turn` — тело к Fake: `parent_id is None`, нет `chat_id`, `session_id` = sid сокета,
    `title_generation`, `user_message.id`/`id` — uuid; ответ `{chat_id, message_id}`.
  - `test_continue_sends_only_the_new_message` — `messages` из одного сообщения, `parent_id` = переданный.
  - `test_approval_mode_full_is_never_sent` — настройка пользователя `full` → поля нет; `ask` → `"ask"`.
  - `test_code_interpreter_dropped_for_pyodide` — `interpreter_engine == "pyodide"` → в `features` нет `code_interpreter`.
  - `test_send_while_answer_runs_reports_queued` — Fake отвечает `{queued: true}` → `{queued: true}`.
  - `test_send_after_revoke_is_409_relogin`.
  - `test_resolve_answer_and_reject` — тело к Fake `{call_id, action: "answer", answers: {...}}`; повтор — 409 `resolved`.
  - `test_stop_calls_task_stop`.
  - `test_upload_waits_for_processing_and_limits_size` — `> FILE_MAX` → 413 `too_large` без запроса к Fake; обычный —
    `{id, name, kind}` после статуса `completed`.
  - `test_events_stream_relays_normalized_events` — SSE отдаёт `link up`, затем `delta`, `done`.
  - `test_events_stream_ends_with_studio_session` — сессию Студии завершили (`auth.end_sessions`) → поток SSE закрыт,
    наблюдатель снят (как у `claude_code._Watch`).
  - `test_push_only_off_screen_and_for_studio_turns` — ход из Студии кончился, `push.on_screen()` пуст → один push со
    словами «ответ готов» и заголовком «Open WebUI»; на экране — нет; ход, начатый не из Студии (событие без `track`) — нет.
  - `test_pause_pushes_decision_needed` — событие `pending` хода из Студии → push «нужно ваше решение», без вопроса модели.
  - `test_send_log_has_no_text_or_ids` (`caplog`) — нет текста сообщения, имени файла, id чата.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_owui_send.py tests/test_sections_meta.py` — Expected: FAIL.

- [ ] **Step 3: Implement.** Как страница Open WebUI 0.13 собирает `features`, `tool_ids`, форму `files` и хранит
  `tool_approval_mode` — по её исходнику (`src/lib/components/chat/Chat.svelte` и соседи тега v0.13.4); форма `answers`
  у `ask_user` — оттуда же.

- [ ] **Step 4: Run** те же файлы — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Open WebUI section: send, stop, resolve, attachments, SSE and push"`.

---

### Task 7: регистрация раздела на странице

**Files:**
- Modify: `app/static/js/sections-meta.js` (`{ id: 'owui', name: tr("Open WebUI"), group: 5 }` между `translate` и `code`),
  `app/static/js/icons.js` (`owui:`), `app/static/js/app.js` (`LOADERS.owui`), `app/static/js/lock.js` (оба списка разделов),
  `app/auth.py::PAGE_SECTIONS` (`"owui"` после `"translate"`), `app/static/en.js`, `docs/glossary.md` (строка `owui`),
  `app/static/login.html`, `app/static/manifest.webmanifest`, `app/static/manifest.en.webmanifest` (описание с новым разделом)
- Create: `app/static/js/owui.js` (заготовка: раскладка панели и правой части, пустые состояния)
- Test: `tests/test_sections_meta.py` (`ORDER`, группы `[1, 2, 2, 3, 3, 4, 4, 5, 5]`)

**Interfaces:**
- Produces: экран `#/owui` (роутер грузит `owui.js`, экспорт — как у прочих загрузчиков `LOADERS`), имя раздела в
  реестре; галочка раздела в области блокировки настроек появляется сама (`settings.js` берёт `sectionPairs()`).

- [ ] **Step 1: Write the failing tests** — правки `ORDER` и групп в `test_sections_meta.py`.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_sections_meta.py` — Expected: FAIL.
- [ ] **Step 3: Implement** регистрацию по списку файлов; тексты, где перечислено «восемь разделов», — «девять»
  (README, `docs/guide.ru.md`, первая строка `CLAUDE.md`, комментарии `sections-meta.js`).
- [ ] **Step 4: Run** те же файлы и `tests/test_i18n.py tests/test_css_tokens.py tests/test_docs.py` — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "Register the Open WebUI section on the rail and router"`.

---

### Task 8: страница — учётки и дерево

**Files:**
- Create: `app/static/js/owui-panel.js`
- Modify: `app/static/js/owui.js`, `app/static/css/app.css`, `app/static/en.js`
- Test: `tests/test_ui_owui.py` (`integration`, файл — своя группа `lane:` по `tests/conftest.py`)

**Interfaces:**
- Consumes: маршруты Tasks 3–4 и `GET …/events` (Task 6); `api()` из `api.js`, `readSSE` из `sse.js`, значки — `icons.js`.
- Produces:
  - `owui-panel.js::panelView(host, { selected, chose, opened })` — учётки (подпись, хост, точка: на связи / нет
    связи / войдите заново), «+ Подключить» (форма; шаг сертификата — отпечаток и «Доверять этому сертификату»),
    «⋯» (переименовать, переподключить, удалить с подтверждением), поиск, «Новый чат», «Закреплённые», папки
    (раскрытие грузит `…/folders/{fid}/chats`), чаты без папки (догрузка страниц при прокрутке), точка идущего ответа,
    акцентная метка паузы; события `list`, `title`, `active`, `pending`, `link` обновляют панель.
  - Адреса `#/owui`, `#/owui/<aid>`, `#/owui/<aid>/<chat>`; последнее место — `localStorage['studio-owui-place']`;
    телефон — панель выезжает и прячется, как `code-panel.js`. Одна SSE-подписка на показанную учётку (`owui.js`
    раздаёт события панели и ленте).

- [ ] **Step 1: Write the failing browser tests** (`tests/test_ui_owui.py`, Fake из `tests/fake_owui.py` за живой
  тестовой службой, как в других `test_ui_*.py`; 320 и 1280 px): `test_connect_account_and_see_tree`,
  `test_certificate_step_shows_fingerprint`, `test_relogin_state_offers_password`, `test_phone_panel_slides_and_hides`.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_owui.py` — Expected: FAIL.
- [ ] **Step 3: Implement** панель. CSS — только токены цветов и `:hover` внутри `@media (hover: hover)`
  (`tests/test_css_tokens.py`); SVG — только в `icons.js`.
- [ ] **Step 4: Run** тот же файл и `tests/test_i18n.py tests/test_css_tokens.py` — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "Open WebUI section page: accounts and chat tree"`.

---

### Task 9: страница — лента, поток, отправка, пауза

**Files:**
- Create: `app/static/js/owui-chat.js`, `app/static/js/owui-pending.js`
- Modify: `app/static/js/owui.js`, `app/static/css/app.css`, `app/static/en.js`
- Test: `tests/test_ui_owui.py`

**Interfaces:**
- Consumes: маршруты Tasks 4 и 6; события SSE от `owui.js` (Task 8); `renderMarkdownInto` из `markdown.js`, `picker`
  из `picker.js`.
- Produces:
  - `owui-chat.js::chatView(host, { account, chatId })` — лента ветки (текст — `renderMarkdownInto`, рассуждение —
    свёрнутый блок как в «Чате», инструменты — плашки с раскрытием результата, картинки — `…/files/{fid}`),
    поток по событиям `delta`/`item`/`done`, после `link up` — перечитать чат; поле ввода с выбором модели
    (`…/models`, умолчание — `default`), вложениями (до `FILE_MAX`), «Остановить», «в очереди» по `queued`.
  - `owui-pending.js::paintOwuiPending(box, pending, onResolve)` — карточка по образцу `chat-pending.js`
    (классы `.msg-pending-card`…): вопросы с вариантами и полем своего текста при `allow_other` — «Ответить» /
    «Отклонить»; подтверждение — имя, аргументы свёрнуто — «Разрешить» / «Отклонить»; срок не отсчитывается;
    решение в другом окне (событие без паузы) — карточка уходит.

- [ ] **Step 1: Write the failing browser tests** (в тот же файл): `test_open_chat_renders_text_reasoning_and_tools`,
  `test_new_chat_streams_answer_and_appears_in_tree`, `test_continue_with_other_model`, `test_attach_image`,
  `test_pending_question_card_answers`, `test_stop_ends_answer`.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_owui.py` — Expected: FAIL.
- [ ] **Step 3: Implement** ленту, поле ввода и карточку.
- [ ] **Step 4: Run** тот же файл, затем `.venv/Scripts/python -m pytest -q` (быстрый набор) — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "Open WebUI section page: chat feed, streaming, sending and pause card"`.

---

### Task 10: выкладка, документация, версия, живая приёмка

**Files:**
- Modify: `deploy/apache-vhost.conf` (`ProxyPass /api/owui/ unix:/run/studio/web.sock|http://studio-backend/api/owui/ timeout=600 flushpackets=on` и `ProxyPassReverse` раньше `ProxyPass /`),
  `tests/test_deploy.py` (порядок и строки, как у `/api/code/`)
- Create: `docs/architecture/open-webui.md` (устройство, протокол 0.13, ловушки: `user-join` после обновления токена,
  доверенный сертификат, пауза, обратные вызовы), `tests/test_owui_live.py` (`manual`, группа `serial`: адрес и
  учётные данные — только из переменных окружения `OWUI_LIVE_URL`, `OWUI_LIVE_EMAIL`, `OWUI_LIVE_PASSWORD`; без них — skip)
- Modify: `CLAUDE.md` (карта модулей: строка «Open WebUI» — `owui`, `owui_tokens`, `owui_view`, `owui_link`, ссылка на
  `open-webui.md`; строка отката 63 по спеке; Apache в «Ловушках» — `/api/owui/`), `docs/architecture/README.md`,
  `docs/guide.ru.md` (как подключить учётку, что нужно от администратора: адрес в списке разрешённых), `README.md`,
  `README.ru.md`, `app/release.py` (`VERSION = "0.45.0"`)
- Test: `tests/test_deploy.py`, `tests/test_docs.py`, `tests/test_owui_live.py`

- [ ] **Step 1: Write the failing tests** — `test_deploy.py`: `ProxyPass /api/owui/` раньше `ProxyPass / ` и с
  `timeout=600 flushpackets=on`; `test_owui_live.py`: подключение, дерево, новый чат с потоком до `done`,
  продолжение, «Остановить», вложение картинки, вопрос модели и `resolve` (если модель задала вопрос — иначе skip
  этой части), отзыв сессии → `relogin`; в конце — удалить тестовые чаты и учётку (сессия отзывается).
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_deploy.py tests/test_docs.py` — Expected: FAIL.
- [ ] **Step 3: Implement** правки выкладки и документации; значения констант в `docs/architecture/open-webui.md` —
  только с модулем (`owui.FILE_MAX` и т. п.), их сверяет `tests/test_docs.py`.
- [ ] **Step 4: Run** быстрый набор `.venv/Scripts/python -m pytest -q` и набор моста по `agent/CLAUDE.md` —
  Expected: PASS; затем живая приёмка `OWUI_LIVE_*=… .venv/Scripts/python -m pytest -q -m manual tests/test_owui_live.py`
  на тестовом пользователе (данные — `~/.claude/.env`, `OWUI_S06_PROBE_*`) — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "Release Studio 0.45.0: Open WebUI section"`.
