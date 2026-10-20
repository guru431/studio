# Блокировка на телефоне — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** студия на телефоне и планшете запирается через минуту после ухода с экрана, сервер не отдаёт запертой сессии ничего, кроме запросов блокировки, а открывается она passkey (Face ID, отпечаток, код устройства) или паролем учётки.

**Architecture:** признак «заперто» считает `auth.guard` по трём полям (`users.screen_lock`, `sessions.mobile`, `sessions.seen`) и отвечает 423 на всё `/api/*`, кроме путей блокировки. Новый модуль `app/screenlock.py` — отметки «на экране», «заперто», отпирание паролем и passkey, ключи учётки, включение и выключение. В браузере `app/static/js/lock.js` ведёт шторку, экран блокировки (оба — `<dialog>` поверх окон просмотра), отметки и ожидание отпирания для `api.js`; блок «Блокировка» — в «Настройки → Прочее».

**Tech Stack:** FastAPI, SQLite, `webauthn==3.0.1` (py_webauthn, Duo Labs), ES-модули без сборки, WebAuthn браузера, CDP (виртуальный аутентификатор) в браузерных тестах.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-screen-lock-design.md](../specs/2026-10-21-studio-screen-lock-design.md)

## Global Constraints

- Время: клиент `AWAY_SEC = 60`, `ALIVE_SEC = 15`; сервер `STALE_SEC = 75`, `CHALLENGE_SEC = 120`, вызов — 32 случайных байта, одноразовый.
- Сессия заперта ⇔ `users.screen_lock = 1` И `sessions.mobile = 1` И (`seen IS NULL` ИЛИ `now - seen > STALE_SEC`).
- `mobile` — защёлка: ставится в 1 отметкой с `touch: true`, не сбрасывается никогда; признак — `matchMedia('(pointer: coarse)')`, не User-Agent.
- Отказ запертой сессии — `423 {"detail": {"code": "screen_locked", "message": "студия заперта"}}`. Код `locked` уже занят паузой (429) — не путать; имена новой части не пересекаются с `auth.LOCK_SEC` / `auth.locked` / `locked_key`.
- Пароль при отпирании, добавлении ключа и выключении блокировки — та же проверка и пауза, что у смены пароля: ключ `("pw", uid)`, 5 неудач — 15 минут, 429 `locked` / 403 `password`.
- passkey: `userVerification: required`, `attestation: none`, RP ID — хост `STUDIO_ORIGIN`, origin — `STUDIO_ORIGIN` без `/` в конце; дескриптор пользователя `f"studio-{uid}".encode()` (не логин и не имя); `user.name` и `displayName` — логин; `rp.name` — «Студия».
- Названия ключей — только из `("iPhone", "iPad", "Android", "Устройство")`.
- Браузер: iOS 16+ — без `PublicKeyCredential.parse*FromJSON` и `toJSON` (они с iOS 18.4); свои base64url-помощники. Вызов `navigator.credentials.*` — в обработчике нажатия без ожидания сети перед ним (Safari).
- Журнал — только события с id; никогда credential id, открытый ключ, вызов, clientDataJSON, подпись, пароль.
- Шаг базы только добавляет (откат `install.sh`); `requirements.txt` — точные пины (`test_requirements_are_exact_pins`).
- Тексты интерфейса: «Студия заперта», «Открыть», «Открыть паролем», «Выйти», «Запирать на телефоне и планшете через минуту после ухода с экрана», «Добавить Face ID / отпечаток этого устройства».
- Время в тестах: сервер — `s.clock` фикстуры `studio`; браузер — подмена `Date.now`, без ожидания минут.

## Review Focus

