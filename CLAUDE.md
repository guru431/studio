# CLAUDE.md — studio-agent

Windows-мост к студии `https://studio.example.com`: держит исходящий канал WebSocket к студии и отдаёт ей
локальный Claude Code владельца — проекты, чаты, ход с карточками разрешений, файлы, git — строго по
протоколу версии 1. Сервер (студия) уже сделан, мост строится по плану.

- Спека: `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-claude-code-design.md` (§2, §4, §6.3,
  §6.6, §6.10, §7, §8).
- Протокол (контракт с сервером): `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`.
- План: `C:/work/projects/studio/docs/superpowers/plans/2026-10-28-studio-agent-bridge.md`.
- Сервер — только читать: `C:/work/projects/studio/app/bridge.py`, `app/claude_code.py`; эталон поведения машины —
  `C:/work/projects/studio/tests/fake_bridge.py` (`FakeMachine`).

Репозиторий — **приватный**, ветка `main`; push — только по слову владельца.

## Устройство

Пакет `studio_agent` (дальше дополняется по плану):

- `limits` — все числа протокола и моста (`FRAME_MAX`, `PIN_TRIES`, `OPEN_IDLE_SEC`, `TEXT_MAX`, `HISTORY_BUDGET`, …).
- `errors` — `Refused(code, message="", **extra)`: отказ просьбы; без слов — `WORDS[code]` («Тексты» плана); у
  `not_found` и `too_large` слова разные по месту — их передаёт вызывающий.
- `dpapi` — `protect`/`unprotect` (DPAPI области пользователя, `CRYPTPROTECT_UI_FORBIDDEN`, энтропия
  `b"studio-agent\0v1"`); чужой или битый блоб — `OSError`.
- `store` — каталог моста `home()` (`STUDIO_AGENT_HOME`, иначе `%LOCALAPPDATA%\studio-agent`), `Config` в
  `config.json`, ключ устройства в `key.bin` под DPAPI (не читается — `None`), `write_atomic`.
- `journal` — логгер `studio_agent` в `agent.log` (1 МиБ × 3, UTF-8).
- `pin` — `PinStore` (`pin.bin` под DPAPI: scrypt-хеш и счёт неверных; попытка пишется на диск до scrypt, проверки
  идут по одной — замок процесса; 5 подряд — `pin_locked` до `reset` на машине; нет файла — PIN не задан, блоб не
  читается — `OSError`) и `OpenSessions` (открытые сессии студии в памяти, срок простоя `OPEN_IDLE_SEC`).
- `projects` — `pid`, `list_projects` (подпапки корня без `.`-имён, скрытых, системных и ведущих наружу), `find_project`
  (настоящий путь или `not_found`).
- `files` — `resolve` (вид пути проверяется **до диска**: имена устройств, ADS `:`, точка или пробел в конце части —
  `invalid`; настоящий путь вне проекта — `outside`), `list_dir`, `read_text`, `read_raw` (base64), `save_text`
  (`version` — sha256 байтов, запись через `store.write_atomic`).
- `gitinfo` — `parse` (porcelain v2 `-z`), `status` (`git -C`, без окна и необязательных замков, 25 с; код ≠ 0 или нет
  git — `not_git`, срок вышел — `TimeoutExpired` наружу).
- `cli` — `find_cli` (`claude.exe` новейшего расширения VS Code, версии числами; нет — CLI, встроенный в SDK),
  `cli_version` (первое слово `--version`, 30 с; сбой — «встроенный в SDK»), `models_from` (`get_server_info()["models"]`
  без `default` → `{"id", "label"}` и усилия в порядке `EFFORTS`), `probe` (короткий клиент в папке проектов,
  `setting_sources` user/project/local, ≤ 60 с, `disconnect` и при сбое; stderr Claude Code отбрасывается).
- `events` — `clip` (строки на любой глубине — до `CLIP` знаков с «…»), `text_of` (текст блоков содержимого, картинка —
  «[изображение]»).
