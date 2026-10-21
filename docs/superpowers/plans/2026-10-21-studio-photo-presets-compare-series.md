# Пресеты, сравнение, серии и ветви фото — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** в разделе «Фото» — пресеты учётки, сравнение двух картинок, серии по seed и по силе, «Варианты» в просмотре и параметры по модели.

**Architecture:** пресеты — своя таблица и модуль `app/presets.py` (роутер `/api/images/presets`, подключён раньше `images.router`); серия — `count` заданий `images.Job` с общим списком `series` и одной задачей `run_series`, которая по очереди зовёт прежний `run_job`; новое состояние `queued`. Страница: новые модули `compare.js`, `presets.js`, `series.js`; `photo.js`, `editor.js`, `select.js`, `chain.js` — правки.

**Tech Stack:** FastAPI, SQLite, pydantic 2; ES-модули без сборки; pytest + браузер через CDP.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-photo-presets-compare-series-design.md](../specs/2026-10-21-studio-photo-presets-compare-series-design.md)

## Global Constraints

- Шаг схемы — `db.STEPS[10]` одним блоком (`PHOTO_10`), `db.VERSION = 10`; номер проставит сливающий (план спеки: 11).
- `SERIES_MAX` = 4, серия 2…4; сила 0.05…1, шаг округления 0.05; seed по модулю 2³².
- `PRESETS_MAX` = 50 (409 `presets` «не больше 50 пресетов — удалите ненужные»); имя 1…60; промпт ≤ 24 000.
- Промпты пресетов и серии — не в журнал (журнал: id и число).
- Чужой id пресета, картинки, варианта — 404.
- Серия держит слот пользователя до последней строки; отмена идущей — `release_after_cancel`, как у одиночной.
- Текст сервера на странице — только `textContent`.

## Review Focus

1. Отмена `queued`-строки, пока первая ещё идёт: первая — `cancelling` с release, остальные — `cancelled`; новое задание до конца — 409 (Task 2: `test_series_cancel_of_a_queued_row_stops_the_whole_rest`).
2. Остановка службы посреди серии: идущая — «прервано перезапуском», `queued` — то же, без `recover()` (Task 2: `test_stop_marks_queued_rows_interrupted`).
3. Стирание истории и удаление учётки посреди серии: `queued` не стартуют после стирания (Task 2: `test_history_wipe_during_a_series_starts_nothing_more`).
4. Серия по силе с повтором значений после округления — 422, а не две одинаковые картинки (Task 2: `test_strength_series_values_and_refusals`).
5. `series` в форме — битый JSON — 422, файлы исходника не остаются (Task 2: `test_series_field_in_a_form_is_json`).

---

### Task 1: Пресеты — схема, API

**Files:** Create `app/presets.py`; Modify `app/db.py`, `app/main.py`; Test `tests/test_images.py`, `tests/test_foundation.py`.

**Interfaces — Produces:** `GET/POST /api/images/presets`, `DELETE /api/images/presets/{id}`; элемент `{id, name, params, created}`, `params` = `{model, connection_id, size, seed, strength, prompt}` (каждое может быть null; size — portrait/square/landscape).

- [ ] Тесты: `test_presets_crud_and_replace_by_name` (201, список новые сверху, то же имя — тот же id и новые params, 204 на DELETE), `test_presets_limit_is_50` (51-й — 409 `presets`, замена по имени при 50 — 201), `test_presets_are_own` (чужой DELETE — 404, список чужого не видит), `test_presets_input_is_validated` (имя ""/61 знак, seed −1, сила 2, лишнее поле, промпт 24001 — 422 `input`), `test_presets_outlive_the_history_wipe_but_not_the_account`; в `test_foundation.py` — `DROP_V10`, `test_migrate_adds_presets_to_a_version_9_base` (таблица и индексы `presets_user`, `images_user_source` в плане запроса), `VERSION == 10`, `DROP_V10` во всех прежних тестах миграции.
- [ ] Красный → `db.PHOTO_10` (таблица + два индекса), `SCHEMA += PHOTO_10`, `STEPS[10]`; `presets.py` с pydantic `ParamsIn` (extra forbid) и `PresetIn`; `main.include_router(presets.router)` до `images.router`.
- [ ] Зелёный, коммит.

### Task 2: Серии, `queued`, параметры по модели — сервер

**Files:** Modify `app/images.py`; Test `tests/test_images.py`.

**Interfaces — Produces:** поле `series` у `POST /api/images` (`{"count", "vary": "seed"|"strength", "strength_to"}`, в форме — строка JSON); ответ `{id, seed, ids}`; состояние `queued` (detail «в очереди серии · N из M»); `images.series_strengths(start, end, count) -> list[float]` (ValueError на повторах).