1. Safari теряет «нажатие», если перед `navigator.credentials.get/create` ждали сеть, — тогда Face ID на iPhone не появится, хотя в Chrome всё работает. Ожидание: варианты (`options`) готовы до нажатия. Тест — задача 5 (`test_open_calls_the_passkey_without_waiting_for_the_network`), задача 4 (два шага добавления).
2. Escape закрывает `<dialog>` — экран блокировки и шторка закрываться не должны. Тест — задача 5 (Escape на открытом `#lock`).
3. Открытое окно просмотра (`#viewer`, тоже `showModal`) поверх шторки — картинка видна в переключателе приложений. Ожидание: шторка выше. Тест — задача 5 (`test_curtain_covers_an_open_viewer`).
4. Несколько запросов получили 423 разом (опросы ленты) — один экран блокировки, все запросы повторены после одного отпирания. Тест — задача 5 (`test_requests_during_the_lock_wait_and_are_retried`).
5. Сессию закрыли (выход на другом устройстве, смена пароля), пока студия заперта: запросы блокировки получают 401 — человек должен попасть на вход, а не застрять. Тест — задача 2 (`test_lock_state_without_a_session_sends_to_login`).

---

### Task 1: Шаг базы 7 и признак «заперто» в `auth.guard`

**Files:**
- Modify: `app/db.py` (VERSION 7, `SCHEMA`, `STEPS[7]`, строка версии в докстринге)
- Modify: `app/auth.py` (`_live`, `create_session`, `guard`, новые имена ниже)
- Modify: `tests/test_foundation.py` (`DROP_V7`, версии 6 → 7, именованные столбцы в `test_deleting_a_user_cascades`)
- Modify: `tests/test_auth.py:314` (именованные столбцы в `INSERT INTO sessions`)
- Create: `tests/test_screenlock.py`

**Interfaces:**
- Produces: `auth.STALE_SEC = 75`; `auth.SCREEN_LOCK_PATHS = frozenset({"/api/lock", "/api/lock/alive", "/api/lock/now", "/api/lock/challenge", "/api/lock/open", "/api/lock/password"})`; `auth.screen_locked(enabled: int, mobile: int, seen: float | None, now: float) -> bool`; `_live(...) -> tuple[User, str, bool, bool] | None` (4-й — заперта ли); `guard` кладёт `request.state.locked: bool` и `request.state.digest: str` (sha256 cookie); `create_session` пишет `seen = now`.
- Таблица `passkeys(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL, transports TEXT NOT NULL, name TEXT NOT NULL, created REAL NOT NULL, used REAL)` + `CREATE INDEX passkeys_user ON passkeys(user_id)`.

- [ ] **Step 1: Тесты**

`tests/test_foundation.py`: `DROP_V7 = "ALTER TABLE users DROP COLUMN screen_lock; ALTER TABLE sessions DROP COLUMN mobile; ALTER TABLE sessions DROP COLUMN seen; DROP TABLE passkeys;"` — в каждый тест старших версий; ожидаемая версия 7; новый тест:

```python
def test_migrate_adds_the_screen_lock_to_a_version_6_base(env):
    # база 6 со строкой users и sessions → migrate → версия 7,
    # users.screen_lock == 0, sessions.mobile == 0, sessions.seen IS NULL, таблица passkeys есть
```

`tests/test_screenlock.py` — помощники `lock_on(uid)` (`UPDATE users SET screen_lock = 1`), `phone(s)` (`UPDATE sessions SET mobile = 1 WHERE digest = <digest cookie s.c>`), `digest(s)`:

```python
def test_new_session_is_open_even_with_the_lock_on(studio):       # lock_on, signin, phone → GET /api/me 200
def test_phone_session_locks_after_stale_sec(studio):
    # signin, lock_on, phone; clock += 75 → /api/me 200; clock += 1 → 423, code "screen_locked";
    # GET /api/media/x → 423; POST /api/connections (с CSRF) → 423
def test_lock_paths_are_not_refused_while_locked(studio):          # для каждого из SCREEN_LOCK_PATHS: GET и POST → != 423
def test_computer_session_is_never_locked(studio):                 # lock_on без phone, clock += 3600 → 200
def test_lock_off_never_locks_a_phone(studio):                     # phone без lock_on, clock += 3600 → 200
def test_lock_refusal_names_no_profile(studio):                    # тело 423 не содержит логин
```

