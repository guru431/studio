# Студия-приложение (studio.example.com) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить личную страницу `llm.example.com/studio/` в публичное многопользовательское PWA
`https://studio.example.com` с учётками, профилями подключений, сохранённой историей и разделами
чат · фото · видео.

**Architecture:** FastAPI-служба на debian (unix-сокет за Apache) разбивается на модули: учётки и
сессии в SQLite на локальном диске, файлы истории на шаре storage. К шлюзу srvllm (и любому адресу
из списка администратора) ходит сервер; к прочим (облачным) подключениям — браузер напрямую.
Фронтенд — ES-модули без сборки, сторонние библиотеки лежат копией в репозитории. Новая служба
строится рядом со старой (`app/main.py` рядом с `app/server.py`), старая удаляется в задаче 14,
когда её поведение и тесты перенесены.

**Tech Stack:** Python 3.13, FastAPI 0.141, uvicorn, httpx, Pillow, sqlite3 (stdlib), hashlib.scrypt,
cryptography (Fernet), pypdf; vanilla JS (ES modules), marked, DOMPurify, highlight.js; Apache 2.4
(mod_proxy, unix-сокет), systemd; pytest + pytest-timeout, CDP-драйвер `tests/cdp_driver.mjs`.

**Spec:** [docs/superpowers/specs/2026-10-19-studio-pwa-design.md](../specs/2026-10-19-studio-pwa-design.md)

## Global Constraints

- Адрес — `https://studio.example.com`, приложение от корня `/` (префикса `/studio` нет).
- Служба слушает только unix-сокет `/run/studio/web.sock`; порт TCP не открывать; адрес клиента —
  последний элемент `X-Forwarded-For`.
- В журнал — только события с id. Промпты, ключи, пароли, токены, картинки, ролики, тексты
  документов, имена файлов, логины в неудачных входах — никогда. uvicorn — `--no-access-log`.
- Секреты — только vault `local-config/secrets/vault.env` и `/etc/studio/studio.env` (0600 root, читает
  systemd). Значения секретов не идут в argv и не печатаются.
- Токен подключения, к которому ходит сервер (адрес из списка разрешённых), в браузер не уходит
  никогда. Сервер не ходит по адресу вне списка разрешённых — ни напрямую, ни перенаправлением.
- SQLite — только на локальном диске (`/var/lib/studio/studio.db`); файлы — `STUDIO_MEDIA_DIR`
  (на хосте `/mnt/studio-media` = `\\storage\dd$\SharedData\studio`).
- Темы: тёмная «Графит», светлая «Бумага» (токены — задача 9, значения дословно); раскладка фото и
  видео — лента во весь экран, промпт снизу.
- Сессия 30 дней (скользящая), cookie `studio_sid` HttpOnly + Secure + SameSite=Lax, Path=/.
- Пауза входа: 5 неудач за 15 минут → 15 минут, на пару «логин+адрес» и на адрес (кроме
  `10.77.1.1`); логин целиком не запирается.
- Чат по умолчанию у владельца — `local-gemma-abl` через подключение «srvllm» `http://10.77.1.15:4000`.
- Модели картинок и размеры — как в нынешнем `app/server.py:97-99` (`lustify-v10-krea2-turbo`,
  `flux.2-klein-4b`; 832x1216, 1024x1024, 1216x832); модель роликов `wan2.2-remix-i2v-v3`.
- Пределы загрузки картинок — `app/image_upload.py` без изменений; документы — до 20 МиБ; Apache
  `LimitRequestBody 22020096` не меньше пределов службы.
- `requirements.txt` — точные пины (`==`); фронтенд без шага сборки, без внешних CDN во время работы.
- Быстрый набор `pytest` ≤ 60 с; часы и паузы — только `clock.NOW` / `clock.SLEEP`; сеть и браузер —
  `integration`, живая приёмка — `manual`.
- `client/*.ps1` не трогать. Файлы `deploy/*` — LF. Репозиторий остаётся приватным.
- Тексты интерфейса — по-русски.

## Review Focus

1. **Перезапуск службы посреди задания** (картинка считается, ролик в очереди, чат пишет ответ через
   сервер) — картинка и ответ становятся «ошибка: прервано перезапуском студии», ролик продолжает
   отслеживаться и доезжает в историю. Тесты: задача 6 `test_restart_marks_running_images_interrupted`,
   задача 7 `test_watcher_resumes_queued_videos_after_restart`, задача 8 `test_restart_marks_server_streams_interrupted`.
2. **Вкладку закрыли посреди ответа чата** (клиент оборвал поток) — сервер дочитывает ответ модели
   и сохраняет его целиком. Тест: задача 8 `test_answer_is_saved_when_the_client_disconnects`.
3. **Шара storage недоступна или не пишется**, когда генерация закончилась — задание «ошибка: не
   удалось сохранить файл», в базе нет записи без файла, служба жива. Тесты: задача 5
   `test_save_into_unwritable_dir_raises_storage_error`, задача 6 `test_unsaved_image_becomes_an_error`.
4. **Шлюз отвечает 401 на токен подключения** — не выход из студии (как было раньше), а сообщение
   «шлюз не принял ключ подключения «srvllm» — проверьте токен в профиле». Тесты: задача 6
   `test_gateway_401_points_to_the_profile_not_the_login`, задача 7 `test_video_gateway_401_keeps_the_session`.
5. **Хитрые адреса подключений** (`http://10.77.1.15:4000@evil.test`, `http://10.77.1.15:40000`,
   `http://10.77.1.15.evil.test:4000`, перенаправление 302 на внутренний адрес) — сервер не идёт.
   Тест: задача 4 `test_tricky_urls_are_not_allowlisted` и `test_upstream_never_follows_redirects`.

---

## Карта файлов

| Файл | Отвечает за |
|---|---|
| `app/clock.py` | `NOW`, `SLEEP` — единственная точка времени (тесты подменяют) |
| `app/config.py` | переменные окружения службы → `Config` |
| `app/state.py` | общие объекты процесса: `CFG`, `DB`, `HTTP`, фабрика `make_http` |
| `app/db.py` | соединение SQLite и схема (версия 1) |
| `app/security.py` | хеш пароля (scrypt), шифрование токенов (Fernet), случайные токены |
| `app/auth.py` | вход/выход, сессии, пауза попыток, CSRF, middleware `guard` |
| `app/profile.py` | `/api/me`, смена пароля, «удалить всю историю» |
| `app/admin.py` | пользователи, подключения пользователя, список разрешённых адресов |
| `app/connections.py` | подключения профиля, адреса API, возможности разделов |
| `app/upstream.py` | список разрешённых адресов, запросы сервера к ним, слова отказов шлюза |
| `app/media.py` | файлы истории на шаре, превью, выдача с `Range` |
| `app/history.py` | удаление истории пользователя целиком |
| `app/images.py` | задания картинок и описаний (перенос из `server.py`), лента фото |
| `app/videos.py` | задания роликов, наблюдатель, лента видео |
| `app/documents.py` | приём файла документа и извлечение текста |
| `app/chat.py` | диалоги, вложения, сборка контекста, поток через сервер |
| `app/main.py` | приложение FastAPI: lifespan, страницы, статика, PWA, роутеры, заголовки |
| `app/manage.py` | CLI хоста: `create-admin`, `backup` |
| `app/static/` | `index.html`, `login.html`, `offline.html`, `css/app.css`, `js/*.js`, `vendor/`, `icons/` |
| `tests/fakes.py` | `FakeGateway` (шлюз srvllm) и `FakeCloud` (OpenAI-совместимый облачный) |
| `tests/live_app.py` | живая служба в потоке для браузерных тестов |
| `deploy/apache-vhost.conf` | vhost `studio.example.com` (вместо `apache-location.conf`) |
| `deploy/studio-backup.service`, `.timer` | ежесуточная копия базы |

Удаляются в задаче 14: `app/server.py`, `app/index.html`, `app/login.html`,
`deploy/apache-location.conf`, `tests/test_studio.py`, `tests/test_studio_upload_browser.py`.

## Схема базы (версия 1, `PRAGMA user_version = 1`)

```sql
users(id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
      is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
      theme TEXT NOT NULL DEFAULT 'auto', default_connection_id INTEGER, default_model TEXT,
      created REAL NOT NULL)
sessions(digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
         csrf TEXT NOT NULL, expires REAL NOT NULL, renewed REAL NOT NULL)
settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)          -- 'allowlist' = JSON-список "host:port"
connections(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('openai','srvllm')),
            base_url TEXT NOT NULL, token_enc BLOB NOT NULL, sections TEXT NOT NULL, created REAL NOT NULL)
media(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
      rel_path TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, has_thumb INTEGER NOT NULL,
      download_name TEXT NOT NULL, created REAL NOT NULL)
chats(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, title TEXT,
      connection_id INTEGER, model TEXT, created REAL NOT NULL, updated REAL NOT NULL)
messages(id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE,
         role TEXT NOT NULL CHECK (role IN ('user','assistant')), content TEXT NOT NULL,
         status TEXT NOT NULL CHECK (status IN ('done','streaming','stopped','error')),
         via TEXT NOT NULL CHECK (via IN ('server','browser')), model TEXT, created REAL NOT NULL)
attachments(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            message_id TEXT REFERENCES messages ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('document','image')), name TEXT NOT NULL,
            media_id TEXT NOT NULL, text TEXT, chars INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)
images(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       kind TEXT NOT NULL CHECK (kind IN ('generation','edit')), model TEXT NOT NULL, prompt TEXT NOT NULL,
       size TEXT NOT NULL, seed INTEGER NOT NULL, strength REAL, source_media_id TEXT, media_id TEXT,
       state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL)
videos(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       connection_id INTEGER NOT NULL, gateway_id TEXT, source_media_id TEXT NOT NULL, media_id TEXT,
       prompt TEXT NOT NULL, prompt_source TEXT, seconds INTEGER NOT NULL, quality TEXT NOT NULL,
       smooth INTEGER NOT NULL, seed INTEGER, state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
       detail TEXT NOT NULL, created REAL NOT NULL, finished REAL)
```
Индексы: `images(user_id, created)`, `videos(user_id, created)`, `chats(user_id, updated)`,
`messages(chat_id, created)`.

## Общий формат ошибок API

Каждый отказ — `{"detail": {"code": "<код>", "message": "<слова по-русски>"}}` (как в нынешнем
`server.py`). Фронтенд показывает `message` как есть.

---

### Task 1: Основа — время, конфиг, база, безопасность

**Files:**
- Create: `app/clock.py`, `app/config.py`, `app/state.py`, `app/db.py`, `app/security.py`
- Modify: `requirements.txt` (добавить `cryptography`, `pypdf` точными пинами), `tests/conftest.py`
- Test: `tests/test_foundation.py`

