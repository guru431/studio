# Мост `studio-agent` — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows-мост, который держит канал к студии `studio.example.com` и отдаёт ей локальный Claude Code владельца: проекты, чаты, ход с карточками разрешений, файлы, git — строго по протоколу версии 1.

**Architecture:** Отдельный приватный проект `C:/work/projects/studio-agent` (Python, пакет `studio_agent`). Ядро — `asyncio`: `Link` держит исходящий WebSocket и переподключается, `Agent` разбирает просьбы студии и проверяет открытость сессий (PIN — на машине), `Runner` ведёт ходы через `ClaudeSDKClient` (клиент на ход, `can_use_tool` → карточка студии). Оболочка — значок в трее (`pystray`) и окно настройки (`tkinter`); ключ устройства и хеш PIN — под DPAPI в `%LOCALAPPDATA%\studio-agent`.

**Tech Stack:** Python 3.14 (не ниже 3.11), `claude-agent-sdk`, `websockets`, `pystray` + `Pillow`, `tkinter`, `ctypes` (DPAPI, мьютекс), `winreg`; тесты — `pytest`, `pytest-asyncio`, `pytest-timeout`.

**Spec:** [docs/superpowers/specs/2026-10-28-studio-claude-code-design.md](../specs/2026-10-28-studio-claude-code-design.md) (§2, §4, §6.3, §6.6, §6.10, §7, §8) и контракт
[docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md](../specs/2026-10-28-studio-code-bridge-protocol.md) — читать оба (пути — в репозитории
`C:/work/projects/studio`). Сервер уже сделан: `app/bridge.py`, `app/claude_code.py`; подставной мост сервера —
`tests/fake_bridge.py` (`FakeMachine` — эталон поведения машины для сервера).

## Global Constraints

- Только Windows. Ключ устройства и PIN — DPAPI области пользователя (`CryptProtectData`, `CRYPTPROTECT_UI_FORBIDDEN`,
  энтропия `b"studio-agent\0v1"`); пароль студии на машине не остаётся, в argv не передаётся (только окно или `getpass`).
- Протокол v1 (значения дословно): кадр ≤ `FRAME_MAX` = 16 МиБ; первый кадр — `hello` с `"v": 1`; `welcome` ждать ≤ 10 с;
  закрытия 4401 (отозван — не переподключаться), 4409 (заменён — не переподключаться), 4400 (пауза 60 с и снова),
  1012/1001/обрыв/отказ рукопожатия — переподключение 1, 2, 4, 8, 16, 32, 60 с; `id` просьбы — 16 hex, `session` — 32 hex,
  `project` — 16 hex, `chat` — UUID строчными 8-4-4-4-12; `message` отказа ≤ 300 знаков; студия ждёт ответа 30 с.
- PIN — 4–8 цифр ASCII, хеш scrypt (`n=2**14, r=8, p=1`, соль 16 байт, `dklen=32`), 5 неверных подряд — `pin_locked` до
  снятия блока на машине; счёт — на диске, попытка считается до scrypt; `OPEN_IDLE_SEC` = 1800; открытые сессии — только
  в памяти, пропадают при `close`, перезапуске моста и **любом** новом канале.
- Пределы: текст файла 2 МиБ (`TEXT_MAX`), `raw` 10 МиБ (`RAW_MAX`), путь ≤ 1024 знака, `history.limit` 1–100, строки
  событий и шагов — до 20 000 знаков с `…` в конце (`CLIP`), страница истории и снимок — ≤ 8 МиБ (`HISTORY_BUDGET`),
  файлов в списке ≤ 5000 (`FILES_MAX`), чатов ≤ 200 (`CHATS_MAX`), файлов git ≤ 2000, одновременных ходов — 2 (`MAX_TURNS`).
- `pid` = первые 16 знаков hex sha256 имени папки (UTF-8).
- Кадры наружу — `json.dumps(frame, ensure_ascii=True, allow_nan=False)` (имена файлов Windows бывают с одинокими суррогатами).
- Журнал `%LOCALAPPDATA%\studio-agent\agent.log` (1 МиБ × 3, UTF-8) — только id, коды, числа, имена типов исключений: ни
  ключа, ни PIN, ни пароля, ни имён проектов, файлов, путей, ни текстов, ни stderr Claude Code.
- Блокирующее (файлы, scrypt, сессии SDK, git) — `asyncio.to_thread`: цикл держит пинги канала.
- Автозапуск — значение `StudioAgent` в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; Task Scheduler не трогать.
- Кодировки: `.py` — UTF-8 без BOM, LF; `.ps1` — UTF-8 **с BOM**, CRLF (`.gitattributes`: `*.ps1 text eol=crlf`).
- Комментарии, докстринги и тексты — по-русски, как в студии.
- Тесты: `.venv/Scripts/python -m pytest -q` — быстрый набор ≤ 60 с, `timeout = 30`, `asyncio_mode = auto`; маркеры
  `integration` (настоящий git, реестр, PowerShell) и `manual` (настоящий Claude Code, тратит квоту) исключены в `addopts`.
  Реального времени в тестах нет: часы — параметр `now`, паузы — параметр `sleep`.