- [ ] **Step 2: Запустить — падают**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_foundation.py tests/test_screenlock.py tests/test_auth.py`
Expected: FAIL (нет столбцов, нет 423).

- [ ] **Step 3: Реализация**

`db.py`: столбцы — в конец `users` и `sessions` в `SCHEMA` (новая и мигрированная базы одинаковы); `passkeys` — константой рядом с `SECTION_TABLES`, в `SCHEMA` и `STEPS[7]`. `auth._live`: SELECT добавляет `u.screen_lock, s.mobile, s.seen`. `guard`: после CSRF, если заперта и путь не в `SCREEN_LOCK_PATHS` — 423 (с `Cache-Control: no-store`); иначе `request.state.locked/digest`. Порядок в guard: сессия → CSRF → блокировка.

- [ ] **Step 4: Запустить — проходят; быстрый набор зелёный**

Run: `.venv/Scripts/python -m pytest -q`
Expected: всё PASS.

- [ ] **Step 5: Коммит** — `Lock phone sessions in the guard after 75 s without marks`

### Task 2: Запросы блокировки без passkey, пароль, включение и выключение

**Files:**
- Create: `app/screenlock.py` (router, prefix `/api`)
- Modify: `app/auth.py` (`confirm_password`), `app/profile.py` (смена пароля через `confirm_password`; `/api/me` + `screen_lock`), `app/main.py` (`include_router(screenlock.router)`)
- Modify: `tests/test_screenlock.py`, `tests/test_profile_admin.py` (ключ `screen_lock` в `set(me)`, значение `False`)

**Interfaces:**
- Consumes: Task 1 (`request.state.locked`, `request.state.digest`, `auth.screen_locked`).
- Produces:
  - `async def auth.confirm_password(uid: int, password: str, what: str, message: str = "пароль неверен") -> None` — 429 `{"code": "locked", "message": "слишком много попыток — подождите 15 минут"}` / 403 `{"code": "password", "message": message}`; журнал: `"пользователь %d: %s — пауза"` / `"… — неверный пароль"`. `change_password` зовёт её с `what="смена пароля", message="текущий пароль неверен"`.
  - `GET /api/lock` → `{"locked": bool, "keys": bool, "csrf": str}` (ровно эти ключи).
  - `POST /api/lock/alive` `{"touch": bool = false}` → `{"locked": bool}`: `touch` → `mobile = 1`; затем заново считает «заперто» по базе; не заперта → `seen = now`.
  - `POST /api/lock/now` → 204, `seen = NULL`; журнал `"пользователь %d: заперто"`.
  - `POST /api/lock/password` `{"password": str ≤1024}` → 204, `seen = now`; журнал `"пользователь %d: открыто (пароль)"`.
  - `POST /api/me/screen-lock` `{"enabled": bool, "password": str | None ≤1024}` → `{"screen_lock": bool}`; выключение — `confirm_password(what="выключение блокировки")`, пустой пароль = неверный.
  - `/api/me` → `"screen_lock": bool`.

- [ ] **Step 1: Тесты** (в `tests/test_screenlock.py`)

```python
def test_mobile_mark_is_a_one_way_latch(studio):                   # alive touch=True → mobile 1; touch=False → всё ещё 1
def test_first_mark_of_an_old_phone_session_finds_it_locked(studio):
    # lock_on; UPDATE sessions SET seen = NULL; alive touch=True → {"locked": True}; /api/me 423
def test_marks_keep_a_phone_open_and_never_open_a_locked_one(studio):
    # phone+lock_on: три раза (clock += 70; alive) → /api/me 200;
    # clock += 76; alive → {"locked": True}; seen в базе не изменился; /api/me 423
def test_lock_now_locks_at_once(studio):                           # phone+lock_on; POST /api/lock/now 204 → /api/me 423
def test_lock_state_gives_csrf_and_keys_but_no_profile(studio):    # запертая: set(GET /api/lock) == {"locked","keys","csrf"}, csrf == signin
def test_lock_state_without_a_session_sends_to_login(studio):      # без cookie: GET /api/lock → 401, code "login"
def test_password_opens_and_shares_the_password_pause(studio):
    # запертая: 5 × неверный → 403 "password"; верный → 429 "locked"; POST /api/me/password → 429;
    # clock += 900; верный → 204; /api/me 200
def test_lock_is_enabled_freely_and_disabled_only_with_the_password(studio):
    # enabled True без пароля → {"screen_lock": True}, /api/me screen_lock True;
    # enabled False без пароля → 403 "password"; с неверным → 403; с верным → {"screen_lock": False}