**Interfaces:**
- Produces:
  - `clock.NOW: Callable[[], float] = time.time`, `clock.SLEEP = asyncio.sleep`
  - `config.REQUIRED = ("STUDIO_DATA_KEY", "STUDIO_DB", "STUDIO_MEDIA_DIR", "STUDIO_ORIGIN")`;
    `@dataclass(frozen=True) class Config: data_key: str; db_path: Path; media_dir: Path; origin: str; hairpin_ip: str = "10.77.1.1"`;
    `config.load() -> Config` (нет переменных — `RuntimeError` со списком имён, без значений)
  - `state.CFG: Config | None`, `state.DB: sqlite3.Connection | None`, `state.HTTP: httpx.AsyncClient | None`,
    `state.make_http() -> httpx.AsyncClient` (таймаут по умолчанию 30 с; тесты подменяют),
    `state.WATCHER: bool = True` (быстрые тесты ставят False: наблюдатель роликов задачи 7 не крутится сам, тест зовёт проход руками)
  - `db.connect(path: Path) -> sqlite3.Connection` (WAL, `foreign_keys=ON`, `row_factory=sqlite3.Row`,
    `check_same_thread=False`, `isolation_level=None` — автокоммит, транзакции явно);
    `db.migrate(conn) -> None` (схема выше; сидирует `settings.allowlist = '["10.77.1.15:4000"]'`; повторный вызов — без изменений)
  - `security.PASSWORD_MIN = 10`; `hash_password(pw: str) -> str` (формат `scrypt$16384$8$1$<salt b64>$<hash b64>`, соль 16 байт, dklen 32);
    `verify_password(pw: str, stored: str) -> bool` (`hmac.compare_digest`); `DUMMY_HASH: str`;
    `encrypt(text: str) -> bytes`, `decrypt(blob: bytes) -> str` (Fernet ключом `state.CFG.data_key`);
    `new_token() -> str` (`secrets.token_urlsafe(32)`); `digest(token: str) -> str` (sha256 hex)
- `tests/conftest.py` дополнить фикстурой `env(tmp_path, monkeypatch)`: ставит все `REQUIRED`
  (`STUDIO_DATA_KEY = Fernet.generate_key()`, база и медиа в `tmp_path`, `STUDIO_ORIGIN=https://testserver`)
  и возвращает `SimpleNamespace(db_path, media_dir)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_foundation.py`:

```python
def test_config_names_missing_variables_without_values(monkeypatch):
    for k in config.REQUIRED: monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError) as e: config.load()
    assert all(k in str(e.value) for k in config.REQUIRED)

def test_migrate_creates_schema_once_and_seeds_the_gateway(env):
    conn = db.connect(env.db_path); db.migrate(conn); db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key='allowlist'").fetchone()[0]) == ["10.77.1.15:4000"]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

def test_deleting_a_user_cascades(env): ...   # user + session + chat + message → DELETE users → всё ушло

def test_password_hash_roundtrip_and_salt():
    a, b = security.hash_password("correct horse"), security.hash_password("correct horse")
    assert a != b and a.startswith("scrypt$16384$8$1$")
    assert security.verify_password("correct horse", a) and not security.verify_password("wrong", a)
    assert not security.verify_password("x", security.DUMMY_HASH)

def test_token_encryption_needs_the_same_key(env): ...  # encrypt → decrypt == исходник; другой ключ → InvalidToken
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q tests/test_foundation.py` — Expected: FAIL (`ModuleNotFoundError: clock`).
- [ ] **Step 3: Implement** модули по Interfaces. `pip install cryptography pypdf` в `.venv`, версии — из `pip show` в `requirements.txt` через `==`.
- [ ] **Step 4: Run** тот же файл и весь быстрый набор — Expected: PASS (старые тесты зелёные, `test_requirements_are_exact_pins` тоже).
- [ ] **Step 5: Commit** `git add app/clock.py app/config.py app/state.py app/db.py app/security.py requirements.txt tests/conftest.py tests/test_foundation.py` → «Add studio foundation: config, schema, password and token crypto».

---

### Task 2: Вход, сессии, приложение `main.py`, первый администратор

**Files:**
- Create: `app/auth.py`, `app/main.py`, `app/manage.py`, `app/static/login.html`
- Test: `tests/test_auth.py`; `tests/conftest.py` — фикстура `studio` и помощники

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `auth.COOKIE = "studio_sid"`, `SESSION_SEC = 30*86400`, `RENEW_SEC = 86400`, `LOCK_FAILS = 5`,
    `LOCK_SEC = 900`, `LOGIN_BODY_MAX = 4096`, `CSRF_HEADER = "x-studio-csrf"`, `LOGIN_RE = r"[a-z0-9._-]{2,32}"`
  - `@dataclass class User: id: int; login: str; name: str; is_admin: bool; theme: str; default_connection_id: int | None; default_model: str | None`
  - `auth.FAILS: dict[tuple, list[float]]` (ключи `("pair", login, ip)` и `("ip", ip)`)
  - `auth.client_ip(request) -> str` (перенос `server.client_ip`)
  - `auth.create_session(user_id: int) -> tuple[str, str]` — (значение cookie, csrf); в базе — `digest(cookie)`
  - `auth.session_user(cookie: str | None) -> tuple[User, str] | None` — живая сессия и csrf; отключённая учётка → None;
    если `NOW() - renewed > RENEW_SEC` — `expires = NOW() + SESSION_SEC`, `renewed = NOW()`
  - `auth.end_sessions(user_id: int, keep_digest: str | None = None) -> None`
  - `auth.require_admin(request) -> User` — иначе 403 `{"code": "admin"}`
  - `auth.router`: `GET /login` (страница; живая сессия → 303 на `/`), `POST /login`, `POST /logout`
  - middleware `auth.guard`: `/api/*` без живой сессии → 401 `{"code":"login","message":"нужен вход"}`; не-GET `/api/*`
    без заголовка `x-studio-csrf`, равного csrf сессии → 403 `{"code":"csrf"}`; ставит `request.state.user`, `request.state.csrf`
  - `main.app`; маршруты: `GET /health` → `{"status":"ok"}` (анонимно), `GET /` — живая сессия → `static/index.html`
    (пока заглушка «Студия»), иначе 303 на `/login`
  - `manage.py create-admin`: stdin — строки `KEY=VALUE`: `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD`,
    необязательно `GATEWAY_KEY_OWNER_IMAGES`; создаёт или обновляет администратора (пароль, `is_admin=1`, `disabled=0`).
    Подключение «srvllm» — задача 4 дописывает сюда.
  - conftest: фикстура `studio(env, monkeypatch)` → `SimpleNamespace(c=TestClient(main.app, base_url="https://testserver"), clock=[T0], sleeps, gw=FakeGateway())`:
    подменяет `clock.NOW`/`clock.SLEEP` (пауза двигает часы, как сейчас в `test_studio.py:100-103`), `state.make_http`
    → `httpx.AsyncClient(transport=httpx.MockTransport(gw.handler))`, `state.WATCHER = False`, чистит `auth.FAILS` (и далее `images.JOBS`, `chat.STREAMS`);
    помощники `make_user(s, login, password="password-123", is_admin=False) -> int`, `login(s, login, password, ip="10.77.48.5", spoofed=None)`,
    `signin(s, login) -> str` (вход + `GET /api/me` → csrf; ставит заголовок CSRF клиенту). `FakeGateway` — в `tests/fakes.py`
    (`TOKEN = "owner-images-test-key"`, `async handler(request)`); в этой задаче достаточно обработчика, отвечающего 404.

**Поведение `POST /login`:** форма `login`, `password` (тело ≤ 4096 байт, иначе 413 «Слишком длинный запрос»);
`Origin`, если есть, должен совпадать с `CFG.origin` (иначе 403); логин приводится к нижнему регистру;
проверка паузы до хеша; неудача записывается до проверки пароля и снимается при успехе (как в
`server.py:362-365`); неизвестный логин проверяется против `DUMMY_HASH`; хеш — в `asyncio.to_thread`.
Успех → 303 на `/`, cookie (`max_age=SESSION_SEC`, `path="/"`, `secure`, `httponly`, `samesite="lax"`).
Неверно → 401 страница «Неверный логин или пароль». Пауза → 429 «Слишком много попыток — подождите 15 минут».
Растущая задержка: неудач логина со всех адресов за 15 минут > 5 → перед ответом на неверный пароль
`await clock.SLEEP(min(2 ** (n - 5), 30))`. Адрес `CFG.hairpin_ip` учитывается только парой.
Журнал: «вход с <ip>: успешно / неверно / пауза» — без логина.

- [ ] **Step 1: Write the failing tests** — `tests/test_auth.py`:

```python
def test_health_is_open_and_api_needs_login(studio):
    assert studio.c.get("/health").json() == {"status": "ok"}
    r = studio.c.get("/api/me"); assert r.status_code == 401 and r.json()["detail"]["code"] == "login"
    assert studio.c.get("/", follow_redirects=False).headers["location"] == "/login"

def test_login_sets_a_lax_secure_httponly_cookie(studio):
    make_user(studio, "owner"); r = login(studio, "Owner", "password-123")
    assert r.status_code == 303 and r.headers["location"] == "/"
    c = r.headers["set-cookie"].lower()
    assert "studio_sid=" in c and "httponly" in c and "secure" in c and "samesite=lax" in c and "path=/" in c
    assert studio.c.get("/", follow_redirects=False).status_code == 200

def test_session_is_stored_as_a_digest_only(studio): ...  # значение cookie не встречается в sessions, digest — встречается

def test_wrong_password_and_unknown_login_read_the_same(studio): ...  # оба 401, одинаковый текст

def test_five_failures_lock_the_pair_but_not_the_login_elsewhere(studio):
    make_user(studio, "owner")
    for _ in range(5): login(studio, "owner", "nope", ip="203.0.113.9")
    assert login(studio, "owner", "password-123", ip="203.0.113.9").status_code == 429
    assert login(studio, "owner", "password-123", ip="198.51.100.7").status_code == 303

def test_five_logins_from_one_address_lock_the_address(studio): ...  # 5 разных логинов → шестой с верным паролем 429

def test_hairpin_address_is_locked_only_per_pair(studio): ...  # 10.77.1.1: 5 неудач по "a" → "owner" с верным паролем 303

def test_spoofed_forwarded_prefix_does_not_reset_the_lock(studio): ...  # перенос test_studio.py:417

def test_lock_expires_after_fifteen_minutes(studio): ...  # clock += 901 → 303

def test_login_failures_add_a_growing_delay(studio): ...  # 7 неудач с разных адресов → studio.sleeps содержит 2 и 4

def test_foreign_origin_cannot_post_login(studio): ...  # Origin: https://evil.test → 403

def test_oversized_login_body_is_refused(studio): ...  # 5000 байт → 413

def test_api_write_needs_the_csrf_header(studio): ...  # POST /api/me/password без заголовка → 403 code csrf (маршрут — задача 3; здесь любой POST /api/... даёт 403 раньше 404)

def test_disabled_user_session_dies(studio): ...  # UPDATE users SET disabled=1 → GET /api/me 401

def test_session_slides_and_expires(studio): ...  # clock += 2 дня → запрос продлевает; clock += 31 день без запросов → 401

def test_logout_ends_the_session_server_side(studio): ...  # старая cookie после /logout → 401

def test_create_admin_reads_stdin_and_is_idempotent(env, monkeypatch, capsys): ...
    # manage.main(["create-admin"]) со stdin "STUDIO_ADMIN_LOGIN=owner\nSTUDIO_ADMIN_PASSWORD=very-long-pass\n" дважды →
    # один пользователь is_admin=1; пароль не в выводе capsys

def test_passwords_and_logins_never_reach_the_log(studio, caplog): ...
```