- `sessions` — функции SDK атрибутами модуля (тесты подменяют): `chats` (сессии проекта без worktree, только `chat`-UUID,
  мс → с, новые сверху, `running`/`asking` из ходов моста), `normalize` (главный агент; ответы до следующего вопроса —
  одно сообщение, итоги инструментов — к шагам), `fit` (хвост ≤ `HISTORY_BUDGET`; одиночный огромный — строки шагов до
  2000, шаги с начала прочь «… первые шаги не показаны», затем хвост текста «… начало ответа не показано»), `history`
  (нет записей — `not_found` «нет такого чата»), `exists`, `last_model` (без `<synthetic>`), `mark_studio` (метка
  `studio`, сбой — в журнал).
- `turns` — `Runner` (ходы через Agent SDK): клиент на ход (новая сессия — `session_id=chat`, есть — `resume=chat`;
  `null` модели и усилия — запомненные `store.Choices`, иначе модель последнего ответа сессии), не больше `MAX_TURNS`
  сразу и один в чате (`busy`, `running`); `can_use_tool` → событие `ask` и ожидание `answer` (карточка переживает
  смену канала), `watch` — снимок `snapshot` (урезан `sessions.fit`), `stop` — отказ карточкам и `interrupt()`
  (`idle` без хода), `close` — мост выходит (`done` «ход прерван: мост остановлен»); метка `studio` новой сессии — после
  `disconnect`.
- `link` — `ws_url` (`https://h` → `wss://h/api/agent/link`; `http` — только `localhost`, `127.0.0.1`, `[::1]`) и `Link`
  (канал к студии): `connect` с `Authorization: Bearer <ключ>`, `max_size=FRAME_MAX`, пинги 20/20 с; первым — `hello`,
  `welcome` ≤ `WELCOME_SEC`, затем `agent.new_link()` и `agent.send = link.send`; `req` — `agent.handle` своей задачей,
  ответы подряд в очередь канала, по которому пришла просьба (оборвался — пропадают; задачи не отменяются); `close` —
  `agent.close_session`; не JSON и прочие `t` — пропуск. Писатель — одна задача на канал, кадры — `ensure_ascii=True,
  allow_nan=False`. Закрытие 4401 — `revoked`, 4409 — `replaced` (выход из `run()`), 4400 — пауза `PROTOCOL_PAUSE_SEC`,
  прочее и отказ рукопожатия — `BACKOFF` по числу неудач подряд (сброс после `welcome`); `on_state`: `connecting`,
  `online`, `offline` (перед каждой паузой), `revoked`, `replaced`, `stopped`. Паузы — параметр `sleep`; тесты —
  подставная студия `tests/fake_studio.py` (`websockets` на `127.0.0.1:0`).
- `agent` — `Agent` (просьбы студии): `hello`, `handle` (кадр `req` → `[res, *после]`; поля — по `OPS` до
  исполнения, иначе `invalid`; `id` не вернуть — без ответа; `open` под замком — порядок Ruling 4: без PIN — открыть,
  `null` открытой — `{}`, блок — `pin_locked`, `null` — `pin_needed`, иначе `pins.check` в потоке; прочие — `touch`
  или `closed`; `watch` без хода — `{"running": false}` без проверки чата; не `Refused` — `error`, в журнал `op` и
  тип; ответ больше `FRAME_MAX` — `too_large`, не кодируется — `error`), `close_session`, `new_link` (все сессии
  прочь), `emit` (в `send`, его ставит `Link`; нет — событие пропадает). `Runner` создаётся раньше и получает `emit`
  поздним связыванием (`lambda f: agent.emit(f)`).
- `enroll` — `enroll(url, login, password, name)` → `(agent_id, device_key)`: `POST <адрес>/api/agent/enroll` JSON
  через `urllib`, без `Origin`, перенаправлениям не следует; адрес — по правилу `link.ws_url` (`base_url`); отказы —
  `EnrollError(code, words)`: 401 `login`, 429 `locked`, 422 с `detail.code == "name"` — `name`, прочее —
  `unavailable` «Студия недоступна (HTTP n | тип ошибки)».
- `setup` — `validate(form, enrolled=)` (слова окна; не привязан — логин и пароль обязательны, привязан — оба или ни
  одного; имя как у студии — 1–64 без управляющих; папка есть; PIN — `pin_errors`) и `apply(h, form)`: с логином —
  привязка, `save_key`, `agent_id`, `name`; всегда — `url`, `projects_root`, `autostart`; PIN — `no_pin` убирает,
  заполненный задаёт, пустой не трогает. Пароль не сохраняется нигде.