def test_lock_events_log_only_ids(studio, caplog):                 # пароль не в caplog.text; "заперто"/"открыто" — с id
```

- [ ] **Step 2: Запустить — падают** — `.venv/Scripts/python -m pytest -q -n0 tests/test_screenlock.py tests/test_profile_admin.py` → FAIL (404).
- [ ] **Step 3: Реализация** — по Interfaces; SQL обновляет сессию по `request.state.digest`.
- [ ] **Step 4: Запустить — проходят** — та же команда → PASS.
- [ ] **Step 5: Коммит** — `Mark phones on screen, lock and open them with the password`

### Task 3: Ключи passkey: добавление, список, удаление, отпирание

**Files:**
- Modify: `requirements.txt` (`webauthn==3.0.1`), затем `.venv/Scripts/python -m pip install -r requirements-dev.txt`
- Modify: `app/screenlock.py`
- Create: `tests/softkey.py` (программный аутентификатор)
- Modify: `tests/conftest.py` (`screenlock.CHALLENGES.clear()` до и после, как `auth.FAILS`), `tests/test_screenlock.py`

**Interfaces:**
- Consumes: Task 2 (`confirm_password`, `request.state.digest`).
- Produces:
  - `screenlock.CHALLENGE_SEC = 120`; `screenlock.CHALLENGES: dict[tuple[str, str], tuple[bytes, float]]` — ключ `(digest, "register" | "open")`, выдача заменяет прежний, взятие удаляет; старше `CHALLENGE_SEC` — нет вызова.
  - `screenlock.NAMES = ("iPhone", "iPad", "Android", "Устройство")`.
  - `POST /api/passkeys/options` `{"password"}` → варианты создания (dict из `options_to_json`): `excludeCredentials` — ключи учётки; `confirm_password(what="ключ")`.
  - `POST /api/passkeys` `{"credential": dict, "name": NAMES}` → 201 `{"id", "name", "created", "used"}`; не прошла проверка или нет вызова → 400 `{"code": "passkey", "message": "устройство не подтвердило ключ"}`; такой id уже есть → 409 `{"code": "exists"}`; журнал `"пользователь %d: ключ добавлен"`.
  - `GET /api/passkeys` → `{"items": [{"id", "name", "created", "used"}]}`, новые сверху. `DELETE /api/passkeys/{key_id}` → 204, чужой/нет → 404 `{"code": "not_found"}`.
  - `POST /api/lock/challenge` → варианты входа: `allowCredentials` — ключи учётки (с transports), UV `required`; ключей нет → 409 `{"code": "no_keys"}`.
  - `POST /api/lock/open` `{"credential": dict}` → 204: ключ ищется по `credential["id"]` И `user_id`; `verify_authentication_response(..., require_user_verification=True)`; новый счётчик, `used = now`, `seen = now`; журнал `"пользователь %d: открыто (passkey)"`. Любой отказ → 403 `{"code": "passkey", "message": "ключ не подошёл — нажмите «Открыть» ещё раз"}`, `seen` не меняется.
  - `tests/softkey.py`: `class SoftKey(origin="https://testserver", rp_id="testserver", uv=True)`; `.id: str` (base64url); `.public_key: bytes` (COSE — для вставки в `passkeys` мимо API); `.register(options: dict) -> dict`; `.assertion(options: dict) -> dict` (счётчик растёт на 1 с каждым вызовом).

`SoftKey` (ключ EC P-256 из `cryptography`, CBOR — `cbor2`, он приходит с `webauthn`):
- authData = `sha256(rp_id)` ‖ флаги (`0x01` UP, `| 0x04` UV, если `uv`; `| 0x40` AT при создании) ‖ счётчик (4 байта big-endian) ‖ при создании: AAGUID (16 нулей) ‖ длина id (2 байта) ‖ id ‖ COSE-ключ `{1: 2, 3: -7, -1: 1, -2: x, -3: y}`.
- clientDataJSON: `{"type": "webauthn.create" | "webauthn.get", "challenge": options["challenge"], "origin": origin, "crossOrigin": false}`.
- создание: `attestationObject = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authData})`; ответ `{"id", "rawId", "type": "public-key", "response": {"clientDataJSON", "attestationObject", "transports": ["internal"]}, "clientExtensionResults": {}}`.
- вход: подпись ECDSA-SHA256 (DER) над `authData ‖ sha256(clientDataJSON)`; ответ `{"id", "rawId", "type": "public-key", "response": {"clientDataJSON", "authenticatorData", "signature", "userHandle": null}, "clientExtensionResults": {}}`. Все байты — base64url без `=`.

- [ ] **Step 1: Тесты**

```python
def test_adding_a_key_needs_the_password(studio):
    # без пароля / неверный → 403 "password"; верный → 200; rp.id "testserver", rp.name "Студия",
    # authenticatorSelection.userVerification "required", attestation "none", user.name == логин,
    # логина нет в base64url-декоде user.id
