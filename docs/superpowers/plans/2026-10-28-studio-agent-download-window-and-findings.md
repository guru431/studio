# Мост: скачивание, окно и трей, отвязка + разбор находок — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** мост скачивается из студии и ставится двойным щелчком; окно и трей моста — в стиле студии и ясно
говорят «на связи · логин», «Отключиться» отвязывает компьютер; все открытые записи `FINDINGS.md` студии и моста
закрыты.

**Architecture:** студия отдаёт ZIP моста, собранный выкладкой (`GET /api/agent/download`), и учится отвязке
мостом (`DELETE /api/agent/enroll`) и логину в `welcome`; мост получает модуль темы, новое окно (вход /
подключение), трей со значком студии и точкой состояния. Находки — по модулям, каждая задача владеет своими
файлами.

**Tech Stack:** студия — FastAPI, SQLite, ES-модули без сборки, pytest (+ xdist, браузерные сценарии CDP); мост —
Python 3.11+, Tk/ttk, pystray, Pillow, websockets, claude-agent-sdk, PowerShell 5.1.

**Spec:** [docs/superpowers/specs/2026-10-28-studio-agent-download-and-window-design.md](../specs/2026-10-28-studio-agent-download-and-window-design.md);
находки — `FINDINGS.md` студии (35 записей) и `C:/work/projects/studio-agent/FINDINGS.md` (10 записей): каждая
задача ниже перечисляет свои по заголовку; текст и место — в самом файле.

## Global Constraints

- Правила CLAUDE.md обоих проектов: приватность журнала (ни логина, ни имён компьютеров, путей, текстов),
  кодировки (`.ps1` — UTF-8 с BOM и CRLF; `.cmd` — ASCII и CRLF; `deploy/*` и `.py` — LF), комментарии и тексты —
  по-русски, слова интерфейса — дословно из спеки.
- Протокол остаётся версии 1: только добавки (`login` в ответе привязки и в `welcome`, `DELETE /api/agent/enroll`).
- Схема базы студии не меняется (шагов `db.STEPS` нет).
- Закрытая находка удаляется из `FINDINGS.md` в том же коммите, что её исправление; отклонённая — в
  `FINDINGS-archive.md` по правилу закрытия (сначала архив, потом удаление).
- Быстрый набор студии ≤ 60 с, без настоящего времени; браузерные сценарии — `integration`. Мост — быстрый
  набор одним процессом, Tk в быстром наборе не поднимается.