- [ ] **Step 2: Run** `pytest -q tests/test_auth.py` — Expected: FAIL (`ModuleNotFoundError: auth`).
- [ ] **Step 3: Implement** `auth.py`, `main.py` (lifespan: `state.CFG = config.load()`, `state.DB = db.connect(...)`,
  `db.migrate`, `state.HTTP = state.make_http()`; закрытие клиента), `manage.py`, `static/login.html`
  (поля `#login`, `#password`, кнопка `#submit`, место `{{error}}` — экранируется `html.escape`; стиль — токены задачи 9 можно вписать позже, пока голая форма).
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS.
- [ ] **Step 5: Commit** «Add accounts: password login, server-side sessions, lockout, CSRF».

---

### Task 3: Профиль и панель администратора (API)

**Files:**
- Create: `app/profile.py`, `app/admin.py`, `app/history.py`
- Modify: `app/main.py` (роутеры)
- Test: `tests/test_profile_admin.py`

**Interfaces:**
- Consumes: Task 2 (`require_admin`, `end_sessions`, `User`).
- Produces:
  - `GET /api/me` → `{id, login, name, is_admin, theme, default_connection_id, default_model, csrf, capabilities}`
    (`capabilities` — `connections.capabilities(user_id)` из задачи 4; до неё — `{"chat": false, "photo": false, "video": false}`)
  - `PATCH /api/me` `{theme?: "auto"|"dark"|"light", default_connection_id?, default_model?}` → me
  - `POST /api/me/password` `{current, new}` → 204; гасит все сессии, кроме текущей; `len(new) < PASSWORD_MIN` → 422
  - `DELETE /api/history` → 204 — `await history.delete_all(user_id)`
  - `async history.delete_all(user_id: int) -> None` — удаляет строки `chats` (каскадом сообщения), `attachments`,
    `images`, `videos`, `media` и их файлы; активные ролики снимает у шлюза (`videos.cancel_at_gateway`, задача 7 —
    до неё ветка не вызывается); падение удаления файла не останавливает удаление строк
  - `GET /api/admin/users` → `{"items": [{id, login, name, is_admin, disabled, created, connections}]}`
  - `POST /api/admin/users` `{login, name, password, is_admin}` → 201 user; логин не по `LOGIN_RE` или занят → 422/409
  - `PATCH /api/admin/users/{id}` `{name?, is_admin?, disabled?, password?}` → user; `disabled=true` или `password` → `end_sessions`
  - `DELETE /api/admin/users/{id}` → 204 (история и файлы — `history.delete_all`, затем строка)
  - себя нельзя отключить, разжаловать и удалить → 409 `{"code":"self"}`
  - `GET /api/admin/allowlist` → `{"entries": [...]}`, `PUT /api/admin/allowlist` `{"entries": [...]}` — проверка
    `upstream.parse_entry` (задача 4; в этой задаче — заглушка, принимающая `host:port`)
  - чужую историю панель не отдаёт: ни один `/api/admin/*` не возвращает чаты, картинки, ролики

- [ ] **Step 1: Write the failing tests** — `tests/test_profile_admin.py`:

```python
def test_me_carries_csrf_and_profile(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    me = studio.c.get("/api/me").json()
    assert me["login"] == "owner" and me["is_admin"] and len(me["csrf"]) >= 32 and me["theme"] == "auto"

def test_theme_is_validated(studio): ...            # "pink" → 422; "dark" → me.theme == "dark"
def test_password_change_needs_current_and_ends_other_sessions(studio): ...
def test_non_admin_cannot_reach_the_panel(studio): ...   # каждый /api/admin/* → 403 code admin
def test_admin_creates_user_who_can_log_in(studio): ...
def test_login_must_be_simple_and_unique(studio): ...    # "Иван", "a", "x"*33 → 422; повтор → 409
def test_reset_password_and_disable_end_sessions(studio): ...
def test_admin_cannot_lock_themselves_out(studio): ...    # PATCH self disabled / is_admin=false, DELETE self → 409 self
def test_deleting_a_user_removes_rows_and_files(studio): ...   # файл в media_dir/u{id}/ пропадает
def test_panel_lists_no_history(studio): ...             # в JSON /api/admin/users нет prompt/title/content
```

- [ ] **Step 2: Run** `pytest -q tests/test_profile_admin.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `profile.py`, `admin.py`, `history.py`; подключить роутеры в `main.py`.
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS.
- [ ] **Step 5: Commit** «Add profile and admin API: users, passwords, allowlist, history wipe».

---

### Task 4: Подключения и запросы сервера только по списку

**Files:**
- Create: `app/connections.py`, `app/upstream.py`
- Modify: `app/admin.py` (проверка списка, подключения пользователя), `app/profile.py` (capabilities), `app/manage.py` (подключение «srvllm»)
- Test: `tests/test_connections.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `upstream.parse_entry(text: str) -> str` — `host:port` в нижнем регистре; хост — IPv4, `[IPv6]` или имя
    (`[a-z0-9.-]`), порт 1–65535 обязателен; иначе `ValueError`
  - `upstream.allowlist() -> list[str]` (из `settings`), `upstream.is_allowed(url: str) -> bool` — схема http/https,
    без userinfo, хост+порт (по умолчанию 80/443) **точно** совпадает с записью
  - `class upstream.UpstreamRefused(Exception)`
  - `async upstream.request(conn, method: str, url: str, **kw) -> httpx.Response` и
    `upstream.stream(conn, method: str, url: str, **kw)` (асинхронный менеджер контекста) — проверяют `is_allowed(url)`
    (иначе `UpstreamRefused`), `follow_redirects=False`, заголовок `Authorization: Bearer <token>`; клиент — `state.HTTP`
  - `upstream.gateway_error(response: httpx.Response, conn_name: str) -> str` — перенос `server.gateway_error`
    (`server.py:244-259`) с одним отличием: 401 → `шлюз не принял ключ подключения «{conn_name}» — проверьте токен в профиле`;
    `upstream.message(response) -> str | None` — перенос `server._message`
  - `connections.KINDS = {"openai": {"chat"}, "srvllm": {"chat", "photo", "video"}}`
  - `@dataclass(frozen=True) class Connection: id: int; user_id: int; name: str; kind: str; base_url: str; sections: frozenset[str]; proxied: bool`
  - `connections.list_for(user_id) -> list[Connection]`, `connections.get(user_id, conn_id) -> Connection` (чужое → 404 `{"code":"gone"}`),
    `connections.token(conn) -> str`
  - `connections.chat_url(conn)` — openai: `{base}/chat/completions`, srvllm: `{base}/v1/chat/completions`;
    `connections.models_url(conn)` — `{base}/models` / `{base}/v1/models`; `connections.slot_url(conn, path)` — srvllm: `{base}/slot{path}`
  - `connections.photo_connection(user_id) -> Connection | None` — первое подключение srvllm с `photo` в разделах и `proxied`;
    `video_connection` — то же для `video`
  - `connections.capabilities(user_id) -> {"chat": bool, "photo": bool, "video": bool}`
  - API: `GET /api/connections` → `{"items": [ConnOut]}`; `POST` `{name, kind, base_url, token, sections}` → 201 ConnOut;
    `PATCH /api/connections/{id}` (поля те же, `token` — только если прислан); `DELETE` → 204;
    `GET /api/connections/{id}/models` → `{"items": [{"id", "context_length"?}]}` (через сервер для proxied — `upstream.request`;
    для прямых — 409 `{"code":"direct"}`: список берёт браузер сам);
    `GET /api/connections/{id}/token` → `{"token"}` только для **не** proxied, иначе 403 `{"code":"proxied"}`
  - `ConnOut = {id, name, kind, base_url, sections: [...], proxied: bool, token_hint: "…" + последние 4 знака}` — токена целиком нет
  - Админ: `GET/POST /api/admin/users/{uid}/connections`, `PATCH/DELETE /api/admin/users/{uid}/connections/{id}` — те же функции с `uid`
  - conftest: помощники `add_connection(s, kind="openai", base_url="https://cloud.test/v1", token="cloud-key", sections=None, name=None) -> int`
    и `add_gateway(s) -> int` (kind `srvllm`, `http://10.77.1.15:4000`, токен `FakeGateway.TOKEN`, все разделы) — ими пользуются задачи 6–8
  - `manage.py create-admin` с `GATEWAY_KEY_OWNER_IMAGES` → подключение «srvllm», kind `srvllm`, `http://10.77.1.15:4000`,
    разделы все три, `default_connection_id` — оно, `default_model = "local-gemma-abl"`; повторный запуск обновляет токен, не дублирует

**Проверка подключения при сохранении:** `base_url` — http/https, без userinfo, query, fragment; хвостовой `/`
срезается. `proxied = is_allowed(base_url)`. Не proxied: схема `https`, кроме хостов `localhost`, `127.0.0.1`,
`[::1]` (их браузер считает безопасными) — иначе 422 «адрес без https браузер не откроет; внутренний адрес
должен быть в списке администратора». `srvllm` обязан быть proxied — иначе 422. Разделы ⊆ `KINDS[kind]`,
не пусто. Токен 1–512 печатных ASCII.

- [ ] **Step 1: Write the failing tests** — `tests/test_connections.py`:

```python
def test_tricky_urls_are_not_allowlisted(studio):
    for url in ["http://10.77.1.15:4000@evil.test", "http://10.77.1.15:40000", "http://10.77.1.15.evil.test:4000",
                "http://evil.test/?h=10.77.1.15:4000", "ftp://10.77.1.15:4000", "http://10.77.1.15"]:
        assert not upstream.is_allowed(url), url
    assert upstream.is_allowed("http://10.77.1.15:4000/v1/models")

def test_upstream_never_follows_redirects(studio): ...
    # FakeGateway отвечает 302 Location: http://10.77.1.1/ → upstream.request возвращает 302, второго запроса нет

def test_request_outside_the_list_is_refused_before_any_io(studio): ...   # UpstreamRefused, gw не получил запроса

def test_proxied_token_never_leaves_the_server(studio):
    cid = add_connection(studio, kind="srvllm", base_url="http://10.77.1.15:4000", token="gw-secret-token")
    assert "gw-secret-token" not in studio.c.get("/api/connections").text
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403

def test_direct_connection_needs_https_except_localhost(studio): ...
def test_srvllm_kind_must_be_on_the_list(studio): ...
def test_direct_token_is_given_to_its_owner_only(studio): ...   # другой пользователь → 404
def test_token_is_encrypted_at_rest(studio): ...                 # в connections.token_enc нет исходного текста
def test_capabilities_follow_connections(studio): ...            # нет подключений → всё false; srvllm(chat,photo) → video false
def test_models_of_a_proxied_connection_come_through_the_server(studio): ...
def test_admin_manages_a_users_connections(studio): ...
def test_allowlist_entries_are_validated(studio): ...            # "10.77.1.15" (без порта), "a b:1" → 422
def test_create_admin_sets_up_the_owner_gateway(env, monkeypatch): ...  # default_model == "local-gemma-abl"
def test_gateway_401_words_point_to_the_profile(): ...           # gateway_error(Response(401), "srvllm") содержит «srvllm» и «профиле»
```