- Коммит — сообщение через файл (`git commit -F <файл>`), в конце строка `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;
  push — только по слову владельца.

## Тексты

Отказы моста (`message`): `closed` «компьютер закрыт»; `pin` «неверный PIN»; `pin_locked` «PIN заблокирован — снимите
блок на самом компьютере»; `pin_needed` «нужен PIN»; `not_found` «нет такого проекта» / «нет такого чата» / «нет такого
файла» / «нет такой папки»; `outside` «путь вне проекта»; `too_large` «файл больше 2 МиБ» / «файл больше 10 МиБ» / «ответ
больше предела канала»; `binary` «двоичный файл»; `conflict` «файл изменился»; `running` «в чате уже идёт ход»; `busy`
«на компьютере уже идут 2 хода — дождитесь конца»; `idle` «хода нет»; `gone` «запроса уже нет»; `not_git` «не репозиторий
git»; `invalid` «неверные поля»; `error` «ошибка моста».

Конец хода с ошибкой (`done.message`): «Claude Code не найден на компьютере»; «Claude Code не запустился»; «ход прерван:
мост остановлен»; ошибка результата — `errors[0]`, иначе `result`, иначе «Claude Code: <subtype>» (до 300 знаков).
Отказ при `stop` открытой карточки — «Остановлено». Отказ без слов (`deny`, `message` пусто) — «Пользователь отказал».

Трей (подсказка «Studio Agent — <состояние>»): «не настроен», «подключается…», «на связи», «нет связи», «компьютер
отозван», «ключ используется другим мостом». Меню: «Настройка…», «Снять блок PIN» (активен при блоке), «Открыть
журнал», «Выход». Второй запуск: «Мост уже запущен».

Окно «Studio Agent — настройка»: «Адрес студии», «Логин», «Пароль», «Имя компьютера», «Папка проектов» (+ «Обзор…»),
«PIN (4–8 цифр, по желанию)», «PIN ещё раз», «Убрать PIN», «Запускать при входе в Windows», кнопки «Привязать»,
«Сохранить», «Снять блок PIN», «Отмена». Подписи: «Пароль студии нужен один раз для привязки и на компьютере не
хранится.» и «Новая привязка заводит новый компьютер в студии — прежний отзовите в профиле студии.» Ошибки: «Неверный
логин или пароль», «Вход на паузе после неудач — попробуйте позже», «Имя компьютера — от 1 до 64 знаков», «Студия
недоступна (<тип ошибки>)», «Адрес студии — https://…», «PIN — от 4 до 8 цифр», «PIN не совпадает», «Папки нет».

## Решения плана (проверить при чтении)

1. Клиент SDK поднимается на каждый ход и гасится по его концу (спека §6.6: «держит только отвечающий чат»): новая сессия —
   `session_id=chat`, есть — `resume=chat`; `model` и `effort` — опции запуска. `set_model` и «перезапуск при смене
   усилия» не нужны — протокол §4 правится в задаче 1.
2. `null` модели — выбор, запомненный мостом для чата (`choices.json`), иначе модель последнего ответа сессии из файла
   (так продолжение сессии VS Code не меняет её модель), иначе умолчание Claude Code; `null` усилия — запомненное или
   умолчание.
3. Настройки Claude Code — явно `setting_sources=["user", "project", "local"]`: правила разрешений, CLAUDE.md и `env`
   `claude-switch` (`.claude/settings.local.json`) действуют, как в VS Code; `permission_mode` не задаётся.
4. Окружение `claude.exe` не вычищается (в отличие от `worker.py`): мост секретов в окружении не держит, а Claude Code
   владельца должен видеть то же, что в VS Code. Добавляется только `CLAUDE_AGENT_SDK_CLIENT_APP=studio-agent/<версия>`.
5. CLI — новейший `claude.exe` расширения VS Code (как `worker.py::_newest_extension_cli`), нет — встроенный в SDK.
6. Список моделей — `get_server_info()["models"]` короткого клиента в папке проектов при каждом подключении канала
   (не чаще раза в час); `default` пропускается (у страницы своё «по умолчанию»).
7. Текст хода — по `StreamEvent` (`text_delta`, `include_partial_messages=True`); если за ход дельт не было (бэкенд без
   потока) — текстом блоков `AssistantMessage`. Шаги и итоги — только главного агента (`parent_tool_use_id is None`);
   карточки подагентов — показываются.
8. Метка `studio` ставится новой сессии после конца хода, когда клиент уже погашен (файл сессии не пишут двое).
9. Время сообщений в `history` — `null`: SDK его не отдаёт (страница его не читает).
10. Запуск без консоли — `studio_agent.pyw` в корне репозитория (`.venv\Scripts\pythonw.exe`); один экземпляр —
    мьютекс `Local\StudioAgent`.

## Review Focus

1. Имена Windows в пути — `CON`, `nul.txt`, `COM1.log`, `a.txt:поток` (ADS), часть с точкой или пробелом в конце: отказ
   `invalid` до обращения к диску, без зависания на устройстве (задача 4).
2. Канал оборвался, пока открыта карточка разрешения: после переподключения `watch` отдаёт снимок с этой карточкой, ответ
   по новому каналу отпускает тот же колбэк, ход идёт дальше (задачи 8 и 10).
3. Сессия, которую сейчас пишет VS Code, огромная сессия или сбой разбора: `history` отдаёт страницу ≤ 8 МиБ, сбой — отказ
   `error`, канал жив (задачи 7 и 9).
4. Два моста с одним ключом (автозапуск и ручной запуск, скопированный каталог): второй экземпляр выходит по мьютексу,
   заменённый (4409) останавливается, а не переподключается по кругу (задачи 10 и 11).
5. Машина уснула или сеть пропала молча: пинги рвут канал, мост переподключается с паузой, открытые сессии сброшены (PIN
   снова), идущий ход продолжается, его события идут в новый канал (задача 10).

---

### Task 1: Уточнения протокола и подставной мост сервера (репозиторий `studio`)

**Files:**
- Modify: `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md` (§2, §3, §4)
- Modify: `C:/work/projects/studio/tests/fake_bridge.py` (`FakeMachine._open`)
- Modify: `C:/work/projects/studio/tests/test_bridge.py` (рядом с `test_fake_machine_follows_the_protocol`)
- Modify: `C:/work/projects/studio/FINDINGS.md` (удалить запись «протокол не говорит, что делать мосту при отказе рукопожатия»)

**Interfaces:**
- Produces: правила протокола, которым следуют задачи 4–10; `FakeMachine` отвечает `{}` на `open` с `pin: null` уже открытой сессии.

- [ ] **Step 1: Дописать протокол** (версия остаётся 1 — сервер этих полей не проверяет):
  - §2, таблица закрытий — строка: «отказ рукопожатия (любой ответ HTTP, кроме 101), сеть недоступна | — | переподключается
    с той же паузой; «отозван» — только по закрытию 4401». Под таблицей: ответ, который не влез бы в `FRAME_MAX`, мост
    заменяет отказом `too_large`.
  - §2.2, общие отказы — `error` (сбой моста при исполнении; сервер отвечает 502 `bridge`).
  - §3 — «`open` с `pin: null` сессии, что уже открыта и не просрочена, — `{}` и продление: страница зовёт `open` с `null`
    при каждом входе в компьютер».
  - §4 `chats` — «не больше 200 новых»; `history` — «мост может выдать меньше `limit`, чтобы кадр не превысил 8 МиБ;
    `start` — номер первого выданного, страница листает от него; `time` — секунды Unix или `null`»; `files` — «не больше
    5000 записей, иначе первые 5000 и `"truncated": true`»; `watch` — «хода нет — `{"running": false}` без проверки чата».
  - §4 `turn` — заменить фразу про `set_model` и перезапуск на: «клиент SDK поднимается на каждый ход (`session_id` — у
    новой сессии, `resume` — у существующей), `model` и `effort` — его опции; `null` — выбор, запомненный мостом для
    чата, иначе модель последнего ответа сессии и усилие по умолчанию».

- [ ] **Step 2: Тест на правило §3** в `tests/test_bridge.py`:

```python
def test_fake_machine_opens_an_open_session_without_the_pin_again():
    m = fake_bridge.FakeMachine(pin="2468")
    res, _ = m.handle({"t": "req", "id": "a" * 16, "op": "open", "session": "s" * 32, "pin": "2468"})
    assert res["ok"] is True
    res, _ = m.handle({"t": "req", "id": "b" * 16, "op": "open", "session": "s" * 32, "pin": None})
    assert res == {"t": "res", "id": "b" * 16, "ok": True, "data": {}}
    res, _ = m.handle({"t": "req", "id": "c" * 16, "op": "open", "session": "t" * 32, "pin": None})
    assert res["code"] == "pin_needed"
```

Run: `.venv/Scripts/python -m pytest -q tests/test_bridge.py -n0 -k open_session_without` → FAIL (`pin_needed`).

- [ ] **Step 3: `FakeMachine._open`** — `pin is None` и сессия в `open_sessions` → `{}` до проверки PIN.

- [ ] **Step 4: Прогон** `.venv/Scripts/python -m pytest -q tests/test_bridge.py tests/test_code*.py` → PASS; затем
  `grep -n "pin_needed\|code" tests/test_ui_browser.py | head` и браузерные сценарии раздела:
  `.venv/Scripts/python -m pytest -q -m integration -k code -n0` → PASS.

- [ ] **Step 5: Коммит в `studio`** — «Протокол моста: отказ рукопожатия, open открытой сессии, пределы ответов».

---

### Task 2: Проект `studio-agent`, хранилище, DPAPI, журнал

**Files:**
- Create: `C:/work/projects/studio-agent/` — `README.md`, `CLAUDE.md`, `AGENTS.md` (15–40 строк), `.gitignore` (`.venv/`,
  `__pycache__/`, `*.local.*`), `.gitattributes`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`
- Create: `studio_agent/__init__.py` (`VERSION = "0.1.0"`), `studio_agent/limits.py`, `studio_agent/errors.py`,
  `studio_agent/dpapi.py`, `studio_agent/store.py`, `studio_agent/journal.py`
- Test: `tests/conftest.py`, `tests/test_store.py`

**Interfaces:**
- Produces:
  - `limits` — все числа «Global Constraints»: `FRAME_MAX, WELCOME_SEC=10, PIN_TRIES=5, OPEN_IDLE_SEC=1800, TEXT_MAX,
    RAW_MAX, PATH_MAX=1024, CLIP=20_000, MESSAGE_MAX=300, HISTORY_BUDGET, FILES_MAX=5000, CHATS_MAX=200,
    GIT_FILES_MAX=2000, MAX_TURNS=2, BACKOFF=(1, 2, 4, 8, 16, 32, 60), PROTOCOL_PAUSE_SEC=60, EFFORTS=("low", "medium",
    "high", "xhigh", "max")`, `MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")`.
  - `errors.Refused(code: str, message: str = "", **extra)` — отказ просьбы; без `message` — слова из `errors.WORDS[code]`
    (раздел «Тексты»).
  - `dpapi.protect(data: bytes) -> bytes`, `dpapi.unprotect(blob: bytes) -> bytes` (`OSError` на чужом или битом блобе).
  - `store.home() -> Path` (`STUDIO_AGENT_HOME`, иначе `%LOCALAPPDATA%\studio-agent`; создаёт каталог);
    `store.Config` (dataclass: `url="https://studio.example.com"`, `agent_id: int | None = None`, `name=""`,
    `projects_root=""`, `autostart=True`); `load_config(h) -> Config`, `save_config(h, cfg)` (`config.json`);
    `save_key(h, key: str)`, `load_key(h) -> str | None` (`key.bin`, DPAPI; блоб не читается — `None`);
    `write_atomic(path: Path, data: bytes)` (временный файл рядом + `os.replace`).
  - `journal.setup(h: Path, console: bool = False) -> logging.Logger` — логгер `studio_agent`.

