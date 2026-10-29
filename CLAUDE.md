# CLAUDE.md — studio-agent

Windows-мост к студии `https://studio.example.com`: держит исходящий канал WebSocket к студии и отдаёт ей
локальный Claude Code владельца — проекты, чаты, ход с карточками разрешений, файлы, git — строго по
протоколу версии 1. Мост собран по плану (задачи 1–13) и проверен сквозным тестом с живой студией и настоящим
Claude Code (`tests/test_live_studio.py`). Как ставить и пользоваться — [README.md](README.md).

- Спека: `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-claude-code-design.md` (§2, §4, §6.3,
  §6.6, §6.10, §7, §8); окно, трей, отвязка и установка из архива студии (версия 0.2.0) —
  `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-agent-download-and-window-design.md` (§5).
- Протокол (контракт с сервером): `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`.
- План: `C:/work/projects/studio/docs/superpowers/plans/2026-10-28-studio-agent-bridge.md`.
- Сервер — только читать: `C:/work/projects/studio/app/bridge.py`, `app/claude_code.py`; эталон поведения машины —
  `C:/work/projects/studio/tests/fake_bridge.py` (`FakeMachine`).

Репозиторий — **приватный**, ветка `main`; push — только по слову владельца.

## Устройство

Пакет `studio_agent`:

- `limits` — все числа протокола и моста (`FRAME_MAX`, `PIN_TRIES`, `OPEN_IDLE_SEC`, `TEXT_MAX`, `HISTORY_BUDGET`, …).
- `errors` — `Refused(code, message="", **extra)`: отказ просьбы; без слов — `WORDS[code]` («Тексты» плана); у
  `not_found` и `too_large` слова разные по месту — их передаёт вызывающий.
- `dpapi` — `protect`/`unprotect` (DPAPI области пользователя, `CRYPTPROTECT_UI_FORBIDDEN`, энтропия
  `b"studio-agent\0v1"`); чужой или битый блоб — `OSError`.
- `store` — каталог моста `home()` (`STUDIO_AGENT_HOME`, иначе `%LOCALAPPDATA%\studio-agent`), `Config` в
  `config.json` (`login` — логин учётки студии для окна и трея: из ответа привязки и `welcome`; битый — не JSON, не
  объект, поле не того вида — `Config()` и строка в журнале, файл не трогается до записи окном), ключ устройства в `key.bin` под DPAPI (не читается — `None`), `write_atomic`.
- `journal` — логгер `studio_agent` в `agent.log` (1 МиБ × 3, UTF-8).
- `pin` — `PinStore` (`pin.bin` под DPAPI: scrypt-хеш и счёт неверных; попытка пишется на диск до scrypt, проверки
  идут по одной — замок процесса; 5 подряд — `pin_locked` до `reset` на машине; нет файла — PIN не задан, блоб не
  читается — `OSError`) и `OpenSessions` (открытые сессии студии в памяти, срок простоя `OPEN_IDLE_SEC`).
- `projects` — `pid`, `list_projects` (подпапки корня без `.`-имён, скрытых, системных и ведущих наружу), `find_project`
  (настоящий путь или `not_found`).
- `files` — `resolve` (вид пути проверяется **до диска**: имена устройств, ADS `:`, точка или пробел в конце части —
  `invalid`; настоящий путь вне проекта — `outside`), `list_dir`, `read_text`, `read_raw` (base64), `save_text`
  (`version` — sha256 байтов, запись через `store.write_atomic`; «только чтение» — `read_only` «файл только для
  чтения» до записи, прочие атрибуты прежнего файла — новому `SetFileAttributesW`).
- `gitinfo` — `parse` (porcelain v2 `-z`), `status` (`git -C`, без окна и необязательных замков, 25 с;
  `GIT_CEILING_DIRECTORIES` — родитель настоящего пути проекта: проект без своего `.git` внутри чужого репозитория —
  `not_git`; код ≠ 0 или нет git — `not_git`, срок вышел — `TimeoutExpired` наружу).