- [ ] **Step 2: Run** `pytest -q tests/test_connections.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `upstream.py`, `connections.py`; подключить в `admin.py`, `profile.py`, `manage.py`, `main.py`.
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS.
- [ ] **Step 5: Commit** «Add per-profile connections; server calls only allowlisted hosts».

---

### Task 5: Файлы истории на шаре

**Files:**
- Create: `app/media.py`
- Modify: `app/main.py`, `app/history.py` (удаление файлов через `media.delete`)
- Test: `tests/test_media.py`

**Interfaces:**
- Produces:
  - `class media.StorageError(Exception)` — текст «не удалось сохранить файл: хранилище недоступно»
  - `media.THUMB_SIDE = 384`
  - `media.save(user_id: int, data: bytes, mime: str, download_name: str, *, thumb: bool = False) -> str` — id `uuid4().hex`;
    путь `u{user_id}/{YYYY}/{MM}/{id}{ext}` (дата — `clock.NOW()`), запись во временный файл рядом + `os.replace`;
    `thumb=True` — ещё `{id}.thumb.webp` (WebP, длинная сторона ≤ 384, Pillow в `asyncio.to_thread` у вызывающего);
    любая `OSError` → `StorageError`, строка в базе появляется только после записи файла
  - `async media.save_stream(user_id, chunks: AsyncIterator[bytes], mime, download_name, max_bytes: int) -> str` — тот же
    контракт потоком (ролики); больше `max_bytes` → `StorageError("файл больше предела")`, временный файл удалён
  - `media.path(user_id, media_id, thumb=False) -> Path` — чужое или нет → 404 `{"code":"gone"}`
  - `media.delete(user_id, media_id) -> None` — нет файла — не ошибка
  - `media.byte_range(header: str, size: int) -> tuple[int, int] | None` — перенос `server.byte_range` (`server.py:712-725`)
  - `GET /api/media/{id}` (`?thumb=1`, `?download=1`) — `Accept-Ranges: bytes`, 206/416 как в `server.py:738-758`, но из файла
    (читается только нужный кусок); `Cache-Control: private, max-age=86400`; `download=1` →
    `Content-Disposition: attachment; filename="<download_name>"`; без превью (`has_thumb=0`) `thumb=1` отдаёт оригинал

- [ ] **Step 1: Write the failing tests** — `tests/test_media.py`:

```python
def test_save_and_read_back_with_ranges(studio): ...       # 1000 байт → Range bytes=10-19 → 206, 10 байт, Content-Range bytes 10-19/1000
def test_suffix_and_bad_ranges(studio): ...                 # bytes=-5 → последние 5; bytes=2000- → 416 c bytes */1000
def test_thumb_is_small_webp(studio): ...                   # PNG 1216x832 → thumb ≤ 384 по длинной стороне, image/webp
def test_other_users_media_is_404(studio): ...
def test_save_into_unwritable_dir_raises_storage_error(studio, monkeypatch): ...
    # media_dir указывает на обычный файл → StorageError, в таблице media строки нет
def test_stream_over_limit_leaves_nothing(studio): ...      # save_stream с max_bytes=10 и 20 байтами → StorageError, в каталоге пусто
def test_download_name_is_attachment(studio): ...
def test_history_wipe_removes_files(studio): ...
```

- [ ] **Step 2: Run** `pytest -q tests/test_media.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `media.py`, маршрут, вызовы из `history.delete_all`.
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS.
- [ ] **Step 5: Commit** «Store history files on the share with thumbnails and ranged reads».

---

### Task 6: Картинки — задания, лента, отмена, описание (перенос из `server.py`)

**Files:**
- Create: `app/images.py`
- Modify: `app/main.py` (роутер; `images.recover()` в lifespan), `tests/fakes.py` (`FakeGateway` — генерация, правка, release, описания:
  перенос класса `Gateway` из `tests/test_studio.py:39-86` с путями `/slot/...` и проверкой ключа подключения)
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes: `connections.photo_connection`, `connections.slot_url`, `connections.chat_url`, `upstream.request`, `upstream.gateway_error`,
  `media.save`, `media.path`.
- Produces:
  - константы — перенос из `server.py:83-113` (`MODELS`, `SIZES`, `DESCRIPTION_MODEL = "gemma4-abl"`, `DESCRIPTION_PROMPT`
    дословно, `GENERATE_TIMEOUT_SEC = 1300`, `SHORT_TIMEOUT_SEC = 30`, `CANCEL_RELEASE_SEC = 60`, `RELEASE_PAUSE_SEC = 2`, `CANCELLING`)
  - `@dataclass class Job` — поля `server.Job` (`server.py:131-151`) без `key`, плюс `user_id: int`, `conn: Connection`,
    `source_media_id: str | None`; `images.JOBS: dict[str, Job]`
  - `POST /api/images` (202) — JSON или multipart как в `server.py:444-478` (те же поля, `JobIn`, `read_image_form`, маска);
    плюс поле `source_media_id` (фото из истории вместо файла; нормализуется тем же `normalize_image`). Нет фото-подключения →
    409 `{"code":"no_gateway","message":"добавьте подключение к шлюзу srvllm в профиле"}`. Активное задание **этого пользователя** →
    409 busy (текст `server.py:438-440`); задания других пользователей не мешают. Ответ `{id, seed}`; в `images` — строка `working`;
    исходник (если был) сохраняется в media (`source_media_id`).
  - `GET /api/images?before=<float>&limit=<1..100, 60>` → `{"items": [ImageOut]}` новые сверху, вместе с идущими
  - `GET /api/images/{id}` → `ImageOut = {id, kind, state, detail, elapsed, model, prompt, size, seed, strength, source_media_id, media_id, created}`
  - `POST /api/images/{id}/cancel` → `{"state"}` (логика `server.py:521-529`)
  - `DELETE /api/images/{id}` → 204; идущее → 409 `{"code":"running","message":"сначала отмените"}`
  - `POST /api/images/descriptions` (202, multipart `image` или `source_media_id`) → `{id}`; `GET /api/images/descriptions/{id}` →
    статус, а при готовности — `{state:"done", text, truncated}` один раз (перенос `server.py:532-539`); описания в историю не пишутся
  - `POST /api/images/release` — только администратор (`require_admin`), его фото-подключение; ответ как `server.py:542-562`
  - `run_job`, `generate`, `describe`, `release_after_cancel` — перенос `server.py:772-901` поверх `upstream.request` и
    `connections.slot_url(conn, "/images/generations" | "/images/edits" | "/images/release" | "/images/descriptions" | "/images/descriptions/release")`;
    готовая PNG → `media.save(..., thumb=True, download_name=f"studio-{model}-{seed}.png")` → строка `done` с `media_id` и итоговым seed;
    `StorageError` → `error` с её текстом
  - `images.recover() -> None` — строки `working`/`cancelling` → `error`, «прервано перезапуском студии»
  - журнал — только id и модель (как сейчас)

- [ ] **Step 1: Write the failing tests** — `tests/test_images.py`: перенести и адаптировать из `tests/test_studio.py`
  (вход — `signin`, подключение «srvllm» — помощник `add_gateway(studio)`):
  `test_job_goes_through_the_gateway_with_the_connection_key` (`:462`), `test_model_is_chosen_on_the_page` (`:479`),
  `test_seed_from_the_gateway_answer_is_shown` (`:485`), `test_gateway_failures_read_as_plain_words` (`:500`),
  `test_gateway_read_timeout_is_an_error_not_a_hang` (`:507`), `test_working_status_shows_the_elapsed_time` (`:514`),
  `test_cancel_waits_for_the_gateway_request_then_releases_the_models_slot` (`:564`), `test_cancel_retries_release_while_the_slot_is_busy` (`:587`),
  `test_cancel_gives_up_release_after_a_minute_of_409` (`:597`), `test_finished_job_is_not_cancelled` (`:609`),
  `test_release_reports_each_model_in_plain_words` (`:625`), `test_release_shows_the_busy_reason` (`:678`),
  `test_prompt_size_seed_and_model_are_validated` (`:649`), `test_uploaded_photo_is_forwarded_to_edits_and_discarded` (`:142`),
  `test_invalid_photo_creates_no_studio_job` (`:173`), `test_masked_edit_preserves_source_size_and_pixels_without_forwarding_mask` (`:180`),
  `test_original_size_requires_a_photo` (`:225`), `test_maximum_prompt_in_ascii_escaped_json_reaches_gateway` (`:231`),
  `test_description_uses_gemma_abl_and_returns_editable_text_once` (`:250`), `test_description_cancellation_waits_then_discards_text_and_releases` (`:282`),
  `test_description_can_be_cancelled_during_model_release` (`:307`), `test_empty_description_errors_and_releases_model` (`:332`),
  `test_description_reports_running_videos` (`:690`), `test_prompt_and_key_never_reach_the_log_or_the_answers` (`:663`, ключ — токен подключения).
  Новые:

```python
def test_finished_image_lands_in_history_with_a_thumb(studio):
    add_gateway(studio); job = start(studio).json()
    item = wait_state(studio, job["id"], {"done"})
    assert item["media_id"] and studio.c.get(f"/api/media/{item['media_id']}").content == PNG_REAL
    assert studio.c.get("/api/images").json()["items"][0]["id"] == job["id"]

def test_one_job_per_user_not_per_service(studio): ...       # у owner идёт → у friend 202, у owner второй → 409 busy
def test_photo_without_gateway_connection_is_explained(studio): ...   # 409 no_gateway
def test_edit_from_history_uses_the_stored_source(studio): ...         # source_media_id → в /images/edits ушёл тот же PNG
def test_release_is_for_admins_only(studio): ...                      # friend → 403 admin
def test_gateway_401_points_to_the_profile_not_the_login(studio): ...  # задание error с «профиле»; GET /api/me по-прежнему 200
def test_unsaved_image_becomes_an_error(studio, monkeypatch): ...       # media.save бросает StorageError → state error, текст «не удалось сохранить файл»
def test_restart_marks_running_images_interrupted(studio): ...          # строка working → images.recover() → error «прервано перезапуском студии»
def test_running_image_cannot_be_deleted_and_done_one_can(studio): ...
def test_feed_pages_by_created(studio): ...                            # before= отдаёт старшие, limit соблюдён
```
  `FakeGateway` отдаёт настоящий PNG (Pillow), чтобы превью строилось: `PNG_REAL`.