- [ ] **Step 1: Каркас.** `git init` (ветка `main`), файлы выше. `requirements.txt` — точно: `claude-agent-sdk==0.2.174`,
  `websockets==17.3`, `pystray==0.19.5`, `pillow==13.0.0`; `requirements-dev.txt` — `-r requirements.txt`, `pytest`,
  `pytest-asyncio`, `pytest-timeout`. `pytest.ini`: `addopts = -q -m "not integration and not manual"`, `timeout = 30`,
  `asyncio_mode = auto`, маркеры `integration`, `manual`. Среда:
  `"/c/Program Files/Python314/python" -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt`.
  `CLAUDE.md` — назначение, ссылки на спеку, протокол и этот план (абсолютные пути в `studio`), «Правила» из Global
  Constraints, команды тестов; `AGENTS.md` — ссылки и ловушки. `tests/conftest.py` — фикстура `home` (`tmp_path`,
  `monkeypatch.setenv("STUDIO_AGENT_HOME", ...)`).

- [ ] **Step 2: Тесты** `tests/test_store.py`:

```python
def test_key_is_stored_under_dpapi_and_comes_back(home):
    store.save_key(home, "secret-device-key-123")
    raw = (home / "key.bin").read_bytes()
    assert b"secret-device-key-123" not in raw and "secret-device-key-123".encode("utf-16-le") not in raw
    assert store.load_key(home) == "secret-device-key-123"

def test_a_broken_key_blob_reads_as_no_key(home):
    (home / "key.bin").write_bytes(b"\x01\x02garbage")
    assert store.load_key(home) is None

def test_unprotect_refuses_a_tampered_blob():
    blob = bytearray(dpapi.protect(b"abc"))
    blob[-1] ^= 1
    with pytest.raises(OSError):
        dpapi.unprotect(bytes(blob))

def test_config_round_trip_and_defaults(home):
    assert store.load_config(home) == store.Config()
    store.save_config(home, store.Config(agent_id=7, name="Рабочий ПК", projects_root="C:\\work\\projects"))
    assert store.load_config(home).name == "Рабочий ПК"

def test_refused_takes_its_words_from_the_table():
    assert errors.Refused("closed").message == "компьютер закрыт"
```

Run: `.venv/Scripts/python -m pytest tests/test_store.py` → FAIL (нет модулей).

- [ ] **Step 3: Реализовать** модули по Interfaces. DPAPI — `ctypes.windll.crypt32.CryptProtectData/CryptUnprotectData`
  со структурой `DATA_BLOB`, память ответа — `LocalFree`.

- [ ] **Step 4:** `.venv/Scripts/python -m pytest` → PASS.

- [ ] **Step 5: Регистрация проекта.** Скилл `registering-new-projects` (реестры `local-config`, вики `projects/studio-agent/`);
  репозиторий `git.example.net/xadmin/studio-agent` — **`"private": true`**, флаг проверить ответом API, зеркало Gitea — с
  `auth_token` (`~/.claude/memory/ref/infra.md`); `origin` прописать, push — по слову владельца.

- [ ] **Step 6: Коммит** — «Каркас моста: хранилище, DPAPI, журнал».

---

### Task 3: PIN и открытые сессии

**Files:**
- Create: `studio_agent/pin.py`
- Test: `tests/test_pin.py`

**Interfaces:**
- Consumes: `store.write_atomic`, `dpapi`, `limits.PIN_TRIES`, `limits.OPEN_IDLE_SEC`.
- Produces:
  - `pin.PIN_RE = re.compile(r"[0-9]{4,8}")` (сверка — `fullmatch` и `str.isascii()`).
  - `pin.PinStore(h: Path)`: свойства `has_pin: bool`, `fails: int`, `locked: bool`; `set_pin(value: str | None) -> None`
    (`ValueError` на неверном виде; сбрасывает счёт); `check(value: str) -> tuple[str, int]` — `("ok", 0)`,
    `("pin", left)`, `("pin_locked", 0)`; `reset() -> None` (снять блок). Файл `pin.bin` — DPAPI от JSON
    `{"hash": "scrypt$<salt hex>$<hash hex>", "fails": n}`.
  - `pin.OpenSessions(now: Callable[[], float] = time.monotonic)`: `open(s)`, `touch(s) -> bool` (есть и не старше
    `OPEN_IDLE_SEC` — продлить, иначе убрать и `False`), `close(s)`, `clear()`.

- [ ] **Step 1: Тесты**:

```python
def test_five_wrong_pins_lock_and_survive_a_restart(home):
    p = pin.PinStore(home); p.set_pin("2468")
    assert [pin.PinStore(home).check("1111") for _ in range(4)] == [("pin", 4), ("pin", 3), ("pin", 2), ("pin", 1)]
    assert pin.PinStore(home).check("1111") == ("pin_locked", 0)
    assert pin.PinStore(home).check("2468") == ("pin_locked", 0)       # и верный не принимается
    pin.PinStore(home).reset()
    assert pin.PinStore(home).check("2468") == ("ok", 0)

def test_the_attempt_is_counted_before_scrypt(home, monkeypatch):
    p = pin.PinStore(home); p.set_pin("2468")
    monkeypatch.setattr(pin.hashlib, "scrypt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        p.check("1111")
    assert pin.PinStore(home).fails == 1

def test_right_pin_resets_the_count(home):
    p = pin.PinStore(home); p.set_pin("2468"); p.check("0000")
    assert p.check("2468") == ("ok", 0) and pin.PinStore(home).fails == 0

@pytest.mark.parametrize("bad", ["123", "123456789", "12a4", "١٢٣٤", " 1234"])
def test_pin_form(home, bad):
    with pytest.raises(ValueError):
        pin.PinStore(home).set_pin(bad)

def test_pin_file_holds_no_plain_hash(home):
    pin.PinStore(home).set_pin("2468")
    assert b"scrypt" not in (home / "pin.bin").read_bytes()

def test_open_session_expires_after_idle():
    t = [0.0]; s = pin.OpenSessions(now=lambda: t[0]); s.open("a" * 32)
    t[0] = 1799; assert s.touch("a" * 32)
    t[0] = 1799 + 1801; assert not s.touch("a" * 32) and not s.touch("a" * 32)
```

- [ ] **Step 2:** прогон → FAIL. **Step 3:** реализовать (`hmac.compare_digest`). **Step 4:** → PASS.
- [ ] **Step 5: Коммит** — «PIN моста: scrypt, счёт на диске, открытые сессии».

---

### Task 4: Проекты и пути

**Files:**
- Create: `studio_agent/projects.py`, `studio_agent/files.py`
- Test: `tests/test_files.py`