- Коммит — сообщение файлом (`git commit -F`), в конце `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  Push и выкладка — только задачей 10.

## Review Focus

1. Отвязка при оборванной сети или старой студии (405) — ключ и PIN всё равно стёрты, окно — «Вход» со словами
   «компьютер остался в списке студии» (задача 8, `test_unenroll_without_studio_still_forgets`).
2. Отвязка во время идущего хода — ход получает `done` «ход прерван: мост остановлен» до закрытия канала, канал
   не переподключается после 4401 своей же отвязки (задача 8, `test_unenroll_stops_turns_and_link_first`).
3. Архив моста отсутствует на хосте (выкладка с машины без `studio-agent`) — ссылка отвечает 404 `no_agent`
   словами, страница не падает (задача 1, `test_download_without_archive_is_404`; выкладка без репозитория моста —
   отказ deploy.sh до копирования кода, `test_deploy`).
4. Тёмная тема Windows и масштаб 150 % — окно читается (контраст токенов студии), ничего не обрезано
   (задача 8: `theme.palette(dark=True)` — пары текст/фон из спеки; ручной осмотр в задаче 10).
5. Повторная установка поверх работающего моста из архива — мост останавливается, файлы обновляются, `.venv`
   остаётся, автозапуск указывает на `%LOCALAPPDATA%\Programs\studio-agent` (задача 8, integration
   `test_install_ps1_installs_into_programs_from_archive`).

---

## Порядок и исполнители

Задачи 1–6 — студия (каждая — свой worktree от свежего `main`, первым шагом `git merge --ff-only main`; среда —
`C:/work/projects/studio/.venv/Scripts/python`); 7–8 — мост (прямо в `C:/work/projects/studio-agent`, по одной).
Волна 1: 1, 3, 5, 7. Волна 2 (после слияния волны 1): 2, 4 (нужна `agentDownload` задачи 3), 6, 8 (нужен
контракт задачи 1). Затем 9 (проверка партии) и 10 (выкладка).

---

### Task 1: Студия — скачивание моста, отвязка мостом, логин в `welcome`; канал моста

**Files:**
- Modify: `app/agents.py`, `app/bridge.py`, `deploy/deploy.sh`, `deploy/install.sh`, `deploy/studio-web.service`,
  `docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`, `CLAUDE.md` (раздел «Устройство», Claude Code),
  `README.md` (раздел «Claude Code»: откуда взять мост)
- Test: `tests/test_agents.py` (или файл, где тесты `enroll` сейчас), `tests/test_bridge.py`, `tests/test_deploy.py`

**Находки:** «закрытие канала ждёт замок отправки», «кадр больше FRAME_MAX рвёт uvicorn кодом 1009, а не 4400»,
«очередь наблюдателя без предела».

**Interfaces:**
- Produces: `GET /api/agent/download` → 200 `application/zip` (`studio-agent.zip`) | 404 `{"code": "no_agent"}`;
  `agents.AGENT_ZIP: Path` (подменяют тесты); `POST /api/agent/enroll` 201 → `{"agent_id", "device_key", "login"}`;
  `DELETE /api/agent/enroll` (заголовок `Authorization: Bearer <ключ>`) → 204 | 401 `{"code": "key"}` | 403
  `{"code": "origin"}`; кадр `welcome` → `{"t": "welcome", "agent_id", "name", "login"}`.

- [ ] **Step 1: тесты (красные)** — `test_download_gives_the_archive` (200, тело = файл `AGENT_ZIP`,
  `content-disposition` с `studio-agent.zip`, `cache-control: private, no-cache`), `test_download_without_archive_is_404`
  (`code == "no_agent"`), `test_download_needs_a_session` (401); `test_enroll_answers_login` (`"login"` в ответе
  — `strip().lower()` введённого); `test_unenroll_by_key_drops_the_computer` (204, строки `agents` нет, событие
  `revoke` с `device == "мост"`, канал `SocketBridge`/`InProcessBridge` закрыт 4401); `test_unenroll_unknown_key_is_401`;
  `test_unenroll_foreign_origin_is_403`; `test_unenroll_works_for_disabled_user`; `test_welcome_carries_login`;
  `test_unenroll_log_has_no_name_or_key` (журнал); `test_deploy`: deploy.sh собирает архив моста из
  `STUDIO_AGENT_REPO` (`git ... archive --format=zip --prefix=studio-agent/ HEAD`), отказ при грязном дереве моста
  или без каталога, install.sh несёт `agent` в `SWAP`; юнит uvicorn — `--ws-max-size` = `FRAME_MAX + 1024`,
  `--ws-ping-interval 20 --ws-ping-timeout 20`.
- [ ] **Step 2: прогон** — `.venv/Scripts/python -m pytest -q -n0 tests/test_agents.py tests/test_bridge.py tests/test_deploy.py` → новые FAIL.
- [ ] **Step 3: код** — по спеке §4.1, §4.3: `download(request)` и `unenroll(request)` в `agents.py`
  (`unenroll` — `security.digest` ключа, `DELETE ... RETURNING id, user_id, name`, `bridge.record(..., "revoke",
  device="мост", ip=...)`, `await bridge.drop(aid)`); `welcome` в `bridge.attach` — `login` учётки (выбрать
  `users.login` вместе с проверкой учётки). Канал: прежний канал при замене и в `drop` закрывать фоном
  (`bridge.TASKS`), `welcome` нового — сразу; `Watch` — очередь с пределом `WATCH_MAX = 1000`, переполнение —
  этому наблюдателю `("offline", None)` и снять его (страница дочитает снимком). deploy/install — по §4.1.
- [ ] **Step 4: прогон** — те же файлы → PASS; `.venv/Scripts/python -m pytest -q` → зелёный.
- [ ] **Step 5: документы** — протокол §1/§2 (добавки, «версия остаётся 1»), CLAUDE.md (download, unenroll,
  login в welcome, `agent/` в выкладке и откате), README (ссылка «Скачать мост» в «Настройках»).
- [ ] **Step 6: commit** — код, тесты, документы, три записи прочь из `FINDINGS.md`.

### Task 2: Студия — сервер раздела Claude Code и выгрузка

**Files:** Modify `app/claude_code.py`, `app/takeout.py`; Test `tests/test_code.py`, `tests/test_takeout.py`.

**Находки:** «глубокая вложенность ответа моста даёт 500», «строки code_chats без сессии на машине становятся
«призраками»», «PDF — раскодирование на цикле событий, память без срока, общий пул потоков», «„Новый чат“ и PATCH
чата без открытого компьютера пишут без предела», «Выгрузка: строки README о Claude Code длиннее соседних».

- [ ] **Step 1: тесты** — `test_deeply_nested_bridge_answer_is_relayed_not_500` (вложенность 2000 → 200 или 502
  `bridge`, не 500); `test_closed_foreign_session_does_not_come_back_as_ghost` (PATCH закрытия сессии, которой нет
  на машине, строку не заводит → 404 `not_found`; призраки списка — только `state = 'open'`);
  `test_pdf_cache_expires_by_ttl_in_background` (часы `clock.NOW` — запись старше `PDF_TTL_SEC` уходит без нового
  запроса `pdf`) и `test_pdf_cache_dropped_on_forget`; `test_new_chats_per_computer_are_limited`
  (`NEW_CHATS_PER_MIN = 10` на компьютер в минуту → 429 `too_many`; `session_start` — не больше того же);
  `test_takeout` — строки README без склейки `"\n  "`.
- [ ] **Step 2: прогон** → FAIL.
- [ ] **Step 3: код** — ответ моста транзитом `Response(json.dumps(data, ensure_ascii=True, allow_nan=False),
  media_type="application/json")` (ошибка кодирования/RecursionError → 502 `bridge`); `_pdf_bytes` — b64decode в
  том же `to_thread`, рендер под `asyncio.Semaphore(2)` раздела; `PDFS` — чистка по сроку при каждом обращении к
  разделу и в `bridge.forget` (через хук); предел новых чатов — память процесса `(aid) → deque` отметок.
- [ ] **Step 4: прогон** → PASS; быстрый набор зелёный.
- [ ] **Step 5: commit** — пять записей прочь из `FINDINGS.md`.

### Task 3: Студия — страница Claude Code (уровни, PIN, файлы) и ссылка «Скачать мост»

**Files:** Modify `app/static/js/code.js`, `app/static/js/code-files.js`, `app/static/js/ui.js`,
`app/static/css/app.css`; Test `tests/test_ui_browser.py` (сценарии `code`), `tests/test_static.py`.

**Находки:** «двойной `toPin` затирает адрес возврата», «правка файла теряется при уходе с уровня проекта»,
«прежний уровень раздела активен до ответа нового», «`?pin=` компьютера не на связи — без слов», «PIN компьютера
предложат сохранить менеджеры паролей», «git показывает «впереди на 0» и «позади на 0»».

**Interfaces:**
- Produces: `ui.js` `export function agentDownload(): HTMLElement` — блок `.agent-download` со ссылкой
  `a#agent-download[href="/api/agent/download"][download]` «Скачать мост для Windows», пометкой «ZIP, нужны Python
  3.11+ и Claude Code» и шагами (тексты — спека §4.2 дословно).

