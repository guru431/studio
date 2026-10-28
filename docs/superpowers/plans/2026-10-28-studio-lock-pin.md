# PIN блокировки на телефоне — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** короткий PIN студии (4–8 цифр) отпирает блокировку на телефоне цифровой панелью; 10 неверных
подряд на учётку — только пароль.

**Architecture:** два столбца `users` (шаг схемы 17); пути PIN — в `screenlock.py` рядом с паролем и
passkey; счёт попыток занимается атомарным `UPDATE … RETURNING` до scrypt. Страница: блок PIN в
«Блокировке» (`settings.js`), цифровая панель в `<dialog id="lock">` (`index.html`, `lock.js`, `app.css`).

**Tech Stack:** FastAPI, SQLite (`RETURNING` — 3.35+), scrypt `security.hash_password`; ES-модули без сборки;
быстрые тесты pytest, браузерные — CDP-драйвер `tests/browser.py`.

**Spec:** [docs/superpowers/specs/2026-10-28-studio-lock-pin-design.md](../specs/2026-10-28-studio-lock-pin-design.md)
— читать целиком; основа — [2026-10-21-studio-screen-lock-design.md](../specs/2026-10-21-studio-screen-lock-design.md).

## Global Constraints

- `PIN_FAILS = 10`; PIN — строка `^[0-9]{4,8}$` только ASCII (`[0-9]`, не `\d` и не `isdigit()`: они
  берут «１２３４» и «١٢٣٤»); PIN везде строка — «0007» ≠ «7».
- `db.VERSION = 17`; шаг 17 только добавляет столбцы `users.pin_hash TEXT`,
  `users.pin_fails INTEGER NOT NULL DEFAULT 0` — константой `PIN_17` (ALTER), дописанной в конец `SCHEMA`,
  как `INTERNET_14`, а не в `CREATE TABLE users` (иначе старые базы тестов уже имели бы столбцы).
- Журнал — только события с id: «пользователь N: открыто (PIN)», «… PIN неверен», «… PIN исчерпан»,
  «… PIN задан», «… PIN убран». Ни PIN, ни хеш, ни число цифр.
- PIN и хеш — не в ответах API (только `pin: bool`, `pin_left: int`), не в выгрузке, не в
  localStorage/sessionStorage; набранные на панели цифры — только в переменной модуля.
- Тексты страницы дословно: «PIN не принимается после 10 ошибок — откройте паролем учётки» (его же шлёт
  сервер в `pin_exhausted`), «Неверный PIN», «Неверный PIN — осталось 5 попыток» / «… осталось 2 попытки» /
  «… осталась 1 попытка»; настройки — «PIN: не задан», «PIN задан», «Задать PIN», «Сменить PIN»,
  «Убрать PIN», подсказка «От 4 до 8 цифр. Не совпадающий с кодом телефона».
- Команды: точечно `.venv/Scripts/python -m pytest tests/<файл>.py -q -n0`; быстрый набор
  `.venv/Scripts/python -m pytest -q`; браузер `.venv/Scripts/python -m pytest -q -m integration
  tests/test_ui_browser.py -n0 -k "<выражение>"`.
- Коммит — сообщение файлом (`git commit -F <файл>`), в конце `Co-Authored-By: Claude Opus 5.5
  <noreply@anthropic.com>`; без push.
- Правка `lock.js`, `index.html` или `settings.js` — перед коммитом прогнать уже существующие браузерные
  сценарии блокировки: `-k "lock or passkey or curtain or phone"` (исполнители их пропускают).

## Review Focus

1. Цифры не ASCII («１２３４», «١٢٣٤») — задать PIN нельзя (422), при отпирании — неверный PIN с тратой
   попытки. Тесты — Task 1 (формат) и Task 2 (трата попытки).
2. Ведущие нули: PIN «0007» задаётся и открывает, «7» и «007» — нет. Тесты — Task 1 и Task 2.
3. Двойное касание «OK» или Enter, пока идёт запрос, — один запрос, одна попытка. Тест — Task 4.
4. Экран блокировки открыт, а PIN тем временем убрали или исчерпали на другом устройстве: «OK» даёт
   `no_pin` / `pin_exhausted` — экран переходит к паролю, а не застревает на панели. Сервер — Task 2,
   страница — Task 4 (ветка `no_pin` = ветка `pin_exhausted`).
5. Набранные цифры при уходе с экрана стираются: две цифры, уход, возвращение — точек 0. Тест — Task 4.

---

