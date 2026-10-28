# Раздел «Claude Code»: сервер студии — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** студия привязывает мосты Claude Code логином и паролем, держит с ними канал WebSocket, открывает
компьютер PIN-ом моста, показывает проекты и чаты машины, передаёт ход в браузер (SSE) с карточками
разрешений и вопросов, даёт файлы с правкой, просмотр `.md` и `.pdf`, статус git и push — всё проверено на
подставном мосте. Сам мост — второй план (проект `studio-agent`).

**Architecture:** четыре новых модуля службы. `bridge.py` — нижний слой: история подключений
(`record`, признак сессии `tag`), канал (`Link`, реестр `LINKS`, просьбы с ответами `call`, раздача событий
наблюдателям `WATCHERS`, уведомление `close`). `agents.py` — привязка и учёт компьютеров. `code.py` —
`/api/code/*` поверх `bridge.call`. `push.py` — Web Push (ключ VAPID в базе, RFC 8291). Шаг схемы 19.
Страница — `code.js`, `code-chat.js`, `code-files.js`, блоки в `settings.js`, обработчик `push` в `sw.js`.
Подставной мост `tests/fake_bridge.py` — машина по протоколу: в процессе службы (быстрые тесты) и через
настоящий WebSocket (браузерные).

**Tech Stack:** FastAPI WebSocket (uvicorn[standard] 0.53, websockets 17), SQLite, cryptography (ECDH
P-256, HMAC-SHA256, AES-GCM, ES256), pypdfium2 через `ocr_render`; ES-модули без сборки; pytest +
TestClient (httpx2: SSE читается по ходу, `s.c.portal` — цикл службы), браузерные — CDP `tests/browser.py`.

**Spec:** [docs/superpowers/specs/2026-10-28-studio-claude-code-design.md](../specs/2026-10-28-studio-claude-code-design.md)
и протокол канала [docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md](../specs/2026-10-28-studio-code-bridge-protocol.md)
— читать оба целиком; протокол — источник истины для кадров, кодов закрытия, просьб, событий и пределов.

## Global Constraints

- `db.VERSION = 19`; шаг `CODE_19` — только `CREATE TABLE` и `CREATE INDEX`, дописан в конец `SCHEMA` и в
  `STEPS[19]`. Прежний код с новой базой работает: новых таблиц он не читает, каскады FK — в схеме.
- Протокол — дословно по документу. Расходится код с документом — правится код; документ меняется только
  новой версией `v`.
- В строке запроса (её пишет журнал Apache) — только id: `agent_id` (int), `pid` (`^[0-9a-f]{16}$`),
  `chat` (UUID). Пути файлов, имена проектов, PIN, тексты — только в теле JSON; поэтому чтения файлов —
  `POST`.
- Журнал службы — только «агент N: …» и «пользователь N: …», коды и числа. Ни ключа устройства, ни PIN, ни
  логина и пароля привязки, ни имён компьютеров, проектов и файлов, ни названий и текстов чатов, ни адреса
  подписки push. Каждый модуль — со своим тестом журнала (`caplog`, `logging.DEBUG`).
- `/api/code/*` никогда не отвечает 423 (его ловит экран блокировки в `api.js`): «компьютер закрыт» — 409
  `closed`.
- Анонимные пути — прежние плюс `POST /api/agent/enroll` (`auth.OPEN_API`). `/api/agent/link` — WebSocket:
  http-middleware `auth.guard` его не видит, вход — ключом устройства.
- Пределы: `bridge.FRAME_MAX = 16 * 1024 * 1024`, `bridge.HELLO_SEC = 10`, `bridge.CALL_SEC = 30`,
  `bridge.MESSAGE_MAX = 300`; `agents.NAME_MAX = 64`, `agents.EVENTS_PAGE = 50`; `code.TEXT_MAX = 100_000`
  (знаков задания), `code.FILE_MAX = 2 * 1024 * 1024`, `code.FILE_BODY_MAX = 3 * 1024 * 1024`,
  `code.RAW_MAX = 10 * 1024 * 1024`, `code.PATH_MAX = 1024`, `code.HISTORY_PAGE = 100`, `code.PING_SEC = 15`,
  `code.PDF_TTL_SEC = 300`; `push.TTL_SEC = 3600`, `push.JWT_SEC = 12 * 3600`, `push.ENDPOINT_MAX = 1024`.
- Тексты страницы — дословно из раздела «Тексты» ниже; тексты сервера — дословно из задач.
- Команды: точечно `.venv/Scripts/python -m pytest tests/<файл>.py -q -n0`; быстрый набор
  `.venv/Scripts/python -m pytest -q` (бюджет 60 с); браузер `.venv/Scripts/python -m pytest -q -m integration
  tests/test_ui_browser.py -n0 -k "<выражение>"`.
- Коммит — сообщение файлом (`git commit -F <файл>`), в конце `Co-Authored-By: Claude Opus 5.5
  <noreply@anthropic.com>`; без push. Правка `index.html`, `app.js`, `settings.js`, `sw.js` — перед коммитом
  прогнать прежние браузерные сценарии оболочки и настроек: `-k "login or menu or settings or notify or wipe
  or export or shell"` (исполнители их пропускают).

### Тексты страницы

- Раздел: вкладка рельса «Код», заголовок «Claude Code»; пусто — «Компьютеров пока нет. Поставьте мост
  Claude Code на компьютер и войдите в нём логином студии.»; строка компьютера — «на связи» / «не на связи»,
  значок «PIN»; нажатие на компьютер не на связи — «Компьютер не на связи — включите его и запустите мост».
- PIN: заголовок «PIN компьютера», кнопка «Открыть», «Неверный PIN — осталось 4 попытки» / «… осталось 2
  попытки» / «… осталась 1 попытка» / «… осталось 5 попыток» (склонение — как у PIN блокировки в `lock.js`),
  «PIN заблокирован после 5 ошибок — разблокируйте его в окне моста на самом компьютере».
- Проект: «Новый чат», вкладки «Чаты», «Файлы», «Git»; пометки «студия», «VS Code или терминал», «закрыт»,
  «идёт ход», «ждёт ответа»; кнопки «Закрыть», «Открыть снова»; чат без названия — «Новый чат».
- Чат: «Отправить», «Остановить», «Закрыть чат», «Показать раньше», «Открыть файл»; подписи выбора — «Модель»,
  «Усилие»; у сессии из VS Code или терминала — «Эту сессию начали в VS Code или терминале. Если она открыта и
  там, не пишите в двух местах сразу — ответы перемешаются.»;
  карточка разрешения — «Claude Code просит разрешения», кнопки «Да», «Да, больше не спрашивать», «Нет»,
  поле «Что сделать вместо этого (необязательно)»; вопрос — кнопка «Ответить»; «Компьютер пропал со связи —
  жду его»; конец хода — «Ход прерван» (`stopped`), «Ошибка: <message>» (`error`).
- Файлы: «Сохранить», «Сохранено», «Файл изменился на компьютере — откройте его заново, чтобы не затереть
  правки», «Файл больше 2 МиБ — не открывается», «Двоичный файл — не открывается», переключатель «Текст» /
  «Вид» (`.md`), листание PDF «‹» «›» и «Страница N из M».
- Git: «Ветка», «впереди на N», «позади на N», «Изменений нет», «Не репозиторий git».
- Настройки → «Прочее»: блок «Компьютеры (Claude Code)», кнопка «Отозвать», диалог «Отозвать компьютер?» с
  текстом «Мост на нём отключится и не войдёт снова без нового входа. История подключений останется.» и
  кнопкой «Отозвать»; «История подключений», «Показать ещё»; события: `enroll` «привязан», `revoke`
  «отозван», `online` «на связи», `offline` «связь потеряна», `open` «открыт», `pin_ok` «открыт PIN»,
  `pin_fail` «неверный PIN», `session_start` «новый чат».
- «Уведомления»: флажок «Push от Claude Code на этом устройстве», под ним «Приходят и при закрытой студии и
  погасшем экране: запрос разрешения, вопрос, конец хода. Идут через серверы Apple или Google — содержимое
  зашифровано, но факт и время им видны. На iPhone — только у студии, добавленной на экран «Домой».»
- Push (сервер): заголовок «Claude Code»; тело «<имя компьютера>: ждёт подтверждения», «…: вопрос», «…: ход
  окончен», «…: ход прерван», «…: ошибка».
- «Удалить всю историю»: к тексту окна дописать «Чаты Claude Code хранятся на компьютерах — студия забудет
  только свои отметки, сами сессии останутся.»
- Выгрузка: разделы «Claude Code: чаты» (`code`) и «Claude Code: подключения» (`agents`).

## Review Focus

1. Мост пропал посреди хода (канал закрылся, пока открыт SSE): браузер получает событие `offline`, поток
   кончается сразу, а не через 600 с; начатые просьбы — 409 `offline` сразу, а не 504 через 30 с. Тесты —
   Task 3 (`call` → `Offline`) и Task 5 (поток).
2. Две вкладки смотрят один чат; ответ на карточку в одной — карточка уходит и во второй (`asked` всем
   наблюдателям). Тест — Task 5.