- [ ] **Step 1: тесты** (grep `code` по `tests/test_ui_browser.py`, сценарии — `-m integration -n0 -k code`):
  `test_code_empty_offers_agent_download` (пустой список → `#code-empty` с `#agent-download`, `href` верный);
  `test_code_double_closed_keeps_return_address` (два отказа `closed` подряд → после PIN — исходный адрес);
  `test_code_unsaved_edit_survives_crumb_navigation` (правка файла, переход по строке пути → окно «Изменения не
  сохранены», «Остаться» возвращает адрес; `beforeunload` поставлен при `dirty`); `test_code_level_frame_drawn_before_answer`
  (медленный ответ проектов → панели PIN уже нет); `test_code_pin_query_for_offline_computer_says_why`
  (`#code-status` = `OFFLINE`); `test_code_pin_field_is_not_a_password` (нет `form` и `input[type=password]` в
  `#code-pin`; поле — `type=text`, `-webkit-text-security: disc`, `autocomplete=off`, Enter открывает);
  `test_git_hides_zero_ahead_behind`.
- [ ] **Step 2: прогон** → FAIL.
- [ ] **Step 3: код** — `toPin`: не трогать `returnTo`, если `location.hash` уже адрес PIN; уровни: `frame()`
  сразу, список — по ответу; `computers` при `?pin=` неонлайн — `OFFLINE`; панель PIN без `<form>`/password;
  `code-files.js` нулевые пометки не показывать, `beforeunload` при `dirty`; `code.js` перед сменой уровня при
  `dirty` — вернуть адрес `history.replaceState` и спросить тем же окном; `agentDownload()` в `ui.js`, стиль
  `.agent-download` в `app.css`; `EMPTY` заменить (спека §4.2).
