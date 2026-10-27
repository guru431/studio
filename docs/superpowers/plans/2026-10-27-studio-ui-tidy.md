# Порядок в кнопках — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** вкладки в нужном порядке и влезают, тема — в меню профиля, вид ленты — одна кнопка с меню, микрофон и «очистить» — в поле, одна аккуратная строка кнопок под полем, без seed.

**Architecture:** только фронтенд (`app/static/`), кроме порядка `sections.SECTIONS`. Новый помощник `field.js` (рамка поля со значками), `views.js` — выпадающее меню, общие классы `.composer-bar/.composer-opts/.composer-go` в `app.css`.

**Tech Stack:** ES-модули без сборки, CSS; браузерные тесты CDP (`-m integration`), pytest.

**Spec:** [docs/superpowers/specs/2026-10-27-studio-ui-tidy-design.md](../specs/2026-10-27-studio-ui-tidy-design.md)

## Global Constraints

- Текст сервера — только `textContent`; новых библиотек нет.
- id элементов сохраняются, где спека не велит иного (тесты и сценарии опираются на них): `#theme-toggle`, `#<s>-view-<вид>`, `#send`, `#photo-go`, `#video-go`, `#speech-go`, `#photo-translate`, `.mic` внутри `#photo-composer`, `#composer`, `#video-composer`, `#editor`.
- Значки внутри поля не закрывают его середину; на 320–390 px нет горизонтальной прокрутки страницы.
- Тесты: браузерные `C:/work/projects/studio/.venv/Scripts/python -m pytest -q -m integration -n0 tests/test_ui_browser.py -k "…"` по затронутым сценариям + `tests/test_static.py`, `tests/test_sections.py`; быстрый набор и полный `integration` — один раз в конце (Task 3).
- Коммит — по задаче, без пуша; сообщение — файлом `tidy-t<N>-msg.txt` в scratchpad; последняя строка `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Меню профиля с пунктом темы на телефоне: меню не уходит за край, пункт нажимается (Task 1, `test_theme_toggle_in_the_profile_menu`).
2. Выпадающий вид в режиме выделения на телефоне: шапка выделения не ломается (Task 2, `test_photo_selection…`, `test_video_selection_in_every_view`).
3. Запись голоса в поле: расширенная кнопка `.mic.on` не закрывает середину поля и не уезжает за рамку на 320 px (Task 3, `test_mic_inserts_text_at_the_cursor` на 320).
4. «Очистить» при открытой записи голоса — запись не теряет вставку в пустое поле (Task 3, тест очистки во время записи не нужен — `cancelRecording` не зовётся; проверить, что очистка не трогает запись).
5. Длинный промпт в поле фото на 390 px: колонка значков не наезжает на текст (паддинг справа) (Task 3, `test_photo_layout_fits_390`).

---

### Task 1: вкладки, порядок разделов, тема в меню профиля

**Files:** `app/static/index.html` (рельс 25-44), `app/static/js/app.js` (тема 108-162, меню 215-242, `HINTS`/`ICONS`/`SECTIONS` порядок), `app/static/css/app.css` (рельс 48-52, 113-137; меню 97-104, 131; тема 56-60), `app/static/js/connection-form.js:17-18`, `app/static/js/search.js:23-26`, `app/static/js/settings.js:299-300`, `app/sections.py:31-33`, `app/takeout.py:41`; тесты `tests/test_ui_browser.py` (82-104, 144-156, 261-268, 583, 814, 2338-2352, `JOBS_PLACED` 4982-4986, `SEARCH_PLACED` 5294-5295), `tests/test_static.py:100-103`, `tests/test_sections.py`.

**Interfaces:** Produces: вкладки `chat, photo, video, transcribe («Записи»), speech, ocr («Сканы»)`; `sections.SECTIONS = ("chat", "photo", "video", "transcribe", "speech", "ocr", "dictation")`; `takeout.SECTIONS = ("chat", "photo", "video", "transcribe", "speech", "voices", "ocr")`; `#theme-toggle` — пункт `#menu` (класс `menu-item menu-theme`), сразу после `.menu-who`.

- [ ] **Step 1: тесты (RED)** — `test_rail_labels_fit_their_tabs`: порядок `["Чат","Фото","Видео","Записи","Озвучка","Сканы"]`, подписи целиком и на 320; `test_theme_toggle_in_the_profile_menu` (заменяет `test_theme_toggle_switches_and_is_kept`): открыть `#avatar` → `#theme-toggle` видим внутри `#menu`, нажатие меняет `data-theme` и `PATCH`, меню остаётся открытым, тема сохраняется после перезагрузки; на 390 меню внутри окна; пункты меню — `["Тёмная тема"|"Светлая тема","Настройки","Панель","Выход"]` (и без «Панель» у не-админа); порядок настроек 583 и выгрузки 814 — по спеке 4.1; `JOBS_PLACED`/`SEARCH_PLACED` без `#theme-toggle`; `test_static` — `#theme-toggle` внутри `#menu`.
- [ ] **Step 2: run, expect FAIL** — `-m integration -n0 tests/test_ui_browser.py -k "rail or theme or avatar or menu or settings_sections or export or jobs_placed or search_placed"` и `tests/test_static.py`.
- [ ] **Step 3: implement** — по спеке 4.1–4.2. На телефоне: `#avatar` right:12, `.jobs-toggle` right:54, `.search-toggle` right:96, шапке `padding-right` — по трём кнопкам.
- [ ] **Step 4: run, expect PASS** (+ `tests/test_sections.py`, `tests/test_takeout.py`).
- [ ] **Step 5: commit** — `UI: вкладки по порядку, «Записи», тема в меню профиля`.

