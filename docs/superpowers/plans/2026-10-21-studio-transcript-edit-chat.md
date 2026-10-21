# Правка расшифровки и расшифровка в чат — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** правка текста реплик и имён собеседников поверх неизменного распознавания, перемотка по
реплике, SRT по пределам, вложение расшифровки в новый диалог чата с правилом локальной модели.

**Architecture:** правки — JSON `transcripts.edits` в базе, файл итога на шаре не меняется; одна
функция `transcripts.view(row, result)` собирает показываемые реплики для GET, выгрузок и вложения.
Новые итоги несут `raw` (сегменты до склейки с номером реплики) — по ним SRT. Вложение из расшифровки —
обычный документ чата со ссылкой `source_transcript_id`; локальность модели — `sections.is_local`,
запрет внешнего сервиса держит `chat.create_turn` (`external_ok`).

**Tech Stack:** FastAPI, SQLite, модули ES без сборки, pytest + браузерные сценарии CDP.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-transcript-edit-chat-design.md](../specs/2026-10-21-studio-transcript-edit-chat-design.md)

## Global Constraints

- Схема — `db.STEPS[10]` одним блоком (`EDITS_10`), `db.VERSION = 10`, новая база — `SCHEMA + EDITS_10`.
- `EDIT_TEXT_MAX = 20_000`, `NAME_EDIT_MAX = 60` (transcripts.py); `SRT_MAX_SEC = 7`, `SRT_MAX_CHARS = 84` (exports.py).
- Текст реплик, имена, текст вложения — не в журнал (только id).
- Чужой id — 404 `gone`; не `done` — 409 `running`; проверки — 422 `input` словами.
- Вложение больше `documents.TEXT_MAX_CHARS` — 413 `too_big`.
- 409 `external`: «Расшифровка уйдёт во внешний сервис «<подключение>» — подтвердите».
- Вопросы `ask`: `discuss` — пусто; `summary` — «Составь краткие итоги этой записи: о чём говорили и к
  чему пришли.»; `tasks` — «Выдели из записи принятые решения и задачи: что сделать, кто отвечает, сроки,
  если названы.»

## Review Focus

- Правка, отправленная, пока идёт другая вкладка: последнее сохранение побеждает, но слияние с `edits`
  — после чтения файла, без await между чтением строки и `UPDATE` (Task 3, тест слияния двух PATCH).
- Пустой текст реплики («стёр всё») — 422 словами, правка на странице не теряется (Task 3, Task 6).
- Внешний сервис без времени (`start = end = 0`) и правленая длинная реплика в SRT — субтитры по 1 с,
  без перекрытий (Task 4).
- Имя, равное другой метке («Собеседник 2» у первого) — число собеседников по показываемым именам (Task 3).
- Удалённая расшифровка после вложения — диалог открывается, ссылки «к записи» нет (Task 5).

---

### Task 1: Шаг схемы 10

**Files:** Modify `app/db.py`; Test `tests/test_foundation.py`

- [ ] Тест: `DROP_V10 = "ALTER TABLE transcripts DROP COLUMN edits; ALTER TABLE attachments DROP COLUMN source_transcript_id;"`
  первым в каждой цепочке DROP (до `DROP_V5`, который сносит таблицу); `VERSION == 10`;
  `test_migrate_adds_transcript_edits_to_a_version_9_base` — строка расшифровки и вложения остаются,
  `edits` и `source_transcript_id` — NULL.
- [ ] Красный → `EDITS_10` в `db.py`, `STEPS[10] = EDITS_10`, `SCHEMA += EDITS_10` → зелёный → коммит.

### Task 2: Сегменты до склейки (`raw`)

**Files:** Modify `app/speech.py`, `app/transcripts.py`; Test `tests/test_speech.py`, `tests/test_transcripts.py`

**Produces:** `speech.turns(segments) -> tuple[list[Segment], list[dict]]` — реплики и
`raw = [{start, end, speaker, text, turn}]`; `speech.speakers` = `turns(...)[0]`.
`transcripts.result_of(uid, row) -> dict` (весь файл итога); `segments_of` остаётся.

- [ ] Тест `test_turns_keep_the_recognized_segments_with_their_turn` (три сегмента из
  `test_transcribe_asks_whisperx_with_diarization` → `turn` 0, 0, 1, метки «Собеседник N»).
- [ ] Тест в test_transcripts: файл итога новой расшифровки содержит `raw` с `turn`; строка без `raw`
  (файл итога записан руками) читается GET и выгрузкой.
- [ ] Код, зелёный, коммит.

### Task 3: Правка — GET, PATCH, DELETE, выгрузки txt/docx

**Files:** Modify `app/transcripts.py`; Test `tests/test_transcripts.py`, `tests/test_exports.py`

**Produces:** `transcripts.view(row, result) -> list[dict]` — реплики
`{start, end, speaker, label, text, edited[, original]}`; `_out` отдаёт `names`, `edited`, `speakers`
по показываемым именам (метки — «Собеседник 1…row.speakers», файл не нужен).

- [ ] Тесты: правка текста и имени; `null` и значение, равное исходному, снимают правку; второй PATCH
  сливается с первым; DELETE `/edits` возвращает исходное; 422 (номер вне пределов, пустой текст,
  длиннее 20 000, чужая метка, имя с `\n`, длиннее 60); 409 у идущей; 404 у чужой; журнал без текста
  и имён; имя «Собеседник 2» у первого → `speakers == 1`; TXT и DOCX выгрузки — с правками.