- [ ] **Step 4: прогон** — сценарии `-k code` и быстрый набор → PASS.
- [ ] **Step 5: commit** — шесть записей прочь.

### Task 4: Студия — чат Claude Code, push, настройки

**Files:** Modify `app/static/js/code-chat.js`, `app/static/js/settings.js`, `app/static/sw.js`; Test
`tests/test_ui_browser.py`, `tests/test_static.py`; план `docs/superpowers/plans/2026-10-28-studio-claude-code-server.md` (перенос строки).

**Находки:** ««Ошибка: » с висящим двоеточием», «у вопроса агента нет своего ответа», «вкладка на простое не
узнаёт о новом ходе», «`done` раньше снимка — ответ хода не виден», «отказ `turn` 409 running и 504 timeout не
возвращает к live», «тест скрытой страницы на фиксированных паузах; нет сценария `asked` из второй вкладки»,
«нет `pushsubscriptionchange`», «флажок push может застрять неактивным», «косметика волны B», «Менеджер паролей
может предложить сохранить PIN из настроек».

**Interfaces:** Consumes `ui.js` `agentDownload()` (задача 3).

- [ ] **Step 1: тесты** — `test_code_turn_error_without_message_says_error` («Ошибка» без двоеточия);
  `test_code_question_own_answer` (поле «Свой ответ» в карточке вопроса → `answers[вопрос] = текст`);
  `test_code_idle_tab_polls_live` (`IDLE_POLL_SEC = 20`, подменяемый; ход из другой вкладки — виден);
  `test_code_done_before_snapshot_reloads_history`; `test_code_turn_running_and_timeout_open_live`;
  `test_code_hidden_page_drops_its_stream_and_returns_to_live` — без фиксированных пауз: ждать
  `bridge.watching(...)` условием в Python между двумя `drive`; `test_code_answer_in_one_tab_removes_card_in_other`;
  `test_push_checkbox_gives_up_without_service_worker` (`ready` не разрешается → через `PUSH_READY_SEC = 10`
  слова и флажок снова активен); `pushsubscriptionchange` в `sw.js` (статический тест: обработчик есть и шлёт
  `POST /api/me/push`); `test_settings_agents_block_has_download` (`#agent-download` под заголовком всегда, и при
  пустом списке); `test_settings_pin_fields_are_not_passwords` (поля PIN блокировки — `type=text` +
  `-webkit-text-security: disc`, `autocomplete="one-time-code"`; пароль учётки — прежний `current-password`).