- `cli` — `find_cli` (`claude.exe` новейшего расширения VS Code, версии числами; нет — CLI, встроенный в SDK),
  `current(path, find)` (прежний путь, пока файл есть, иначе `find()` заново — как `worker.py::_claude_bin`),
  `drop_session_env` (из окружения моста прочь `SESSION_ENV`, `SESSION_PREFIXES`, `TERM_PROGRAM=vscode` и
  `GIT_ASKPASS`/`GIT_EDITOR`, ведущие в папку VS Code),
  `cli_version` (первое слово `--version`, 30 с; сбой — «встроенный в SDK»), `models_from` (`get_server_info()["models"]`
  без `default` → `{"id", "label"}` и усилия в порядке `EFFORTS`), `probe` (короткий клиент в папке проектов,
  `setting_sources` user/project/local, ≤ 60 с, `disconnect` и при сбое; stderr Claude Code отбрасывается).
- `events` — `clip` (строки на любой глубине — до `CLIP` знаков с «…»), `bound` (событие `step`, `result`, `ask` больше
  `EVENT_MAX` 1 МиБ в ASCII-JSON — `input`/`text`/`questions` до `CLIP_TIGHT` 2000, всё ещё больше — `input` пометкой
  `HIDDEN`; шаг снимка хранится уже урезанным, CLI получает ввод целиком), `text_of` (текст блоков содержимого,
  картинка — «[изображение]»).
- `sessions` — функции SDK атрибутами модуля (тесты подменяют): `chats` (сессии проекта без worktree, только `chat`-UUID,
  мс → с, новые сверху, `running`/`asking` из ходов моста), `normalize` (главный агент; ответы до следующего вопроса —
  одно сообщение, итоги инструментов — к шагам), `fit` (хвост ≤ `HISTORY_BUDGET`; одиночный огромный — строки шагов до
  2000, шаги с начала прочь «… первые шаги не показаны», затем хвост текста «… начало ответа не показано»), `history`
  (нет записей — `not_found` «нет такого чата»), `exists`, `last_model` (без `<synthetic>`), `mark_studio` (метка
  `studio`, сбой — в журнал).
- `turns` — `Runner` (ходы через Agent SDK): клиент на ход (новая сессия — `session_id=chat`, есть — `resume=chat`;
  `null` модели и усилия — запомненные `store.Choices`, иначе модель последнего ответа сессии), не больше `MAX_TURNS`
  сразу и один в чате (`busy`, `running`); `can_use_tool` → событие `ask` и ожидание `answer` (карточка переживает
  смену канала), `answer`/`stop`/`watch` — только хода того же `pid` (иначе как без хода), `watch` — снимок `snapshot` (урезан
  `sessions.fit` и закодирован в потоке; события хода тем временем — в `held`, за снимком), `stop` — отказ карточкам
  и `interrupt()`
  (`idle` без хода; interrupt упал или не успел, или `stop` повторный — отмена задачи хода: `_run` доводит `done`
  `stopped`, `disconnect` гасит процесс), `close` — мост выходит или отозван (`done` «ход прерван: мост остановлен»;
  пока идёт — `start` отвечает `busy` «мост выходит»; `final` — выход моста — и после; `hold` — отвязка, отзыв,
  замена — и после, до `reopen()` (ядро зовёт его в `_relink` перед новым каналом): просьба `turn`, чей `find_project`
  ещё шёл, хода не начнёт; `stop` во время `_finish` задачу не отменяет — `finishing`); путь `claude.exe` — `cli.current` в начале каждого хода (в потоке `_choose`);
  метка `studio` новой сессии — после `disconnect`.
- `link` — `ws_url` (`https://h` → `wss://h/api/agent/link`; `http` — только `localhost`, `127.0.0.1`, `[::1]`) и `Link`
  (канал к студии): `connect` с `Authorization: Bearer <ключ>`, `max_size=FRAME_MAX`, пинги 20/20 с; первым — `hello`,
  `welcome` ≤ `WELCOME_SEC` (кадр — колбэку `on_welcome` в цикле, его сбой канал не рвёт), затем `agent.new_link()` и `agent.send = link.send`; `req` — `agent.handle` своей задачей,
  ответы подряд в очередь канала, по которому пришла просьба (оборвался — пропадают; задачи не отменяются); `close` —
  `agent.close_session`; не JSON и прочие `t` — пропуск. Писатель — одна задача на канал, кадры — `ensure_ascii=True,
  allow_nan=False` (`events.Ready` — готовой строкой). Закрытие 4401 — `revoked`, 4409 — `replaced` (выход из `run()`), 4400 — пауза `PROTOCOL_PAUSE_SEC`,
  прочее, отказ рукопожатия и любое неожиданное исключение канала (в журнал — имя типа) — `BACKOFF` по числу неудач
  подряд (сброс после `welcome`); `stop()` досылает очередь писателю не дольше `FLUSH_SEC` (2 с: `done` ходов при
  выходе), затем закрывает; `on_state`: `connecting`,
  `online`, `offline` (перед каждой паузой), `revoked`, `replaced`, `stopped`. Паузы — параметр `sleep`; тесты —
  подставная студия `tests/fake_studio.py` (`websockets` на `127.0.0.1:0`).