### Task 1: Шаг схемы 17 и настройка PIN на сервере

**Files:**
- Modify: `app/db.py` (константа `PIN_17`, `SCHEMA`, `STEPS[17]`, `VERSION = 17`)
- Modify: `app/screenlock.py` (модели, `PUT/DELETE /api/me/pin`)
- Modify: `app/profile.py:53-61` (`_me` — `pin`)
- Test: `tests/test_foundation.py`, `tests/test_screenlock.py`, `tests/test_takeout.py`

**Interfaces:**
- Produces: столбцы `users.pin_hash`, `users.pin_fails`; в `screenlock.py` — `PIN_FAILS = 10`,
  `PIN_RE = re.compile(r"[0-9]{4,8}")`, `PIN_MAX = 64` (предел строки в теле), модель
  `class NewPin(BaseModel): password: str | None = Field(None, max_length=PASSWORD_MAX); pin: str = Field(max_length=PIN_MAX)`;
  `PUT /api/me/pin` → 200 `{"pin": true}`; `DELETE /api/me/pin` → 204; `GET /api/me` → `"pin": bool`.
  Ошибка формата — 422 `{"code": "pin_format", "message": "PIN — от 4 до 8 цифр"}`.

- [ ] **Step 1: Тесты базы.** В `tests/test_foundation.py`: `DROP_V17 = "ALTER TABLE users DROP COLUMN pin_hash; ALTER TABLE users DROP COLUMN pin_fails;"`
  первым в каждой строке, ставящей `PRAGMA user_version` ниже 17 (18 мест — `grep "PRAGMA user_version = "`);
  `db.VERSION == 16` → `== 17`; новый тест:

```python
def test_step_17_adds_the_pin_to_users(env):
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V17} PRAGMA user_version = 16;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created, screen_lock) VALUES (1, 'u', 'U', 'h', 0, 1)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 17
    assert tuple(conn.execute("SELECT login, screen_lock, pin_hash, pin_fails FROM users").fetchone()) == ("u", 1, None, 0)
```

- [ ] **Step 2: Тесты настройки** в `tests/test_screenlock.py` (новый раздел `# ---- PIN ----`):
  - `test_pin_is_set_only_with_the_password_and_removed_without`: `PUT /api/me/pin` с `{"pin": "0007"}`,
    `{"password": "wrong-pw", "pin": "0007"}` — 403 `password`, `GET /api/me` → `pin is False`; с
    `"password-123"` — 200 `{"pin": True}`, `pin is True`, в базе `pin_hash` проверяется
    `security.verify_password("0007", h)` и не `"7"`; `DELETE` — 204, `pin is False`, `pin_hash IS NULL`.
  - `test_pin_format_is_four_to_eight_ascii_digits`: для `"123", "123456789", "12a4", "１２３４", "١٢٣٤", " 1234", "", "12 34"`
    — 422 `pin_format`, пауза пароля не тронута (`auth.FAILS` без `("pw", uid)`); `"1234"` и `"12345678"` — 200.
  - `test_locked_studio_cannot_set_or_remove_the_pin`: `locked_owner` → `PUT` с верным паролем и `DELETE` — 423,
    `pin_hash` прежний.
  - `test_pin_events_log_only_ids`: `caplog` — «пользователь {uid}: PIN задан» / «PIN убран»; `"0007"` и
    `pin_hash` в журнале нет.
  - В `tests/test_takeout.py::test_export_holds_no_secrets` — задать `pin_hash` учётке и проверить, что его
    байтов и слова `pin_hash` в архиве нет.

- [ ] **Step 3: Запустить** `tests/test_foundation.py tests/test_screenlock.py tests/test_takeout.py` — новые
  тесты падают (нет столбцов, 404/405 на `/api/me/pin`).

- [ ] **Step 4: Реализовать.** `db.py`: `PIN_17` (два ALTER), в конец `SCHEMA` и `STEPS[17]` с комментарием
  по образцу соседних. `screenlock.py`: `PUT /me/pin` — сначала формат (`PIN_RE.fullmatch`), затем
  `auth.confirm_password(uid, body.password or "", "PIN")`, хеш `await asyncio.to_thread(security.hash_password, pin)`,
  `UPDATE users SET pin_hash = ?`, журнал; `DELETE /me/pin` — `pin_hash = NULL`, журнал. `pin_fails` оба не
  трогают. `profile._me` — `pin_hash IS NOT NULL AS pin`. Докстринг модуля `screenlock.py` — дописать PIN.