- [ ] **Step 2: прогон** → FAIL.
- [ ] **Step 3: код** — по предложениям находок; «Свой ответ» — поле и кнопка «Ответить» в карточке `question`,
  отправка `answers` строкой; в `settings.js` блок компьютеров берёт `agentDownload()`, строка пустого списка —
  «Компьютеров пока нет.»; `test_static.py` `settings = (`; перенос строки в плане.
- [ ] **Step 4: прогон** — сценарии `-k "code or push or settings or lock"` и быстрый набор → PASS.
- [ ] **Step 5: commit** — десять записей прочь.

### Task 5: Студия — «Перевод» и окна карты распознавания

**Files:** Modify `app/translate.py`, `app/ocr.py`, `app/static/js/translate.js`, `app/static/js/feed.js`,
`app/takeout.py` (только `_translations`); Test `tests/test_translate*.py`, `tests/test_ocr*.py`,
`tests/test_takeout.py`, `tests/test_ui_browser.py` (`-k translate`).

**Находки:** «„Перевод“: строка идущей записи пересоздаётся, ошибка опроса может остаться», «Перевод: опрос
записи шлёт исходник всех недоделанных кусков», «Перевод: лента и выгрузка читают тексты целиком», «Перевод:
удаление записи оставляет её скан в «Сканах»», «Перевод: окна карты вокруг распознавания».

Решения: удаление записи уносит её скан, если на него не ссылается другая запись `translations`; окно
подтверждения говорит «Распознанный документ в «Сканах» удалится вместе с переводом» (только у записи со
`scan_id`). Начало ленты — `substr` из `translation_parts` n = 1 (без нового столбца).

- [ ] **Step 1: тесты** — `test_delete_translation_removes_its_scan` / `..._keeps_scan_shared_by_repeat`;
  `test_poll_from_k_omits_source_of_waiting_parts` (кроме снимка); `test_feed_reads_heads_from_first_part`;
  `test_takeout_text_translation_without_parts_texts` (у `text`/`file` — только запись, у `photo` — куски);
  `test_translate_scan_waits_without_deadline_while_own_ocr_holds_card` (M1), `test_borrowed_marks_settling`
  (M2: пока идёт `_settle`, `ocr.idle` ложна), `test_photo_after_slow_ocr_releases_images` (M3); браузер:
  строка ждущей записи не пересоздаётся (тот же узел через два обновления), ошибка опроса снята при `open()` той
  же записи.
- [ ] **Step 2–4:** FAIL → код по предложениям → PASS; быстрый набор.
- [ ] **Step 5: commit** — пять записей прочь.

### Task 6: Студия — чат, подключения, шаткие тесты

**Files:** Modify `app/chat.py`, `app/connections.py`, `app/videos.py`, `app/static/js/chat.js`,
`tests/test_ui_browser.py` (`problems`), `tests/conftest.py` (если найдётся общий ресурс); Test
`tests/test_chat*.py`, `tests/test_connections*.py`, `tests/test_videos*.py`.

**Находки:** «Чат: PDF со сканами части страниц без «Распознавания» уходит модели без них» [P2], «Подключение —
не больше 8 разделов, а их уже 8», «test_chat_reasoning_switch_and_thinking_line: «Рассуждала 1 с» против «2 с»»,
«test_slow_download_gives_up_by_the_whole_clip_deadline падает под нагрузкой», «Браузерный сценарий упал на
`net::ERR_NO_BUFFER_SPACE` под нагрузкой», «Быстрый набор дважды завис при параллельных прогонах».

- [ ] **Step 1: тесты** — `test_mixed_scan_pdf_without_ocr_is_refused` (422 `scan`, слова как у переводчика
  «это скан — подключите „Распознавание“»); `test_connection_with_all_sections_saves` (предел —
  `len(sections.SECTIONS)`); событие SSE `done` несёт `reasoning_sec` сервера, страница пишет его (браузерный
  сценарий сравнивает после этого); срок скачивания ролика — через `clock` (тест без 50 мс настоящего времени);
  `problems()` пропускает `net::ERR_NO_BUFFER_SPACE` (сбой среды).