**Interfaces:**
- Consumes: `errors.Refused`, `limits`.
- Produces:
  - `projects.pid(name: str) -> str`; `projects.list_projects(root: str) -> list[dict]` — `[{"pid", "name"}]` по имени:
    подпапки первого уровня, без начинающихся с `.` и без атрибутов «скрытый»/«системный», без тех, чей настоящий путь вне
    корня; корня нет — `[]`. `projects.find_project(root: str, pid: str) -> Path` — настоящий путь, иначе
    `Refused("not_found", "нет такого проекта")`.
  - `files.resolve(project: Path, path: str) -> Path` — вид пути: строка ≤ 1024, без `\`, `:`, управляющих знаков и
    суррогатов, части через `/` без пустых, `.`, `..`, без точки или пробела в конце части, без имён устройств
    (`(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?`, без учёта регистра) — иначе `invalid`; настоящий путь
    (`os.path.realpath`) вне настоящего пути проекта — `outside`.
  - `files.list_dir(project, path) -> dict` — `{"items": [{"name", "dir", "size", "modified"}]}` (папки, потом файлы, по
    имени; `modified` — секунды; записи, ведущие наружу, пропускаются; больше `FILES_MAX` — первые и `"truncated": True`);
    не папка — `not_found` «нет такой папки».
  - `files.read_text(project, path) -> dict` (`{"text", "version"}`), `files.read_raw(project, path) -> dict`
    (`{"data", "size", "version"}`), `files.save_text(project, path, text: str, version: str) -> dict` (`{"version"}`) —
    размер по `stat` до чтения; `binary` — байт NUL или не UTF-8 (`decode("utf-8")`, BOM остаётся в тексте); `save` — только
    существующий файл, `conflict` с `"version"` нынешнего, запись `text.encode("utf-8")` через `store.write_atomic`.

- [ ] **Step 1: Тесты** (на `tmp_path`):

```python
def test_pid_is_the_head_of_sha256_of_the_folder_name():
    assert projects.pid("studio") == hashlib.sha256("studio".encode()).hexdigest()[:16]

def test_projects_skip_hidden_and_outside(tmp_path):
    root = tmp_path / "projects"; (root / "b").mkdir(parents=True); (root / "a").mkdir(); (root / ".git").mkdir()
    outside = tmp_path / "elsewhere"; outside.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "jump"))
    assert [p["name"] for p in projects.list_projects(str(root))] == ["a", "b"]
    assert projects.list_projects(str(tmp_path / "нет")) == []

@pytest.mark.parametrize("bad", ["CON", "sub/nul.txt", "COM1.log", "a.txt:поток", "dir./x", "x ", "..", "a//b",
                                 "a\\b", "/abs", "a\x01b", "x" * 1025])
def test_bad_paths_are_invalid_before_the_disk(tmp_path, bad):
    with pytest.raises(errors.Refused) as e:
        files.resolve(tmp_path, bad)
    assert e.value.code == "invalid"

def test_a_junction_out_of_the_project_is_outside_and_not_listed(tmp_path):
    proj = tmp_path / "p"; proj.mkdir(); out = tmp_path / "secret"; out.mkdir(); (out / "k.txt").write_text("x")
    _winapi.CreateJunction(str(out), str(proj / "link"))
    assert files.list_dir(proj, "")["items"] == []
    with pytest.raises(errors.Refused) as e:
        files.read_text(proj, "link/k.txt")
    assert e.value.code == "outside"

def test_save_keeps_bom_and_crlf_and_checks_the_version(tmp_path):
    f = tmp_path / "a.txt"; f.write_bytes(b"\xef\xbb\xbfone\r\ntwo")
    got = files.read_text(tmp_path, "a.txt")
    assert got["text"] == "\ufeffone\r\ntwo"
    new = files.save_text(tmp_path, "a.txt", got["text"] + "\r\nthree", got["version"])
    assert f.read_bytes() == b"\xef\xbb\xbfone\r\ntwo\r\nthree"
    with pytest.raises(errors.Refused) as e:
        files.save_text(tmp_path, "a.txt", "x", got["version"])
    assert e.value.code == "conflict" and e.value.extra["version"] == new["version"]

def test_binary_and_size_limits(tmp_path):
    (tmp_path / "n.bin").write_bytes(b"a\0b"); (tmp_path / "w.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "big.txt").write_bytes(b"a" * (2 * 1024 * 1024 + 1))
    assert _code(files.read_text, tmp_path, "n.bin") == "binary"
    assert _code(files.read_text, tmp_path, "w.txt") == "binary"
    assert _code(files.read_text, tmp_path, "big.txt") == "too_large"
    assert files.read_raw(tmp_path, "big.txt")["size"] == 2 * 1024 * 1024 + 1
```

(`_code(fn, *args)` — помощник теста: код `Refused`.) Плюс: список > `FILES_MAX` — `truncated`; `save` нового файла —
`not_found`; текст больше 2 МиБ в `save` — `too_large`.

- [ ] **Step 2:** → FAIL. **Step 3:** реализовать. **Step 4:** → PASS.
- [ ] **Step 5: Коммит** — «Мост: проекты и пути в пределах папки».

---

### Task 5: Статус git

**Files:**
- Create: `studio_agent/gitinfo.py`
- Test: `tests/test_gitinfo.py`

**Interfaces:**
- Produces: `gitinfo.parse(out: bytes) -> dict` — вывод `git status --porcelain=v2 --branch -z` →
  `{"branch", "upstream", "ahead", "behind", "files": [{"path", "status"}]}` (`branch` — `None` при `(detached)`,
  `upstream` — `None` без `# branch.upstream`; `status` — `XY` без точек (`.M` → `M`, `R.` → `R`, `MM` → `MM`), строка
  `u` — `U`, `?` — `??`; у переименования путь — новый; не больше `GIT_FILES_MAX`). `gitinfo.status(project: Path) -> dict`
  — `git -C <project> status …` с `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, сроком 25 с; код выхода ≠ 0 —
  `Refused("not_git")`.

- [ ] **Step 1: Тесты** — разбор готового вывода (переименование `2 R. … \0новый\0старый`, `u UU`, `? x`, `# branch.ab +3 -1`,
  путь с кириллицей и пробелом) и один `integration` с настоящим `git init` во временной папке: новый файл → `??`, после
  коммита и правки → `M`; папка без репозитория → `not_git`.

```python
def test_parse_branch_upstream_and_files():
    out = (b"# branch.oid abc\0# branch.head main\0# branch.upstream origin/main\0# branch.ab +3 -1\0"
           b"1 .M N... 100644 100644 100644 a b \xd0\xbf\xd0\xb0\xd0\xbf\xd0\xba\xd0\xb0/f 1.txt\0"
           b"2 R. N... 100644 100644 100644 a b R100 new.txt\0old.txt\0? x.log\0")
    assert gitinfo.parse(out) == {"branch": "main", "upstream": "origin/main", "ahead": 3, "behind": 1,
        "files": [{"path": "папка/f 1.txt", "status": "M"}, {"path": "new.txt", "status": "R"},
                  {"path": "x.log", "status": "??"}]}
```

- [ ] **Step 2–4:** FAIL → реализовать → PASS (быстрый набор и `-m integration tests/test_gitinfo.py`).
- [ ] **Step 5: Коммит** — «Мост: статус git».

---

### Task 6: Claude Code на машине — CLI, версия, модели