- [ ] **Step 5: Запустить** те же три файла — PASS; `test_lock_paths_are_not_refused_while_locked` и прочие
  старые тесты блокировки — PASS.

- [ ] **Step 6: Коммит** «Блокировка: PIN учётки — шаг схемы 17, задать и убрать».

---

### Task 2: Отпирание PIN-кодом и счёт попыток

**Files:**
- Modify: `app/screenlock.py` (`POST /api/lock/pin`, `GET /api/lock`, `open_with_password`)
- Modify: `app/auth.py:53-54` (`SCREEN_LOCK_PATHS` + `"/api/lock/pin"`)
- Modify: `CLAUDE.md` («Устройство» → `screenlock`: PIN, `PIN_FAILS`, счёт на учётку, занятие до scrypt)
- Test: `tests/test_screenlock.py`

**Interfaces:**
- Consumes: Task 1 — столбцы, `PIN_FAILS`, `PIN_RE`, `PIN_MAX`.
- Produces: `POST /api/lock/pin {"pin": str}` → 204 | 403 `{"code": "pin", "message": "неверный PIN", "left": int}` |
  403 `{"code": "pin_exhausted", "message": "PIN не принимается после 10 ошибок — откройте паролем учётки"}` |
  409 `{"code": "no_pin", "message": "PIN не задан — откройте паролем"}`;
  `GET /api/lock` → `{"locked", "keys", "csrf", "pin": bool, "pin_left": int}` (`pin_left = PIN_FAILS - pin_fails`,
  не ниже 0, независимо от `pin`).

- [ ] **Step 1: Тесты** (помощники: `set_pin(uid, pin)` — хеш прямо в базу; `pin(s, value)` — `POST /api/lock/pin`;
  две сессии — `second_session`, ей `mobile = 1` и `/api/lock/now`):
  - `test_lock_state_gives_csrf_and_keys_but_no_profile` — ожидание дополнить `"pin": False, "pin_left": 10`.
  - `test_right_pin_opens_and_resets_the_count`: 3 неверных (`left` 9, 8, 7), верный «0007» — 204, `/api/me` 200,
    `pin_fails == 0`; «7» и «007» — неверные.
  - `test_ten_wrong_pins_leave_only_the_password`: 10 неверных → последний 403 `pin` с `left == 0`; 11-й с верным
    PIN — 403 `pin_exhausted`, сообщение дословно, `/api/me` 423; `GET /api/lock` → `pin_left == 0`; пароль —
    204 и `pin_fails == 0`; после `/api/lock/now` верный PIN снова 204.
  - `test_wrong_pins_of_two_sessions_add_up_and_a_password_elsewhere_resets`: 5 неверных в сессии A, 5 в B → обе
    `pin_exhausted`; пароль в B → верный PIN в A открывает.
  - `test_new_pin_and_new_login_do_not_reset_the_count`: 10 неверных; `second_session` (компьютер) задаёт новый
    PIN через `PUT /api/me/pin` и сам входит заново — в A и новый PIN — `pin_exhausted`.
  - `test_the_attempt_is_counted_before_the_hash_is_checked` (`monkeypatch` `security.verify_password` — читает
    `pin_fails` из базы в момент проверки и пишет в список): при 0 ошибок проверка видит 1; при 9 — проверка
    видит 10, следующая попытка до `verify_password` не доходит (список длиной 1).
  - `test_pin_changed_during_the_check_opens_nothing`: подмена `verify_password`, как в
    `test_password_changed_during_the_check_confirms_nothing`, меняет `pin_hash` посреди проверки — верный прежний
    PIN даёт 403 `pin`, `/api/me` 423.
  - `test_malformed_pin_is_a_wrong_pin`: `"１２３４"`, `"12a4"`, `""` — 403 `pin` и каждая тратит попытку; строка
    длиннее `PIN_MAX` — 422.
  - `test_pin_without_a_pin_is_409_and_passkey_still_opens_an_exhausted_one`: без PIN — 409 `no_pin`; с PIN и 10
    ошибками ключ (`add_key` до запирания, `open_with`) — 204.
  - `test_pin_and_password_pauses_are_apart`: 5 неверных паролей (пауза 429) — верный PIN открывает; 10 неверных
    PIN — `auth.FAILS` без `("pw", uid)`.
  - `test_lock_events_log_only_ids` — дополнить: «открыто (PIN)», «PIN неверен», «PIN исчерпан»; PIN в журнале нет.

- [ ] **Step 2: Запустить** `tests/test_screenlock.py` — новые падают.