- [ ] **Step 2–4:** FAIL → код → PASS.
- [ ] **Step 5: зависание набора** — три прогона `pytest -q -n 4 -p no:cacheprovider -o console_output_style=classic`
  с выводом в файл; ищется общий ресурс процессов (порт, файл, каталог). Не воспроизвелось и не нашлось —
  запись остаётся с датой и итогом проверки в `Context`.
- [ ] **Step 6: commit** — закрытые записи прочь.

### Task 7: Мост — находки ядра

**Files:** Modify `studio_agent/turns.py`, `cli.py`, `store.py`, `agent.py`, `link.py`, `files.py`, `app.py`
(`_probe_loop`), `README.md` («Неполадки»); Test `tests/test_turns.py`, `test_cli.py`, `test_store.py`,
`test_agent.py`, `test_link.py`, `test_files.py`, `test_app.py`.

**Находки:** все 10 записей `studio-agent/FINDINGS.md`. Решения: `stop` во время `_finish` — флаг
`t.finishing` (не отменять задачу); выход — `close(final=True)` и слова отказа «мост выходит»; чистка окружения —
`CLAUDE_EFFORT`, `TRACEPARENT`, `CLAUDE_CODE_INVOKED_SKILLS`, `CODEX_COMPANION_*`, `GIT_ASKPASS`, `GIT_EDITOR`
(только если указывает в VS Code), `ELECTRON_RUN_AS_NODE`; `load_config` ловит `OSError`; часовая проба при
смене набора моделей — `restart_link()`; `answer/stop/watch` сверяют `pid` хода; `save_text` — read-only →
`Refused("read_only", "файл только для чтения")`, атрибуты прежнего файла переносятся; `open` — поколение канала;
`has_pin` — в памяти `Agent` (обновляет `set_pin`), `watch` — `to_thread`, ответ кодируется один раз.

- [ ] **Step 1–4:** тест на каждую запись (имена — по сути записи) → FAIL → код → PASS
  (`.venv/Scripts/python -m pytest -q`).
- [ ] **Step 5: commit** — все записи прочь из `FINDINGS.md` моста.

### Task 8: Мост — окно, трей, тема, отвязка, установка из архива

**Files:**
- Create: `studio_agent/theme.py`, `install.cmd`
- Modify: `studio_agent/window.py` (переписать), `tray.py`, `app.py`, `store.py` (`Config.login`), `enroll.py`
  (`login`, `unenroll`), `link.py` (`on_welcome`), `setup.py`, `__init__.py` (`VERSION = "0.2.0"`), `install.ps1`,
  `.gitattributes`, `README.md`, `CLAUDE.md`, `tests/fake_studio.py`
- Test: `tests/test_app.py`, `test_enroll.py`, `test_store.py`, `test_link.py`, новые `test_theme.py`,
  `test_tray.py`, `test_window_logic.py`, `test_install.py` (integration)

**Interfaces:**
- Consumes (задача 1): ответ привязки `login`; `welcome.login`; `DELETE <base>/api/agent/enroll` + Bearer → 204/401.
- Produces: `enroll.enroll(...) -> tuple[int, str, str]` (id, ключ, логин); `enroll.unenroll(url, key, *, timeout=15) -> int`
  (код HTTP; сеть — `EnrollError("unavailable")`); `Link(..., on_welcome: Callable[[dict], None] | None)`;
  `Controller.unenroll(*, ask_studio=True) -> tuple[bool, str]`, `set_pin(pin: str | None)`,
  `set_folder(path: str) -> list[str]`, `set_autostart(on: bool)`, `has_pin: bool`, `login: str`;
  `theme.palette(dark: bool) -> dict[str, str]`, `theme.is_dark() -> bool`, `theme.apply(root) -> dict`;
  `tray.image(state) -> Image`, `tray.title(state, login) -> str`, `tray.status_line(state, login) -> str`;
  `window.view_of(enrolled: bool) -> str` (`"login"`/`"link"`), `window.STATUS[state] -> (слова, цвет-токен)`.