3. Чужой компьютер, а также `pid`, `chat` или путь недопустимого вида — 404/422 до моста: подставной мост не
   получает ни одной просьбы. Тесты — Task 4 и Task 6.
4. Мост шлёт кривое: неизвестный `t`, ответ на неизвестный `id`, событие без `k`, кадр больше `FRAME_MAX`,
   `data` не объект, ответ без `code`. Служба не падает: неизвестный `t` и большой кадр — закрытие 4400,
   прочее пропускается; запрос владельца — 502 `bridge`. Тесты — Task 3 и Task 4.
5. Запертая студия: `/api/code/*` — 423 от `guard`, а отпирание шлёт мосту `close` этой сессии, и следующая
   просьба — 409 `closed` (цифровая панель снова). Тесты — Task 3 (`close`) и Task 4 (`closed` после
   отпирания).

---

### Task 1: Шаг схемы 19

**Files:**
- Modify: `app/db.py` (`CODE_19`, `SCHEMA`, `STEPS[19]`, `VERSION = 19`)
- Test: `tests/test_foundation.py`

**Interfaces:**
- Produces: таблицы `agents`, `agent_events`, `code_chats`, `push_subs` (SQL ниже — дословно).

```sql
CREATE TABLE agents(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, has_pin INTEGER NOT NULL DEFAULT 0,
       projects_root TEXT, cc_version TEXT, created REAL NOT NULL, last_seen REAL);
CREATE INDEX agents_user ON agents(user_id);
CREATE TABLE agent_events(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL, agent_name TEXT NOT NULL, created REAL NOT NULL,
       kind TEXT NOT NULL CHECK (kind IN ('enroll','revoke','online','offline','open','pin_ok','pin_fail','session_start')),
       device TEXT, ip TEXT);
CREATE INDEX agent_events_user ON agent_events(user_id, id);
CREATE TABLE code_chats(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL, session_id TEXT NOT NULL,
       state TEXT NOT NULL CHECK (state IN ('open','closed')), created REAL NOT NULL, updated REAL NOT NULL,
       UNIQUE (agent_id, session_id));
CREATE INDEX code_chats_user ON code_chats(user_id, created);
CREATE TABLE push_subs(endpoint TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       digest TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
       created REAL NOT NULL);
CREATE INDEX push_subs_user ON push_subs(user_id);
CREATE INDEX push_subs_digest ON push_subs(digest);
```

- [ ] **Step 1: Тесты.** В `tests/test_foundation.py`: `DROP_V19 = "DROP TABLE push_subs; DROP TABLE code_chats;
  DROP TABLE agent_events; DROP TABLE agents;"` — первым в каждой строке, ставящей `PRAGMA user_version` ниже
  19 (`grep -n "PRAGMA user_version = "`); `db.VERSION == 18` → `== 19`; новый тест
  `test_step_19_adds_code_tables_and_cascades`: база 18 (`DROP_V19`, `user_version = 18`) с пользователем и
  сессией → `migrate` → версия 19; затем строки во все четыре таблицы и проверки каскадов: удаление сессии
  уносит её `push_subs`; удаление агента уносит его `code_chats`, `agent_events` остаются; удаление
  пользователя уносит всё.
- [ ] **Step 2: Запустить** `tests/test_foundation.py` — новый тест падает (нет таблиц).
- [ ] **Step 3: Реализовать** `CODE_19` в `db.py` с комментарием по образцу соседних (спека 6.1, 6.8; агент —
  компьютер с мостом; события — только добавляются, стирание истории их не трогает; `code_chats` — слой
  студии поверх сессий машины; подписка push живёт, пока жива сессия).
- [ ] **Step 4: Запустить** `tests/test_foundation.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: шаг схемы 19 — компьютеры, история подключений, чаты, push».

---

### Task 2: Привязка моста, компьютеры учётки, история подключений

**Files:**
- Create: `app/bridge.py` (пока только история: `tag`, `device`, `record`), `app/agents.py`, `tests/test_agents.py`,
  `tests/fake_bridge.py` (пока только помощник `enroll`, Task 3 его дополняет)
- Modify: `app/auth.py` (`check_password` из тела `login`, `OPEN_API` в `guard`, `_foreign` → `foreign_origin`)
- Modify: `app/main.py` (`include_router(agents.router)`)
- Test: `tests/test_agents.py`, `tests/test_auth.py` (`ANONYMOUS` += `"/api/agent/enroll"`)

**Interfaces:**
- Produces в `auth.py`: `OPEN_API = frozenset({"/api/agent/enroll"})` — `guard` пропускает их без сессии и
  CSRF; `async def check_password(name: str, password: str, ip: str) -> int | None` — неудача записывается до
  scrypt, успех снимает пару и эту попытку адреса, неудача ждёт растущую задержку; вызывающий сперва проверил
  `locked(name, ip)`. `login` пользуется ею, поведение `/login` прежнее (старые тесты `test_auth.py` — без правок).
  `foreign_origin(request) -> bool` — прежний `_foreign`.
- Produces в `bridge.py`: `def tag(digest: str) -> str` — `sha256(("studio-code\0" + digest).encode()).hexdigest()[:32]`;
  `def device(request: Request) -> str` — «телефон» или «браузер» по `sessions.mobile` сессии запроса, пробел,
  `tag(request.state.digest)[:6]`; `def record(user_id: int, agent_id: int, agent_name: str, kind: str, *,
  device: str | None = None, ip: str | None = None) -> None` — строка `agent_events` с `clock.NOW()`.
- Produces в `agents.py`: `router` (без префикса), `NAME_MAX = 64`, `EVENTS_PAGE = 50`;
  - `POST /api/agent/enroll` → 201 `{"agent_id": int, "device_key": str}`; отказы: 403 `origin` «запрос пришёл
    с чужого сайта», 413 `too_long` «слишком длинный запрос» (тело больше `auth.LOGIN_BODY_MAX`), 422 `invalid`
    «неверные данные запроса» (не JSON-объект или поля не строки), 422 `name` «имя компьютера — от 1 до 64
    знаков без управляющих», 429 `locked` «слишком много попыток — подождите 15 минут», 401 `login` «неверный
    логин или пароль». Логин — `strip().lower()`, имя — `strip()`; управляющие — `unicodedata.category(c)` на `C`.
  - `GET /api/agents` → `{"items": [{"id", "name", "online", "has_pin", "cc_version", "projects_root", "created",
    "last_seen", "models", "efforts"}]}` по `created`; в этой задаче `online` — `False`, `models`/`efforts` — `[]`.
  - `DELETE /api/agents/{aid}` → 204 (чужой или нет — 404 `not_found` «нет такого компьютера»); событие `revoke`
    с `device`/`ip` запроса.
  - `GET /api/agents/events?before=<id>` → `{"items": [{"id", "created", "kind", "agent_id", "agent_name",
    "device", "ip"}], "next": int | None}` — свои, `id < before`, по убыванию `id`, по `EVENTS_PAGE`; `next` —
    последний `id`, если страница полная.

- [ ] **Step 1: Тесты** `tests/test_agents.py` (помощник `tests/fake_bridge.py::enroll(s, login="owner",
  name="Рабочий ПК", password="password-123", ip="10.77.48.5") -> tuple[int, str]` — POST с `X-Forwarded-For`,
  201, `(agent_id, device_key)`):
  - `test_enroll_returns_a_key_once_and_keeps_its_hash`: 201; `len(device_key) >= 43`; в `agents` —
    `key_hash == security.digest(key)`, ключа целиком в базе нет (`SELECT * … ` по всем строкам — без
    `device_key`); событие `enroll` с `agent_name`, `ip`.
  - `test_enroll_counts_failures_together_with_login`: три неверных пароля через enroll и два через `/login`
    с того же адреса — шестая попытка с верным паролем и через enroll, и через `/login` — 429 / страница паузы.
  - `test_enroll_refuses_foreign_origin_long_body_and_bad_name`: `Origin: https://evil.example` — 403;
    тело 4097 байт — 413; `name` `""`, `"  "`, 65 знаков, `"ПК\x07"`, `"ПК\u202e"` — 422 `name`; тело `[]` и
    `{"login": 1}` — 422 `invalid`; ни одной строки в `agents`.
  - `test_agents_belong_to_their_user`: компьютер `owner` не виден `friend` в `GET /api/agents`; `DELETE` его
    от `friend` — 404, строка цела.
  - `test_revoke_removes_the_agent_and_keeps_the_history`: `DELETE` — 204, строки нет; события `enroll`,
    `revoke` с прежним `agent_name`.
  - `test_events_come_in_pages`: 55 событий → 50 и `next`; вторая страница — 5 и `next is None`; чужих нет.
  - `test_agent_log_holds_no_secrets`: `caplog` — нет ключа, пароля, логина, имени компьютера.