- [ ] **Step 2: Run** `pytest -q tests/test_images.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `images.py` переносом кода `server.py` (поведение отмены и release не менять; сравнить диффом с оригиналом).
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS, включая старый `tests/test_studio.py` (старый `server.py` пока не тронут).
- [ ] **Step 5: Commit** «Move image jobs onto accounts and history».

---

### Task 7: Ролики — задания, наблюдатель, лента

**Files:**
- Create: `app/videos.py`
- Modify: `app/main.py` (роутер; `if state.WATCHER: videos.start_watcher()` в lifespan, отмена при остановке), `app/history.py` (`videos.cancel_at_gateway`),
  `tests/fakes.py` (`FakeGateway`: `/slot/videos` создание, статус, `content`, удаление — перенос фикстуры `videos` из `tests/test_studio.py:700-756`)
- Test: `tests/test_videos.py`

**Interfaces:**
- Consumes: `connections.video_connection`, `connections.slot_url(conn, "/videos")`, `upstream.request`/`upstream.stream`, `media.save`, `media.save_stream`.
- Produces:
  - константы: `VIDEO_MODEL = "wan2.2-remix-i2v-v3"`, `VIDEO_ID_RE` (`server.py:116`), `VIDEO_TIMEOUT_SEC = 120`,
    `VIDEO_CONTENT_MAX = 300 * 1024 * 1024`, `POLL_SEC = 5`; `VideoIn` — перенос `server.py:567-572`
  - `POST /api/videos` — multipart: файл `image` (`read_image_form(..., max_upload=VIDEO_UPLOAD, max_pixels=VIDEO_PIXELS)`) **или** поле
    `source_media_id`; поля `prompt, seconds, quality, smooth, seed` (пустое поле — «по умолчанию», как `server.py:627`).
    Источник сохраняется в media (с превью), затем шлюзу — форма `server.py:633-641` (имя файла `input.png`).
    Нет видео-подключения → 409 `no_gateway`. Ответ 201 `VideoOut`.
  - `VideoOut = {id, state, progress, detail, prompt, prompt_source, seconds, quality, smooth, seed, source_media_id, media_id, created, elapsed}`;
    `state ∈ {"queued","in_progress","done","failed","cancelled"}`
  - `GET /api/videos?before&limit` → `{"items": [...]}`; `GET /api/videos/{id}`; `POST /api/videos/{id}/cancel` (DELETE у шлюза, `cancelled`);
    `DELETE /api/videos/{id}` → 204 (активный — сначала снять у шлюза; файлы и строка удаляются)
  - `async videos.poll_once() -> None` — по всем строкам `queued`/`in_progress`: `GET {slot}/videos/{gateway_id}`; обновляет
    `state/progress/detail/prompt/prompt_source/seed`; `completed` → `upstream.stream` `.../content` → `media.save_stream(..., max_bytes=VIDEO_CONTENT_MAX,
    download_name=f"studio-video-{seed}.mp4")` → `DELETE` у шлюза → `done`; `failed` → `failed` c `detail`; 404 у шлюза → `failed`
    «шлюз забыл задание»; 401 → `failed` с `gateway_error` (сессию не трогает); сеть недоступна — строка не меняется (следующий проход)
  - `videos.start_watcher() -> asyncio.Task` — цикл `poll_once()` + `await clock.SLEEP(POLL_SEC)`; исключения прохода пишутся в журнал (без промптов) и не останавливают цикл
  - `async videos.cancel_at_gateway(user_id: int) -> None` — снять у шлюза все активные ролики пользователя (для `history.delete_all`)
  - каждый видит только свои строки; список шлюза (`GET /slot/videos`) студия больше не читает

- [ ] **Step 1: Write the failing tests** — `tests/test_videos.py`: перенести и адаптировать из `tests/test_studio.py`
  `test_video_is_created_as_gateway_multipart_without_private_names` (`:757`), `test_video_defaults_and_seed_reach_the_gateway` (`:775`),
  `test_video_fields_are_validated_before_the_gateway` (`:787`), `test_video_needs_a_readable_image` (`:793`),
  `test_video_upload_limit_is_twenty_megabytes_and_json_jobs_keep_theirs` (`:800`), `test_video_gateway_refusals_keep_their_code` (`:814`),
  `test_bad_video_id_never_reaches_the_gateway` (`:840`). Новые:

```python
def test_watcher_brings_a_finished_video_into_history(studio):
    add_gateway(studio); v = create_video(studio).json()
    studio.gw.video_status = "completed"; studio.c.portal.call(videos.poll_once)
    item = studio.c.get(f"/api/videos/{v['id']}").json()
    assert item["state"] == "done" and item["media_id"]
    assert studio.gw.video_deleted == [v_gateway_id(studio)]
    r = studio.c.get(f"/api/media/{item['media_id']}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.headers["content-type"] == "video/mp4"

def test_progress_and_detail_follow_the_gateway(studio): ...
def test_failed_and_forgotten_jobs_become_failed(studio): ...       # failed → detail шлюза; 404 → «шлюз забыл задание»
def test_video_gateway_401_keeps_the_session(studio): ...            # failed с «профиле»; /api/me → 200
def test_watcher_resumes_queued_videos_after_restart(studio): ...    # новый TestClient (перезапуск) → poll_once доводит до done
def test_video_from_history_image(studio): ...                       # source_media_id → шлюзу ушёл тот же PNG
def test_users_see_only_their_videos(studio): ...
def test_cancel_and_delete(studio): ...                             # cancel → DELETE у шлюза, state cancelled; delete → строки и файлов нет
def test_too_big_video_fails_cleanly(studio): ...                    # content > предела → failed, файлов нет
def test_watcher_survives_a_gateway_outage(studio): ...              # gw.down → строка без изменений, исключения наружу нет
```
  (`poll_once` идёт в цикле приложения — через `studio.c.portal.call(videos.poll_once)`: там живёт `state.HTTP`. Помощники `create_video`,
  `v_gateway_id` — локальные в файле теста.)

- [ ] **Step 2: Run** `pytest -q tests/test_videos.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `videos.py`.
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS.
- [ ] **Step 5: Commit** «Move video jobs onto accounts; server fetches finished clips into history».

---

### Task 8: Чат — диалоги, вложения, поток через сервер

**Files:**
- Create: `app/documents.py`, `app/chat.py`
- Modify: `app/main.py` (роутер; восстановление потоков в lifespan), `tests/fakes.py` (`FakeGateway`: `/v1/models`, `/v1/chat/completions` с `stream`)
- Test: `tests/test_documents.py`, `tests/test_chat.py`

**Interfaces:**
- Consumes: `connections.get/chat_url/models_url`, `upstream.stream`, `media.save`, `image_upload.normalize_image`.
- Produces:
  - `documents.DOC_MAX = 20 * 1024 * 1024`, `TEXT_MAX_CHARS = 2_000_000`, `SUFFIXES = (".pdf", ".docx", ".txt", ".md")`;
    `class DocumentError(ValueError)`; `async documents.read_upload(request) -> tuple[str, bytes]` (multipart, одно поле `file`,
    поток с пределом `DOC_MAX + 128 КиБ`, имя файла — из `filename`); `documents.extract(name: str, data: bytes) -> str` —
    PDF `pypdf` (страницы через `\n\n`), DOCX — `zipfile` + `word/document.xml` (абзацы `w:p`, текст `w:t`), TXT/MD — UTF-8, иначе cp1251.
    Отказы словами: «поддерживаются PDF, DOCX, TXT и MD», «в PDF нет текстового слоя — это скан», «документ повреждён», «текст длиннее 2 млн знаков»
  - `chat.PING_SEC = 15`, `chat.RESERVE_TOKENS = 4096`, `chat.MAX_IMAGES = 4`, `chat.CONTEXT = {"local-gemma-abl": 131072}`, `chat.TITLE_CHARS = 60`
  - `chat.STREAMS: dict[str, StreamTask]` (ключ — id ответа ассистента)
  - `chat.compose(chat_id: str, upto: str) -> list[dict]` — сообщения OpenAI до `upto` включительно, только `done`/`stopped`;
    документы — перед текстом пользователя блоками `<document name="{name}">\n{text}\n</document>\n\n`; картинки — части
    `{"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}` (не больше 4 на сообщение)
  - `chat.estimate_tokens(messages) -> int` — `ceil(знаков текста / 3) + 1000 × картинок`
  - API:
    - `GET /api/chats` → `{"items": [{id, title, connection_id, model, updated}]}` (новые сверху)
    - `POST /api/chats` `{connection_id?, model?}` → chat (по умолчанию — из профиля); `PATCH /api/chats/{id}` `{title?, connection_id?, model?}`; `DELETE /api/chats/{id}` → 204
    - `GET /api/chats/{id}` → `{chat, messages: [{id, role, content, status, model, created, attachments: [{id, kind, name, media_id, chars}]}]}`
    - `POST /api/attachments` (multipart `file`) → `{id, kind, name, chars, media_id}` — картинка (PNG/JPEG/WebP → `normalize_image`)
      или документ (`extract`); файл — в media; не привязано к сообщению до хода
    - `POST /api/chats/{id}/turns` `{text, attachment_ids: [], regenerate: false}`:
      сохраняет сообщение пользователя (или при `regenerate` удаляет последний ответ ассистента), привязывает вложения,
      заводит ответ ассистента `streaming`; название пустого диалога — первые 60 знаков первой строки текста;
      проверка контекста: известный предел (`CONTEXT` или `context_length` из списка моделей подключения) и оценка
      `> предел − RESERVE_TOKENS` → 413 `{"code":"context","message":"Не помещается в контекст модели: ~{N} тыс. токенов при пределе {M} тыс. Уберите документ или начните новый диалог."}`
      (сообщение пользователя при этом не сохраняется);
      - подключение proxied → `text/event-stream`: `event: turn` `{user_message_id, assistant_message_id}`, `event: delta` `{text}`,
        `event: done` `{status}`, `event: error` `{message}`; комментарий `: ping` каждые `PING_SEC`, пока нет данных.
        Запрос к модели (`stream: true`) ведёт фоновая задача `StreamTask`: читает до конца и сохраняет ответ (`done`) даже
        если клиент ушёл; ошибка шлюза → `error` с `gateway_error`;
      - прямое подключение → JSON `{user_message_id, assistant_message_id, url: chat_url(conn), model, messages: compose(...)}`
    - `PUT /api/chats/{id}/messages/{mid}` `{content, status: "done"|"stopped"|"error"}` — только ответ ассистента в `streaming`, `via='browser'`
    - `POST /api/chats/{id}/stop` → 204 — отменяет `StreamTask`, ответ сохраняется как есть со статусом `stopped`
  - `chat.recover() -> None` — `streaming` с `via='server'` → `error`, содержимое + «\n\n[прервано перезапуском студии]»

- [ ] **Step 1: Write the failing tests** — `tests/test_documents.py`:

```python
def test_pdf_docx_txt_md_are_extracted(): ...      # фикстуры создаются в тесте: PDF через pypdf (страница с текстом), DOCX — zip с document.xml
def test_cp1251_text_is_decoded(): ...
def test_scanned_pdf_is_explained(): ...           # PDF без текста → DocumentError «скан»
def test_unknown_or_broken_files_are_refused(): ...
```
  `tests/test_chat.py`:

```python
def test_proxied_turn_streams_and_saves(studio):
    add_gateway(studio); chat = studio.c.post("/api/chats", json={}).json()
    events = sse(studio.c.post(f"/api/chats/{chat['id']}/turns", json={"text": "Привет"}))
    assert [e for e, _ in events][0] == "turn" and "".join(d["text"] for e, d in events if e == "delta") == "Здравствуйте!"
    msgs = studio.c.get(f"/api/chats/{chat['id']}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["status"] == "done"
    assert studio.gw.chat_requests[0]["model"] == "local-gemma-abl" and studio.gw.chat_requests[0]["stream"] is True

def test_answer_is_saved_when_the_client_disconnects(studio): ...
    # читать поток до первой delta и закрыть ответ → после окончания у шлюза сообщение done с полным текстом
def test_pings_keep_a_slow_model_alive(studio): ...   # шлюз молчит 40 «секунд» (clock) → в потоке ≥ 2 «: ping» до первой delta
def test_stop_keeps_the_partial_answer(studio): ...
def test_document_goes_in_full_before_the_question(studio): ...   # compose: блок <document name="договор.pdf"> … перед текстом
def test_too_big_document_is_refused_before_saving(studio): ...  # 413 context, сообщений в диалоге нет
def test_images_go_as_image_url_parts_up_to_four(studio): ...     # 5 картинок → 422
def test_direct_turn_returns_messages_and_accepts_the_answer(studio): ...
    # openai-подключение https://cloud.test/v1 → JSON с url https://cloud.test/v1/chat/completions; PUT done → сохранено; повторный PUT → 409
def test_regenerate_replaces_the_last_answer(studio): ...
def test_title_comes_from_the_first_line(studio): ...
def test_chats_are_private(studio): ...                         # чужой chat id → 404 на GET/PATCH/DELETE/turns
def test_restart_marks_server_streams_interrupted(studio): ...
def test_chat_text_never_reaches_the_log(studio, caplog): ...
```

- [ ] **Step 2: Run** `pytest -q tests/test_documents.py tests/test_chat.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `documents.py`, `chat.py` (`StreamTask`: `asyncio.Task` + `asyncio.Queue`; генератор ответа читает очередь;
  пинги — отдельная корутина на время потока: `await clock.SLEEP(PING_SEC)` → в очередь маркер «ping», если с прошлого маркера данных не было
  (реального времени в тестах нет); уход клиента задачу модели не отменяет). Помощник `sse(response)` — локальный в файле теста.
- [ ] **Step 4: Run** `pytest -q` — Expected: PASS; набор укладывается в 60 с (`--durations=10`, тестов дольше 1 с нет).
- [ ] **Step 5: Commit** «Add chat: conversations, documents and photos, streaming through the server».

---

### Task 9: Оболочка приложения — темы, раскладка, вход, PWA

**Files:**
- Create: `app/static/index.html`, `app/static/offline.html`, `app/static/css/app.css`, `app/static/js/api.js`, `app/static/js/app.js`,
  `app/static/js/sse.js`, `app/static/js/markdown.js`, `app/static/vendor/{marked.min.js, purify.min.js, highlight.min.js, hl-paper.css, hl-graphite.css}`,
  `app/static/icons/{icon.svg, icon-192.png, icon-512.png, icon-maskable-512.png, apple-touch-icon.png}`, `app/static/sw.js`, `app/static/manifest.webmanifest`,
  `tests/live_app.py`, `tests/test_ui_browser.py`
- Modify: `app/static/login.html` (стиль), `app/main.py` (статика, `/manifest.webmanifest`, `/sw.js`, `/offline.html`, заголовки)
- Test: `tests/test_static.py` (быстрый), `tests/test_ui_browser.py` (integration)

**Interfaces:**
- Consumes: `/api/me`, `/login`, `/logout`.
- Produces:
  - `main.STATIC_VERSION` — первые 12 hex sha256 всех файлов `app/static` при старте; `index.html` и `login.html` получают ссылки
    `?v=STATIC_VERSION` (подстановка `{{v}}`); `/sw.js` отдаётся с `const VERSION = "<STATIC_VERSION>"` в начале, `Cache-Control: no-cache`,
    `Service-Worker-Allowed: /`
  - заголовки HTML-ответов: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`,
    `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`
  - анонимно доступны: `/health`, `/login`, `/logout`, `/static/*`, `/manifest.webmanifest`, `/sw.js`, `/offline.html`
  - `js/api.js`: `export async function api(method: string, path: string, body?: object | FormData): Promise<any>` — CSRF-заголовок из
    `me.csrf`, JSON; отказ → `throw new ApiError(status, code, message)`; `code === "login"` → `location.href = "/login"`;
    `export let me` + `export async function loadMe()`
  - `js/sse.js`: `export async function* readSSE(response: Response): AsyncGenerator<{event: string, data: any}>` (комментарии пропускает)
  - `js/markdown.js`: `export function renderMarkdown(text: string): DocumentFragment` — marked → DOMPurify → highlight.js по `pre code`,
    у каждого `pre` кнопка `.copy` «Копировать»; ссылки `target=_blank rel=noopener`
  - `js/app.js`: роутер по hash: `#/chat`, `#/chat/<id>`, `#/photo`, `#/video`, `#/profile`, `#/admin`; модуль раздела —
    `export function mount(el: HTMLElement, ctx: {me, navigate}): void` и `export function unmount(): void`; по умолчанию `#/chat`;
    тема: `document.documentElement.dataset.theme = me.theme` (`auto` — атрибута нет, работает `prefers-color-scheme`);
    регистрирует `/sw.js`; разделы без возможности (`me.capabilities.photo === false`) открываются экраном-подсказкой
    «Чтобы генерировать фото, добавьте в профиле подключение к шлюзу srvllm» со ссылкой на `#/profile`
  - разметка оболочки: `nav#rail` (`a[data-section=chat|photo|video]`, внизу `button#avatar` → меню: Профиль, Панель (только `me.is_admin`), Выход),
    `main#view`; ширина ≤ 760px — `nav#rail` становится нижними вкладками (`position: fixed; bottom: 0`), меню аватара — в шапке раздела
  - `css/app.css` — токены (дословно из макетов `visual-style.html`/`photo-layout.html`):

```css
:root, [data-theme="light"] { --bg:#fbfaf7; --side:#f3f0ea; --rail:#ebe7df; --panel:#ffffff; --panel-2:#efebe4; --bubble:#eeeae3;
  --text:#1f1d1a; --muted:#7b766d; --border:#e3dfd6; --accent:#c96442; --accent-text:#ffffff; --ok:#2f9e6f; --danger:#b42318; }
[data-theme="dark"] { --bg:#1b1b1b; --side:#131313; --rail:#0d0d0d; --panel:#262626; --panel-2:#303030; --bubble:#2f2f2f;
  --text:#ececec; --muted:#8f8f8f; --border:#2a2a2a; --accent:#ececec; --accent-text:#111111; --ok:#3ecf8e; --danger:#f87171; }
@media (prefers-color-scheme: dark) { :root:not([data-theme]) { /* те же значения, что [data-theme="dark"] */ } }
```
    шрифт `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`; радиусы 10–16px; общие классы `.composer` (поле ввода снизу),
    `.chip`, `.btn`, `.btn-accent`, `.feed-grid`, `.card`, `.dialog` — их используют задачи 10–13
  - `manifest.webmanifest`: `name` «Студия», `short_name` «Студия», `start_url` `/`, `scope` `/`, `display` `standalone`, `lang` `ru`,
    `background_color` `#1b1b1b`, `theme_color` `#1b1b1b`, иконки 192/512/maskable-512; в `index.html` — `meta theme-color` двумя
    `media`-вариантами (`#fbfaf7` / `#1b1b1b`) и `apple-touch-icon`
  - `sw.js`: кэш `studio-shell-<VERSION>` — `/offline.html`, `css/app.css`, иконки; `fetch`: навигация → сеть, при ошибке — `/offline.html`;
    `/api/*`, `/static/js/*`, медиа — никогда не кэшируются; `activate` удаляет чужие версии
  - `offline.html` — «Нет связи. Студия откроется, когда появится интернет.» в обеих темах
  - иконка — скруглённый квадрат цвета `#c96442` с белым знаком «искра»/волна (на выбор исполнителя); PNG делаются из SVG Pillow-скриптом
    исполнителя один раз, в репозиторий — готовые файлы
  - `tests/live_app.py`: `start_live_app(tmp_path) -> LiveApp(url="http://localhost:<port>", gw: FakeGateway, cloud: FakeCloud, stop())` —
    окружение как у `env`, `state.make_http` → MockTransport `FakeGateway`, uvicorn в потоке на свободном порту `localhost`;
    создаёт администратора `owner`/`password-123` с подключением «srvllm», `state.WATCHER = True` (с `POLL_SEC`, сокращённым до 0.2 с).
    Chromium принимает Secure-cookie с `http://localhost`; если браузер откажет — поднять uvicorn с самоподписанным сертификатом и
    запускать браузер с `--ignore-certificate-errors`. `FakeCloud` — настоящий HTTP-сервер на `localhost` с CORS
    (`Access-Control-Allow-Origin: *`, `Authorization` в `Allow-Headers`), отвечающий `/v1/models` и потоковым `/v1/chat/completions`
- Сторонние библиотеки: последние стабильные `marked`, `dompurify`, `highlight.js` (сборка common) из npm (`npm pack`), файлы кладутся
  как есть с баннером версии. **Скачивание — с разрешения владельца** (назвать файлы, источник, размер) — правило безопасности.

- [ ] **Step 1: Write the failing tests** — `tests/test_static.py`:

```python
def test_static_has_no_external_urls():
    for p in (APP / "static").rglob("*"):
        if p.suffix in {".html", ".js", ".css", ".webmanifest"} and "vendor" not in p.parts:
            assert not re.search(r"https?://(?!localhost)", p.read_text(encoding="utf-8")), p

def test_manifest_makes_an_installable_app(): ...    # name, start_url "/", display standalone, иконки 192 и 512 есть на диске
def test_service_worker_never_caches_api_or_media(): ...   # в sw.js есть ветка /api/ без cache.put
def test_pages_send_csp_and_nosniff(studio): ...     # /login и / (после входа): CSP содержит "script-src 'self'", frame-ancestors 'none'
def test_shell_files_are_public_and_api_is_not(studio): ...  # /sw.js, /manifest.webmanifest, /static/css/app.css → 200 без входа
def test_sw_carries_the_static_version(studio): ...  # const VERSION = "<12 hex>"
```
  `tests/test_ui_browser.py` (`pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]`, браузер — `browser.launch`):

```python
@pytest.mark.parametrize("width", [390, 1280])
def test_login_shell_and_theme(browser, live, width):
    r = browser.drive(live.url + "/", [("idle",), ("type", "#login", "owner"), ("type", "#password", "password-123"),
        ("click", "#submit"), ("wait", "document.querySelector('#rail')"),
        ("eval", "tabsAtBottom", "getComputedStyle(document.querySelector('#rail')).position === 'fixed'"),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "sw", "navigator.serviceWorker.getRegistration().then(r => !!r)")], width=width, mobile=width == 390)
    assert not r.get("failed") and not r["values"]["overflow"] and r["values"]["sw"]
    assert r["values"]["tabsAtBottom"] == (width == 390)
```
  плюс `test_wrong_password_shows_words` и `test_photo_without_gateway_shows_the_hint` (пользователь без подключений → `#/photo` с текстом подсказки).

- [ ] **Step 2: Run** `pytest -q tests/test_static.py` — Expected: FAIL; `pytest -q -m integration tests/test_ui_browser.py` — Expected: FAIL.
- [ ] **Step 3: Спросить владельца о скачивании библиотек; Implement** файлы оболочки, маршруты, `live_app.py`, `FakeCloud`.
- [ ] **Step 4: Run** оба набора — Expected: PASS; глазами — скриншот 390 и 1280 в обеих темах (CDP `Page.captureScreenshot` через драйвер или `run`-скилл).
- [ ] **Step 5: Commit** «Add the app shell: Graphite and Paper themes, rail and tabs, PWA».

---

### Task 10: Экраны «Профиль» и «Панель»

**Files:**
- Create: `app/static/js/profile.js`, `app/static/js/admin.js`, `app/static/js/connection-form.js`
- Modify: `app/static/js/app.js`, `app/static/css/app.css`, `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: API задач 3–4.
- Produces:
  - `connection-form.js`: `export function connectionForm(initial?: ConnOut): {el: HTMLElement, value(): object}` — поля
    название, тип (OpenAI-совместимый / шлюз srvllm), адрес, токен (пустой при правке — «не менять»), разделы (галочки по типу);
    метку «через сервер» / «из браузера» показывает список после сохранения (поле `proxied` ответа); отказ сохранения (422) — словами под адресом
  - Профиль `#/profile`: блоки «Подключения» (список `.conn` с названием, адресом, разделами, меткой «через сервер»/«из браузера»,
    «Изменить», «Удалить»), «Чат по умолчанию» (выбор подключения → модели: proxied — `GET /api/connections/{id}/models`,
    прямые — браузер сам `GET {base}/models` с токеном из `/token`), «Тема» (Авто / Графит / Бумага), «Пароль» (текущий, новый, повтор),
    «Удалить всю историю» (двойное подтверждение в своём диалоге, не `confirm()`)
  - Панель `#/admin` (только `me.is_admin`, иначе переход на `#/chat`): таблица пользователей (логин, имя, админ, отключён, подключений),
    «Создать пользователя» (логин, имя, пароль, админ), у строки — «Сбросить пароль», «Отключить/Включить», «Удалить» (диалог с вводом логина),
    «Подключения» (тот же `connectionForm` для выбранного пользователя; токен маскирован); блок «Разрешённые внутренние адреса» (список `host:port`, добавить/удалить, сохранить)
  - id для тестов: `#conn-add`, `#conn-save`, `#theme-dark`, `#theme-light`, `#theme-auto`, `#user-add`, `#user-save`, `#allowlist-save`