def test_key_is_added_listed_and_excluded_next_time(studio):        # 201 name "iPhone"; список из 1; следующие options: excludeCredentials ids == [key.id]
def test_registration_needs_a_fresh_single_use_challenge(studio):  # повтор того же ответа → 400; новый options, clock += 121 → 400
def test_unknown_key_name_is_refused(studio):                      # name "Мой телефон" → 422
def test_passkey_opens_a_locked_phone(studio):
    # ключ; phone+lock_on; /api/lock/now; challenge: allowCredentials ids == [key.id], userVerification "required";
    # open → 204; /api/me 200; у ключа used == now, sign_count вырос;
    # снова заперли, 5 × неверный пароль (пауза) → passkey всё равно открывает
def test_open_refuses_a_foreign_key_a_replay_no_uv_and_a_stale_challenge(studio):
    # ключ другой учётки → 403; тот же ответ второй раз → 403; SoftKey(uv=False) (добавлен в базу напрямую) → 403;
    # challenge, clock += 121 → 403; после всех /api/me 423
def test_challenge_without_keys_is_409(studio):                    # → 409 "no_keys"
def test_keys_are_removed_one_by_one_and_with_the_account(studio): # DELETE своего → 204, список пуст; чужого → 404; DELETE users → passkeys пусто
def test_passkey_events_log_no_key_material(studio, caplog):       # key.id, вызов, base64 открытого ключа — не в caplog.text
```

- [ ] **Step 2: Запустить — падают** — `.venv/Scripts/python -m pytest -q -n0 tests/test_screenlock.py` → FAIL.
- [ ] **Step 3: Реализация** — функции `webauthn` по сигнатурам 3.0.1 (`generate_registration_options`, `verify_registration_response`, `generate_authentication_options`, `verify_authentication_response`, `options_to_json`, `base64url_to_bytes`); исключения проверки ловить как отказ.
- [ ] **Step 4: Запустить — проходят; быстрый набор зелёный** — `.venv/Scripts/python -m pytest -q` → PASS.
- [ ] **Step 5: Коммит** — `Add passkeys that open a locked phone`

### Task 4: Помощники passkey в браузере и блок «Блокировка» в настройках

**Files:**
- Create: `app/static/js/lock.js` (часть 1)
- Modify: `app/static/js/settings.js` (`lockBlock()` в «Прочее» между `passwordBlock()` и `wipeBlock(host)`), `app/static/css/app.css` (строки списка ключей — в стиле блоков «Прочее»)
- Modify: `tests/cdp_driver.mjs`, `tests/browser.py` (`drive(..., touch: bool = False, authenticator: bool = False)`), `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: Task 2–3 (пути и ответы).
- Produces (`lock.js`): `isTouch(): boolean` (`(pointer: coarse)`); `deviceName(): string` (iPhone → «iPhone»; iPad или Macintosh с `maxTouchPoints > 1` → «iPad»; Android → «Android»; иначе «Устройство»); `b64u(bytes): string`; `unb64u(text): Uint8Array`; `creationOptions(json)`, `requestOptions(json)` — JSON сервера → объект для `navigator.credentials` (декод `challenge`, `user.id`, `excludeCredentials[].id`, `allowCredentials[].id`); `credentialJSON(credential): object` — обратно в форму `SoftKey`; `async passkeyOptions(password): object`; `async addPasskey(options): object` (`create` + `POST /api/passkeys` с `deviceName()`).
- Разметка блока: `#lock-enabled` (checkbox), `#lock-off-password`, `#lock-off` («Выключить»), `#passkeys` (строки `.passkey`: название, «добавлен …», «открывал …» / «ещё не открывал», `.passkey-delete`), `#passkey-password`, `#passkey-next` («Далее»), `#passkey-add` («Добавить Face ID / отпечаток этого устройства», виден после «Далее»), `#lock-status`. Пароль и «Далее» / «Добавить …» — только при `isTouch()`. После смены переключателя — `me.screen_lock` обновлён.
- Драйвер: `job.touch` → `Emulation.setTouchEmulationEnabled({enabled: true, maxTouchPoints: 5})` до перехода; `job.authenticator` → `WebAuthn.enable` и `WebAuthn.addVirtualAuthenticator({options: {protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true}})`. Аутентификатор живёт во вкладке: ключ, добавленный в одном `drive`, в следующем не виден.