### Task 2: вид ленты — одна кнопка с меню

**Files:** `app/static/js/views.js:17-39`, `app/static/js/photo.js:123-129` (и меню ⋯ — образец `photo-menu-wrap` 106-113, `popups` 274-281, 950-964), `app/static/js/video.js:79-85`, `app/static/css/app.css:782-785, 840`; тесты `tests/test_ui_browser.py` 2360-2404, 2478-2484, 2780-2802, 3366, 3431-3443.

**Interfaces:** Produces: `viewSwitch(section, fallback, onChange)` → `{node, get value}` как прежде; `node` — `div.view-pick` с кнопкой `#<s>-view` (`aria-haspopup=menu`, `aria-expanded`) и меню `div.view-menu[role=menu]` из `#<s>-view-<вид>` (`role=menuitemradio`, `aria-checked`).

- [ ] **Step 1: тесты (RED)** — в перечисленных сценариях добавить шаг «открыть `#<s>-view`» перед выбором и проверку `aria-checked` вместо `aria-pressed`; новый: меню закрывается выбором, Escape и нажатием вне; на 390 меню внутри окна; кнопка показывает значок текущего вида.
- [ ] **Step 2: run, expect FAIL** — `-k "views or selection or theme_and_view or video_tiles"`.
- [ ] **Step 3: implement** — по спеке 4.3.
- [ ] **Step 4: run, expect PASS.**
- [ ] **Step 5: commit** — `UI: вид ленты — одна кнопка с меню`.

### Task 3: поле со значками и строка под полем, без seed

**Files:** Create `app/static/js/field.js`; Modify `app/static/js/chat.js:457-476`, `photo.js:131-173` (+ seed: 52, 147-154, 301, 306-314, 335, 353, 573-586), `editor.js:36-69`, `video.js:87-120` (+ seed: 31, 110-112, 119, 464-476), `speech.js:64-73`, `search.js:73-92`, `presets.js` (74, 81, 96 — без seed), `series.js` (28, 44, 85 — подписи без поля seed), `mic.js:107-169` (только если нужен иной `host` ошибки), `app/static/css/app.css` (159-164, 312-319, 495-499, 616-637, 727); тесты `tests/test_ui_browser.py` (микрофон 3497-3599, 4868; макеты 1678-1704, 2081-2111, 3305-3348; seed 2878-2913, 2960-3015, 3031-3039; озвучка 4245-4268; без модели 4425-4455), `tests/test_static.py`.

**Interfaces:**
- Produces: `fieldBox(field: HTMLTextAreaElement|HTMLInputElement, {mic = true, clear = true, tools = []} = {}) -> {box: HTMLElement, error: HTMLElement}` в `field.js`: `box` — `div.field-box` (поле + `div.field-tools` с `.field-clear`, затем `tools`, затем `.mic`), `error` — место ошибки микрофона под рамкой (передаётся как `host` в `attachMic`). «Очистить» скрыт при пустом поле (`hidden` по событию `input`).
- Классы строки: `div.composer-bar` > `div.composer-opts` (чипы) + главная кнопка с классом `composer-go`.

- [ ] **Step 1: тесты (RED)** — в каждом разделе (чат, фото, «Изменить», видео, озвучка, поиск): `.field-box .mic` и `.field-box .field-clear` внутри рамки поля; «очистить» скрыт при пустом поле, после ввода виден, нажатие очищает и даёт фокус; `elementFromPoint` середины поля — само поле; на 390: чат — `#attach`, `#web-toggle`, `#think-toggle`, `#send` на одной строке (одна `top` ± 2 px); фото, видео, озвучка — главная кнопка справа на строке последнего чипа, ни одной строки только из главной кнопки; нет `#chip-seed`, `#chip-vseed`, `#preset-with-seed`; запрос фото и ролика без `seed`; пресет без seed. Обновить сценарии seed/пресетов/серии под отсутствие поля (серия «по seed» — разные seed шлюза, не `[7,8,9]`).
- [ ] **Step 2: run, expect FAIL** — `-k "mic or layout or seed or preset or series or speech or clear or field"`.
- [ ] **Step 3: implement** — по спеке 4.4–4.5; `div.mic-note` убрать; EN — в `tools` поля фото.
- [ ] **Step 4: run, expect PASS** по затронутым; затем быстрый набор `C:/work/projects/studio/.venv/Scripts/python -m pytest -q` и полный `cmd /c "chcp 65001 >nul && C:\work\projects\studio\.venv\Scripts\python.exe -m pytest -q -m integration"` (известный красный вне задачи — `test_studio_video_client.py::…gives_up_after_three_tries[ps]`, FINDINGS P3).
- [ ] **Step 5:** README (раздел интерфейса, если там описаны кнопки/seed/тема) и CLAUDE.md (строки про `views.js`, микрофон, тему) — по коду.
- [ ] **Step 6: commit** — `UI: микрофон и «очистить» в поле, одна строка кнопок, без seed`.