- `agent` — `Agent` (просьбы студии): `hello` (`has_pin` — в памяти, к диску не ходит: читается при создании и `refresh_pin` ядра перед каждым каналом;
  нечитаемый `pin.bin` — `true`), `handle` (кадр `req` → `[res, *после]`; поля — по `OPS` до
  исполнения, иначе `invalid`; `id` не вернуть — без ответа; `open` под замком — порядок Ruling 4: без PIN — открыть,
  `null` открытой — `{}`, блок — `pin_locked`, `null` — `pin_needed`, иначе `pins.check` в потоке; прочие — `touch`
  или `closed`; `watch` без хода — `{"running": false}` без проверки чата; не `Refused` — `error`, в журнал `op` и
  тип; ответ кодируется один раз — `events.Ready`; больше `FRAME_MAX` — `too_large`, не кодируется — `error`),
  `close_session`, `new_link` (все сессии прочь и поколение канала: `open`, чья проверка PIN пережила новый канал, —
  `closed`), `emit` (в `send`, его ставит `Link`; нет — событие пропадает). `Runner` создаётся раньше и получает `emit`
  поздним связыванием (`lambda f: agent.emit(f)`).
- `enroll` — `enroll(url, login, password, name)` → `(agent_id, device_key, login)`: `POST <адрес>/api/agent/enroll` JSON
  через `urllib`, без `Origin`, перенаправлениям не следует; адрес — по правилу `link.ws_url` (`base_url`); логин — из
  ответа, у прежней студии (поля нет) — введённый `strip().lower()`; отказы — `EnrollError(code, words)`: 401 `login`,
  429 `locked`, 422 с `detail.code == "name"` — `name`, прочее — `unavailable` «Студия недоступна (HTTP n | тип
  ошибки)»; неверный или усечённый ответ HTTP (`http.client.HTTPException`: `BadStatusLine`, `IncompleteRead`,
  `LineTooLong` — urllib их не оборачивает) — тоже `unavailable`. `unenroll(url, key, timeout=15)` → `(код HTTP,
  забыта)`: `DELETE` того же пути, ключ — `Authorization: Bearer`, без тела и `Origin`, без перенаправлений; забыта —
  204 и 401 с `detail.code == "key"`; прочий 401 (`login` от `guard` прежней студии), 403, 405, 5xx — нет (слова
  `KEPT`); сеть и адрес — `EnrollError`.
- `setup` — `validate(form, enrolled=)` (слова окна; не привязан — логин и пароль обязательны, привязан — оба или ни
  одного; имя как у студии — 1–64 без управляющих; папка есть; PIN — `pin_errors`) и `apply(h, form)`: с логином —
  привязка, `save_key`, `agent_id`, `name`, `login`; всегда — `url`, `projects_root`, `autostart`; PIN — `no_pin` убирает,
  заполненный задаёт, пустой не трогает. Пароль не сохраняется нигде.
- `single` — `acquire()`: мьютекс `Local\StudioAgent`, уже есть — `None` (второй мост выходит).
- `autostart` — `HKCU\…\Run` значение `StudioAgent` = `command()` (`.venv\Scripts\pythonw.exe` + `studio_agent.pyw`
  репозитория), `enabled` (значение есть и равно `command()` этой копии без учёта регистра; другая копия — `False`),
  `set_enabled`; `key` — параметр (тесты пишут в `Software\studio-agent-test\Run`).