- [ ] **Step 1: тесты** — `test_unenroll_asks_studio_and_forgets` (204 → нет `key.bin`, `pin.bin`, `agent_id`,
  `login`; адрес, имя, папка остались; состояние `setup`); `test_unenroll_without_studio_still_forgets`
  (`unavailable`/405 → `ok=False`, слова «Студия недоступна — компьютер остался в её списке…», всё стёрто);
  `test_unenroll_401_counts_as_done`; `test_unenroll_stops_turns_and_link_first` (порядок: `runner.close`,
  `link.stop`, потом запрос; после — канал не поднимается); `test_reenroll_after_revoked_skips_studio`
  (`ask_studio=False` — запроса нет); `test_login_from_enroll_and_welcome` (привязка пишет логин ответа;
  `welcome` с другим — перезаписан; без `login` в `welcome` — прежний); `test_set_pin_restarts_link`;
  `test_set_folder_validates_and_restarts`; `fake_studio` — `login` в `welcome`, `DELETE` отвязки;
  `test_palette_matches_studio_tokens` (значения спеки §5.3 обеих тем); `test_tray_title_with_and_without_login`
  («Studio Agent — на связи · example-user», без логина — без хвоста, ≤ 127); `test_tray_image_has_status_dot`
  (пиксель точки — цвет состояния, фон — рисунок значка); `test_status_line` («● На связи — example-user»);
  `test_view_of`; integration: `test_install_cmd_is_ascii_crlf`, `test_install_ps1_installs_into_programs_from_archive`
  (каталог-«архив» без `.git`, `LOCALAPPDATA` временный: файлы скопированы, `.venv` не пересоздана при повторе).
- [ ] **Step 2: прогон** → FAIL.
- [ ] **Step 3: код** — по спеке §5; окно — `open_main(root, controller)` (прежнее имя `open_setup` оставить
  синонимом для трея), состояние окна обновляется подпиской через очередь Tk из `app.main` (`("state", s)` уже
  идёт — передавать и окну); подтверждения и окно PIN — `Toplevel` той же темы; уведомления трея — по §5.4.
- [ ] **Step 4: прогон** — быстрый набор, затем `-m integration -k install`.
- [ ] **Step 5: ручной осмотр** — `STUDIO_AGENT_HOME` во временной папке, `.venv/Scripts/python -c "from studio_agent
  import app; ..."` не нужно: запустить `pythonw studio_agent.pyw` с временным `STUDIO_AGENT_HOME`, снимок экрана
  окна «Вход» в светлой и тёмной теме, выйти. Рабочий мост этого компьютера не трогать.
- [ ] **Step 6: commit**.

### Task 9: Проверка партии

- [ ] Студия: `.venv/Scripts/python -m pytest -q` и полный `-m integration` (PowerShell, `chcp 65001`, вывод в
  файл); мост: `-q` и `-m integration`.
- [ ] Одно ревью партии (студия + мост) — внешний взгляд на дифф; находки ревью — в `FINDINGS.md` соответствующего
  проекта, P1/P2 — чинить сразу.
- [ ] `FINDINGS.md` обоих проектов — только шапка или записи с объяснением, почему не закрыты.

### Task 10: Выкладка

- [ ] `git push origin main` студии и моста.
- [ ] `bash deploy/deploy.sh` → `==> готово`; `curl` `/health` 200.
- [ ] Скачивание: вход живой учёткой не нужен — проверка на хосте: `test -s /opt/services/studio/agent/studio-agent.zip`
  и `unzip -l` (есть `install.cmd`, нет `tests/`).
- [ ] Мост этого компьютера: выйти в трее, запустить новой версией (`pythonw studio_agent.pyw` из репозитория),
  трей зелёный, в окне — «На связи», логин из `welcome`.