- [ ] **Step 2: Запустить** `tests/test_agents.py tests/test_auth.py` — падают (нет маршрутов).
- [ ] **Step 3: Реализовать** `auth.check_password` (тело — из `login`, `login` зовёт её), `OPEN_API` первым
  делом в `guard` после проверки префикса `/api`, `bridge.py` (докстринг модуля — канал моста и история
  подключений, спека 6.2–6.4; протокол — ссылкой), `agents.py` (докстринг — привязка и учёт), `main.py`.
  Журнал: «пользователь N: компьютер M привязан», «… отозван», отказ привязки — «привязка с <адрес>: <код>».
- [ ] **Step 4: Запустить** `tests/test_agents.py tests/test_auth.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: привязка моста и история подключений».

---

### Task 3: Канал моста и подставной мост

**Files:**
- Modify: `app/bridge.py` (канал), `app/agents.py` (`online`, `models`, `efforts`; отзыв — `bridge.drop`)
- Modify: `app/screenlock.py` (`bridge.forget` в `lock_now` и трёх отпираниях), `app/admin.py`
  (`bridge.drop_user` рядом с каждым `auth.end_sessions(uid)`), `app/main.py` (`include_router(bridge.router)`,
  в `lifespan` после `chat.stop_all()` — `await bridge.stop()`)
- Create: `tests/fake_bridge.py`, `tests/test_bridge.py`
- Modify: `tests/test_auth.py` (`guarded_routes`: `WebSocketRoute` `/api/agent/link` пропускается, остальное —
  по-прежнему `Route`)

**Interfaces:**
- Produces в `bridge.py`:
  - коды закрытия `CLOSE_PROTOCOL = 4400`, `CLOSE_AUTH = 4401`, `CLOSE_REPLACED = 4409`, `CLOSE_RESTART = 1012`;
    `EVENT_KINDS = frozenset({"text", "step", "result", "ask", "asked", "snapshot", "done"})`;
    `EFFORTS = ("low", "medium", "high", "xhigh", "max")`.
  - `class ProtocolError(ValueError)`, `class Offline(Exception)`, `class BridgeError(Exception)` с полями
    `code: str`, `message: str`, `extra: dict` (только `left: int` и `version: str`, если пришли верного типа).
  - `class Link`: поля `agent_id: int`, `user_id: int`, `name: str`, `hello: dict`, `ip: str | None`,
    `pending: dict[str, asyncio.Future]`; `async def send(self, frame: dict) -> None`,
    `async def close(self, code: int) -> None` — в базовом классе `NotImplementedError`. `class SocketLink(Link)`
    — поверх `WebSocket`, отправка под `asyncio.Lock`.
  - `LINKS: dict[int, Link]`, `WATCHERS: dict[tuple[int, str], set[asyncio.Queue]]`, `TASKS: set[asyncio.Task]`.
  - `def parse_hello(frame: dict) -> dict` — по протоколу §2.1, иначе `ProtocolError`; возвращает
    `{"cc_version", "projects_root", "has_pin", "models", "efforts"}`.
  - `async def attach(link: Link) -> None` — заменяет прежний канал агента (тот — `close(CLOSE_REPLACED)`),
    пишет `has_pin`, `cc_version`, `projects_root`, `last_seen`, событие `online`, шлёт `welcome`.
  - `async def detach(link: Link) -> None` — если в `LINKS` именно он: убирает, все его `pending` — исключение
    `Offline`, наблюдателям его чатов — `("offline", None)`, `last_seen`, событие `offline`; иначе — только
    `pending`. Повторный вызов безвреден.
  - `def deliver(link: Link, frame: dict) -> None` — `res`: будущий по `id` получает кадр (неизвестный `id` —
    пропуск); `ev`: проверка вида (`project` — pid, `chat` — UUID, `e` — объект с `k` из `EVENT_KINDS`), затем
    `put_nowait(("ev", e))` в каждую очередь `WATCHERS[(agent_id, chat)]`; кривое событие — пропуск и
    предупреждение «агент N: неверное событие»; иной `t` — `ProtocolError`.
  - `async def call(agent_id: int, op: str, session: str, timeout: float = CALL_SEC, **args) -> dict` — кадр
    `req` с `id = secrets.token_hex(8)`; нет канала — `Offline`; `ok: true` — `data` (не объект — `{}`);
    `ok: false` — `BridgeError(code[:40], message[:MESSAGE_MAX], extra)`, без `code` — `BridgeError("bridge", …)`;
    срок — `TimeoutError`.
  - `def watch(agent_id: int, chat: str) -> asyncio.Queue`, `def unwatch(agent_id: int, chat: str,
    q: asyncio.Queue) -> None`, `def watching(agent_id: int, chat: str) -> bool`.
  - `def forget(user_id: int, digest: str) -> None` — каждому каналу пользователя `{"t": "close", "session":
    tag(digest)}` задачей (`TASKS`; сбой отправки — только предупреждение).
  - `async def drop(agent_id: int, code: int = CLOSE_AUTH) -> None`, `async def drop_user(user_id: int) -> None`,
    `async def stop() -> None` (все — `CLOSE_RESTART`).
  - `router`, `@router.websocket("/api/agent/link")`: ключ — `Authorization: Bearer …`; строка
    `agents JOIN users` с `key_hash = digest(ключ)` и `disabled = 0`; `accept()`; нет строки — `close(CLOSE_AUTH)`;
    первый кадр за `HELLO_SEC` → `parse_hello`, иначе `CLOSE_PROTOCOL`; затем цикл `receive_text` →
    (длиннее `FRAME_MAX` или не JSON-объект — `ProtocolError`) → `deliver`; `ProtocolError` — `CLOSE_PROTOCOL`;
    `finally` — `detach`. Адрес — `auth.client_ip(ws)`.
- Produces в `tests/fake_bridge.py`:
  - `def pid(name: str) -> str` (протокол §5).
  - `class FakeMachine(projects: dict | None = None, pin: str | None = None)`: `projects` — имя → `{"files": {путь:
    bytes}, "chats": {chat: {"title", "created", "modified", "tag", "messages": [...]}}, "git": dict | None}`;
    поля `open_sessions: set[str]`, `pin_fails: int`, `requests: list[dict]` (все просьбы), `script: list[dict]`
    (события следующего хода, без `t`/`project`/`chat`), `hello` (свойство: кадр `hello` по протоколу,
    модели `[{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}]`, усилия `["low", "high"]`).
    `def handle(self, req: dict) -> tuple[dict, list[dict]]` — ответ `res` и кадры `ev` после него, по протоколу
    §3–§7: `turn` отдаёт события `script` до первого `ask` включительно, `answer` — `asked` и дальше до
    следующего `ask` или `done`, `stop` — `asked` открытого запроса и `done` `stopped`, `watch` идущего хода —
    `snapshot`; `turn` в незнакомый чат заводит его с меткой `studio`; 5 неверных PIN — `pin_locked`.
  - `class InProcessBridge(bridge.Link)`: `send` пишет кадр в `sent` и на `req` планирует
    `loop.call_soon(bridge.deliver, self, f)` для ответа и событий по порядку; `close` пишет `closed_with`.
  - `enroll` (Task 2), `def connect(s, agent_id: int, machine: FakeMachine) -> InProcessBridge` (через
    `s.c.portal.call(bridge.attach, link)`),
    `def emit(s, link, project: str, chat: str, *events: dict) -> None`, `def drop(s, link) -> None`.
  - `class SocketBridge(url: str, key: str, machine: FakeMachine)`: поток с
    `websockets.sync.client.connect(ws_url, additional_headers={"Authorization": f"Bearer {key}"})`, шлёт
    `hello`, отвечает машиной; `start()`, `stop()`, `send_events(project, chat, *events)`.

- [ ] **Step 1: Тесты** `tests/test_bridge.py` — канал настоящим WebSocket TestClient
  (`s.c.websocket_connect("/api/agent/link", headers={"Authorization": f"Bearer {key}"})`), просьбы — через
  `fut = s.c.portal.start_task_soon(bridge.call, aid, "projects", "t" * 32)`, ответ — `ws.send_json(...)`,
  итог — `fut.result(1)`:
  - `test_link_needs_a_known_key`: без заголовка, с чужим ключом, у отключённой учётки — закрытие 4401
    (`WebSocketDisconnect.code`); строки событий `online` нет.
  - `test_hello_brings_the_agent_online`: `welcome {"agent_id", "name"}`; `GET /api/agents` — `online: true`,
    `has_pin`, `cc_version`, `projects_root`, `models`, `efforts` из `hello`; закрытие сокета — `online: false`,
    `last_seen` = часы, события `online`, `offline`.
  - `test_bad_hello_or_silence_closes_4400`: `hello` с `v: 2`, без `models`, с `efforts: ["huge"]`; молчание при
    `HELLO_SEC = 0.05`.
  - `test_new_link_replaces_the_old_one`: первый сокет получает 4409, `online` остаётся у второго, события
    `offline` за первый нет.
  - `test_call_round_trip_and_errors`: успех — `data`; `ok: false` с `left` — `BridgeError` с `extra == {"left": 3}`;
    без ответа при `CALL_SEC = 0.05` — `TimeoutError`; сокет закрылся, пока ждали, — `Offline` (быстрее срока).
  - `test_odd_frames_are_ignored_or_close_4400`: ответ с неизвестным `id`, событие без `k`, событие с `chat`
    не-UUID — канал жив (следующий `call` проходит); `{"t": "xyz"}` и кадр длиннее `FRAME_MAX = 1024` — 4400.
  - `test_revoke_and_disable_drop_the_link`: `DELETE /api/agents/{aid}` — сокет 4401; у второго компьютера
    отключение учётки в панели — 4401.
  - `test_password_change_keeps_the_device_key`: смена пароля учётки (`POST /api/me/password`) — канал жив, новое
    подключение тем же ключом проходит (спека 6.2).
  - `test_lock_and_unlock_close_the_session_on_the_bridge` (`InProcessBridge`): `POST /api/lock/now` — в `sent`
    `{"t": "close", "session": bridge.tag(<дайджест cookie>)}`; отпирание паролем — ещё один такой же.
  - `test_link_log_holds_no_key`: `caplog` без ключа и имени компьютера.
- [ ] **Step 2: Тест машины** `test_fake_machine_follows_the_protocol` (в том же файле, без службы): PIN — верный,
  неверный с `left`, пятый — `pin_locked`; `closed` без `open`; `turn` со `script` `[text, ask, text, done]` —
  ответ и `text`, `ask`; `answer` — `asked`, `text`, `done`; `history` с `before`/`limit`.
- [ ] **Step 3: Тест через сеть** `test_socket_bridge_comes_online` (`@pytest.mark.integration`, в
  `tests/test_ui_browser.py` у фикстуры `live`; живая студия — в потоке этого же процесса, `bridge.LINKS` виден
  тесту): привязка через `httpx.post(f"{live.url}/api/agent/enroll", json=…)`, `SocketBridge(...).start()`,
  ожидание `aid in bridge.LINKS` до 5 с, `bridge.LINKS[aid].hello["has_pin"]` — как у машины; `stop()` — агента
  нет в `LINKS` до 5 с.
- [ ] **Step 4: Запустить** `tests/test_bridge.py tests/test_agents.py tests/test_auth.py tests/test_screenlock.py
  tests/test_profile_admin.py` — новые падают.
- [ ] **Step 5: Реализовать** канал по Interfaces; `agents.list` берёт `online`, `models`, `efforts` из
  `bridge.LINKS`; `agents.revoke` — `await bridge.drop(aid)` после удаления строки. `screenlock`:
  `bridge.forget(uid, request.state.digest)` в `lock_now`, `open_with_password`, `open_with_pin` (только при
  успехе), `open_with_passkey` (успех). Выход и конец сессий мосту не сообщаются: мёртвая сессия больше ни одной
  просьбы не пришлёт, а её признак не повторится — открытая на мосте запись уйдёт по сроку (спека 6.3).
  Журнал: «агент N: на связи», «агент N: связь потеряна», «канал: неизвестный ключ с <адрес>», «агент N:
  нарушение протокола». `conftest.studio` очищает `bridge.LINKS`, `bridge.WATCHERS`, `bridge.TASKS` до и после
  теста, как прочие модульные состояния.
- [ ] **Step 6: Запустить** те же файлы и `-m integration tests/test_ui_browser.py -n0 -k socket_bridge` — PASS.
- [ ] **Step 7: Коммит** «Claude Code: канал моста и подставной мост».

---

### Task 4: Раздел `code`: открыть компьютер, проекты, чаты, история

**Files:**
- Create: `app/code.py`, `tests/test_code.py`
- Modify: `app/main.py` (`include_router(code.router)`)

**Interfaces:**
- Consumes: `bridge.call`, `bridge.LINKS`, `bridge.tag`, `bridge.device`, `bridge.record`; `fake_bridge.*`.
- Produces в `code.py`: `router = APIRouter(prefix="/api/code")`; `PID_RE = re.compile(r"[0-9a-f]{16}")`;
  `STATUS = {"closed": 409, "pin": 403, "pin_locked": 403, "pin_needed": 409, "not_found": 404, "outside": 403,
  "too_large": 413, "binary": 415, "conflict": 409, "busy": 409, "running": 409, "idle": 409, "gone": 410,
  "not_git": 409, "invalid": 422}`;
  - `def own_agent(request: Request, aid: int) -> sqlite3.Row` — 404 `not_found` «нет такого компьютера»;
  - `def check_pid(pid: str) -> str` — 422 `project` «неверный проект»; `def check_chat(chat: str) -> str` —
    `str(uuid.UUID(chat)) == chat`, иначе 422 `chat` «неверный чат»;
  - `async def ask(request: Request, aid: int, op: str, **args) -> dict` — `bridge.call(aid, op,
    bridge.tag(request.state.digest), **args)`; `Offline` — 409 `offline` «компьютер не на связи»;
    `TimeoutError` — 504 `timeout` «компьютер не ответил»; `BridgeError` — статус по `STATUS`, тело
    `{"code", "message", **extra}`; код не из `STATUS` — 502 `bridge` «мост ответил непонятно».
  - `POST /api/code/agents/{aid}/open` `{"pin": str | null}` (`max_length=64`) → 204; события: `open` (PIN не
    прислан), `pin_ok` (прислан), `pin_fail` (отказ `pin` или `pin_locked`) — с `bridge.device(request)` и
    `auth.client_ip(request)`.
  - `GET /api/code/agents/{aid}/projects` → `{"items": [{"pid", "name"}]}`.
  - `GET /api/code/agents/{aid}/projects/{pid}/chats` → `{"items": [{"chat", "title", "created", "modified",
    "origin": "studio" | "other", "state": "open" | "closed", "running", "asking"}]}`: чаты машины; `state` — из
    `code_chats` (нет строки — `open`), `origin` — `studio` при `tag == "studio"`; строки `code_chats` без чата
    на машине (новый, ещё без хода) — тоже, с `title: null`, `modified = updated`, `origin: "studio"`; порядок —
    открытые, затем закрытые, внутри — `modified` по убыванию.
  - `POST /api/code/agents/{aid}/projects/{pid}/chats` → 201 `{"chat": str(uuid.uuid4())}`, строка `code_chats`
    `open`, событие `session_start`; мост не зовётся (компьютер мог закрыться — строка безвредна).
  - `PATCH /api/code/agents/{aid}/projects/{pid}/chats/{chat}` `{"state": "open" | "closed"}` → `{"state"}`;
    строки нет — заводится (`INSERT … ON CONFLICT(agent_id, session_id) DO UPDATE`); мост не зовётся.
  - `GET /api/code/agents/{aid}/projects/{pid}/chats/{chat}?before=<int>` → `data` просьбы `history`
    (`limit = HISTORY_PAGE`).

- [ ] **Step 1: Тесты** `tests/test_code.py` (машина с проектами «alpha» и «beta», в «alpha» чат A с меткой
  `studio`, чат B без метки, 250 сообщений в A):
  - `test_someone_elses_agent_is_404_before_the_bridge`: каждый маршрут раздела (`guarded_routes` из
    `test_auth.py`, отбор по `/api/code/`; тело — верное по виду для маршрута: `{"pin": None}`, `{"state":
    "closed"}`, `{"text": "x"}`, `{"request_id": "r1", "decision": "allow"}`, `{"path": ""}`, `{"path": "a",
    "text": "", "version": "v"}`, `{"path": "a.pdf", "page": 1}`; `pid` — 16 hex, `chat` — UUID) от `friend` с id
    компьютера `owner` — 404; `machine.requests == []`. Маршруты задач 5 и 6 дописываются сюда же.
  - `test_offline_agent_is_409_offline` — без `connect`.
  - `test_pin_opens_and_failures_are_recorded`: `pin: "1111"` при PIN `"2580"` — 403 `{"code": "pin", "left": 4}`,
    событие `pin_fail` с `device` («браузер …» / «телефон …» после отметки `touch`) и `ip`; верный — 204,
    `pin_ok`; пять неверных — 403 `pin_locked`; без PIN у машины без PIN — 204 и `open`; `null` при заданном —
    409 `pin_needed`.
  - `test_unopened_computer_is_409_closed_and_unlock_closes_it_again`: `projects` до `open` — 409 `closed`;
    после `open` — 200; `POST /api/lock/now`, отпирание паролем — снова 409 `closed`.
  - `test_chats_merge_machine_sessions_with_studio_marks`: B закрыт `PATCH` → порядок `[A open studio, B closed
    other]`; новый чат без хода — в списке с `title: None`; `running`/`asking` — как у машины.
  - `test_new_chat_writes_a_row_and_an_event_but_not_the_bridge`.
  - `test_history_pages_from_the_end`: без `before` — 100 последних, `start == 150`; `before=150` — следующие.
  - `test_bad_ids_are_422`: `pid` `"ABC"`, `"a" * 17`; `chat` `"x"`, UUID в верхнем регистре — 422, мост пуст.
  - `test_odd_bridge_answers_are_502_and_long_words_are_cut`: ответ с кодом `"zzz"` — 502 `bridge`; `message` в
    1000 знаков — 300; `data` строкой — `{}`.
  - `test_slow_bridge_is_504` (`bridge.CALL_SEC = 0.05`, машина не отвечает).
  - `test_code_log_holds_no_names`: `caplog` без «alpha», заголовков чатов, PIN.
- [ ] **Step 2: Запустить** `tests/test_code.py` — падают (нет маршрутов).
- [ ] **Step 3: Реализовать** по Interfaces; докстринг модуля — что и как ретранслируется (спека 6.5–6.7),
  почему в строке запроса только id. Журнал — «пользователь N: компьютер M — <op>: <код>» только на отказ.
- [ ] **Step 4: Запустить** `tests/test_code.py tests/test_auth.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: компьютеры, проекты, чаты и история на сервере».