- [ ] **Step 1: Тесты** (`tests/test_ui_browser.py`)

```python
def test_touch_emulation_gives_a_coarse_pointer(browser, live):    # touch=True: matchMedia('(pointer: coarse)').matches is True; без touch — False
def test_lock_settings_add_list_and_remove_a_key(browser, live):
    # 390, mobile, touch, authenticator: #/settings/other; клик #lock-enabled → fetch /api/me .screen_lock True;
    # #passkey-password + #passkey-next → #passkey-add виден; клик → строк .passkey 1, название из NAMES;
    # .passkey-delete → 0 строк; снять #lock-enabled → пароль, #lock-off → screen_lock False; нет problems(r)
def test_add_key_is_absent_on_a_computer(browser, live):           # 1280 без touch: #lock-enabled есть, #passkey-add и #passkey-password нет
```

- [ ] **Step 2: Запустить — падают** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "coarse or lock_settings or add_key"` → FAIL.
- [ ] **Step 3: Реализация** — по Interfaces. «Добавить …» вызывает `create` сразу в обработчике нажатия, варианты получены на «Далее».
- [ ] **Step 4: Запустить — проходят** — та же команда → PASS.
- [ ] **Step 5: Коммит** — `Turn the lock on and add passkeys in the settings`

### Task 5: Шторка, экран блокировки, отметки и ожидание отпирания

**Files:**
- Modify: `app/static/index.html` (после `<main id="view">`: `<dialog id="curtain" class="curtain">` и `<dialog id="lock" class="lock" aria-labelledby="lock-title">` со значком, `#lock-title`, `#lock-open`, `#lock-use-password`, полем `#lock-password`, `#lock-password-go`, `#lock-status` и формой `POST /logout` с кнопкой `#lock-logout`)
- Modify: `app/static/js/lock.js` (часть 2), `app/static/js/api.js`, `app/static/js/app.js`, `app/static/css/app.css`
- Modify: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: Task 2–4.
- Produces:
  - `api.js`: `export const hooks = { locked: null }` — ответ 423 при заданном `hooks.locked` → `await hooks.locked()` и повтор того же `api(method, path, body)`; `export function useCsrf(token)` — заголовок CSRF берётся из `me.csrf`, а без `me` — из него.
  - `lock.js`: `export function installLock()` — ставит `hooks.locked` (одно общее ожидание на все 423: `GET /api/lock` → `useCsrf`, показать `#lock`, вернуть обещание отпирания), вешает `visibilitychange` и отметки (`ALIVE_SEC`, только `isTouch()`, только когда видно и не заперто). Уход (при `me.screen_lock` и `isTouch()`): `#curtain.showModal()`, запомнить `Date.now()`. Возврат: прошло ≥ `AWAY_SEC` → `POST /api/lock/now` и экран блокировки; иначе отметка → `locked` — экран, нет — `curtain.close()`; сбой сети — `curtain.close()`.
  - Экран: `showModal()` (поверх открытых окон просмотра); `cancel` у `#lock` и `#curtain` — `preventDefault()`; ключи есть — варианты входа (`POST /api/lock/challenge`) запрашиваются при показе и после каждой неудачи, `#lock-open` получает `data-ready="1"`, нажатие сразу вызывает `navigator.credentials.get`; ключей нет — сразу поле пароля. Отперли → `loadMe()`, закрыть `#lock` и `#curtain`, разрешить ожидание.
  - `app.js`: `start()` зовёт `installLock()` до `loadMe()` (запертый старт ждёт отпирания внутри `api`).