- [ ] **Step 1: Write the failing tests** — сценарии в `tests/test_ui_browser.py`:
  `test_profile_adds_a_cloud_connection_and_switches_theme` (FakeCloud `http://localhost:<port>/v1` → в списке «из браузера»;
  `#theme-dark` → `documentElement.dataset.theme === 'dark'`, после перезагрузки сохраняется),
  `test_admin_creates_a_friend_who_sees_no_owner_history` (владелец пишет в чат и генерирует картинку; создаёт `friend`;
  выход; вход `friend` → список чатов пуст, лента фото пуста, `#/admin` недоступен),
  `test_history_wipe_clears_chats_and_feed`.
- [ ] **Step 2: Run** `pytest -q -m integration tests/test_ui_browser.py -k "profile or admin or wipe"` — Expected: FAIL.
- [ ] **Step 3: Implement** экраны.
- [ ] **Step 4: Run** — Expected: PASS; 390 px без горизонтальной прокрутки.
- [ ] **Step 5: Commit** «Add profile and admin screens».

---

### Task 11: Экран «Чат»

**Files:**
- Create: `app/static/js/chat.js`
- Modify: `app/static/css/app.css`, `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: API задачи 8, `readSSE`, `renderMarkdown`, `api`.
- Produces (разметка и поведение, по макету `visual-style.html`):
  - слева `aside#chats`: «Новый чат» (`#chat-new`), список по группам «Сегодня / Вчера / Ранее» (`.chat-item`, у пункта меню — «Переименовать», «Удалить»);
    ≤ 760px — список в выдвижной панели по кнопке в шапке
  - шапка: чип модели `#model-chip` («● local-gemma-abl ▾», рядом название подключения) — выпадающий список моделей всех чат-подключений; выбор пишется в диалог (`PATCH`)
  - лента сообщений `#messages`: пользователь — пузырь справа (`--bubble`), вложения чипами; ассистент — слева, markdown; у ответа — «Копировать», «Ответить заново» (только у последнего)
  - `form.composer#composer`: скрепка `#attach` (input file: `.pdf,.docx,.txt,.md,image/*`, несколько), чипы вложений с крестиком, `textarea#chat-input`
    (Enter — отправить, Shift+Enter — перенос), кнопка `#send` ↔ `#stop` во время ответа
  - до первого токена — строка «загружаю модель · 0:42» (время с начала хода)
  - отправка: вложения → `POST /api/attachments` по одному (ошибка — словами у чипа) → `POST /api/chats/{id}/turns`;
    ответ `text/event-stream` → `readSSE`; JSON (прямое подключение) → токен `GET /api/connections/{id}/token` (кэш в памяти вкладки) →
    `fetch(url, {method:"POST", headers:{Authorization:"Bearer …", "Content-Type":"application/json"}, body: JSON.stringify({model, messages, stream:true}), signal})` →
    разбор `data:` OpenAI → по окончании `PUT /api/chats/{id}/messages/{mid}`; `TypeError` от `fetch` → «Сервис не разрешает запросы из браузера (CORS) —
    подключите его через сервер» и `PUT ... status:"error"`
  - «Остановить»: proxied — `POST /api/chats/{id}/stop`; прямое — `AbortController.abort()` + `PUT ... status:"stopped"`
  - перерисовка markdown во время потока — не чаще раза в 100 мс; автопрокрутка, пока пользователь сам не прокрутил вверх
  - 413 `context` — текст отказа над полем ввода, введённый текст и вложения остаются