- [ ] Код, зелёный, коммит.

### Task 4: SRT по пределам

**Files:** Modify `app/exports.py`, `app/transcripts.py` (export передаёт `raw`); Test `tests/test_exports.py`

**Produces:** `exports.srt(segments: list[dict], raw: list[dict] | None = None) -> str`.

Куски: префикс имени приклеивается к тексту первого куска реплики до деления; деление — по
предложениям, затем по словам, кусок ≤ `SRT_MAX_CHARS` (слово длиннее — режется); чтобы куски не
были длиннее 7 с, предел куска уменьшается до `ceil(len / ceil(dur / 7))`, но слова при этом не
режутся. Время куска — доля времени отрезка по длине. Проход по всем субтитрам: начало не раньше
конца прежнего; без длительности — 1 с; не длиннее 7 с; конец не дальше начала следующего, если оно позже.

- [ ] Тесты: прежние два остаются; SRT по `raw` у неправленой реплики (время сегментов), правленая —
  по тексту; длинная реплика 60 с / 600 знаков — каждый субтитр ≤ 7 с и ≤ 84 знаков, время не убывает,
  без перекрытий, первый начинается в начале реплики, текст склеивается в исходный; «Да.» на 20 с — один
  субтитр 7 с; внешний сервис без времени — по 1 с подряд; маршрут `format=srt` новой расшифровки.
- [ ] Код, зелёный, коммит.

### Task 5: Вложение из расшифровки, локальная модель, `external_ok`

**Files:** Modify `app/sections.py`, `app/transcripts.py`, `app/chat.py`; Test `tests/test_sections.py`, `tests/test_chat.py`

**Produces:** `sections.is_local(conn, model) -> bool` (`srvllm`, `proxied`, `local-*`); `local_models`
у каждой ссылки `section_out`; `POST /api/transcripts/{tid}/attachments` → 201
`{id, kind, name, chars, media_id, text, tokens}`; `TurnIn.external_ok: bool = False`; у вложения
сообщения — `source: {transcript_id, exists}`.

- [ ] Тесты (test_chat): текст вложения — заголовок «Расшифровка «Звонок Ивану.m4a» · 01:01» и реплики
  с правками, файл .txt читается `/api/media`, чужая — 404, идущая — 409, крестик удаляет, уборка через
  сутки, отправка к `local-gemma-abl` привязывает и шлёт `<document name="Звонок Ивану.txt">`, `source.exists`
  — true, после удаления расшифровки — false; предел — 413 `context` при малом пределе модели; больше
  `TEXT_MAX_CHARS` — 413 `too_big`; к `gemma4-abl` без `external_ok` — 409 `external`, ничего не записано,
  с ним — ход идёт; журнал без текста.
- [ ] Тест (test_sections): `is_local` — `local-*` шлюза да, `gemma4-abl` шлюза нет, openai `local-x` нет;
  `local_models` в `GET /api/sections`.
- [ ] Код, зелёный, коммит.

### Task 6: Страница расшифровки

**Files:** Modify `app/static/js/transcribe.js`, `app/static/js/feed.js` (`select(id, quiet)`), `app/static/css/app.css`; Test `tests/test_ui_browser.py`

Элементы: `#tr-edit`, `textarea.tr-edit-text`, `.tr-mark` «изменено», `.tr-revert` «вернуть»,
`#tr-save`, `#tr-edit-cancel`; `#tr-names` → `#tr-names-dialog` (`input[data-label]`, `#tr-names-save`);
`#tr-reset` → `#tr-reset-dialog` / `#tr-reset-confirm`; `#tr-chat` → `.tr-chat-menu [data-ask]`.
Черновик правки `{id, drafts: Map}` переживает перерисовку того же id.

- [ ] Браузерный тест `test_transcribe_edits_names_seeks_and_exports`: правка реплики, «Имена» →
  «Иван», TXT выгрузка с правкой и именем, нажатие по второй реплике — `currentTime` ≈ 3 и
  `aria-current` у неё, «Сбросить правки» — исходный текст.
- [ ] Код, зелёный, коммит.

### Task 7: Чат из расшифровки

**Files:** Modify `app/static/js/chat.js`, `app/static/css/app.css`; Test `tests/test_ui_browser.py`

- [ ] Браузерный тест `test_transcript_to_chat_with_the_local_model_and_back`: «В чат ▾» → «Составить
  итоги» → `#/chat`, чип «Звонок.txt» с числом знаков, окно текста, вопрос в поле, модель
  `local-gemma-abl`, `/turns` не было; отправка → ссылка «к записи» → `#/transcribe?id=…` с этой записью.
- [ ] Браузерный тест `test_transcript_to_chat_without_a_local_model_asks_first`: в чате отмечена только
  `gemma4-abl` — «модель не выбрана», «Отправить» закрыто; выбор модели → отправка → окно «уйдёт во
  внешний сервис» → «Отправить» — ход с `external_ok`.
- [ ] Код, зелёный, коммит.

### Task 8: Документы и быстрый набор

**Files:** Modify `README.md` («Расшифровка», «Чат»), `CLAUDE.md` («Устройство»)

- [ ] Тексты; быстрый набор `.venv/Scripts/python -m pytest -q` (+ `--durations=10`); свои браузерные
  сценарии и соседние (`transcribe`, `chat_with_a_pdf`, `context_refusal`); коммит.