---

### Task 5: Ход: поток SSE, наблюдение, ответы на карточки, остановка

**Files:**
- Modify: `app/code.py`
- Test: `tests/test_code.py`

**Interfaces:**
- Consumes: `bridge.watch`, `bridge.unwatch`, `bridge.LINKS[aid].hello`.
- Produces:
  - `POST …/chats/{chat}/turn` `{"text": str, "model": str | null, "effort": str | null}` → SSE: первым
    `event: turn` `{"turn"}`, затем события хода по `k` (`event: <k>`, `data` — событие без `k`), каждые
    `PING_SEC` без событий — `: ping`; кончается на `done` или `offline` (`{}`). Отказы до потока (JSON): 422
    `text` «задание пустое или длиннее 100 000 знаков», 422 `model` «такой модели нет на компьютере», 422
    `effort` «такого усилия нет на компьютере», и всё из `ask`.
  - `GET …/chats/{chat}/live` → SSE: хода нет — одно `event: idle` `{}`; идёт — события с `snapshot` первым.
  - `POST …/chats/{chat}/answer` `{"request_id": ^[A-Za-z0-9_-]{1,64}$, "decision": "allow" | "always" | "deny",
    "message": str | null (до 2000, только при deny), "answers": {str: str} | null (до 20 пар по 2000)}` → 204.
  - `POST …/chats/{chat}/stop` → 204.
  - Поток — `class _Watch(StreamingResponse)` по образцу `chat._EventStream`: `unwatch` и `aclose()` генератора
    в `finally` его `__call__` (генератор может не начаться).