- [ ] **Step 1: Write the failing tests** — `tests/test_ui_browser.py`:
  `test_chat_streams_through_the_server_and_survives_reload` (ответ FakeGateway виден, после перезагрузки `#/chat/<id>` — оба сообщения),
  `test_chat_with_a_pdf_attachment` (файл через `DataTransfer` в `#attach`; `FakeGateway.chat_requests[-1]` содержит `<document name="`),
  `test_direct_cloud_chat_saves_the_answer` (подключение FakeCloud, выбор модели в `#model-chip`, ответ сохранён — `GET /api/chats/{id}` из `eval`),
  `test_stop_keeps_partial_answer`, `test_markdown_code_has_copy_button`, `test_chat_layout_fits_390`.
- [ ] **Step 2: Run** `pytest -q -m integration tests/test_ui_browser.py -k chat` — Expected: FAIL.
- [ ] **Step 3: Implement** `chat.js`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** «Add the chat screen».

---

### Task 12: Экран «Фото»

**Files:**
- Create: `app/static/js/feed.js`, `app/static/js/photo.js`, `app/static/js/editor.js`
- Modify: `app/static/css/app.css`, `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: API задачи 6, `api`.
- Produces (по макету `photo-layout.html`, вариант Б):
  - `feed.js`: `export function createFeed(el, {load(before): Promise<items>, render(item): HTMLElement, key(item)})` → `{prepend(item), update(item), remove(id), reload()}` —
    сетка карточек по дням («Сегодня», «Вчера», дата), бесконечная прокрутка по `before`; на компьютере 6 колонок, ≤ 760px — 2; используется и в задаче 13
  - `photo.js`: лента `GET /api/images`; карточка — превью `/api/media/{id}?thumb=1`, подпись модели; идущая — «Генерирую · 0:42 / Отменить»
    (опрос `GET /api/images/{id}` раз в 1.5 с, после перезагрузки страницы — сам находит идущие в ленте)
  - `form.composer#photo-composer`: `textarea#photo-prompt` (до 24000 знаков), чипы `#chip-model` (Lustify/FLUX), `#chip-size`
    (Портрет 832×1216 / Квадрат 1024×1024 / Альбом 1216×832), `#chip-seed` (пусто — случайный), `#chip-photo` (исходное фото с превью и
    ползунком «Сила изменения» 0.05–1, по умолчанию 0.25, только для Lustify), кнопка `#photo-go` «Создать»; меню раздела `#photo-menu` —
    «Описать фото с устройства…», «Освободить карту» (только `me.is_admin`, итог по каждой модели словами)
  - просмотр картинки — диалог `#viewer` во весь экран; действия: «Изменить» (`.act-edit`), «Оживить» (`.act-animate` → `#/video?source=<media_id>`),
    «Повторить с тем же seed» (`.act-repeat`: тот же промпт, модель, размер, seed, исходник и сила), «Описать» (`.act-describe`), «Скачать» (`.act-download`, `?download=1`), «Удалить» (`.act-delete`)
  - `editor.js`: `export function openEditor(item, model): void` — перенос диалога правки из `app/index.html:313-410` (кисть по маске, «Изменить только
    выделенную область», размер кисти, очистка, снимок маски перед отправкой, `size=original`, сила для Lustify 0.25); отправка — `POST /api/images`
    с `source_media_id` и `mask`
  - «Описать»: перенос `app/index.html:412-475` — диалог с текстом gemma-abl, счётчик до 24000 знаков, «Создать по описанию» кладёт текст в `#photo-prompt` и запускает

- [ ] **Step 1: Write the failing tests** — перенести сценарии `tests/test_studio_upload_browser.py:25-221` на новый экран (те же проверки:
  исходное фото, сила 0.4/0.25, маска пустая → ошибка, маска → `mask` в форме, смена модели между правками, описание редактируется и запускается без исходника)
  против `live_app` (проверки полей — по `FakeGateway`), плюс `test_photo_feed_survives_reload_with_a_running_job` и `test_photo_layout_fits_390`.
- [ ] **Step 2: Run** `pytest -q -m integration tests/test_ui_browser.py -k photo` — Expected: FAIL.
- [ ] **Step 3: Implement** `feed.js`, `photo.js`, `editor.js`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** «Add the photo screen: feed, composer chips, brush editor, describe».

---

### Task 13: Экран «Видео»