- `single` — `acquire()`: мьютекс `Local\StudioAgent`, уже есть — `None` (второй мост выходит).
- `autostart` — `HKCU\…\Run` значение `StudioAgent` = `command()` (`.venv\Scripts\pythonw.exe` + `studio_agent.pyw`
  репозитория), `enabled`, `set_enabled`; `key` — параметр (тесты пишут в `Software\studio-agent-test\Run`).
- `app.Controller` — ядро без Tk и трея (его же берёт `run --console`): поток с циклом `asyncio` — `find_cli`,
  `cli_version`, `Runner` и `Agent` (поздний `emit`), `cli.probe` до первого канала и раз в `PROBE_EVERY_SEC` (пустой
  итог прежние модели не стирает, новые уйдут в следующий `hello`), `Link`. Публичные методы — из любого потока:
  `restart_link()` (канал заново с перечитанными ключом, настройками и `has_pin`; состояния прежнего канала отсекает
  поколение `_gen`), `apply_setup(form)` (блокирует — не из потока Tk; «привязан» — есть ключ **для этого адреса**:
  другой адрес требует новой привязки, ключ чужой студии туда не уходит), `reset_pin()`, `quit()` (`runner.close()`,
  затем `link.stop()`), `state` + `on_state`: `setup` (нет ключа или папки — канал не поднимается), `connecting`,
  `online`, `offline`, `revoked`, `replaced` (оба — до новой настройки), слова — `WORDS`. Зависимости (Link, SDK, CLI,
  probe, привязка, автозапуск, `sleep`) — параметры: тесты `tests/test_app.py` без сети и реального времени.
- `__main__` — `python -m studio_agent [run] [--console] | enroll | set-pin | clear-pin | reset-pin | status`: пароль
  и PIN — `getpass`; `run --console` — мьютекс, журнал ещё и в stderr, выход на `setup`/`revoked`/`replaced` и Ctrl+C.
- `journal.setup` ставит логгеру `websockets` уровень не ниже INFO: на DEBUG он пишет заголовок `Authorization`.
- `app.main` — мост с треем: `single.acquire` (занят — окно «Мост уже запущен», выход), `journal.setup`; главный поток —
  скрытый корень Tk и опрос очереди команд `root.after(100, …)` (меню трея и состояния ядра — только через очередь);
  не настроен — окно настройки сразу; «Выход» — трей прочь, `Controller.quit()`, конец `mainloop`. Tk и pystray
  импортируются только здесь — `Controller` и консоль без них.
- `tray` — `Tray` (pystray в своём потоке): круг Pillow — зелёный `online`, серый `connecting`/`offline`/`setup`,
  красный `revoked`/`replaced` (`COLORS`, `image`), подсказка «Studio Agent — <состояние>» (`title`), меню «Настройка…»
  (по умолчанию — щелчок по значку), «Снять блок PIN» (активен при блоке), «Открыть журнал», «Выход».
- `window` — `open_setup(root, controller)`: `Toplevel` «Studio Agent — настройка» (одно окно — повтор поднимает
  открытое), поля и подписи «Текстов» плана; «Привязать» / «Сохранить», ошибки под формой; `apply_setup` — в своём
  потоке (привязка до 30 с), окно ждёт итог `root.after`; пароль стирается из поля сразу после отправки.
- `studio_agent.pyw` (корень репозитория) — запуск без консоли: `.venv\Scripts\pythonw.exe studio_agent.pyw`.
- `install.ps1` (UTF-8 с BOM, CRLF) — `.venv` рядом (`-Python`, иначе `py -3.14`, нет `py` — `python`), `pip install -r
  requirements.txt`, запуск `pythonw.exe studio_agent.pyw` без ожидания; повтор безопасен; автозапуск ставит окно.

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
    .venv/Scripts/python -m pytest -q -m integration        # настоящий git, реестр, PowerShell
    .venv/Scripts/python -m pytest -q -m manual             # настоящий Claude Code, тратит квоту

`timeout = 30`, `asyncio_mode = auto`. Реального времени в тестах нет: часы — параметр `now`, паузы — параметр
`sleep`. Каталог моста в тестах — фикстура `home` (`tests/conftest.py`, `STUDIO_AGENT_HOME` во временной папке).
