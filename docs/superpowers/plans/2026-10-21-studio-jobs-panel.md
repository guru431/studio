# «В работе» и уведомления — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** свои задания всех разделов — одним списком (`GET /api/jobs`), значок «В работе» с панелью и
уведомление о готовности, пока студия открыта в фоне.

**Architecture:** `app/jobs.py` только читает базу (строки `images`, `videos`, `transcripts`,
`speeches`, `messages`) и память модулей (`detail` идущих), к шлюзу не ходит. Модуль оболочки
`app/static/js/jobs.js` опрашивает его, рисует значок и панель (`<dialog>`), показывает уведомления
через `ServiceWorkerRegistration.showNotification`; `sw.js` открывает запись по нажатию.

**Tech Stack:** FastAPI, SQLite, ES-модули без сборки, service worker.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-jobs-panel-design.md](../specs/2026-10-21-studio-jobs-panel-design.md)

## Global Constraints

- Схема базы не меняется; `queued` у картинок вводит другой исполнитель — здесь он только считается идущим.
- `JOBS_RECENT_SEC = 3600`, предел `finished` — 50, `JOBS_POLL_MS = 5000`, опрос без идущих — 30 с.
- Ни одного запроса к шлюзу ради панели; журнал — ничего (ни названий, ни имён файлов).
- Уведомление — только `showNotification` регистрации; текст без промпта, текста и имён: «Студия: картинка готова» и т. п.
- `localStorage`: `studio-jobs-seen` (время сервера), `studio-notify` (`'1'`) — свойство устройства.
- `sw.js` ничего не кладёт в кеш сверх оболочки (`.put(` нет).
- Опрос не ставит `seen` сессии: только `GET /api/jobs`; 423 ждёт отпирания в `api()`.

## Review Focus

1. Задание кончилось между двумя опросами, ни разу не показавшись идущим, — всё равно «готово» и уведомление (`finished >= since`, повтор id отсекает страница). Тест: `test_finished_since_includes_the_boundary`.
2. Чужие задания и чужие ответы чата не видны (чат — через `chats.user_id`). Тест: `test_other_users_jobs_are_not_listed`.
3. Ответ чата из браузера (`via='browser'`) и законченный ответ — не в списке. Тест: `test_chat_only_server_streaming_answers`.
4. «Открыть» на том же адресе, что открыт, — раздел всё равно переходит к записи (разделы снимают `?id` из адреса `replaceState`, `jobs.js` при совпадении шлёт `hashchange` сам).
5. `postMessage` от service worker — только адреса вида `photo?id=…`/`chat/<id>` (`jobs.js` `HREF`), иное игнорируется.

---

### Task 1: `GET /api/jobs`

**Files:** Create `app/jobs.py`, `tests/test_jobs.py`; Modify `app/main.py` (роутер).

**Interfaces:** Produces `GET /api/jobs?since=<float>` → `{now, running, finished}`; запись
`{section, id, state, title, detail, started, finished, href}` (+ `progress` у роликов).

- [ ] Тесты (`tests/test_jobs.py`, строки — прямо в базу, `detail` идущих — объекты в `*.JOBS`):
  `test_running_jobs_of_every_section` (картинка working/cancelling/queued, ролик queued/in_progress
  с `progress`, расшифровка working с `detail` из памяти «распознаю · 1:05», озвучка working «озвучиваю 2 из 5»,
  ответ чата streaming/server; title: «Картинка · Lustify», «Правка · FLUX», «Ролик · 5 с»,
  «Расшифровка «Звонок.m4a»», «Озвучка · <40 знаков>…», «Ответ · <название>»; href каждого раздела);
  `test_other_users_jobs_are_not_listed`; `test_finished_window_since_and_limit` (старше часа нет,
  без since — за час, since отсекает раньше, 60 готовых → 50 новейших); `test_finished_since_includes_the_boundary`;
  `test_states_are_normalized` (failed→error, cancelled, done; чат не в finished);
  `test_chat_only_server_streaming_answers`; `test_running_picture_via_api_and_no_gateway_call`
  (hold, число запросов к шлюзу до и после равно); `test_jobs_need_a_session_and_wait_for_the_lock`
  (401, 423); `test_jobs_log_nothing` (caplog без названий).