- [ ] **Step 1: Тесты** (`with s.c.stream("POST", url, json=…) as r: events = events_of(r.iter_lines())`,
  `events_of` — из `tests/test_chat.py`):
  - `test_turn_streams_until_done`: `script = [text «При», text «вет», step, result, done]` → `turn`, `text`,
    `text`, `step`, `result`, `done`; машина получила `turn` с `text`, `model: None`, `effort: None`.
  - `test_turn_checks_text_model_and_effort`: `""`, `" "`, 100 001 знак, `model: "gpt"`, `effort: "max"` — 422,
    машина без `turn`; `model: "sonnet"`, `effort: "high"` — доходят до машины.
  - `test_second_turn_in_a_running_chat_is_409_running`.
  - `test_permission_card_round_trip`: `script = [text, ask(permission, request_id "r1", always true), text, done]`;
    поток дошёл до `ask` → `POST answer {"request_id": "r1", "decision": "always"}` — 204 → `asked`, `text`,
    `done`; у машины `answer` с `decision: "always"`, `message: None`.
  - `test_question_answers_reach_the_bridge`: `ask` вида `question` → `answers: {"Какой цвет?": "Синий"}` →
    машина получила их; `deny` с `message` — тоже.
  - `test_two_watchers_see_the_card_and_its_close`: поток хода и второй `live` (первым — `snapshot` с открытым
    `ask`); ответ — `asked` в обоих.
  - `test_live_of_an_idle_chat_says_idle`.
  - `test_stop_ends_the_turn_stopped`: `ask` в потоке → `POST stop` — 204 → `asked`, `done {"status": "stopped"}`.
  - `test_lost_link_ends_streams_with_offline`: посреди потока `fake_bridge.drop` → `offline`, поток кончился;
    `answer` после — 409 `offline`.
  - `test_answer_needs_no_memory_of_the_card`: `answer` с `request_id`, которого служба не видела, доходит до
    машины; та отвечает `gone` — 410.
  - `test_idle_stream_pings` (`code.PING_SEC = 0.01`): между событиями — `ping`.
  - `test_turn_text_never_reaches_the_log`.
- [ ] **Step 2: Запустить** `tests/test_code.py` — новые падают.
- [ ] **Step 3: Реализовать** по Interfaces: очередь наблюдения заводится **до** просьбы (`watch`, затем `ask`;
  отказ — `unwatch` и ошибка), ожидание очереди — `asyncio.wait_for(q.get(), PING_SEC)`.
- [ ] **Step 4: Запустить** `tests/test_code.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: ход потоком, карточки разрешений и вопросов, остановка».

---

### Task 6: Файлы, правка, просмотр PDF, git

**Files:**
- Modify: `app/code.py`
- Test: `tests/test_code.py` (раздел `# ---- файлы ----`)

**Interfaces:**
- Produces:
  - `def check_path(path: object) -> str` — строка до `PATH_MAX`, `""` или части через `/` без пустых, `.`,
    `..`, без `\`, без управляющих, не с `/`; иначе 422 `path` «неверный путь».
  - `POST /api/code/agents/{aid}/projects/{pid}/files` `{"path"}` → `data` просьбы `files`.
  - `POST …/read` `{"path"}` → `{"text", "version"}`.
  - `PUT …/file` — тело читается `auth.small_body(request, FILE_BODY_MAX)` (больше — 413 `too_large` «файл
    больше 2 МиБ» без моста), JSON `{"path", "text", "version"}`; `len(text.encode()) > FILE_MAX` — то же 413;
    → `{"version"}`; `conflict` — 409 с `version` моста.
  - `POST …/pdf` `{"path", "page": int ≥ 1}` → `image/png`, заголовок `X-Studio-Pages: <число>`; PDF — просьба
    `raw`, base64 → байты (битый base64 — 502 `bridge`), не `%PDF-` — 415 `not_pdf` «это не PDF»; кэш
    `PDFS: dict[int, tuple[tuple, bytes, int, float]]` (пользователь → ключ `(aid, pid, path)`, байты, страниц,
    время) на `PDF_TTL_SEC`; страниц — `ocr_render.pdf_page_count`, страница — `ocr_render.render_page(data,
    page - 1)`, обе — `asyncio.to_thread`; `ScanError` — 422 со словами; `page` больше числа — 404 `page`
    «нет такой страницы».
  - `GET /api/code/agents/{aid}/projects/{pid}/git` → `data` просьбы `git`.

- [ ] **Step 1: Тесты** (машина: «alpha» с `README.md`, `src/app.py`, `docs/a.pdf` — двухстраничный PDF из
  `tests/scans.py`, `bin/x.dat` с NUL):
  - `test_files_list_and_read_and_save`: корень, `src`; `read` `src/app.py` — текст и `version`; `PUT` с этим
    `version` — новый `version`, у машины новые байты; второй `PUT` со старым — 409 `conflict` с `version`.
  - `test_bad_paths_never_reach_the_bridge`: `"../x"`, `"/etc"`, `"a\\b"`, `"a//b"`, `"./a"`, `"a/.."`,
    `"a\x00b"`, `"x" * 1025`, `5` — 422 `path`, машина пуста.
  - `test_bridge_refusals_map_to_statuses`: `outside` — 403, `binary` — 415, `too_large` — 413, `not_found` — 404.
  - `test_file_body_over_the_limit_is_413_before_the_bridge`.
  - `test_pdf_pages_render_once_per_fetch`: страница 1 — PNG (`\x89PNG`), `X-Studio-Pages: 2`; страница 2 — без
    новой просьбы `raw`; часы + `PDF_TTL_SEC` — новая просьба; страница 3 — 404 `page`; `README.md` — 415.
  - `test_git_status_passes_through_and_not_git_is_409`.
  - `test_file_paths_never_reach_the_log`.
- [ ] **Step 2: Запустить** — новые падают.
- [ ] **Step 3: Реализовать** по Interfaces; `conftest.studio` очищает `code.PDFS`.
- [ ] **Step 4: Запустить** `tests/test_code.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: файлы, правка, PDF и git на сервере».

---

### Task 7: Push: ключ VAPID, шифрование, подписка устройства, поводы

**Files:**
- Create: `app/push.py`, `tests/test_push.py`
- Modify: `app/bridge.py` (`deliver`: повод push), `app/main.py` (`include_router(push.router)`),
  `tests/fakes.py` (`FakeGateway`: хосты push), `CLAUDE.md` («Правила» — исключение из списка адресов)