- `app.Controller` — ядро без Tk и трея (его же берёт `run --console`): поток с циклом `asyncio` — `find_cli`,
  `cli_version`, `Runner` и `Agent` (поздний `emit`), `cli.probe` до первого канала и раз в `PROBE_EVERY_SEC` (пустой
  итог прежние модели не стирает; набор сменился — канал заново, новый `hello`, кроме `setup`/`revoked`/`replaced`), `Link`. Публичные методы — из любого потока:
  `restart_link()` (канал заново с перечитанными ключом, настройками и `has_pin`; состояния прежнего канала отсекает
  поколение `_gen`), `apply_setup(form)` (блокирует — не из потока Tk; «привязан» — есть ключ **для этого адреса**:
  другой адрес требует новой привязки, ключ чужой студии туда не уходит; с логином — `fresh`: первый `online` —
  уведомление трея), `unenroll(*, ask_studio=True) -> (стёрто, words)` («Отключиться», блокирует: под замком
  `_relink` — `runner.close(hold=True)` (не `final`: `done` уходит в канал; новых ходов нет до `reopen`),
  `_drop_link()`, затем `enroll.unenroll` (любой сбой — «не забыта») и всегда стирание: `key.bin` первым, затем
  `pin.bin`, `agent_id`, `login` — адрес, имя, папка, автозапуск остаются; `PermissionError` (файл читает другой поток:
  окно, трей) — повтор `ERASE_TRIES` (10) раз через `ERASE_PAUSE_SEC` (50 мс, пауза — параметр `sleep`); стёрто —
  `setup` и слова `""` или `KEPT`; не стёрто — `(False, ERASE_FAILED)`, строка журнала с типом и канал заново
  (`_relink`: ключ остался — `connecting`, ключа уже нет — `setup`; сбой отвязки не оставляет «на связи» без канала);
  «Войти заново» после отзыва — `ask_studio=False`), `set_pin(pin | None)`, `set_folder(path) -> [слова]`,
  `set_autostart(on)` (блокируют; папка и PIN — канал заново, кроме `revoked`/`replaced`: заменённый отнял бы ключ),
  `has_pin`, `pin_status()` (`none`/`set`/`locked`), `login`, `autostart_enabled()` (реестр через `autostart.enabled`,
  не `config.autostart`), `reset_pin()`, `quit()` (`runner.close(final=True)`, затем `link.stop()`); `on_pin_changed`
  — `set_pin`, `reset_pin` и блок PIN из студии (`Agent.on_pin_locked`); правки `config.json` — под `_cfg_lock`
  (`welcome` пишет `login` и `name` в потоке, только своего поколения: отвязка не вернёт стёртый логин; логин
  сменился — после записи подписчикам `on_login(callback)`), `state` + `on_state`: `setup` (нет ключа или папки — канал не поднимается), `connecting`,
  `online`, `offline`, `revoked`, `replaced` (оба — до новой настройки), слова — `WORDS`. Зависимости (Link, SDK, CLI,
  probe, привязка, автозапуск, `sleep`) — параметры: тесты `tests/test_app.py` без сети и реального времени.
  `_refresh_cli` (перед каждым каналом `_relink` и перед часовой пробой) — `cli.current` и, если путь сменился, версия
  заново в `agent.cc_version` (студия видит её в следующем `hello`). Канал кончился `revoked`/`replaced` текущего
  поколения — `runner.close(hold=True)` (ходы прерваны, карточкам отказ, `claude.exe` гаснет; новые — `busy` до
  нового канала) и `agent.open.clear()`.
  `prepare_env()` — `cli.drop_session_env` до `Controller` (зовут `app.main` и `run --console`).
- `__main__` — `python -m studio_agent [run] [--console] | enroll | set-pin | clear-pin | reset-pin | status`: пароль
  и PIN — `getpass`; `run --console` — мьютекс, журнал ещё и в stderr, выход на `setup`/`revoked`/`replaced` и Ctrl+C.