- [ ] **Step 1: Тесты** — помощники в тесте: `hide()` / `show()` (подмена `document.visibilityState` и `document.hidden` + событие), `later(ms)` (сдвиг `Date.now`), `phone_with_key(login)` — шаги задачи 4 до ключа и включённой блокировки; все — 390, mobile, touch, authenticator.

```python
def test_phone_locks_a_minute_after_leaving_and_opens_with_the_passkey(browser, live):
    # #/photo, ввести «кот на крыше» в #photo-prompt; hide → #curtain[open]; later(61000); show →
    # #lock[open]; fetch('/api/me').status == 423; Escape → #lock[open] всё ещё; ждать #lock-open[data-ready="1"];
    # клик → нет #lock[open] и #curtain[open]; #photo-prompt.value == «кот на крыше»; /api/me 200
def test_phone_back_within_a_minute_is_not_locked(browser, live):   # hide; later(30000); show → нет curtain/lock; /api/me 200
def test_computer_never_locks(browser, live):                       # 1280 без touch, блокировка включена запросом; hide; later(3_600_000); show → нет curtain/lock; /api/me 200
def test_password_opens_and_a_reload_stays_locked(browser, live):   # заперли; location.reload() → #lock[open] до раздела; #lock-use-password, пароль, #lock-password-go → раздел виден
def test_curtain_covers_an_open_viewer(browser, live):              # картинка в ленте, открыт #viewer; hide → elementFromPoint(центр) внутри #curtain
def test_requests_during_the_lock_wait_and_are_retried(browser, live):
    # заперли; eval: import('/static/js/api.js') → Promise.all([api GET /api/sections, api GET /api/me]) → window.done = true;
    # window.done false; один #lock; открыть passkey → window.done true
def test_open_calls_the_passkey_without_waiting_for_the_network(browser, live):
    # заперли, #lock-open[data-ready="1"]; обёртка fetch считает вызовы, обёртка navigator.credentials.get
    # запоминает их число; клик #lock-open → число запросов между нажатием и get == 0
```

- [ ] **Step 2: Запустить — падают** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "locks or locked or never_locks or reload_stays or curtain or during_the_lock or without_waiting"` → FAIL.
- [ ] **Step 3: Реализация** — по Interfaces.
- [ ] **Step 4: Запустить — проходят; прежние браузерные тесты темы, вида и выхода зелёные** — та же команда и `-k "theme or views or comes_back or logout or video_tiles"` → PASS.
- [ ] **Step 5: Коммит** — `Cover and lock the studio on phones, open it with a passkey`

### Task 6: Документы, полная проверка, выкладка

**Files:**
- Modify: `README.md` (раздел «Блокировка на телефоне»: включение, ключи, что не защищает — по разделу 7 спеки), `CLAUDE.md` («Устройство»: `screenlock`, `lock.js`; «Ловушки»: 423 `screen_locked` ≠ 429 `locked`; варианты passkey — до нажатия ради Safari; экран блокировки — `<dialog>` из-за окон просмотра)

- [ ] **Step 1: Быстрый набор** — `.venv/Scripts/python -m pytest -q` → PASS, бюджет 60 с.
- [ ] **Step 2: Браузерный набор целиком** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py` → PASS.
- [ ] **Step 3: Коммит документов** — `Document the phone lock`
- [ ] **Step 4: Выкладка** — `bash deploy/deploy.sh` → «готово»; на хосте `PRAGMA user_version` = 7 и `.venv/bin/pip show webauthn` → 3.0.1; `https://studio.example.com/health` → ok.
- [ ] **Step 5: Владельцу** — проверить на iPhone и Android по критерию успеха спеки (пп. 1–7).