**Files:**
- Create: `studio_agent/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `cli.VSCODE_EXTENSIONS = Path.home() / ".vscode" / "extensions"`; `cli.find_cli(extensions: Path = VSCODE_EXTENSIONS)
    -> Path | None` — `resources/native-binary/claude.exe` новейшей папки `anthropic.claude-code-<версия>-win32-x64`
    (версии — числами: 2.1.312 > 2.1.99).
  - `cli.cli_version(path: Path | None) -> str` — первое слово `<cli> --version` (срок 30 с, ≤ 64 знака); `None` или сбой —
    `"встроенный в SDK"`.
  - `cli.models_from(info: dict | None) -> tuple[list[dict], list[str]]` — `info["models"]` (`value`, `displayName`,
    `supportedEffortLevels`) → `[{"id", "label"}]` без `default` и без `id` не по `MODEL_ID` (не больше 50, `label` ≤ 100);
    усилия — объединение `supportedEffortLevels` в порядке `EFFORTS`.
  - `async cli.probe(root: str, cli_path: Path | None, factory=ClaudeSDKClient) -> tuple[list[dict], list[str]]` — клиент
    с `cwd=root`, `cli_path`, `setting_sources=["user", "project", "local"]`: `connect()`, `get_server_info()`,
    `disconnect()`, всё ≤ 60 с; сбой — `([], [])`, в журнал — имя типа.

- [ ] **Step 1: Тесты** — `find_cli` на папке с `2.1.99`, `2.1.312` и `2.1.400` без `claude.exe` → `2.1.312`; пусто → `None`;
  `models_from` на `{"models": [{"value": "default", ...}, {"value": "opus", "displayName": "Opus 5.5",
  "supportedEffortLevels": ["low", "high"]}, {"value": "sonnet[1m]", "displayName": "Sonnet 1M",
  "supportedEffortLevels": ["medium", "max"]}, {"value": "bad id", "displayName": "x"}]}` →
  `([{"id": "opus", "label": "Opus 5.5"}, {"id": "sonnet[1m]", "label": "Sonnet 1M"}], ["low", "medium", "high", "max"])`;
  `models_from(None) == ([], [])`; `probe` с подставным клиентом, у которого `connect` бросает, → `([], [])`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Коммит** — «Мост: claude.exe расширения, версия, модели».

---

### Task 7: Сессии — список чатов и история

**Files:**
- Create: `studio_agent/events.py` (`clip`, `text_of`), `studio_agent/sessions.py`
- Test: `tests/test_sessions.py`

**Interfaces:**
- Consumes: функции SDK `list_sessions`, `get_session_info`, `get_session_messages`, `tag_session` (модуль держит их
  атрибутами — тесты подменяют `monkeypatch.setattr(sessions, "list_sessions", ...)`), `SessionMessage`, `SDKSessionInfo`.
- Produces:
  - `events.clip(value)` — копия, где каждая строка (в словарях и списках на любой глубине) длиннее `CLIP` урезана до
    `CLIP - 1` знаков + `…`. `events.text_of(content) -> str` — строка как есть; список блоков — тексты `text` через
    `\n`, `image` — «[изображение]», прочие — пропуск.
  - `sessions.chats(project: Path, live: dict[str, bool]) -> list[dict]` — `live`: чат → ждёт ли ответа; `list_sessions(
    directory=str(project), limit=CHATS_MAX, include_worktrees=False)` → `{"chat", "title", "created", "modified", "tag",
    "running", "asking"}`; `title` — `custom_title` или `summary` или `first_prompt[:120]`, не длиннее 200; времена SDK — мс
    Unix (сверить в исходнике установленной версии) → секунды; только `chat` вида UUID.
  - `sessions.normalize(messages: list[SessionMessage]) -> list[dict]` — элементы `{"id", "role", "text", "steps",
    "time": None}`: сообщения подагентов (`parent_tool_use_id`) — прочь; пользовательское с текстом или картинкой — свой
    элемент; пользовательское только с `tool_result` — итоги к шагам предыдущего ответа по `tool_use_id`
    (`text_of(content)`, `error` — `is_error`), элемента нет; подряд идущие ответы ассистента до следующего вопроса —
    один элемент: тексты блоков `text` через `\n\n`, `tool_use` — шаг `{"id", "tool", "input": clip(input), "text": "",
    "error": False}`, `thinking` и незнакомые — пропуск; `id` элемента — `uuid` его первой записи.
  - `sessions.fit(items: list[dict], budget: int = HISTORY_BUDGET) -> list[dict]` — хвост, что влезает в `budget` байтов
    `json.dumps(..., ensure_ascii=True)`, хотя бы один элемент; один элемент больше бюджета — строки его шагов урезаются до
    2000 знаков, затем шаги отбрасываются с начала, а в `text` добавляется строка «… первые шаги не показаны».
  - `sessions.history(project: Path, chat: str, before: int | None, limit: int) -> dict` — `normalize`; сообщений нет —
    `Refused("not_found", "нет такого чата")`; `end = len` или `before` в пределах, срез последних `limit`, затем `fit`;
    ответ `{"items", "start", "total"}`, `start` — номер первого выданного.
  - `sessions.exists(project, chat) -> bool` (`get_session_info`); `sessions.last_model(project, chat) -> str | None` —
    `message["model"]` последнего ответа ассистента главного агента, кроме `<synthetic>` и не по `MODEL_ID`;
    `sessions.mark_studio(project, chat) -> None` — `tag_session(chat, "studio", directory=...)`, ошибки глотаются.

- [ ] **Step 1: Тесты** на готовых `SessionMessage` (настоящий класс SDK):

```python
def test_normalize_merges_one_answer_and_attaches_results():
    msgs = [U("u1", "Сделай"), A("a1", [text("Смотрю."), tool("t1", "Bash", {"command": "git status"})]),
            U("u2", [result("t1", "чисто")]), A("a2", [text("Готово.")]), A("s1", [text("подагент")], parent="t9"),
            U("u3", "Ещё")]
    assert sessions.normalize(msgs) == [
        {"id": "u1", "role": "user", "text": "Сделай", "steps": [], "time": None},
        {"id": "a1", "role": "assistant", "text": "Смотрю.\n\nГотово.", "time": None,
         "steps": [{"id": "t1", "tool": "Bash", "input": {"command": "git status"}, "text": "чисто", "error": False}]},
        {"id": "u3", "role": "user", "text": "Ещё", "steps": [], "time": None}]

def test_history_page_keeps_start_true_when_the_budget_cuts_it(monkeypatch, tmp_path):
    big = [A(f"a{i}", [text("x" * 1_000_000)]) for i in range(12)]
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: big)
    page = sessions.history(tmp_path, CHAT, None, 100)
    assert page["total"] == 1 and len(json.dumps(page, ensure_ascii=True)) <= limits.HISTORY_BUDGET + 1000

def test_history_of_an_unknown_chat_is_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **k: [])
    with pytest.raises(errors.Refused) as e:
        sessions.history(tmp_path, CHAT, None, 100)
    assert e.value.code == "not_found"
```

(`U`, `A`, `text`, `tool`, `result` — помощники теста, собирают `SessionMessage` с сырым `message` вида транскрипта:
`{"role": "assistant", "model": "...", "content": [...]}`.) Плюс: 300 элементов, `before=250, limit=100` → `start == 150`,
`len(items) == 100`; страница, срезанная бюджетом, — `start` больше `end - limit` и `items` — хвост; одиночный огромный
элемент — пометка в `text`; `chats` — мс → секунды, `title` по цепочке, `running`/`asking` из `live`, не-UUID пропущен;
`last_model` пропускает `<synthetic>`; `clip` урезает вложенную строку до 20 000 знаков с `…`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Коммит** — «Мост: чаты и история сессий».

---

### Task 8: Ход — `Runner`

**Files:**
- Create: `studio_agent/turns.py`
- Modify: `studio_agent/events.py` (`Feed`, `ask_event`), `studio_agent/store.py` (`Choices`)
- Test: `tests/fake_sdk.py`, `tests/test_turns.py`

**Interfaces:**
- Consumes: `sessions.exists/last_model/mark_studio`, `events.clip/text_of`, `cli.find_cli`, типы SDK
  (`ClaudeAgentOptions`, `ClaudeSDKClient`, `AssistantMessage`, `UserMessage`, `ResultMessage`, `StreamEvent`,
  `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `PermissionResultAllow`, `PermissionResultDeny`, `HookMatcher`,
  `CLINotFoundError`, `ResultError`).
- Produces:
  - `store.Choices(h)`: `get(chat) -> tuple[str | None, str | None]`, `put(chat, model, effort) -> None` (`choices.json`,
    ≤ 500 чатов, старые прочь).
  - `events.Feed()` — разбор потока одного хода: `feed.events(message) -> list[dict]` (события без обёртки, `{"k": ...}`):
    `StreamEvent` главного агента — `content_block_delta`/`text_delta` → `text`; `content_block_start` текста, когда уже
    есть текст хода не на `\n`, — сперва `text` «\n\n»; `AssistantMessage` — `ToolUseBlock` → `step`; его `TextBlock` —
    `text`, только если за ход не было ни одной дельты; `UserMessage` — `ToolResultBlock` → `result {"id", "text",
    "error"}`; подагенты (`parent_tool_use_id`) — ничего. `feed.text`, `feed.steps` — накопленное для снимка.
  - `events.ask_event(request_id, tool: str, data: dict, always: bool) -> dict` — `AskUserQuestion` → `{"k": "ask",
    "request_id", "kind": "question", "questions": [{"question", "header", "multi": multiSelect, "options": [{"label",
    "description"}]}]}`, иначе `{"k": "ask", "request_id", "kind": "permission", "tool", "input": clip(data), "always"}`.
  - `turns.Runner(emit: Callable[[dict], None], *, choices: store.Choices, cli_path: Path | None,
    factory=ClaudeSDKClient, max_turns: int = MAX_TURNS)`; `emit` получает кадр `{"t": "ev", "project", "chat", "turn",
    "e"}`. Методы: `start(project: Path, pid: str, chat: str, text: str, model: str | None, effort: str | None) -> str`
    (синхронно: проверка `running`/`busy`, запись хода, задача; `turn` — `secrets.token_hex(8)`);
    `watch(chat) -> tuple[bool, list[dict]]` (идёт — `[кадр snapshot]`); `answer(chat, request_id, decision, message,
    answers) -> None`; `async stop(chat) -> None`; `async close() -> None` (остановить все при выходе);
    `live() -> dict[str, bool]` (чат → есть открытая карточка).
  - Опции хода — `turns.options_for(...) -> ClaudeAgentOptions`: `cwd=project`, `cli_path`, `session_id=chat` (новая) или
    `resume=chat`, `model`, `effort`, `include_partial_messages=True`, `can_use_tool`, `hooks={"PreToolUse":
    [HookMatcher(matcher=None, hooks=[_keep_alive])]}` (холостой, `{"continue_": True}`),
    `setting_sources=["user", "project", "local"]`, `env={"CLAUDE_AGENT_SDK_CLIENT_APP": f"studio-agent/{VERSION}"}`,
    `stderr` — отбрасывать.