**Interfaces:**
- Produces в `push.py`:
  - `HOSTS = ("fcm.googleapis.com", ".push.apple.com", "updates.push.services.mozilla.com", ".notify.windows.com")`
    — точное имя или окончание (с точкой); `def allowed(endpoint: str) -> bool` — `https`, без порта, без
    пользователя, хост из `HOSTS`, до `ENDPOINT_MAX`.
  - `def b64u(data: bytes) -> str`, `def unb64u(text: str) -> bytes` (без `=`, с допуском `=`).
  - `def encrypt(payload: bytes, p256dh: bytes, auth: bytes, *, salt: bytes | None = None,
    private: ec.EllipticCurvePrivateKey | None = None) -> bytes` — RFC 8291 + RFC 8188 одной записью
    (алгоритм ниже).
  - `def vapid_key() -> ec.EllipticCurvePrivateKey` — `settings` `vapid` (`security.encrypt` от 32 байт
    закрытого числа в base64url); нет — новый; не расшифровывается (`InvalidToken`, сменили `STUDIO_DATA_KEY`) —
    новый, `DELETE FROM push_subs`, предупреждение «push: ключ VAPID заменён, подписки сброшены»; держится в
    модуле `KEY` (тесты очищают).
  - `def public_key() -> str` — base64url несжатой точки (65 байт).
  - `def vapid_header(endpoint: str) -> str` — `vapid t=<JWT>, k=<public_key()>`; JWT ES256: заголовок
    `{"typ": "JWT", "alg": "ES256"}`, поля `{"aud": "<схема>://<хост>", "exp": int(clock.NOW()) + JWT_SEC,
    "sub": state.CFG.origin}`; подпись — `decode_dss_signature` → `r || s` по 32 байта.
  - `async def notify(user_id: int, body: str, href: str, tag: str) -> None` — каждой подписке пользователя
    `POST` через `state.HTTP` с заголовками `TTL: 3600`, `Urgency: high`, `Content-Encoding: aes128gcm`,
    `Content-Type: application/octet-stream`, `Authorization: vapid_header(...)`; тело — `encrypt` JSON
    `{"title": "Claude Code", "body", "href", "tag"}`; 404 и 410 — подписка удаляется; прочие отказы и сбои сети —
    «push: ответ <код>» / «push: сбой сети», без адреса. `def notify_later(...) -> None` — задачей в
    `bridge.TASKS`-подобном `TASKS`.
  - `router = APIRouter(prefix="/api")`: `GET /api/me/push` → `{"key": public_key(), "subscribed": bool}`
    (есть ли подписка у сессии запроса); `POST /api/me/push` `{"endpoint", "keys": {"p256dh", "auth"}}` → 204
    (`allowed` — иначе 422 `endpoint` «адрес не известной службы push»; `p256dh` — 65 байт с `0x04`, `auth` —
    16 байт, иначе 422 `keys` «неверные ключи подписки»; адрес уже есть — строка переходит этой сессии и
    учётке); `DELETE /api/me/push` → 204 (подписки сессии).
- Produces в `bridge.deliver`: событие `ask` или `done` чата, у которого нет наблюдателей
  (`not watching(aid, chat)`), — `push.notify_later(link.user_id, body, f"code/{aid}/{project}/{chat}",
  f"code-{chat}")`, тело — по разделу «Тексты» (`ask` `permission` — «ждёт подтверждения», `question` —
  «вопрос»; `done` по `status`).
- Produces в `tests/fakes.py`: запросы `FakeGateway.handler` к хостам `push.HOSTS` пишутся в
  `self.push_requests` (до общего `self.requests`) и отвечают `httpx.Response(self.push_status)`
  (`push_status = 201`).

Шифрование (`encrypt`), всё — `cryptography` (HMAC-SHA256 — `hmac`/`hashlib` стандартной библиотеки):

```text
ua_pub   = p256dh (65 байт), as_key = private или ec.generate_private_key(SECP256R1)
as_pub   = as_key.public_key() в несжатой точке (65 байт)
shared   = as_key.exchange(ECDH, ua_pub как ключ)
prk_key  = HMAC(auth, shared)
ikm      = HMAC(prk_key, b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")          # 32 байта
salt     = salt или os.urandom(16)
prk      = HMAC(salt, ikm)
cek      = HMAC(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
nonce    = HMAC(prk, b"Content-Encoding: nonce\x00\x01")[:12]
body     = salt + (4096).to_bytes(4, "big") + bytes([65]) + as_pub + AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
```

- [ ] **Step 1: Тесты** `tests/test_push.py`:
  - `test_encrypt_matches_rfc8291_appendix_a`: значения из RFC 8291, Appendix A — дословно с
    https://www.rfc-editor.org/rfc/rfc8291#appendix-A (открытый текст, `as_private`, `ua_public`, `salt`,
    `auth_secret`, итог); `encrypt(..., salt=..., private=<as_private>) == unb64u(<итог>)`.
  - `test_vapid_header_is_a_valid_es256_jwt`: разобрать `t`, проверить подпись открытым ключом (`r||s` →
    `encode_dss_signature`), `aud == "https://fcm.googleapis.com"`, `exp == T0 + 43200`,
    `sub == "https://testserver"`, `k == public_key()`.
  - `test_vapid_key_is_stored_encrypted_and_replaced_when_unreadable`: в `settings` нет сырого ключа; смена
    `state.CFG` на новый `STUDIO_DATA_KEY` и `push.KEY = None` — ключ другой, `push_subs` пуста.
  - `test_only_known_push_hosts_are_accepted`: `http://fcm.googleapis.com/x`, `https://fcm.googleapis.com.evil.io/x`,
    `https://evil.io/fcm.googleapis.com`, `https://u@fcm.googleapis.com/x`, `https://fcm.googleapis.com:8443/x`,
    `https://10.77.1.10/x` — 422 `endpoint`; `https://fcm.googleapis.com/fcm/send/a`,
    `https://web.push.apple.com/b`, `https://updates.push.services.mozilla.com/wpush/v2/c`,
    `https://db5p.notify.windows.com/w/?token=d` — 204; кривые ключи — 422 `keys`.
  - `test_subscription_lives_with_its_session`: после `POST /logout` строки нет; `GET /api/me/push` другой сессии —
    `subscribed: false`.
  - `test_notify_sends_an_encrypted_payload_and_forgets_gone_subscriptions`: подписка с ключами тестовой пары;
    `notify` → запрос с нужными заголовками, тело расшифровывается тестом (обратный ход по RFC: ECDH своим
    закрытым ключом) в `{"title": "Claude Code", "body", "href", "tag"}`; `push_status = 410` — подписки нет.
  - `test_cards_and_ends_push_only_without_watchers` (`fake_bridge`): `emit` `ask` без наблюдателей — один
    запрос push с телом «Рабочий ПК: ждёт подтверждения»; при открытом `live` — ни одного; `done` `stopped` —
    «Рабочий ПК: ход прерван».
  - `test_push_log_holds_no_endpoint`.
- [ ] **Step 2: Запустить** `tests/test_push.py` — падают.
- [ ] **Step 3: Реализовать** по Interfaces; `conftest.studio` — `push.KEY = None` перед тестом. В `CLAUDE.md`
  («Правила», абзац «Исключение из правила списка разрешённых адресов») дописать: адреса подписок push —
  только `push.HOSTS` (https, без порта и пользователя); адрес присылает браузер, сервер шлёт туда лишь
  зашифрованное уведомление без текстов (`push.allowed`, сторожит `test_push.py`).