- `journal.setup` ставит логгеру `websockets` уровень не ниже INFO: на DEBUG он пишет заголовок `Authorization`.
- `app.main` — мост с треем: `single.acquire` (занят — окно «Мост уже запущен», выход), `journal.setup`; главный поток —
  скрытый корень Tk (`window.prepare`: тема и значок окон) и опрос очереди команд `root.after(100, …)` (меню трея и
  состояния ядра — только через очередь: `("state", s)` — трею, окну `window.update` и уведомлению `tray.notice`;
  `("login", l)` — подсказке трея и строке «Вход» окна `window.login_changed`; `pin` — меню трея и строке PIN окна);
  не подключён — окно сразу; «Снять блок PIN» трея — в своём потоке (DPAPI); «Выход» — трей прочь,
  `Controller.quit()`, конец `mainloop`. Tk и pystray импортируются только здесь — `Controller` и консоль без них.
- `theme` — палитры студии `PAPER` / `GRAPHITE` (токены спеки §5.3 дословно, плюс свои `warn`, `field`, `edge`),
  `palette(dark)`, `is_dark()` (`AppsUseLightTheme`), `dark_title(win, dark)` (`DwmSetWindowAttribute` 20),
  `apply(root, dark=None)` — стили ttk на `clam` (`Accent.`, `Danger.` кнопки, `Card.*`, `Muted.`, `Title.`,
  `Status.`, `Error.`, `Badge.`), шрифт Segoe UI Variable Text (нет — Segoe UI).
- `tray` — рисунок `icons/icon.svg` студии Pillow-ом в 512 px (дуги и кривые — многоугольником) → `icon(size)` (окно) и
  `image(state)` (64 px, точка `COLORS` в правом нижнем углу с белой обводкой: зелёная `online`, серая
  `connecting`/`setup`, жёлтая `offline`, красная `revoked`/`replaced`), `title(state, login)` («Studio Agent — на
  связи · ivanov», ≤ 127), `status_line` («● На связи — ivanov» — первая неактивная строка меню), `notice(state,
  fresh)`; `Tray` (pystray в своём потоке): «Открыть Studio Agent» (по умолчанию), «Снять блок PIN», «Открыть
  журнал», «Выход»; `refresh` — `update_menu` в своём потоке (pystray на Windows строит меню заранее), `notify`.
  Блок PIN проверкой из студии трей узнаёт от `Agent.on_pin_locked` → `Controller.on_pin_changed` → команда `pin`
  очереди Tk (меню и окно заново): смены состояния канала при этом нет; так же — «Снять блок» и PIN из окна.
- `window` — `open_main(root, controller)` (`open_setup` — синоним): одно окно «Studio Agent», вид по `view_of(enrolled)`
  — «Вход» (форма, «Подключить» → `apply_setup`) или «Подключение» (карточка `card_of(state)` → слова, токен цвета
  точки, приглушённая строка, кнопка `MAIN_BUTTONS`: «Отключиться» с подтверждением, «Войти заново» только при
  `revoked`, при `replaced` — «Подключить снова» = `restart_link()` и строка «Подключить снова — этот компьютер заберёт
  канал у другого моста»: ничего не стирается — второй мост той же учётки Windows в другом сеансе живёт с тем же
  `key.bin` и `pin.bin`; затем «Студия», «Вход», «Компьютер»; карточка «Настройки» — папка, автозапуск — галочка по
  `autostart_enabled()`, строка PIN `pin_row`); «привязан» — состояние ядра не `setup` (DPAPI окно не читает). Смена
  состояния — `state_action(вид, state, busy)`: вид сменился — окно заново, тот же — карточка на месте (`show_card`),
  окно занято (`busy`: операция `work` или открытый диалог; кнопки вида гаснут) — ждёт и применяет последнее после;
  строка PIN и папка тоже меняются на месте — ошибки «Настроек» остаются видны. `PinDialog` и `confirm` — модальные
  `Toplevel` той же темы. Блокирующее — `MainWindow.work` (свой поток, итог — `root.after`); пароль и PIN стираются из
  полей сразу после нажатия; слова отказа студии при отвязке — над формой входа, «не стёрто» — в карточке.