Ход (`_run`): `factory(options)` → `connect()` → `query(text)` → `receive_response()` через `Feed` → `emit`. Карточка:
`can_use_tool(tool, data, ctx)` — `request_id` = `ctx.tool_use_id`, если он по `[A-Za-z0-9_-]{1,64}`, иначе
`token_hex(8)`; событие `ask`, ожидание ответа; `allow` → `PermissionResultAllow(updated_input=data)`; `always` → ещё
`updated_permissions=ctx.suggestions`; `deny` → `PermissionResultDeny(message=message or "Пользователь отказал")`;
вопрос → `PermissionResultAllow(updated_input={**data, "answers": answers})`; после — событие `asked`. Конец, в этом
порядке: открытые карточки — отказ «Остановлено» с `interrupt=True` и `asked`; `disconnect()` (≤ 10 с); новой сессии —
`mark_studio`; ход прочь из `running`; событие `done` (`stopped` — после `stop`; `error` — `CLINotFoundError`, иной сбой,
`ResultError` или `ResultMessage.is_error`, слова — раздел «Тексты»; иначе `done`). `answer` проверяет: решение из трёх;
вопрос — только `allow` со словарём `answers` (строки или списки строк); карточка разрешения — `answers is None`;
`message` — строка ≤ 2000 или `None`; иначе `invalid`; нет карточки — `gone`.

- [ ] **Step 1: Подставной SDK** `tests/fake_sdk.py`: `FakeClient(options)` — `connect`, `query`, `receive_response` (идёт по
  `script` — списку настоящих сообщений SDK и указаний `("ask", tool, data, suggestions)`: на указании зовёт
  `options.can_use_tool` и ждёт его итога, итог кладёт в `client.decisions`), `interrupt` (обрывает сценарий с
  `ResultMessage(subtype="error_during_execution", is_error=True, ...)`), `disconnect`; фабрика `FakeFactory(script,
  fail=None)` помнит выданные опции.

- [ ] **Step 2: Тесты** `tests/test_turns.py`:

```python
async def test_turn_streams_text_steps_and_ends_done(home, project):
    sent, f = [], FakeFactory([delta("При"), delta("вет"), assistant(tool_use("t1", "Read", {"file_path": "a"})),
                               user(tool_result("t1", "ok")), result_ok()])
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=f)
    turn = r.start(project, PID, CHAT, "Привет", None, None)
    await settle(r)
    assert [e["e"]["k"] for e in sent] == ["text", "text", "step", "result", "done"]
    assert {e["turn"] for e in sent} == {turn} and sent[-1]["e"] == {"k": "done", "status": "done"}
    assert f.options[0].session_id == CHAT and f.options[0].resume is None
    assert f.options[0].setting_sources == ["user", "project", "local"]

async def test_permission_card_allow_always_deny(home, project):
    ...  # ask → событие ask с always=True при suggestions; answer("always") → PermissionResultAllow с теми suggestions;
         # повтор answer → Refused gone; deny без слов → PermissionResultDeny(message="Пользователь отказал")

async def test_question_returns_answers_in_updated_input(home, project): ...

async def test_stop_during_a_card_denies_it_and_ends_stopped(home, project):
    ...  # события: ask, asked, done{status: stopped}; решение карточки — PermissionResultDeny(interrupt=True)

async def test_second_turn_in_the_chat_is_running_and_third_chat_is_busy(home, project): ...

async def test_missing_cli_ends_with_words_and_frees_the_chat(home, project):
    f = FakeFactory([], fail=CLINotFoundError("нет"))
    ...  # done{status: error, message: "Claude Code не найден на компьютере"}; затем новый start того же чата — без running

async def test_existing_session_resumes_with_the_remembered_choice_then_the_last_model(home, project, monkeypatch):
    ...  # exists → resume=CHAT, session_id None; model=None, effort=None → из Choices; Choices пуст → last_model, effort None

async def test_new_session_is_marked_studio_after_disconnect(home, project, monkeypatch): ...

async def test_watch_gives_a_snapshot_with_the_open_card_and_answer_still_works(home, project):
    ...  # во время карточки: watch → (True, [snapshot{text, steps, asks:[без k]}]); answer → ход идёт дальше до done

async def test_done_comes_after_the_chat_left_running(home, project):
    ...  # в момент emit(done) r.live() не содержит CHAT и r.start того же чата не бросает running
```

(`settle(r)` — дождаться задач хода; `delta`, `assistant`, `user`, `result_ok` — помощники `fake_sdk`.)
- [ ] **Step 3–4:** реализовать → PASS.
- [ ] **Step 5: Коммит** — «Мост: ход через Agent SDK, карточки, остановка».

---

### Task 9: `Agent` — просьбы студии

**Files:**
- Create: `studio_agent/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `pin.PinStore`, `pin.OpenSessions`, `projects`, `files`, `gitinfo`, `sessions`, `turns.Runner`, `store.Config`.
- Produces: `agent.Agent(cfg: store.Config, pins: PinStore, runner: Runner, *, now=time.monotonic, cc_version: str,
  models: list[dict], efforts: list[str])`:
  - `hello() -> dict` — `{"t": "hello", "v": 1, "cc_version", "projects_root": cfg.projects_root, "has_pin":
    pins.has_pin, "models", "efforts"}`; `set_models(models, efforts)`.
  - `async handle(frame: dict) -> list[dict]` — кадр `req` → `[res, *после]` (снимок `watch` — после `res`); кадр
    `res` ≤ `FRAME_MAX`, иначе вместо него отказ `too_large` «ответ больше предела канала».
  - `close_session(session: str)`, `new_link()` (все открытые сессии прочь), `emit(frame)` — для `Runner`: отдаёт в
    `self.send`, если он задан (ставит `Link`), иначе событие пропадает.
  - Проверка полей до исполнения (иначе `invalid`): `id` — 16 hex (ответ на кадр с плохим `id` — с тем же `id`, если это
    строка ≤ 64, иначе без ответа); `session` — 32 hex; `project`, `chat`, `path` (как `files.resolve`), `before` — `int ≥ 0`
    или `None`, `limit` — `int` 1–100, `text` — строка ≤ 100 000, `model` — из `hello` или `None`, `effort` — из `hello`
    или `None`, `version` — 64 hex.
  - `open`: под `asyncio.Lock`; PIN не задан — открыть; `pin is None` и сессия открыта — продлить; `pin is None` —
    `pin_needed`; иначе `pins.check` в потоке: `("pin", left)` — отказ с `"left"`, `pin_locked`; верный — открыть.
  - Остальные — `touch(session)` или `closed`; затем `projects` / `chats` (`live=runner.live()`) / `history` / `turn` /
    `watch` / `answer` / `stop` / `files` / `read` / `raw` / `save` / `git`. Неизвестный `op` — `invalid`. Исключение не
    `Refused` — отказ `error`, в журнал — `op` и имя типа.

- [ ] **Step 1: Тесты** (подставной `Runner` из `fake_sdk` или настоящий с `FakeFactory`):

```python
async def test_everything_but_open_is_closed_until_opened(agent):
    assert (await agent.handle(req("projects")))[0]["code"] == "closed"
    assert (await agent.handle(req("open", pin=None)))[0]["ok"] is True
    assert (await agent.handle(req("projects")))[0]["data"]["items"] == [{"pid": projects.pid("demo"), "name": "demo"}]