- [ ] **Step 3: Реализовать** `open_with_pin(body: Pin, request)` в `screenlock.py` (`class Pin: pin: str = Field(max_length=PIN_MAX)`):

```text
нет pin_hash                         → 409 no_pin
UPDATE users SET pin_fails = pin_fails + 1
 WHERE id = ? AND pin_hash IS NOT NULL AND pin_fails < PIN_FAILS
 RETURNING pin_fails, pin_hash        → нет строки: pin_hash пуст → 409 no_pin, иначе 403 pin_exhausted (журнал «PIN исчерпан»)
верно = PIN_RE.fullmatch(pin) and await asyncio.to_thread(security.verify_password, pin, hash)
верно и UPDATE users SET pin_fails = 0 WHERE id = ? AND pin_hash = <тот hash> изменил строку
                                     → _seen(now), журнал «открыто (PIN)», 204
иначе                                → журнал «PIN неверен» (+ «PIN исчерпан», если fails == PIN_FAILS),
                                       403 pin с left = PIN_FAILS - fails
```

  `open_with_password` после `confirm_password` — `UPDATE users SET pin_fails = 0 WHERE id = ?`. `lock_state` —
  `pin`, `pin_left`. `auth.SCREEN_LOCK_PATHS` — `"/api/lock/pin"`. CLAUDE.md — абзац `screenlock` в «Устройство».

- [ ] **Step 4: Запустить** `tests/test_screenlock.py` — PASS; затем быстрый набор — PASS.

- [ ] **Step 5: Коммит** «Блокировка: отпирание PIN-кодом, 10 попыток на учётку».

---

### Task 3: Блок PIN в настройках

**Files:**
- Modify: `app/static/js/settings.js:122-262` (`lockBlock`)
- Modify: `app/static/css/app.css` (только если вёрстке нужно)
- Test: `tests/test_ui_browser.py` (раздел блокировки)

**Interfaces:**
- Consumes: Task 1 — `PUT/DELETE /api/me/pin`, `me.pin`.
- Produces: элементы `#pin-state` (текст «PIN: не задан» | «PIN задан»), `#pin-set` («Задать PIN» | «Сменить PIN»),
  `#pin-remove` («Убрать PIN», скрыт без PIN), форма `#pin-form` (скрыта до `#pin-set`): `#pin-password`
  (`type=password`, `autocomplete=current-password`), `#pin-new`, `#pin-repeat` (`type=password`,
  `inputmode=numeric`, `autocomplete=off`, `maxlength=8`), `#pin-save`; сообщения — в `note` блока
  (`#screen-lock-status`). Блок — на любом устройстве.

- [ ] **Step 1: Браузерный тест** `test_lock_settings_set_change_and_remove_the_pin` (компьютер, `width=1280`):
  `lock_settings(login)` → `#pin-state` «PIN: не задан», `#pin-remove` скрыт → `#pin-set` → пароль, «1234», повтор
  «1243», `#pin-save` → статус «PIN и повтор не совпадают», `fetch('/api/me')` → `pin === false` → повтор «1234» →
  ждать `me.pin === true`, `#pin-state` «PIN задан», `#pin-set` «Сменить PIN», поля очищены → `#pin-remove` → ждать
  `me.pin === false`. Плюс на `PHONE`: `#pin-new` имеет `inputmode === 'numeric'`. `assert not problems(r)`.

- [ ] **Step 2: Запустить** `-k "set_change_and_remove_the_pin"` — FAIL (нет `#pin-state`).

- [ ] **Step 3: Реализовать** в `lockBlock`: строка состояния, кнопки, форма с подсказкой «От 4 до 8 цифр. Не
  совпадающий с кодом телефона»; несовпадение — сообщение без запроса; успех — `me.pin = true`, поля пусты,
  «PIN задан»; ошибки — `words(err)`. Блок вставить после переключателя, до списка ключей.

- [ ] **Step 4: Запустить** новый тест и `-k "lock or passkey or curtain or phone"` — PASS.

- [ ] **Step 5: Коммит** «Настройки: задать, сменить и убрать PIN блокировки».

---

### Task 4: Цифровая панель на экране блокировки