- [ ] Тесты: `test_seed_series_goes_one_by_one` (seed 7 → три строки 7, 8, 9; пока первая держится — две `queued`; к шлюзу по одной; все `done`; ответ `ids` из трёх), `test_random_seed_series_has_distinct_seeds`, `test_strength_series_values_and_refusals` (0.25…0.75 ×3 → 0.25/0.5/0.75 у шлюза; 0.25…0.3 ×4 — 422 «слишком узкий диапазон»; FLUX и без фото — 422 `series`; нет `strength_to` — 422), `test_series_with_a_mask_is_refused`, `test_series_field_in_a_form_is_json`, `test_external_series_sends_no_seed`, `test_series_cancel_of_the_running_row_stops_the_rest` (releases как у одиночной), `test_series_cancel_of_a_queued_row_stops_the_whole_rest`, `test_series_error_of_one_does_not_stop_the_next`, `test_series_keeps_the_slot_until_the_last` (409 между строками), `test_queued_row_cannot_be_deleted`, `test_restart_marks_queued_rows_interrupted` (recover), `test_stop_marks_queued_rows_interrupted`, `test_history_wipe_during_a_series_starts_nothing_more`, `test_flux_edit_keeps_no_strength`; поправить `test_uploaded_photo_is_forwarded_to_edits_and_discarded` (у FLUX `strength` None).
- [ ] Красный → `SeriesIn`, `JobIn.series`, `Job.series: list[Job] | None`, `run_series(jobs)`, `cancel_job` / `forget_user` по серии, `recover` и `stop` для `queued`, `strength` в строке — только Lustify.
- [ ] Зелёный (весь `test_images.py`), коммит.

### Task 3: Варианты — сервер

**Files:** Modify `app/images.py`; Test `tests/test_images.py`.

**Interfaces — Produces:** `GET /api/images/{id}/variants` → `{siblings, children}` (поля ленты, без `chain`), `VARIANTS_MAX = 24`.

- [ ] Тесты: `test_variants_are_siblings_and_children` (генерация G, правки A и B из G, C из A: у A siblings [B], children [C]; у G siblings [] и children [B, A]; не готовые и без файла — нет), `test_variants_of_another_user_are_404`, `test_variants_are_capped` (25 правок → 24).
- [ ] Красный → маршрут; зелёный; коммит.

### Task 4: Страница — параметры по модели, `queued`, серия, пресеты

**Files:** Create `app/static/js/series.js`, `app/static/js/presets.js`; Modify `photo.js`, `editor.js`, `chain.js`, `app.css`; Test `tests/test_ui_browser.py`.

**Interfaces:** `chain.js` экспортирует `isExternal(model)`, `strengthOf(step)` (сила только у `lustify`), `seedOf(step)` (null у внешней); `series.js` — `seriesStrengths(from, to, count)` (как на сервере) и `seriesControl({ prefix, canStrength, from }) → { panel, label(), value(), reset(), refresh() }`; `presets.js` — `presetChip({ root, popups, snapshot, apply, say })`.

- [ ] Браузерные тесты: `test_photo_series_by_seed_shows_queued_then_done` (чип `#chip-series` ×3, seed 7 → три карточки, `queued` с «в очереди серии», затем три `done`, seeds 7/8/9, чип снова «×1»), `test_photo_strength_series_shows_the_values` (Lustify с фото: «сила», до 0.75 → строка «0.25 · 0.50 · 0.75», в форме `series`), `test_photo_preset_saves_and_applies` (сохранить с промптом и seed, сменить модель/размер/seed, применить; при непустом поле — окно «Заменить промпт пресетом?», «Оставить свой»), `test_photo_external_model_hides_the_seed` (чип seed скрыт; строка внешней модели — без seed, «Повторить»); правка `test_photo_layout_fits_390` (чипы ★ и ×1 влезают).
- [ ] Код, зелёные сценарии, коммит.

### Task 5: Страница — сравнение, «Варианты», выделение

**Files:** Create `app/static/js/compare.js`; Modify `photo.js`, `select.js`, `app.css`; Test `tests/test_ui_browser.py`.

**Interfaces:** `openCompare(host, left, right)` (`{ media_id, label }`); `createSelection({ …, extraActions })`, где действие — `{ id, label, shown(items), run(items) }`.

- [ ] Браузерные тесты: `test_photo_compare_from_the_viewer_and_the_selection` (правка → «Сравнить с исходником» → `#compare`: слева исходник, ползунок 30 → `clip-path` и граница на 30 %, перетаскивание мышью двигает, «Поменять местами», Escape; выделение: одна — «Сравнить» скрыто, две — открывает, режим остаётся), `test_photo_variants_open_a_variant`.
- [ ] Код, зелёные сценарии, коммит.

### Task 6: Документы и быстрый набор

- [ ] README «Фото»: пресеты (не история), серия, сравнение, варианты; CLAUDE.md «Устройство»: `presets`, `queued`, варианты.
- [ ] `pytest -q` (быстрый), браузерные `-k "photo or chain or viewer or selection"`; коммит.