- `studio_agent.pyw` (корень репозитория) — запуск без консоли: `.venv\Scripts\pythonw.exe studio_agent.pyw`.
- `install.ps1` (UTF-8 с BOM, CRLF) — рядом нет `.git` (архив студии) — копия в `%LOCALAPPDATA%\Programs\studio-agent`:
  мост этой копии (`python.exe`/`pythonw.exe` с её `studio_agent.pyw` в командной строке — и лаунчер `.venv`, и базовый
  Python) останавливается, файлы архива — поверх, кроме `.venv`, `Unblock-File` («Mark of the Web»); из git-копии — на
  месте. Затем `.venv` (`-Python`, иначе `py -3`, нет `py` — `python`; проверка ≥ 3.11 до `venv` с понятной ошибкой),
  `pip install -r requirements.txt`, запуск `pythonw.exe studio_agent.pyw` без ожидания; `-NoVenv`, `-NoStart`,
  `-RunKey` (ключ автозапуска, тесты — `HKCU:\Software\studio-agent-test\Run`) — для тестов; повтор безопасен;
  автозапуск ставит окно (`autostart.REPO` — та копия, что запущена), а установка в `Programs` значение `StudioAgent`,
  если оно есть, переписывает на эту копию (как `autostart.command()`), нет — не заводит.
- `install.cmd` (ASCII, CRLF) — двойной щелчок: `install.ps1` с `-ExecutionPolicy Bypass`, код не 0 — `pause`.
- Архив моста собирает выкладка студии (`git archive` HEAD): `.gitattributes` `export-ignore` — тесты, `.claude/`,
  `CLAUDE.md`, `AGENTS.md`, `FINDINGS*`, `IDEAS*`, `pytest.ini`, `requirements-dev.txt`, `.gitignore`, `.gitattributes`;
  `*.cmd text eol=crlf` — последней строкой.

## Решения

Плана (`Решения плана`) и контролёра (`progress.md` плана, «Rulings»), где они задают поведение:

- **Клиент SDK — на ход** (спека §6.6): поднимается в `Runner._run` и гаснет в `_finish`; новая сессия —
  `session_id=chat`, существующая — `resume=chat`; модель и усилие — опции запуска (`set_model` и перезапуска при смене
  усилия нет). «Новая» — `sessions.exists`: `get_session_info(...) is not None` **или** есть хоть одна запись
  `get_session_messages(limit=1)` — SDK отдаёт `None` и сессии без извлекаемого названия (Ruling 9), а `session_id`
  существующей сессии CLI не примет.
- `null` модели — `choices.json` чата, иначе модель последнего ответа сессии (без `<synthetic>`: продолжение сессии VS
  Code не меняет её модель), иначе умолчание Claude Code; `null` усилия — запомненное или умолчание.
- **`setting_sources=["user", "project", "local"]`** явно, `permission_mode` не задаётся: правила разрешений, CLAUDE.md
  и `env` проекта действуют, как в VS Code. Окружение `claude.exe` — окружение моста: при старте из него убраны только
  переменные сессии того, кто запустил мост (`cli.SESSION_ENV`, `CLAUDE_CODE_MESSAGING_*`, `VSCODE_*`,
  `CODEX_COMPANION_*`, `TERM_PROGRAM=vscode`, `GIT_ASKPASS`/`GIT_EDITOR` из папки VS Code; `install.ps1` запускают и из
  Claude Code), прочее (`ANTHROPIC_*`, настройки `CLAUDE_CODE_*`) —
  как есть; добавляется только `CLAUDE_AGENT_SDK_CLIENT_APP=studio-agent/<версия>` — и в ходе, и в `cli.probe` (Ruling 10).
- CLI — новейший `claude.exe` расширения VS Code, нет — встроенный в SDK; путь сверяется перед каждым ходом и пробой
  (VS Code ставит обновление в новую папку и удаляет прежнюю — без этого мост после первого обновления отвечал бы
  «Claude Code не найден на компьютере» до перезапуска). Модели — `get_server_info()["models"]`
  короткого клиента до первого канала и раз в час; новые студия видит только в следующем `hello` (обновить знакомство
  протокол v1 не умеет), поэтому сменившийся набор моделей или усилий поднимает канал заново.
- Текст хода — по `text_delta` (`include_partial_messages=True`), без дельт — блоками `AssistantMessage`; шаги и итоги —
  только главного агента, карточки подагентов — показываются. Метка `studio` — новой сессии после `disconnect` (файл
  сессии не пишут двое) и только если запрос ушёл к CLI. Время сообщений истории — `null`.