- [ ] `pytest tests/test_jobs.py` — FAIL (404).
- [ ] `app/jobs.py`: `list_jobs(request, since: float | None = None)`; по разделу функции
  `_photo`, `_video`, `_transcribe`, `_speech`, `_chat` → (running, finished); detail идущих —
  `images.JOBS[id].detail`, `transcripts.detail(job)`, `speeches.JOBS[id].stage`, иначе строка базы.
  finished: `finished >= max(since, now - JOBS_RECENT_SEC)`, по `finished` убыв., `LIMIT 50` у каждой
  таблицы и общий срез 50. Роутер — в `main.py`.
- [ ] PASS, коммит.

### Task 2: значок, опрос и панель

**Files:** Create `app/static/js/jobs.js`; Modify `index.html` (кнопка `#jobs`, `<dialog id="jobs-panel">`),
`app.js` (`setupJobs({ navigate })` в `enter`), `app.css`, `photo.js`/`video.js`/`transcribe.js`/`speech.js`/`chat.js` (`kick()` после запуска).

**Interfaces:** `setupJobs({ navigate })`, `kick()`; `#jobs` несёт `data-running` (число) и
`data-new` (`'1'` — есть непросмотренные), `.jobs-count`, `.jobs-dot`; строки панели
`.jobs-row[data-id][data-section]`, `.jobs-row.new`, кнопка `.jobs-open`.

- [ ] Браузерный тест `test_jobs_badge_counts_a_running_picture` (390/1280; hold): после «Создать» —
  `.jobs-count` «1», панель «Идёт» со строкой «Картинка · Lustify», значок внутри окна и не на теме.
- [ ] Браузерный тест `test_jobs_panel_shows_ready_and_opens_the_record`: готовая до перезагрузки
  картинка — точка, строка `.new` в «Готово за час», открытие панели снимает точку и после перезагрузки,
  «Открыть» — `#viewer` открыт, адрес `#/photo`.
- [ ] Реализация; опрос: 5 с при идущих или открытой панели, иначе 30 с; скрытая страница — только с
  уведомлениями и идущими; `visibilitychange` → опрос сразу.
- [ ] PASS, коммит.

### Task 3: переход к записи по `?id`

**Files:** Modify `photo.js`, `video.js`, `transcribe.js`, `speech.js`.

- [ ] Браузерный тест `test_jobs_open_selects_records_in_every_section` (строки — `live_sql`):
  `#/transcribe?id=` и `#/speech?id=` — запись выбрана (`aria-current`), `#/video?id=` — карточка с
  классом `jobs-focus`; адрес — без `?id`.
- [ ] Реализация: фото/видео — `GET /api/<раздел>/<id>`, готовая — просмотр, иначе карточка ленты
  прокручивается и подсвечивается (`jobs-focus`) при появлении (`onChange`); расшифровка/озвучка —
  `select(id)`; затем `history.replaceState(null, '', '#/<раздел>')`.
- [ ] PASS, коммит.

### Task 4: уведомления

**Files:** Modify `jobs.js` (`notifySupported()`, `notifyEnabled()`, `setNotify(on)`), `settings.js`
(блок «Уведомления»: `#notify-enabled`, `#notify-status`), `sw.js` (`notificationclick`),
`tests/test_static.py`.

- [ ] `test_service_worker_opens_the_job_from_a_notification` (static: `notificationclick`,
  `clients.matchAll`, `postMessage`, `openWindow('/#/'`; `.put(` по-прежнему нет).
- [ ] Браузерный `test_notification_only_when_hidden_and_without_content` (init подменяет
  `visibilityState`, `Notification.permission`, `showNotification`): переключатель в «Прочем»
  включается; готовое при видимой странице — без уведомления; при скрытой — одно, «Студия: картинка
  готова», `tag` — id, `data.href` — `photo?id=<id>`, промпта нет.
- [ ] `test_notify_toggle_explains_a_refusal` — `requestPermission` → `'denied'`: переключатель
  снят, слова «браузер запретил уведомления — разрешите их в настройках сайта».
- [ ] PASS, коммит.

### Task 5: документация и прогоны

- [ ] README — раздел «В работе и уведомления» (ограничения без push), CLAUDE.md — `jobs` в «Устройство».
- [ ] Быстрый набор `.venv/Scripts/python -m pytest -q`; браузерные:
  `-n0 -m integration tests/test_ui_browser.py -k "jobs or notif or login_shell or offline or lock or theme_toggle or rail_labels or layout_fits or phone_avatar"`.
- [ ] Коммит.