- [ ] **Step 4: Запустить** `tests/test_push.py tests/test_code.py tests/test_bridge.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: Web Push — VAPID в базе, RFC 8291, подписка устройства».

---

### Task 8: Стирание истории, выгрузка, удаление учётки

**Files:**
- Modify: `app/history.py` (`code_chats` в перечне `delete_all`), `app/takeout.py` (разделы `code`, `agents`,
  README), `app/static/js/settings.js` (`EXPORT_SECTIONS`, текст окна «Удалить всю историю»)
- Test: `tests/test_code.py` (стирание — у каждого раздела свой тест в своём файле), `tests/test_takeout.py`,
  `tests/test_profile_admin.py`

**Interfaces:**
- `takeout.SECTIONS` += `"code", "agents"` (в конец); `TABLES["code"] = ("code_chats", _code_chats)`,
  `TABLES["agents"] = ("agent_events", _agent_events)`; `_media_rows` пропускает разделы без `REFS`;
  `_code_chats` — `agent_id`, `project`, `session_id`, `state`, `created`, `updated`; `_agent_events` — `id`,
  `created`, `kind`, `agent_id`, `agent_name`, `device`, `ip`. README — строки: «- code_chats/ — чаты Claude Code:
  компьютер, проект (id), номер сессии, открыт или закрыт; текста нет — он на компьютере.» и «- agent_events/ —
  история подключений компьютеров Claude Code.» В «Ключей … нет» дописать «ключей устройств».

- [ ] **Step 1: Тесты:**
  - `test_wipe_forgets_code_chats_but_keeps_agents_events_and_push`: после `DELETE /api/history` — `code_chats`
    пуста, `agents`, `agent_events`, `push_subs` целы; машина не получила ни одной просьбы.
  - `test_export_has_code_chats_and_agent_events`: архив с `"sections": ["code", "agents"]` — `code_chats/0001.json`,
    `agent_events/0001.json` с полями из Interfaces; `key_hash`, ключа, адреса подписки в архиве нет
    (дописать в `test_export_holds_no_secrets`).
  - `test_deleting_a_user_drops_its_links_and_rows` (`test_profile_admin.py`): удаление учётки в панели —
    `InProcessBridge.closed_with == 4401`, строк в четырёх таблицах нет.
- [ ] **Step 2: Запустить** — падают.
- [ ] **Step 3: Реализовать**; `EXPORT_SECTIONS` — пары `['code', 'Claude Code: чаты'], ['agents', 'Claude Code:
  подключения']`; текст окна стирания — по разделу «Тексты».
- [ ] **Step 4: Запустить** те же файлы и браузерные `-k "wipe or export"` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: стирание истории и выгрузка».

---

### Task 9: Apache — канал WebSocket и поток раздела

**Files:**
- Modify: `deploy/apache-vhost.conf`, `tests/test_deploy.py`

**Interfaces:**
- Правила раньше `ProxyPass /`, с комментариями по образцу соседних:
  - `ProxyPass /api/agent/link unix:/run/studio/web.sock|http://studio-backend/api/agent/link upgrade=websocket timeout=300`
    (тишину рвут штатные пинги службы каждые 20 с) и `ProxyPassReverse` тем же адресом;
  - `ProxyPass /api/code/ unix:/run/studio/web.sock|http://studio-backend/api/code/ timeout=600 flushpackets=on`,
    `ProxyPassReverse`, `SetEnvIf Request_URI "^/api/code/" no-gzip=1`.

- [ ] **Step 1: Тесты** в `tests/test_deploy.py`: `test_vhost_details` — обе строки дословно и `SetEnvIf`;
  `test_vhost_is_public_and_streams_chat` — оба правила раньше `ProxyPass / `; новый
  `test_bridge_link_and_code_stream_routes`: `_route_timeout("/api/agent/link") == 300`,
  `_route_timeout("/api/code/agents/1/projects/a/chats/b/turn") == 600 > bridge.CALL_SEC`,
  `"upgrade=websocket"` только в правиле канала; в `test_apache_lets_through_the_largest_body_the_studio_accepts`
  — `limit >= code.FILE_BODY_MAX`.
- [ ] **Step 2: Запустить** `tests/test_deploy.py` — падают.
- [ ] **Step 3: Реализовать** правила.
- [ ] **Step 4: Запустить** `tests/test_deploy.py` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: Apache — канал моста и поток раздела».

---

### Task 10: Страница: «Компьютеры» в настройках, push на устройстве

**Files:**
- Modify: `app/static/js/settings.js` (`agentsBlock`, `notifyBlock`), `app/static/sw.js` (`push`),
  `app/static/css/app.css`
- Test: `tests/test_ui_browser.py` (раздел `# ---- Claude Code ----`), `tests/test_static.py` (если там сверяют
  `sw.js`)

**Interfaces:**
- `agentsBlock(host)` в «Прочем» после `notifyBlock()`: `#agents` — `li[data-id]` с именем, «на связи» / «не на
  связи», значком «PIN», кнопкой `.agent-revoke` → диалог `#agent-revoke-dialog` с `#agent-revoke-confirm`;
  `#agent-events` — `li` «<время> · <имя> · <событие> · <device> · <ip>», `#agent-events-more` «Показать ещё»
  (по `next`). Данные — `GET /api/agents`, `GET /api/agents/events`.
- `notifyBlock()`: второй флажок `#push-enabled` с подписью и подсказкой из «Текстов»; включение —
  `navigator.serviceWorker.ready` → `GET /api/me/push` → `pushManager.subscribe({userVisibleOnly: true,
  applicationServerKey: <байты key>})` → `POST /api/me/push` с `subscription.toJSON()`; выключение —
  `unsubscribe()` и `DELETE /api/me/push`; отмечен — флаг устройства `localStorage` `studio-push` = `'1'` и
  `subscribed` с сервера; нет `PushManager` — флажок выключен, подсказка про экран «Домой».
- `sw.js`: `self.addEventListener('push', …)` — `event.data.json()` (сбой — `{}`) →
  `self.registration.showNotification(data.title || 'Студия', { body, tag, data: { href } })`; нажатие — прежний
  `notificationclick`. Комментарий в шапке — без «push здесь нет».

- [ ] **Step 1: Браузерные тесты** (`live`, `browser`; компьютеры — `fake_bridge.SocketBridge`):
  - `test_settings_lists_computers_and_revokes_one`: два компьютера, один на связи; «Отозвать» → диалог →
    подтверждение — строка ушла, мост получил 4401, в истории «отозван».
  - `test_settings_push_toggle_subscribes_this_device`: до нажатия подменить
    `PushManager.prototype.subscribe` и `getSubscription` JS-заглушками с верными по виду ключами и адресом
    `https://fcm.googleapis.com/fcm/send/test`; включение — строка `push_subs` у сессии; выключение — строки нет.
- [ ] **Step 2: Запустить** их — падают.
- [ ] **Step 3: Реализовать** по Interfaces (стили — светлая и тёмная тема, ширина 320 px).
- [ ] **Step 4: Запустить** новые и прежние `-k "login or menu or settings or notify or wipe or export or shell"` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: компьютеры и push в настройках».

---

### Task 11: Раздел «Код»: компьютеры, PIN, проекты, чаты проекта

**Files:**
- Create: `app/static/js/code.js`
- Modify: `app/static/index.html` (вкладка рельса после «Перевода»), `app/static/js/app.js` (`SECTIONS.code`,
  `ICONS.code`, `allowed`: у `code` хвост адреса допустим), `app/static/css/app.css`
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Вкладка: `<a class="tab" href="#/code" data-section="code">` со значком `<path d="M4 6l5 5-5 5M11 18h9"/>` и
  подписью «Код»; `SECTIONS.code = { title: 'Claude Code', load: () => import('./code.js') }`.
- Адреса: `#/code` — компьютеры; `#/code/<aid>` — проекты; `#/code/<aid>/<pid>` — проект; `#/code/<aid>/<pid>/<chat>`
  — чат (рисует `code-chat.js`, Task 12). `code.js` экспортирует `mount`, `update`, `unmount`; хвост `ctx.id`
  делится по `/`.
- Компьютеры — `#code-agents li[data-id]`; пусто — `#code-empty`. Нажатие на компьютер на связи — `POST …/open`
  с `pin: null`; 409 `pin_needed` — панель `#code-pin` (поле `#code-pin-input`, `inputmode="numeric"`,
  `autocomplete="off"`, кнопка `#code-pin-open`, слова `#code-pin-error`); 204 — `#/code/<aid>`. Набранный PIN —
  только в поле и переменной, после ответа поле очищается.
- Любой ответ 409 `closed` в разделе — переход на `#/code` с панелью PIN этого компьютера.
- Проекты — `#code-projects li[data-pid]`. Проект: `#code-new-chat` («Новый чат» → `POST …/chats` →
  `#/code/<aid>/<pid>/<chat>`), вкладки `#code-tab-chats`, `#code-tab-files`, `#code-tab-git` (две последние
  рисует `code-files.js`, Task 13), чаты — `#code-chats li[data-chat]` с названием (`null` — «Новый чат»),
  временем, пометками `.code-origin` («студия» / «VS Code или терминал»), «закрыт», «идёт ход», «ждёт ответа»,
  кнопками `.code-chat-close` / `.code-chat-reopen` (`PATCH`).

- [ ] **Step 1: Браузерные тесты** (машина `SocketBridge`, PIN `"2580"`, проекты «alpha», «beta»):
  - `test_code_section_lists_computers_and_says_when_there_are_none` (ширина 320 и 1280).
  - `test_code_pin_pad_counts_down_and_opens`: «1111» — «Неверный PIN — осталось 4 попытки»; «2580» — проекты.
  - `test_code_offline_computer_shows_only_its_row`.
  - `test_code_project_lists_chats_with_marks_and_closes_one`: «студия» / «VS Code или терминал»; «Закрыть» —
    «закрыт» и в конце списка; «Открыть снова».
  - `test_code_new_chat_opens_its_page`.
  - `test_code_lock_and_unlock_asks_for_the_pin_again`: блокировка студии (как в сценариях блокировки) и
    отпирание — следующий переход по проекту ведёт к панели PIN.
- [ ] **Step 2: Запустить** — падают.
- [ ] **Step 3: Реализовать** по Interfaces.
- [ ] **Step 4: Запустить** новые и прежние `-k "login or menu or settings or shell or translate"` — PASS.
- [ ] **Step 5: Коммит** «Claude Code: раздел — компьютеры, PIN, проекты, чаты».

---

### Task 12: Чат: лента, ход, карточки, остановка

**Files:**
- Create: `app/static/js/code-chat.js`
- Modify: `app/static/js/code.js` (уровень чата), `app/static/css/app.css`
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- `export function chatView(host, { aid, pid, chat, agent, project, origin, navigate, openFile }) -> { unmount() }`;
  `agent` — строка `GET /api/agents`, `project` — `{pid, name}`, `origin` — из списка чатов проекта (`code.js`
  берёт его и при прямом переходе по адресу из push), `openFile(path)` — вкладка «Файлы» с этим файлом.
- `origin: "other"` — над лентой `#code-shared-note` со словами из «Текстов».
- Шаг `Read`, `Edit`, `Write` с `input.file_path` внутри проекта (начало — `agent.projects_root` + `\` +
  `project.name`, без учёта регистра; хвост, `\` → `/`) — ссылка `.code-step-file` «Открыть файл» →
  `openFile(хвост)`.
- Лента `#code-feed`: сообщения `GET …/chats/<chat>` — `.code-msg.user` (текст как есть), `.code-msg.assistant`
  (текст — `markdown.js`, как в чате студии), шаги — `details.code-step` (`summary` — инструмент и начало ввода,
  внутри — ввод JSON и итог; ошибка — класс `error`); `#code-more` «Показать раньше» (`before = start`).
- Открытие чата — сразу `GET …/live`: `snapshot` заменяет хвост текущего хода, `idle` — ничего.
- Поле — `fieldBox` из `field.js` (`#code-text`), `#code-send`, `#code-stop` (виден, пока идёт ход), выбор
  `#code-model` и `#code-effort` из `models`/`efforts` компьютера (`GET /api/agents`); выбор помнится на
  устройстве `localStorage` `studio-code-choice:<aid>` (`{model, effort}`) и берётся новым чатом;
  `#code-close-chat` («Закрыть чат» → `PATCH` и назад к проекту).
- Поток: `text` — дописать в ответ хода; `step` — новый `details`; `result` — итог в шаг по `id`; `ask` —
  карточка `.code-ask[data-request]`: разрешение — «Claude Code просит разрешения», инструмент и ввод,
  `.code-ask-yes`, `.code-ask-always` (только при `always`), `.code-ask-no` и `textarea.code-ask-instead`;
  вопрос — варианты (`multi` — флажки, иначе переключатели) и `.code-ask-answer`; нажатие — `POST …/answer`,
  кнопки карточки — `disabled` до ответа; `asked` — карточка уходит; `done` — `stopped`: «Ход прерван»,
  `error`: «Ошибка: <message>»; `offline` — `#code-status` «Компьютер пропал со связи — жду его» и повтор `live`
  раз в 5 с, пока страница видна.
- Ничего из ленты и поля — в `localStorage`/`sessionStorage` (кроме выбора модели и усилия).

- [ ] **Step 1: Браузерные тесты** (`SocketBridge`, машина со `script`):
  - `test_code_turn_streams_text_and_steps`: «Отправить» → текст по кускам, шаг `Bash` с итогом, кнопка
    «Остановить» пропала после `done`.
  - `test_code_permission_card_yes_and_always`: карточка → «Да» → ход дошёл до `done`; у машины `decision:
    "allow"`; второй ход — «Да, больше не спрашивать» → `always`.
  - `test_code_permission_no_with_words`: «Нет» с текстом — у машины `deny` и `message`.
  - `test_code_question_card_sends_answers`.
  - `test_code_stop_and_offline`: «Остановить» — «Ход прерван»; затем мост остановлен — «Компьютер пропал со
    связи — жду его», мост снова на связи — надпись ушла.
  - `test_code_reopened_chat_shows_history_and_more`: 250 сообщений — 100 последних, «Показать раньше» — ещё 100.
  - `test_code_choice_of_model_is_remembered_for_a_new_chat`.
  - `test_code_foreign_session_warns_about_two_writers`: чат B («VS Code или терминал») — `#code-shared-note`;
    чат A («студия») — без неё.
  - Ширина 320 — карточка и поле без горизонтальной прокрутки.
- [ ] **Step 2: Запустить** — падают.
- [ ] **Step 3: Реализовать** по Interfaces (`readSSE` из `sse.js`, `api.js` для JSON).
- [ ] **Step 4: Запустить** новые и Task 11 — PASS.
- [ ] **Step 5: Коммит** «Claude Code: чат — поток хода, карточки, остановка».

---

### Task 13: Файлы, правка, просмотр `.md` и `.pdf`, git

**Files:**
- Create: `app/static/js/code-files.js`
- Modify: `app/static/js/code.js` (вкладки «Файлы», «Git»), `app/static/css/app.css`
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- `export function filesView(host, { aid, pid }) -> { open(path), unmount() }` — `open` показывает папку
  файла и открывает его (нужен `openFile` чата, Task 12: `code.js` переключает вкладку и зовёт `open`);
  `export function gitView(host, { aid, pid }) -> { unmount() }`. До этой задачи `openFile` в Task 12 — заглушка,
  ссылка видна, но ведёт только на вкладку «Файлы».
- Файлы: `#code-path` (хлебные крошки), `#code-files li[data-name]` (папки сначала); текстовый —
  `#code-editor` (`textarea`, моноширинный), `#code-save`, `#code-file-status` («Сохранено» / слова 409
  `conflict`); `.md` — переключатель `#code-md-view` «Текст» / «Вид» (`markdown.js`); `.pdf` — `#code-pdf img`
  из `POST …/pdf` (blob URL, прежний освобождается), `#code-pdf-prev`, `#code-pdf-next`, `#code-pdf-page`
  «Страница N из M»; `binary` и `too_large` — слова из «Текстов».
- Git: `#code-git` — ветка, «впереди на N» / «позади на N» (если есть `upstream`), список `path` и `status`;
  пусто — «Изменений нет»; 409 `not_git` — «Не репозиторий git».

- [ ] **Step 1: Браузерные тесты**:
  - `test_code_files_open_edit_and_save`: папка `src`, правка `app.py`, «Сохранено», у машины новые байты.
  - `test_code_file_conflict_is_said_in_words`: машина меняет файл после открытия — слова 409.
  - `test_code_markdown_and_pdf_view`: `README.md` «Вид» — заголовок `h1`; `a.pdf` — картинка, «›» — «Страница
    2 из 2».
  - `test_code_git_panel`.
  - `test_code_step_file_link_opens_the_file`: шаг `Edit` с `file_path` `<projects_root>\alpha\src\app.py` —
    «Открыть файл» → вкладка «Файлы», в редакторе `app.py`; шаг с путём вне проекта — без ссылки.
- [ ] **Step 2: Запустить** — падают.
- [ ] **Step 3: Реализовать** по Interfaces.
- [ ] **Step 4: Запустить** новые и Task 11–12 — PASS.
- [ ] **Step 5: Коммит** «Claude Code: файлы, правка, просмотр и git на странице».

---

### Task 14: Документы, полный прогон, выкладка

**Files:**
- Modify: `README.md` (раздел «Claude Code»: что умеет, привязка моста — ссылкой на будущий `studio-agent`,
  PIN и его срок, push и его подсказка), `CLAUDE.md`, `FINDINGS.md`/`IDEAS.md` (если по ходу всплыло)
- Create: `C:/work/projects/local-config/wiki/projects/studio/architecture-claude-code.md` (устройство, протокол
  ссылкой, решения, ловушки; не меньше двух `[[wikilinks]]`, строка в `_log.md` проекта вики)

**Interfaces:**
- `CLAUDE.md`, «Устройство»: модули `agents`, `bridge`, `code`, `push` абзацем по образцу «Перевода» (спека,
  протокол, шаг схемы 19, пределы, `pid` и пути только в теле, 409 `closed` вместо 423, `forget` при
  блокировке, подставной мост `tests/fake_bridge.py`); «Ловушки»: `upgrade=websocket` у канала, `guard` не видит
  WebSocket, ключ VAPID в `settings` шифром `STUDIO_DATA_KEY`; «Откат на код до шага схемы 19»: прежний код
  таблиц `agents`, `agent_events`, `code_chats`, `push_subs` не читает; мосты получают отказ рукопожатия и
  переподключаются; «Удалить всю историю» прежнего кода `code_chats` не стирает — вернувшись вперёд, стереть
  снова; каскады удаления учётки и сессий работают (FK в схеме).

- [ ] **Step 1: Документы** по Interfaces.
- [ ] **Step 2: Быстрый набор** `.venv/Scripts/python -m pytest -q` — PASS в бюджете 60 с (`--durations=15`:
  новые тесты дольше 1 с — чинить или в `integration`).
- [ ] **Step 3: Браузерные** `-m integration tests/test_ui_browser.py -n0 -k "code or computers or push or
  socket_bridge or settings or wipe or export"` — PASS.
- [ ] **Step 4: Коммит** «Claude Code: README, CLAUDE.md, вики».
- [ ] **Step 5: Версия Apache** на debian: `ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.10 apache2 -v` —
  не ниже 2.4.47 (`upgrade=websocket` у `mod_proxy_http`); ниже — остановиться и сказать владельцу.
- [ ] **Step 6: Выкладка** `bash deploy/deploy.sh` — `/health` 200 через Apache.
- [ ] **Step 7: Смоук канала через Apache**: с workstation `websockets.sync.client.connect("wss://studio.example.com/api/agent/link",
  additional_headers={"Authorization": "Bearer wrong"})` → закрытие с кодом 4401 (рукопожатие прошло Apache и
  sslh); `sudo journalctl -u studio-web --since "-5min"` — строка «канал: неизвестный ключ», ключа в ней нет.
- [ ] **Step 8: Итог владельцу**: что выложено, что проверено смоуком; `git push` — только по его слову (ночной
  `ClaudeGitPushAll` отправит и сам), `cld` догоняет сессия на build-host (CLAUDE.md).