async def test_pin_flow_and_open_null_for_an_open_session(agent_with_pin):
    a = agent_with_pin
    assert (await a.handle(req("open", pin=None)))[0]["code"] == "pin_needed"
    assert (await a.handle(req("open", pin="1111")))[0] | {"id": None} == {"t": "res", "id": None, "ok": False,
        "code": "pin", "message": "неверный PIN", "left": 4}
    assert (await a.handle(req("open", pin="2468")))[0]["ok"]
    assert (await a.handle(req("open", pin=None)))[0]["ok"]           # уже открыта — без PIN

async def test_close_and_new_link_close_sessions(agent): ...

async def test_watch_sends_res_then_snapshot(agent): ...   # [res{running: True}, ev{snapshot}]

async def test_bad_fields_are_invalid(agent):
    ...  # session 31 hex, project "XYZ", chat с заглавными, limit 0 и 101, before -1, path "CON", model вне hello

async def test_a_crash_in_an_op_is_error_and_the_next_request_works(agent, monkeypatch):
    monkeypatch.setattr(sessions, "history", lambda *a: (_ for _ in ()).throw(ValueError("битый JSONL")))
    ...  # res code "error", message "ошибка моста"; следующая просьба отвечает

async def test_oversized_answer_is_refused_too_large(agent, monkeypatch): ...

async def test_journal_has_no_secrets(agent_with_pin, caplog):
    ...  # сценарий с PIN "2468", проектом «секретный-проект», файлом «пароли.txt», текстом хода «тайное задание»:
         # ни одной из этих строк в caplog.text
```
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Коммит** — «Мост: просьбы студии, открытость сессий».

---

### Task 10: `Link` — канал

**Files:**
- Create: `studio_agent/link.py`
- Test: `tests/fake_studio.py`, `tests/test_link.py`

**Interfaces:**
- Consumes: `agent.Agent` (`hello`, `handle`, `close_session`, `new_link`, `send`), `limits`.
- Produces:
  - `link.ws_url(url: str) -> str` — `https://h` → `wss://h/api/agent/link`; `http://` — только `localhost`, `127.0.0.1`,
    `[::1]` (тесты); иное — `ValueError`.
  - `link.Link(url: str, key: str, agent: Agent, on_state: Callable[[str], None] = lambda s: None, *,
    connect=websockets.asyncio.client.connect, sleep=asyncio.sleep)`; `async run() -> str` — до `revoked`, `replaced` или
    `stop()`; `send(frame: dict) -> None` — в очередь отправки текущего канала, нет канала — пропуск; `async stop()`.
    Состояния `on_state`: `connecting`, `online`, `offline`, `revoked`, `replaced`, `stopped`.
  - Подключение: `connect(ws_url, additional_headers={"Authorization": f"Bearer {key}"}, max_size=FRAME_MAX,
    ping_interval=20, ping_timeout=20, open_timeout=15, user_agent_header=f"studio-agent/{VERSION}")`; первым — `hello`;
    `welcome` ≤ 10 с (иначе закрыть и пауза); после `welcome` — `agent.new_link()`, `agent.send = self.send`, пауза
    переподключения — сначала. Кадры: `req` — `agent.handle` своей задачей, ответы — в очередь (кадры одного `handle`
    подряд); `close` — `agent.close_session`; прочие `t` — пропуск. Одна задача-писатель на канал.
  - Закрытие: 4401 — `revoked`, выход; 4409 — `replaced`, выход; 4400 — пауза `PROTOCOL_PAUSE_SEC`; прочее, отказ
    рукопожатия (`InvalidStatus`), `OSError`, `TimeoutError` — пауза по `BACKOFF` (с последней ступенью дальше).
    Задачи просьб при обрыве не отменяются (запись файла не рвётся) — их ответы пропадут.

- [ ] **Step 1: Подставная студия** `tests/fake_studio.py` — `websockets.asyncio.server.serve` на `127.0.0.1:0`:
  `Authorization` сверяет с ключом (иначе принимает и закрывает 4401, как сервер), ждёт `hello`, шлёт `welcome`;
  `await studio.request(op, **fields) -> dict` (кадр `res` с тем же `id`), `studio.events` — полученные `ev`,
  `studio.hellos`, `await studio.close(code)`, `studio.reject_handshake = 401` (через `process_request`).

- [ ] **Step 2: Тесты**:

```python
async def test_hello_first_then_requests_are_answered(fake_studio, agent):
    link = Link(fake_studio.url, KEY, agent, sleep=no_sleep)
    task = asyncio.create_task(link.run())
    await fake_studio.connected()
    assert fake_studio.hellos[0]["v"] == 1 and fake_studio.hellos[0]["has_pin"] is False
    assert (await fake_studio.request("open", pin=None))["ok"] is True

async def test_revoked_stops_and_replaced_stops(fake_studio, agent):
    ...  # close(4401) → run() == "revoked", подключений больше нет; отдельно 4409 → "replaced"

async def test_restart_reconnects_and_closes_open_sessions(fake_studio, agent):
    ...  # open; close(1012); после переподключения projects → closed; паузы — 1, затем снова 1 (сброс после welcome)

async def test_handshake_rejection_is_retried_not_revoked(fake_studio, agent):
    ...  # reject_handshake = 401 дважды, затем приём: паузы [1, 2], состояние online

async def test_turn_res_comes_before_its_events_and_a_card_survives_reconnect(fake_studio, home, project):
    ...  # настоящий Agent + Runner с FakeFactory: turn → res раньше первого ev; на карточке close(1012);
         # после переподключения open, watch → res{running: True}, затем snapshot с этой карточкой;
         # answer allow по новому каналу → ход дошёл до done, done пришёл в новый канал

async def test_events_without_a_channel_are_dropped(agent): ...

def test_ws_url_rules():
    assert link.ws_url("https://studio.example.com") == "wss://studio.example.com/api/agent/link"
    assert link.ws_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/api/agent/link"
    with pytest.raises(ValueError):
        link.ws_url("http://studio.example.com")
```
- [ ] **Step 3–4:** реализовать → PASS (весь быстрый набор ≤ 60 с).
- [ ] **Step 5: Коммит** — «Мост: канал к студии, переподключение».

---

### Task 11: Привязка, настройка, запуск без окна

**Files:**
- Create: `studio_agent/enroll.py`, `studio_agent/setup.py`, `studio_agent/single.py`, `studio_agent/__main__.py`
- Test: `tests/test_enroll.py`, `tests/test_setup.py`

**Interfaces:**
- Produces:
  - `enroll.EnrollError(Exception)` с `code` и `words` (раздел «Тексты»: 401 `login`, 429 `locked`, 422 `name`,
    413/403/прочее — «Студия недоступна (…)»). `enroll.enroll(url, login, password, name, *, timeout=30) -> tuple[int, str]`
    — `POST <url>/api/agent/enroll` JSON `urllib.request`, без `Origin`; адрес — по правилу `link.ws_url` (https,
    `http` — только петля).
  - `setup.validate(form: dict, *, enrolled: bool) -> list[str]` — поля `url, login, password, name, projects_root, pin,
    pin2, no_pin, autostart`; логин и пароль обязательны, если `not enrolled` (иначе — оба или ни одного); имя 1–64 знака
    без управляющих; папка есть; PIN — пусто или по `PIN_RE`, `pin == pin2`.
  - `setup.apply(h: Path, form: dict, *, enroll_fn=enroll.enroll) -> store.Config` — с логином: привязка, `save_key`,
    `agent_id`, `name`; всегда: `projects_root`, `url`, `autostart` → `save_config`; PIN: `no_pin` — `set_pin(None)`,
    заполнен — `set_pin(pin)`, пусто — не трогать. Пароль нигде не сохраняется.
  - `single.acquire(name: str = "Local\\StudioAgent") -> object | None` — `CreateMutexW`; уже есть (`GetLastError() ==
    183`) — `None`.
  - `__main__` — `python -m studio_agent [run] [--console]` (по умолчанию — с треем, задача 12), `enroll` (адрес, логин,
    имя, папка — `input`, пароль — `getpass`), `set-pin`, `clear-pin`, `reset-pin` (снять блок), `status` (адрес, id,
    имя, папка, PIN задан/блок — без ключа). `run --console` — канал без трея, журнал ещё и в консоль.