**Files:**
- Modify: `app/static/index.html:56-67` (`<dialog id="lock">`)
- Modify: `app/static/js/lock.js` (состояние, показ, ввод, отпирание)
- Modify: `app/static/css/app.css:932-944` (панель)
- Modify: `README.md:56-87` («Блокировка на телефоне»: PIN, 10 попыток на учётку, без iCloud)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: Task 2 — `GET /api/lock` `pin`, `pin_left`; `POST /api/lock/pin` и коды ответов.
- Produces: разметка `#lock-pin` (скрыта по умолчанию) с `#lock-pin-dots` (по `<span>` на набранную цифру, класс
  `.on` не нужен — число детей = число цифр), кнопки `button[data-digit="0".."9"]`, `#lock-pin-back` («⌫»,
  `aria-label="Стереть"`), `#lock-pin-ok` («OK», `disabled` до 4 цифр и пока идёт запрос). Порядок в диалоге:
  логотип, заголовок, `#lock-open` (passkey, только при ключах), `#lock-pin`, `#lock-use-password`, форма пароля,
  статус, «Выйти».

- [ ] **Step 1: Браузерные тесты** (помощник `phone_with_pin(login)`: `lock_settings` → включить блокировку →
  задать PIN «2580» через блок Task 3; `pin_locked(login)`: уход, `later(61000)`, возвращение, ждать
  `LOCK_OPEN` и 423; `tap(digits)` — клики по `[data-digit="…"]`):
  - `test_phone_opens_with_the_pin_keeping_the_text`: на «Фото» набрать промпт, `pin_locked` → `#lock-pin`
    виден, `#lock-password-form` скрыт, `#lock-open` скрыт (ключей нет) → «1111», OK → точек 0, статус «Неверный
    PIN» → «2580», OK → экран закрыт, промпт цел, `/api/me` 200.
  - `test_ten_wrong_pins_turn_the_pad_into_the_password`: через API (`API.api('POST','/api/lock/pin', …)` в
    `eval`) истратить 5 → на панели «0000» OK → статус «Неверный PIN — осталось 4 попытки» → ещё 3 через панель
    (после последней — «Неверный PIN — осталась 1 попытка») → 10-я: `#lock-pin` скрыт, форма пароля видна, статус
    «PIN не принимается после 10 ошибок — откройте паролем учётки» → пароль → открыто → снова `pin_locked` →
    панель видна.
  - `test_double_ok_sends_one_pin`: подсчёт `fetch` к `/api/lock/pin` (обёртка `window.fetch` в `init`) — «0000»,
    два клика по OK подряд и Enter → один запрос.
  - `test_typed_digits_are_gone_after_leaving`: 9 нажатий цифр → 8 точек, `#lock-pin-ok` не `disabled`; «⌫» → 7;
    очистить, «25» → `#lock-pin-ok` `disabled`; `hide()`, `show()` → `#lock-pin-dots` без детей.
  - `test_physical_keys_type_the_pin`: `("press", "2")`, … `("press", "Enter")` → открыто.
  - `test_lock_without_pin_is_as_before` — существующий `test_password_opens_and_a_reload_stays_locked` должен
    пройти без изменений (панели нет).

- [ ] **Step 2: Запустить** `-k "pin"` — новые FAIL.

- [ ] **Step 3: Реализовать** в `lock.js`: `pinLeft` и `pinOn` из `lockState()` в `showLock`; `offerPin()` (панель
  видна, форма пароля скрыта, `#lock-open` по `keys`); `offerPassword()` прячет и панель; `digits` — строка в
  модуле, стирается в `opened()`, при уходе с экрана (`onVisibility` hidden) и после каждой попытки;
  `openWithPin()` — защита от повторного входа флагом, как `calling`; ответы: 204 → `opened()`; `pin` → точки 0,
  подпись по `left` (`left ≤ 5` — со склонением, `left == 0` — как исчерпан); `pin_exhausted` и `no_pin` →
  `pinOn = false`, сообщение (`pin_exhausted` — из ответа, `no_pin` — без подписи), `offerPassword()`. Клавиши:
  `keydown` на `document`, пока `screen.open` и панель видна, а фокус не в `#lock-password`: `0-9`, `Backspace`,
  `Enter`. Склонение — функция `attemptsLeft(n)` → «осталась 1 попытка» / «осталось N попытки» (2–4) / «осталось
  N попыток». CSS: сетка 3×4, круглые кнопки ≥ 64 px, тема из переменных `app.css`, ширина 320 px без
  горизонтальной прокрутки. README — абзац о PIN и строка «Ключ» про iCloud.

- [ ] **Step 4: Запустить** `-k "pin"` и `-k "lock or passkey or curtain or phone"` — PASS; быстрый набор — PASS.

- [ ] **Step 5: Коммит** «Экран блокировки: цифровая панель PIN».