- `open` — порядок Ruling 4 (как `FakeMachine`): PIN не задан — открыть; `null` открытой сессии — `{}` и продление;
  блок — `pin_locked` (раньше `pin_needed`: иначе страница покажет панель PIN вместо «заблокирован»); `null` —
  `pin_needed`; иначе проверка.
- `Runner` получает `emit` поздним связыванием `lambda f: agent.emit(f)` (Ruling 5) — сборка в `app.Controller` и в
  фикстурах тестов; `stop` без хода — `Refused("idle")` (Ruling 6).
- `sessions.fit`: одиночный элемент больше бюджета и после отбрасывания шагов — `text` урезается до хвоста с первой
  строкой «… начало не показано» (Ruling 7, 10; элемент бывает и вопросом).
- `run --console` собирает ядро тем же `app.Controller`, что и трей (Ruling 11): сборка Agent/Runner/Link одна.
- Логгер `websockets` — не ниже INFO и в консоли (Ruling 13, `journal.setup`).

## Правила

- Только Windows. Ключ устройства и PIN — DPAPI области пользователя; пароль студии на машине не остаётся и в argv не
  передаётся (только окно или `getpass`).
- Протокол v1, значения дословно: кадр ≤ 16 МиБ; первый кадр — `hello` с `"v": 1`; `welcome` ждать ≤ 10 с; закрытия
  4401 (отозван — не переподключаться), 4409 (заменён — не переподключаться), 4400 (пауза 60 с и снова),
  1012/1001/обрыв/отказ рукопожатия — переподключение 1, 2, 4, 8, 16, 32, 60 с; `id` просьбы — 16 hex, `session` —
  32 hex, `project` — 16 hex, `chat` — UUID строчными 8-4-4-4-12; `message` отказа ≤ 300 знаков; студия ждёт ответа 30 с.
- PIN — 4–8 цифр ASCII, scrypt (`n=2**14, r=8, p=1`, соль 16 байт, `dklen=32`); 5 неверных подряд — `pin_locked` до
  снятия блока на машине; счёт — на диске, попытка считается до scrypt. Открытые сессии — только в памяти, срок
  простоя 1800 с; пропадают при `close`, перезапуске моста и **любом** новом канале.
- Пределы: текст файла 2 МиБ, `raw` 10 МиБ, путь ≤ 1024 знака, `history.limit` 1–100, строки событий и шагов — до
  20 000 знаков с `…` в конце, страница истории и снимок — ≤ 8 МиБ, файлов в списке ≤ 5000, чатов ≤ 200, файлов git
  ≤ 2000, одновременных ходов — 2.
- `pid` = первые 16 знаков hex sha256 имени папки (UTF-8).
- Кадры наружу — `json.dumps(frame, ensure_ascii=True, allow_nan=False)` (имена файлов Windows бывают с одинокими
  суррогатами).
- **Журнал** `%LOCALAPPDATA%\studio-agent\agent.log` — только id, коды, числа, имена типов исключений: ни ключа, ни
  PIN, ни пароля, ни имён проектов, файлов, путей, ни текстов, ни stderr Claude Code.
- Блокирующее (файлы, scrypt, сессии SDK, git) — `asyncio.to_thread`: цикл держит пинги канала.
- Автозапуск — значение `StudioAgent` в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; Task Scheduler не трогать.
- Кодировки: `.py` — UTF-8 без BOM, LF; `.ps1` — UTF-8 **с BOM**, CRLF (`.gitattributes`).
- Комментарии, докстринги и тексты — по-русски, как в студии; слова отказов и интерфейса — дословно из «Тексты» плана.
- Коммит — сообщение через файл (`git commit -F <файл>`), в конце `Co-Authored-By: Claude Opus 5.5
  <noreply@anthropic.com>`.

## Тесты

    "/c/Program Files/Python314/python" -m venv .venv
    .venv/Scripts/python -m pip install -r requirements-dev.txt
    .venv/Scripts/python -m pytest -q                       # быстрый набор, ≤ 60 с
    .venv/Scripts/python -m pytest -q -m integration        # настоящий git, реестр, PowerShell, установка
    .venv/Scripts/python -m pytest -q -m manual             # настоящий Claude Code, тратит квоту