- [ ] **Step 1: Тесты** — `enroll` против `http.server` в потоке (201 → `(7, "k")`; 401 → `code == "login"`, слова «Неверный
  логин или пароль»; 429 → `locked`; тело запроса — ровно `{"login", "password", "name"}`, заголовка `Origin` нет);
  `validate` — пустая папка, PIN «12», несовпадение, имя 65 знаков, `enrolled` без логина — без ошибок; `apply` с
  подставным `enroll_fn` — ключ в `key.bin`, пароля нет ни в одном файле `home` (поиск байтов пароля в UTF-8 и UTF-16-LE);
  `single.acquire` — второй вызов того же имени → `None`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Коммит** — «Мост: привязка, настройка, запуск из консоли».

---

### Task 12: Трей, окно настройки, автозапуск, установка

**Files:**
- Create: `studio_agent/autostart.py`, `studio_agent/tray.py`, `studio_agent/window.py`, `studio_agent/app.py`,
  `studio_agent.pyw`, `install.ps1`
- Test: `tests/test_autostart.py` (`integration` — пишет реестр), `tests/test_app.py`

**Interfaces:**
- Consumes: всё выше.
- Produces:
  - `autostart.RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"`, `VALUE = "StudioAgent"`;
    `command() -> str` — `"<repo>\.venv\Scripts\pythonw.exe" "<repo>\studio_agent.pyw"`; `enabled(key=RUN_KEY) -> bool`,
    `set_enabled(on: bool, key=RUN_KEY) -> None` (`winreg`, `HKEY_CURRENT_USER`).
  - `app.Controller` — без Tk и pystray (тестируется): `start()` (поток с циклом `asyncio`: `cli.find_cli`,
    `cli_version`, `Agent`, `Runner`, `Link.run`; `cli.probe` — до первого подключения и затем раз в час фоном,
    итог — `agent.set_models`), `restart_link()` (после настройки и смены PIN),
    `apply_setup(form) -> list[str]` (`setup.validate` + `setup.apply` + `autostart.set_enabled` + `restart_link`),
    `reset_pin()`, `quit()` (`runner.close()`, `link.stop()`, остановка цикла), `state: str`, подписка `on_state`.
  - `app.main(argv)` — `single.acquire` (иначе окно «Мост уже запущен» и выход), `journal.setup`; главный поток — скрытый
    корень Tk (`withdraw`), опрос очереди команд `root.after(100, …)`; трей — `pystray.Icon` в своём потоке, значок —
    круг Pillow (зелёный «на связи», серый «подключается…»/«нет связи»/«не настроен», красный «отозван»/«заменён»),
    меню и подсказка — раздел «Тексты»; команды меню — в очередь Tk. Не настроен (нет ключа) — окно настройки сразу.
  - `window.open_setup(root, controller)` — `Toplevel` с полями и подписями раздела «Тексты»; «Привязать» (не привязан) /
    «Сохранить» (привязан; логин и пароль — по желанию, для новой привязки); ошибки — под формой; «Снять блок PIN» —
    `controller.reset_pin()`; пароль после отправки стирается из поля.
  - `studio_agent.pyw` — `from studio_agent.app import main; main(sys.argv[1:])`.
  - `install.ps1` (UTF-8 BOM, CRLF) — параметр `-Python` (по умолчанию `py -3.14`, нет — `python`), `.venv` рядом,
    `pip install -r requirements.txt`, запуск `pythonw.exe studio_agent.pyw` без ожидания; автозапуск ставит окно
    (галочка). Повторный запуск безопасен.

- [ ] **Step 1: Тесты** — `Controller.apply_setup` с подставными `enroll_fn`, `Link` и `autostart` (галочка снята →
  `set_enabled(False)`; ошибки `validate` — без привязки и перезапуска); смена PIN → `restart_link` вызван; `quit()` зовёт
  `runner.close()` и `link.stop()`. `integration`: `set_enabled(True, key=r"Software\studio-agent-test\Run")` → значение
  равно `command()`, `enabled` → `True`, затем `False` и ключ удалён; `install.ps1` разбирается парсером PowerShell без
  ошибок и начинается с BOM.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Проверка руками** — `.venv/Scripts/python studio_agent.pyw` с `STUDIO_AGENT_HOME` во временной папке:
  значок появился, окно настройки открылось, «Выход» гасит процесс; второй запуск — «Мост уже запущен».
- [ ] **Step 6: Коммит** — «Мост: трей, окно настройки, автозапуск».

---

### Task 13: Сквозная проверка со студией, документы, установка

**Files:**
- Create: `tests/live_studio_server.py` (запускается Python студии), `tests/test_live_studio.py` (`manual`)
- Modify: `README.md`, `CLAUDE.md`, `AGENTS.md` (studio-agent); `C:/work/projects/studio/CLAUDE.md` (строка «вторым
  планом, ещё нет» → проект `C:/work/projects/studio-agent`), `C:/work/projects/studio/README.md` (как подключить компьютер)
- Create: `C:/work/projects/local-config/wiki/projects/studio-agent/architecture.md` (+ строка в `_log.md`)

- [ ] **Step 1: Живая студия локально.** `tests/live_studio_server.py <tmp>` — `sys.path` + `<STUDIO_REPO>/app` и
  `<STUDIO_REPO>/tests`, `start_live_app(Path(tmp))`, печатает `READY <url>`, ждёт закрытия stdin, гасит.
  `tests/test_live_studio.py` (`manual`, `STUDIO_REPO` — по умолчанию `C:/work/projects/studio`): поднимает его
  `<STUDIO_REPO>/.venv/Scripts/python`; временные `home` и папка проектов с проектом `probe` (файл `a.md`, `git init`,
  `.claude/settings.json` = `{"permissions": {"ask": ["Bash(echo studio-agent-probe*)"]}}`); `enroll.enroll(url, "owner",
  "password-123", "Проба")`; настоящие `Agent`, `Runner` (настоящий SDK и CLI), `Link` в цикле теста. Сторона браузера —
  `urllib`: вход `POST /login` как в `tests/conftest.py` студии (cookie из `Set-Cookie` — руками в `Cookie`: cookiejar не
  шлёт Secure по http; CSRF — как там же). Сценарий: `GET /api/agents` — «на связи»; `open` → проекты → `probe`;
  «Новый чат»; ход «Ответь одним словом: готово» — поток SSE до `done`, в тексте «готово»; ход «Выполни в Bash: echo
  studio-agent-probe» — пришла карточка `permission` с `Bash`, `answer allow`, затем `result` с `studio-agent-probe` и
  `done`; `GET …/chats` — чат с `origin: "studio"`; история — 4 сообщения; `files`, `read a.md`, `git`. Потом PIN:
  `set_pin("2468")`, перезапуск канала → `open` с `null` → 409 `pin_needed`, с «2468» → 204.

Run: `.venv/Scripts/python -m pytest -m manual tests/test_live_studio.py -n0` (два коротких хода квоты) → PASS.

- [ ] **Step 2: Полный прогон** `.venv/Scripts/python -m pytest -q` (≤ 60 с, замер `--durations=10`) и
  `-m integration` → PASS.
- [ ] **Step 3: Документы.** README — установка (`install.ps1`), настройка, PIN и снятие блока, отзыв, где журнал, что
  мост делает и чего не делает; CLAUDE.md — устройство по модулям, решения плана, ловушки (ключ — заголовком, PIN — на
  машине, кадры ASCII, `setting_sources`, клиент на ход); вики — страница архитектуры с `[[wikilinks]]` на
  `[[studio/architecture-claude-code]]`; правки `studio` (CLAUDE.md, README) — отдельным коммитом в `studio`.
- [ ] **Step 4: Установка на workstation** (учётка `developer`): `install.ps1`; значок «не настроен», окно открыто. Привязку
  к `studio.example.com` делает владелец своим паролем (пароль из vault — восстановления, вход им запрёт владельца; не
  пробовать).
- [ ] **Step 5: Коммиты** в `studio-agent` и `studio`; push — по слову владельца.
- [ ] **Step 6: Итог владельцу** — что проверить по критериям спеки §2 (1–6): привязка в окне, «на связи», PIN, новый
  чат и ход, карточка и push при погасшем экране, сессия VS Code в списке; build-host (учётка `Services`) — тот же
  `install.ps1` из клона `origin`, по его слову.