**Files:**
- Create: `app/static/js/video.js`
- Modify: `app/static/css/app.css`, `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: API задачи 7, `createFeed`.
- Produces:
  - лента `GET /api/videos` через `createFeed`; карточка — превью источника, статус словами (`detail`), полоса `progress`, «Отменить»;
    готовая — `<video controls playsinline preload="metadata" poster="/api/media/{source}?thumb=1" src="/api/media/{media_id}">`, «Скачать», «Удалить»;
    опрос активных — `GET /api/videos/{id}` раз в 3 с
  - `form.composer#video-composer`: источник `#video-source` (файл с устройства или пришедший `?source=<media_id>` из фото — превью с крестиком),
    `textarea#video-prompt` (подсказка «пусто — движение придумает gemma-abl», до 4000 знаков), чипы `#chip-seconds` (1–30, 5), `#chip-quality`
    (Быстро 480p / Обычно 576p / Высоко 720p), `#chip-smooth` (плавность 32 к/с, вкл), `#chip-vseed`; кнопка `#video-go`

- [ ] **Step 1: Write the failing tests** — перенести `tests/test_studio_upload_browser.py:223-312` («Оживить» из фото → ролик готов и играет), плюс
  `test_video_feed_shows_progress_and_survives_reload`, `test_video_layout_fits_390`.
- [ ] **Step 2: Run** `pytest -q -m integration tests/test_ui_browser.py -k video` — Expected: FAIL.
- [ ] **Step 3: Implement** `video.js`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** «Add the video screen».

---

### Task 14: Переход — убрать старую службу, выкладка на studio.example.com, копия базы, документы

**Files:**
- Delete: `app/server.py`, `app/index.html`, `app/login.html`, `deploy/apache-location.conf`, `tests/test_studio.py`, `tests/test_studio_upload_browser.py`
- Create: `deploy/apache-vhost.conf`, `deploy/studio-backup.service`, `deploy/studio-backup.timer`
- Modify: `deploy/studio-web.service`, `deploy/install.sh`, `deploy/deploy.sh`, `tests/test_deploy.py`, `README.md`, `CLAUDE.md`, `AGENTS.md`

**Interfaces:**
- Produces:
  - юнит: `ExecStart=/opt/services/studio/.venv/bin/uvicorn main:app --app-dir /opt/services/studio/app --uds /run/studio/web.sock --no-access-log --no-proxy-headers`;
    `Environment=STUDIO_DB=/var/lib/studio/studio.db`, `Environment=STUDIO_MEDIA_DIR=/mnt/studio-media`, `Environment=STUDIO_ORIGIN=https://studio.example.com`;
    `EnvironmentFile=/etc/studio/studio.env` (одна строка `STUDIO_DATA_KEY=`); `StateDirectory=studio`, `StateDirectoryMode=0700`;
    `ReadWritePaths=-/mnt/studio-media`; `STUDIO_GATEWAY_URL` убран; остальное (`User`, `Group=www-data`, `UMask=0007`, `RuntimeDirectory`, `MemoryMax=2G`,
    `LimitCORE=0`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`) — как было
  - `deploy/apache-vhost.conf`: `<VirtualHost *:8443>` `ServerName studio.example.com`, TLS — wildcard `/etc/letsencrypt/live/example.net/` (строки SSL —
    как в существующем vhost `*.example.com` на хосте), `ProxyPass /api/chats/ unix:/run/studio/web.sock|http://studio-backend/api/chats/ timeout=600 flushpackets=on`
    раньше общего `ProxyPass / unix:/run/studio/web.sock|http://studio-backend/ timeout=120`, `ProxyPassReverse`, `RequestHeader set X-Forwarded-Proto "https"`,
    `SetEnvIf Request_URI "^/api/chats/" no-gzip=1`, `LimitRequestBody 22020096`, `Header always set X-Frame-Options "DENY"`,
    `Header always set Strict-Transport-Security "max-age=31536000"`; **без** `Require ip`; `ServerName studio.example.com` — единственный
  - `install.sh`: `--secret-stdin` принимает строку `STUDIO_DATA_KEY=<44 знака base64url>`; проверка файла — одна строка `STUDIO_DATA_KEY=`;
    `--admin-stdin` → `"$R/.venv/bin/python" "$R/app/manage.py" create-admin` от `studio` с окружением юнита (`systemd-run --pipe --wait -p User=studio -p EnvironmentFile=... -p Environment=...`
    или эквивалент), stdin пробрасывается; health — `http://studio-backend/health`; ставит и включает `studio-backup.timer`;
    проверка записи в `/mnt/studio-media` от `studio` (`sudo -u studio test -w`) — иначе ОШИБКА с подсказкой про монтирование
  - `deploy.sh`: `URL=${STUDIO_URL:-https://studio.example.com}`; `--new-secret` — `STUDIO_DATA_KEY` из vault; `--create-admin` — строки
    `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD`, `GATEWAY_KEY_OWNER_IMAGES` из vault потоком в `install.sh --admin-stdin`; health — `$URL/health`
  - `studio-backup.service`: `Type=oneshot`, `User=studio`, окружение как у службы, `ExecStart=... manage.py backup`; `.timer` — `OnCalendar=*-*-* 04:30`, `Persistent=true`
  - `manage.py backup`: `sqlite3.Connection.backup` в `$STUDIO_MEDIA_DIR/backup/studio-YYYYMMDD.db` (временное имя + `os.replace`), хранит 7 последних
  - README и CLAUDE.md переписаны под новое устройство (адрес, учётки, подключения, хранение истории, правило приватности журналов; правило
    «на диск ничего не ложится» заменено на «история — в базе и на шаре, в журнал — ничего»); AGENTS.md — ссылки на новые файлы

- [ ] **Step 1: Write the failing tests** — `tests/test_deploy.py`: обновить существующие (`main:app`, `REQUIRED` из `config.py` ⊆ окружения юнита + `studio.env`,
  `apache-vhost.conf` вместо location) и добавить:

```python
def test_vhost_is_public_and_streams_chat():
    v = (DEPLOY / "apache-vhost.conf").read_text(encoding="utf-8")
    assert "ServerName studio.example.com" in v and "Require ip" not in v
    assert v.index("ProxyPass /api/chats/") < v.index("ProxyPass / ")
    assert "flushpackets=on" in v and "studio-backend" in v and "localhost" not in re.sub(r"^#.*$", "", v, flags=re.M)

def test_state_and_media_paths_are_writable_in_the_sandbox(): ...  # StateDirectory=studio, ReadWritePaths=-/mnt/studio-media
def test_backup_timer_runs_as_the_service_user(): ...
def test_admin_secrets_travel_by_stdin_only(): ...                   # grep из vault | ssh ... --admin-stdin; значений в argv нет
def test_apache_lets_through_the_largest_body_the_studio_accepts(): ...  # перенос test_studio.py:918 на documents.DOC_MAX и VIDEO_UPLOAD
```
- [ ] **Step 2: Run** `pytest -q tests/test_deploy.py` — Expected: FAIL.
- [ ] **Step 3: Implement** правки `deploy/`, удалить старые файлы, переписать документы.
- [ ] **Step 4: Run** `pytest -q` (быстрый набор ≤ 60 с, `--durations=10`) и `pytest -q -m integration` — Expected: PASS.
- [ ] **Step 5: Commit** «Switch to the new studio: public vhost, accounts, backups; drop the old page».

---

### Task 15: Хост, шлюз, выкладка и приёмка

**Files:**
- Modify (другие репозитории): `C:/work/projects/llm_routers/gateway/consumers.yaml`, `C:/work/projects/llm_routers/dashboard/deploy/apache-vhost.conf`,
  `C:/work/projects/network/dashboards.html`, `C:/work/projects/local-config/wiki/projects/llm_routers/architecture-srvllm-studio.md`, страница проекта `studio` в вики
- Modify: `tests/test_studio_upload_live.py` (живая приёмка под учётку и подключение)

**Шаги** (каждый проверяется командой; хост — `ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.10`):

- [ ] **Step 1: Ключ шлюза.** В `consumers.yaml` у `owner-images` `roles: [embed]` → `roles: [local-gemma-abl]`, заметку — про чат студии.
  Тесты `llm_routers` для `consumers.yaml` зелёные; применить по `gateway/README.md:380-384, 610` (`issue_keys.py --sync --apply --only owner-images`).
  Verify: `curl http://10.77.1.15:4000/v1/chat/completions` ключом `owner-images` (из vault, без печати) с `local-gemma-abl` → 200; `/slot/models` → 200.
- [ ] **Step 2: Секреты в vault**: `STUDIO_DATA_KEY` (новый Fernet-ключ), `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD` (≥ 16 знаков) +
  записи в `local-config/project-knowledge-base.yaml::api_keys`. `STUDIO_SESSION_SECRET` пометить устаревшим.
- [ ] **Step 3: Шара.** На хосте — точка `/mnt/studio-media` = `//10.77.1.5/dd$/SharedData/studio` (fstab: учётные данные — те же, что у `/mnt/storage`;
  `uid=studio,gid=studio,file_mode=0600,dir_mode=0700,nofail,_netdev,x-systemd.automount`). Verify: `sudo -u studio touch /mnt/studio-media/.probe && sudo -u studio rm /mnt/studio-media/.probe`.
- [ ] **Step 4: Выкладка.** `bash deploy/deploy.sh --new-secret`, затем `bash deploy/deploy.sh --create-admin`. Подключить vhost: найти, как включён
  `/opt/services/apache/llm-0-55.conf` (`grep -n Include /opt/services/apache/apache2.conf`), так же включить `/opt/services/studio/deploy/apache-vhost.conf`;
  `sudo apache2ctl configtest && sudo systemctl reload apache2`. Verify: `curl -s https://studio.example.com/health` из LAN и снаружи (через внешний
  сервис проверки или телефон без Wi-Fi) → `{"status":"ok"}`; сертификат — `*.example.com`.
- [ ] **Step 5: Старый адрес.** В `llm_routers/dashboard/deploy/apache-vhost.conf` — `RedirectMatch permanent ^/studio(/.*)?$ https://studio.example.com/`
  вместо `IncludeOptional /opt/services/studio/deploy/apache-location.conf`; выложить vhost по правилам `llm_routers`. Verify: `curl -sI https://llm.example.com/studio/` → 301 на новый адрес.
- [ ] **Step 6: Живая приёмка** (`manual`): `tests/test_studio_upload_live.py` переписать — вход владельцем, картинка Lustify, правка, ролик 1 с,
  чат `local-gemma-abl` с PDF; запустить `pytest -q -m manual`. Руками с телефона (вне Wi-Fi): поставить приложение, пройти критерий успеха
  спеки (раздел 3, пп. 1–5).
- [ ] **Step 7: Реестры.** Карточка студии в `network/dashboards.html` (адрес `https://studio.example.com`, метка «публичный», PWA) — коммит в `network`;
  вики — новое устройство (адрес, учётки, хранение); коммиты в `llm_routers` и `local-config` по их правилам.
- [ ] **Step 8: Commit** в `studio` — живая приёмка; итог задачи — список проверок с выводом команд.