`timeout = 30`, `asyncio_mode = auto`; pytest-xdist нет — набор идёт одним процессом (~7 с). Реального времени в
тестах нет: часы — параметр `now`, паузы — параметр `sleep`. Каталог моста в тестах — фикстура `home`
(`tests/conftest.py`, `STUDIO_AGENT_HOME` во временной папке). Студия в быстрых тестах — подставная
(`tests/fake_studio.py`, `websockets` на `127.0.0.1:0`), SDK — `tests/fake_sdk.py`. Окно и трей — без Tk: чистые
функции `window`, `tray`, `theme` (`test_window_logic.py`, `test_tray.py`, `test_theme.py`); снимки вида — вручную.
Установка — `tests/test_install.py` (`integration`): «архив» — рабочее дерево без `export-ignore` в папке без `.git`,
`LOCALAPPDATA` временная, `-NoVenv -NoStart` (сети нет), `-RunKey` — тестовый ключ (настоящий `Run` тесты не трогают);
работающий мост подменяет `python.exe` с путём копии.

Сквозная проверка — `tests/test_live_studio.py` (`manual`, **четыре коротких хода квоты** — не гонять по
кругу): `tests/live_studio_server.py` поднимает живую студию её средой (`<STUDIO_REPO>/.venv`, по умолчанию
`C:/work/projects/studio`; `start_live_app`, `READY <адрес>`, гаснет по закрытию stdin), мост — настоящие `Agent`,
`Runner` (SDK и `claude.exe`) и `Link` в цикле теста, браузер — `urllib` в потоке. Сценарий: привязка, «на связи»,
`open`, проекты, новый чат, ход «Ответь одним словом: готово», ход с Bash — карточка `permission`, `allow`, итог шага;
чаты (`origin: "studio"`), история из 4 сообщений, файлы, чтение, git; ход с Bash — карточка, «Остановить» (настоящий
`interrupt()`) — `done` `stopped`; следующий ход того же чата (не `running`) — вопрос `AskUserQuestion`, `answers`,
в ответе выбранный вариант; PIN на машине и новый канал — `pin_needed`, с PIN — 204. Окружение моста — как у
`app.prepare_env` (тест идёт и из Claude Code). Файл сессии пробы тест убирает из `~/.claude/projects`. Перед ним — отладить всё без хода.

## Ловушки

- **Ключ устройства — только заголовком** `Authorization: Bearer` (рукопожатие WebSocket и привязка — телом POST):
  строку запроса пишет журнал Apache. Логгер `websockets` на DEBUG пишет заголовки — с ключом; `journal.setup` держит
  его не ниже INFO.
- **PIN проверяет мост, не студия**: хеш и счёт — в `pin.bin` машины; студия знает только `has_pin` из `hello`. Значит,
  `set-pin`/`clear-pin` из консоли при работающем мосте действуют на новые `open` сразу, уже открытые сессии — до
  нового канала, а `has_pin` студии — до перезапуска моста или `restart_link` (переподключение самого `Link` берёт
  `has_pin` из памяти; окно — `Controller.set_pin` — делает `restart_link` само).
- **Кадры — ASCII-JSON** (`ensure_ascii=True, allow_nan=False`): имя файла Windows с одиноким суррогатом в UTF-8 не
  кодируется — канал упал бы на первом таком списке.
- **`setting_sources` задавать явно**: без него SDK не грузит настройки — ни правил разрешений проекта, ни CLAUDE.md,
  ни `env` `claude-switch`. Режим разрешений — из настроек: у владельца в `~/.claude/settings.json` `defaultMode:
  "auto"` и `allow: ["Bash", …]`, поэтому карточка приходит только на то, что Claude Code спросил бы и в VS Code
  (живой тест кладёт проекту `defaultMode: "default"` и `ask` — проектная настройка сильнее пользовательской).
- **Клиент на ход, не на чат**: держать клиентов открытыми — процесс `claude.exe` на каждый открытый чат. `can_use_tool`
  требует потокового ввода и холостого хука `PreToolUse` (`turns._keep_alive`) — без него SDK закрывает ввод, пока
  карточка ждёт ответа.
- stderr `claude.exe` отбрасывается (`cli._drop`): в нём пути и тексты.
- Сессии Claude Code пишутся в настоящий `~/.claude/projects` (по пути проекта) — у тестов с настоящим CLI тоже.
