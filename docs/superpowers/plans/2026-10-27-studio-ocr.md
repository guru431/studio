# Распознавание (OCR): раздел и сканы в чате — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** раздел «Распознавание» (HunyuanOCR шлюза srvllm, скан рядом с текстом) и сканы PDF в чате, где ход ждёт распознавания на сервере.

**Architecture:** шлюз `llm_routers` открывает ключу студии `/pages/ocr` и маршрут освобождения `/pages/ocr-release`. В студии: таблицы `scans` и `scan_pages` (шаг схемы 16), рендер страниц `ocr_render.py` (pypdfium2, Pillow), задания и API `ocr.py` по образцу `transcripts.py`, очередь по пользователю. Чат при загрузке скана создаёт документ раздела, а `StreamTask` до запроса к модели ждёт его готовности и шлёт событие SSE `ocr`. Фронтенд — `ocr.js` (раскладка Б) и плашка в `chat.js`.

**Tech Stack:** Python 3.13 / FastAPI, SQLite (FTS5), httpx, pypdf, **pypdfium2==5.14.0** (новая), Pillow; ES-модули без сборки; pytest (+ xdist), браузерные тесты CDP.

**Spec:** [docs/superpowers/specs/2026-10-27-studio-ocr-design.md](../specs/2026-10-27-studio-ocr-design.md) — исполнитель читает её целиком вместе с планом.

## Global Constraints

- Журнал — только id (первые 8 знаков), коды и числа; имя файла, текст страницы, картинка — никогда (CLAUDE.md «Приватность — контракт»).
- Каждый запрос к истории и файлам — с `user_id` сессии; чужое — 404.
- Схема: `db.VERSION = 16`, шаг `STEPS[16] = OCR_16` только добавляет; `SCHEMA` тоже получает `OCR_16`. Откат `install.sh` на прежний код должен работать с новой базой.
- Модель в запросе — `"tencent/HunyuanOCR"`, короткое имя студии — `"hunyuan-ocr"`; маршрут — `<base_url>/pages/ocr`, освобождение — `POST <base_url>/pages/ocr-release` с телом `{}`.
- Запрос страницы: `temperature 0`, `max_tokens 16384`, сообщения `[{"role":"system","content":""}, {"role":"user","content":[картинка data:image/png;base64, затем текст PROMPT]}]`. PROMPT — дословно `cleaner/cleaner/local_llm.py:35-44` (`_OCR_PROMPT`).
- Пределы: `PAGES_MAX = 50`, загрузка — `documents.DOC_MAX` (20 МиБ) на всё, тело — `documents.BODY_MAX`; рендер 300 DPI, длинная сторона ≤ 2200; фото — PNG, JPEG, WebP, ≤ `image_upload.VIDEO_PIXELS`.
- Страница со слоем — у pypdf ≥ 40 знаков без пробельных (`LAYER_MIN_CHARS = 40`).
- Очередь пользователя — не больше `QUEUE_MAX = 5` ждущих; распознаётся одно за раз.
- Сроки: страница ждёт `PAGE_TIMEOUT_SEC = 700` (маршрут шлюза — 660 с), 503 — повтор раз в `RETRY_SEC = 15` до `WAIT_MAX_SEC = 1800`.
- Тесты: `.venv/Scripts/python -m pytest -q` (быстрый набор ≤ 60 с, без сети и реального времени — `clock.NOW`/`clock.SLEEP`); браузерные — `-m integration`. Тест дольше 1 с — чинить.
- Коммиты — по задаче; многострочное сообщение — через файл и `git commit -F`; без пуша.

## Review Focus

1. **Шесть вкладок на телефоне 320 px**: добавленная вкладка сужает остальные — «Расшифровка» может перестать влезать; ожидание — все подписи влезают (Task 7, `test_rail_labels_fit_their_tabs` на 6 подписей).
2. **Перезапуск службы посреди ожидания скана в чате**: ответ — «прервано», документ — ошибка, у вложения текст-пометка, а не NULL (иначе следующий ход шлёт модели `None`) (Task 6, тест `test_restart_while_a_turn_waits_leaves_a_marker`).
3. **Два пользователя распознают одновременно**: первый, кончив, не освобождает карту под вторым (Task 4, тест `test_release_waits_for_the_last_scan_of_the_studio`).
4. **В чате PDF, где страницы без слоя — только пустые**: ни `free_card`, ни запроса к `/pages/ocr`, ни освобождения; вложение — текст слоя (Task 6, тест `test_only_blank_scanned_pages_do_not_touch_the_card`).
5. **Отмена посреди страницы**: шлюз запрос не обрывает — студия дожидается ответа, выбрасывает его и освобождает карту без 409 (Task 4, тест `test_cancel_waits_for_the_page_and_releases`).

---

### Task 1: llm_routers — допуск `owner-images` к `/pages/ocr` и маршрут `/pages/ocr-release`

Репозиторий `C:\work\projects\llm_routers`, коммит там. Выкладка шлюза — в Task 10, не здесь.

**Files:**
- Modify: `gateway/slot_gate.py:757-808` (`SlotGate._release`), докстринг `:24-26`
- Modify: `gateway/roles_lint.py:814-819` (`_check_route_maps`)
- Modify: `gateway/routes.json` (новая запись после `/pages/ocr/chat/completions`; `_comment` `:27-29`)
- Modify: `gateway/litellm-config.yaml` (публикация рядом с `/slot/images/descriptions/release`, `:1023-1030`)
- Modify: `gateway/consumers.yaml:485-491` (`owner-images.routes`) и абзац `note`
- Modify: `gateway/README.md:136-137`, новая строка после `:148`, текст `:177-182`, `:340-341`
- Test: `gateway/tests/test_slot_gate_models.py`, `gateway/tests/test_slot_gate.py:32-45`, `gateway/tests/test_roles_lint.py`

**Interfaces:**
- Produces: `POST /pages/ocr-release` (тело любое, не разбирается) → `200 {"ok": true, "slot": "ocr"}`; `409 {"error": {"type": "slot_busy", …}}`, пока через slot-gate идёт запрос к слоту `ocr`; `502` — контроллер не ответил. Ключ `owner-images` получает `/pages/ocr` (и по префиксу `/pages/ocr/chat/completions`) и `/pages/ocr-release`.

- [ ] **Step 1: failing tests**

В `_routes(svc)` (`test_slot_gate_models.py:143-159`) добавить `{"path": "/plain/release", "release_of": "/plain"}`.

```python
def test_release_of_a_slot_route_without_a_map_frees_its_slot(gate):
    base, asked, released, _srv = gate
    assert _call(base + "/plain/release", {}) == (200, {"ok": True, "slot": "ocr"})
    assert _call(base + "/plain/release", b"") == (200, {"ok": True, "slot": "ocr"})
    assert _call(base + "/plain/release", {"model": "anything"}) == (200, {"ok": True, "slot": "ocr"})
    assert released == ["ocr", "ocr", "ocr"] and asked == []

def test_release_of_a_slot_route_is_refused_while_its_request_runs(gate, service):
    # по образцу test_release_is_refused_while_a_request_to_that_slot_is_in_work (:309-336):
    # запрос на /plain держит service.hold → /plain/release → 409 slot_busy, released == []
    ...

def test_real_ocr_release_is_published_and_points_at_the_ocr_route(real):
    route = slot_gate.match_route("/pages/ocr-release", real.routes)
    assert route.release_of == "/pages/ocr" and not route.with_video_models
```

В `test_roles_lint.py`: `release_of` на маршрут со `slot` без карты — не нарушение; `models_of` на такой маршрут и `release_of` на `/nowhere` — по-прежнему нарушения (`test_service_routes_must_point_at_a_map` остаётся зелёным). В `test_slot_gate.py::test_routes_load_from_file` — добавить `/pages/ocr-release` в список путей.

- [ ] **Step 2: run, expect FAIL** — `cd gateway && python -m pytest tests/test_slot_gate_models.py tests/test_roles_lint.py tests/test_slot_gate.py -q` → новые тесты красные (500 «без карты моделей», маршрута нет).

- [ ] **Step 3: implement**
  - `_release`: если `source` найден, `source.models` пуст и `source.slot` задан — `spec = source`, модель `None`, тело не разбирается; иначе прежний путь. Логи `:795-807` — без `None` в тексте (`модель {model}` только при модели).
  - `_check_route_maps`: `release_of` может ссылаться на маршрут с `slot` (без карты); `models_of` — только на маршрут с картой.
  - `routes.json`: `{"path": "/pages/ocr-release", "release_of": "/pages/ocr", "_note": "POST: slot-gate снимает аренду слота ocr (HunyuanOCR) у контроллера; 409 slot_busy, пока идёт запрос к слоту. Вне поддерева /pages/ocr — по префиксу его получили бы translate-bot, canary, cleaner; выдан только owner-images (студия освобождает карту после документа)"}`; `_comment` о `release_of` — «маршрут с картой или со слотом».
  - `litellm-config.yaml`: как `/slot/images/descriptions/release` — `target http://slot-gate:8100/pages/ocr-release`, `X-Slot-Gate-Token`, `auth: true`, `timeout: 120`, `methods: ["POST"]`, `cost_per_request: 0`.
  - `consumers.yaml`: в `owner-images.routes` — `"/pages/ocr", "/pages/ocr-release"`; в `note` абзац «С 2026-10-27 — распознавание студии (спека studio 2026-10-27-studio-ocr-design): `/pages/ocr` (HunyuanOCR, страница — запрос, $0.002) и `/pages/ocr-release` — после документа студия освобождает слот `ocr`: иначе аренда 900 с держит карту от gemma-abl и comfy. Перед первой страницей студия освобождает модели картинок и gemma-abl».
  - README: строки `/pages/ocr`, `/pages/ocr/chat/completions` — добавить `` `owner-images` ``; новая строка `` | `/pages/ocr-release` | снимает аренду слота `/pages/ocr`, кроме идущего запроса | — | `owner-images` | `` (в ячейке «Кому открыт» — только ключи: регулярка `ROLE` ловит любое слово в обратных кавычках); `:177-182` и `:340-341` — по смыслу.

- [ ] **Step 4: run, expect PASS** — `python run_tests.py --quick gateway/` и `python gateway/roles_lint.py` → зелёно, линтер без нарушений (`test_readme_contract.py` — тоже).

- [ ] **Step 5: commit** (в llm_routers) — `gateway: owner-images получает /pages/ocr и /pages/ocr-release (распознавание студии)`.

---

### Task 2: схема 16, раздел `ocr`, поиск, связь с файлами

**Files:**
- Modify: `app/db.py` (`VERSION`, `OCR_16`, `SCHEMA`, `STEPS`)
- Modify: `app/sections.py:31-37`, `app/connections.py:42-43`
- Modify: `app/search.py:42-43,58`, `app/media.py:440-450` (`referenced`), `app/history.py:54`
- Test: `tests/test_foundation.py` (миграция), `tests/test_sections.py`, `tests/test_search.py`, `tests/test_media.py`; поправить тесты, перечисляющие разделы (`grep -rn "dictation" tests/`)

**Interfaces:**
- Produces (таблицы):
```sql
CREATE TABLE scans(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
      connection_id INTEGER, model TEXT NOT NULL, name TEXT NOT NULL, source_media_id TEXT,
      origin TEXT NOT NULL CHECK (origin IN ('section','chat')), attachment_id TEXT,
      pages INTEGER NOT NULL, done_pages INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, detail TEXT NOT NULL,
      text TEXT, created REAL NOT NULL, finished REAL);
CREATE TABLE scan_pages(scan_id TEXT NOT NULL REFERENCES scans ON DELETE CASCADE,
      user_id INTEGER NOT NULL, n INTEGER NOT NULL, media_id TEXT,
      state TEXT NOT NULL CHECK (state IN ('waiting','done','failed','layer','blank')),
      text TEXT, detail TEXT, PRIMARY KEY (scan_id, n));
CREATE INDEX scans_user_created ON scans(user_id, created);
CREATE INDEX scans_attachment ON scans(attachment_id);
CREATE INDEX scans_state ON scans(state);
```
  `scans.state` ∈ `queued, working, cancelling, done, error, cancelled`; `n` — с 1. `attachment_id` — без FK (вложение удаляют, документ остаётся).
- Produces: `sections.SECTIONS = ("chat", "photo", "video", "transcribe", "ocr", "speech", "dictation")`, `NAMES["ocr"] = "Распознавание"`, `"ocr"` в `SERVER_ONLY`, `CATALOG["ocr"] = ["hunyuan-ocr"]`; `connections.KINDS["srvllm"]` с `"ocr"` (у `openai` — нет).
- Produces: поиск — раздел `ocr`, документ `search_docs(section='ocr', item=scans.id, part='', model=scans.model, body=name || ' ' || substr(COALESCE(text,''), 1, INDEX_CHARS))`.

- [ ] **Step 1: failing tests**

```python
def test_step_16_links_every_srvllm_connection_to_ocr(tmp_path):
    # база версии 15 со srvllm и openai подключениями двух пользователей → migrate
    assert links("ocr") == [(u1, srv1, ["hunyuan-ocr"]), (u2, srv2, ["hunyuan-ocr"])]   # openai — нет
    assert defaults("ocr") == [(u1, srv1, "hunyuan-ocr"), (u2, srv2, "hunyuan-ocr")]   # MIN(connection_id) пользователя
    assert user_version == 16

def test_new_srvllm_connection_gets_ocr(studio):
    add_gateway(studio)
    ocr = next(s for s in studio.c.get("/api/sections").json()["sections"] if s["id"] == "ocr")
    assert ocr["name"] == "Распознавание" and ocr["links"][0]["catalog"] == ["hunyuan-ocr"]

def test_scan_text_is_found_and_removed_with_the_scan(studio):
    # INSERT scans (state 'done', name 'Договор.pdf', text 'арендатор обязуется') → POST /api/search {"q": "арендатор"}
    # → item section 'ocr'; UPDATE text → старое слово не находится; DELETE scans → не находится
    ...

def test_scan_files_are_referenced(studio):
    # scans.source_media_id и scan_pages.media_id → media.referenced(uid, id) is True
    ...
```
(Точные ключи ответа `/api/sections` и поиска — по существующим тестам `test_sections.py`, `test_search.py`.)

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pytest -q -n0 tests/test_foundation.py tests/test_sections.py tests/test_search.py tests/test_media.py`

- [ ] **Step 3: implement**
  - `OCR_16` = таблицы и индексы выше + триггеры `search_scans_ai` (AFTER INSERT), `search_scans_au` (AFTER UPDATE OF name, text WHEN изменились), `search_scans_ad` (AFTER DELETE) по образцу `search_transcripts_*` (`db.py:161-168`) + связи: `INSERT INTO section_links(user_id, section, connection_id, models, created) SELECT user_id, 'ocr', id, '["hunyuan-ocr"]', created FROM connections WHERE kind = 'srvllm'` и `section_defaults` — `MIN(connection_id)` пользователя из этих связей. На новой базе (`SCHEMA`) вставки ничего не делают — это нормально.
  - `search.TITLES["ocr"] = ("scans", "name")`, `Literal[…, 'ocr']`.
  - `media.referenced`: ещё `scans.source_media_id` и `scan_pages.media_id` (у `scan_pages` — свой `user_id`).
  - `history.delete_all`: `scans` в списке таблиц перед `media` (`scan_pages` — каскадом).
  - Проверить, что прежний код переживает строку `section_links` раздела `ocr`: `connections.py:93` отдаёт `linked` всех строк — страница прежнего кода с неизвестным разделом не должна падать (посмотреть `connection-form.js`/`settings.js`, где читается `linked`; если падает — отфильтровать в `connections.py:93` по `sections.SECTIONS` **нельзя**, это новый код; тогда записать находку в FINDINGS и сказать в отчёте).

- [ ] **Step 4: run, expect PASS** — те же тесты и `.venv/Scripts/python -m pytest -q` (быстрый набор: тесты, сверявшие список разделов, поправлены).

- [ ] **Step 5: commit** — `OCR: шаг схемы 16 — scans и scan_pages, раздел ocr, поиск`.

---

### Task 3: рендер страниц — `ocr_render.py`

**Files:**
- Create: `app/ocr_render.py`
- Modify: `app/documents.py` (новая `pdf_page_texts`), `requirements.txt` (`pypdfium2==5.14.0`), `requirements-dev.txt` (если пины там свои)
- Test: `tests/test_ocr_render.py`, `tests/scans.py` (помощники PDF и фото для тестов)

**Interfaces:**
- Produces:
```python
# documents.py
def pdf_page_texts(data: bytes) -> list[str]   # текст pypdf каждой страницы (strip), DocumentError(BROKEN) на битом, TOO_LONG сверх TEXT_MAX_CHARS
# ocr_render.py
RENDER_DPI = 300; MAX_SIDE = 2200; LAYER_MIN_CHARS = 40; PAGES_MAX = 50; BLANK_STDDEV = 2.0
PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
class ScanError(ValueError): status: int        # слова для человека, как DocumentError
def has_layer(text: str) -> bool                # ≥ LAYER_MIN_CHARS непробельных
def pdf_page_count(data: bytes) -> int          # ScanError «документ повреждён» / «PDF защищён паролем»
def render_page(data: bytes, index: int) -> bytes   # PNG страницы index (с 0): 300 DPI, длинная сторона ≤ MAX_SIDE
def check_photo(raw: bytes) -> None             # только заголовок: PNG/JPEG/WebP, ≤ VIDEO_PIXELS, иначе ScanError 415/413
def photo_png(raw: bytes) -> bytes              # image_upload.normalize_image(raw, VIDEO_PIXELS) → длинная сторона ≤ MAX_SIDE → PNG
def is_blank(png: bytes) -> bool                # ImageStat.stddev серой картинки < BLANK_STDDEV
```
  Все функции синхронные (зовутся через `asyncio.to_thread`); pypdfium2 — документ открывается и закрывается внутри вызова.
- Produces (тесты): `tests/scans.py` — `scan_pdf(*pages) -> bytes` (страница: `"blank"` — белая картинка, `"ink"` — картинка с тёмными полосами, `str` — текстовая страница через `test_documents.pdf`-подобную сборку; склейка pypdf `PdfWriter.append`), `photo(fmt="JPEG", size=(800, 600)) -> bytes`.

- [ ] **Step 1: failing tests**

```python
def test_pages_render_at_300_dpi_capped_at_2200():
    png = ocr_render.render_page(scan_pdf("ink"), 0)        # A4 612×792 pt
    assert max(Image.open(io.BytesIO(png)).size) == 2200
def test_layer_needs_forty_letters():
    assert not ocr_render.has_layer("Scanned with CamScanner") and ocr_render.has_layer("x" * 40)
def test_blank_page_is_detected():
    assert ocr_render.is_blank(ocr_render.render_page(scan_pdf("blank"), 0))
    assert not ocr_render.is_blank(ocr_render.render_page(scan_pdf("ink"), 0))
def test_photo_is_turned_by_exif_and_capped():
    # JPEG 4000×3000 с EXIF Orientation=6 → PNG 1650×2200
def test_heic_and_huge_photos_are_refused_in_words():
    # не PNG/JPEG/WebP → ScanError 415 «нужны PDF или фото PNG, JPEG, WebP»; > VIDEO_PIXELS → 413
def test_page_texts_per_page():
    assert documents.pdf_page_texts(scan_pdf("Payment is due in 30 days", "blank")) == ["Payment is due in 30 days", ""]
def test_broken_pdf_is_words():
    # pdf_page_count(b"%PDF-1.4 junk") → ScanError «документ повреждён»
```

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pip install -r requirements-dev.txt` (после правки requirements), затем `.venv/Scripts/python -m pytest -q -n0 tests/test_ocr_render.py tests/test_documents.py`

- [ ] **Step 3: implement** `ocr_render.py` и `documents.pdf_page_texts` (общая часть с `_pdf` — без изменения поведения `extract`). Масштаб рендера — `min(RENDER_DPI / 72, MAX_SIDE / max(w_pt, h_pt))`.

- [ ] **Step 4: run, expect PASS**; каждый тест < 1 с (`--durations=5`).

- [ ] **Step 5: commit** — `OCR: рендер страниц PDF и фото (pypdfium2)`.

---

### Task 4: задания распознавания — `ocr.py` (клиент шлюза, очередь, карта)

**Files:**
- Create: `app/ocr.py` (часть 1: клиент, задания, `recover`/`stop`/`stop_user`, создание документов)
- Modify: `app/main.py` (`ocr.recover()` рядом с `transcripts.recover()` `:74`, `await ocr.stop()` рядом с `:86`), `app/history.py:17-25` (`await ocr.stop_user(user_id)`)
- Modify: `tests/fakes.py` (`/pages/ocr`, `/pages/ocr-release`)
- Test: `tests/test_ocr.py`

**Interfaces:**
- Consumes: Task 2 (таблицы, `sections`), Task 3 (`ocr_render.*`), `speech.free_card(conn)`, `upstream.request/gateway_error/json_body/refused_words/message`, `media.save_async/path/delete_async`, `clock.NOW/SLEEP`.
- Produces:
```python
MODEL = "tencent/HunyuanOCR"; GATEWAY = {"hunyuan-ocr": MODEL}; PROMPT = "…"  # дословно cleaner _OCR_PROMPT
MAX_TOKENS = 16384; PAGE_TIMEOUT_SEC = 700; RETRY_SEC = 15; WAIT_MAX_SEC = 1800; QUEUE_MAX = 5
RUNNING = ("queued", "working", "cancelling"); TERMINAL = ("done", "error", "cancelled")
INTERRUPTED = "прервано перезапуском студии"
class OcrError(Exception)          # отказ документа словами
class Busy(OcrError)               # 503 шлюза — ждать и повторить
class PageFailed(OcrError)         # страница не прочитана: «ответ обрезан», «на странице не найден текст», «модель вернула разметку без текста»
async def recognize(conn, png: bytes) -> str                     # Markdown страницы
async def release(conn) -> None                                  # POST /pages/ocr-release {}; 409 — повтор через RETRY_SEC до 3 раз; ошибки — в журнал, не наружу
def create(user_id, conn, model, name, *, origin, source_media_id, pages: list[dict], attachment_id=None) -> str
    # pages: [{"n", "state": "waiting"|"layer", "media_id"|None, "text"|None}]; строка 'queued', detail «в очереди»; QUEUE_MAX — HTTPException 409 busy
def kick(user_id: int) -> None                                  # запускает раннер пользователя, если его нет
async def request_cancel(user_id: int, scan_id: str) -> str      # queued → cancelled сразу; working → cancelling (раннер доделает); итог — state
def detail(job) -> str                                           # «жду карту · м:сс» со временем этапа, остальное — как есть
def recover() -> None   # RUNNING → error INTERRUPTED; у документов из чата — пометка во вложении (attachment_marker)
async def stop() -> None; async def stop_user(user_id: int) -> None
def attachment_text(scan_id: str) -> str   # готовое: «Страница N\n\n<текст>» через пустую строку; failed — «[страница N не распознана: …]»; blank — пропуск
def attachment_marker(detail: str) -> str  # f"[скан не распознан: {detail}]"
JOBS: dict[str, Job]   # идущие: Job(id, user_id, conn, stage, stage_at, cancel: bool)
```
- Поведение раннера (по образцу `transcripts.run`): берёт старейший `queued` своего пользователя → `working`. По страницам `waiting` по порядку: PDF — `render_page` из `source_media_id` (в потоке) → `media.save_async(png, "image/png", f"{stem}-{n}.png")` → `scan_pages.media_id`; фото — `photo_png` из своего `media_id` (не сохраняется). `is_blank` → `blank`. Перед первой непустой страницей — `speech.free_card(conn)`. Страница: `done` с текстом / `failed` с `detail`; `done_pages += 1`; `detail = f"распознаю · стр. {k} из {m}"` (k — номер распознаваемой среди всех страниц документа). Между страницами — проверка `job.cancel`; идущий запрос не прерывается. Итог: `done` (+ `scans.text` — тексты страниц через `\n\n`) / `error` / `cancelled`; у `attachment_id` — `UPDATE attachments SET text, chars` (`attachment_text` или `attachment_marker`). После документа, если он звал шлюз и в базе нет ни одной строки `scans` в `RUNNING` (кроме себя), — `release(conn)`.
- `tests/fakes.py`: `self.ocr_requests = []`, `self.ocr_replies = []` (очередь `(код, тело)`), затем `self.ocr_reply = {"choices": [{"message": {"content": "# Договор\n\nТекст страницы."}, "finish_reason": "stop"}]}`; `self.ocr_hold = False` (ответ ждёт снятия — для отмены); `/pages/ocr-release` — в `self.releases` с моделью `None` и ответом из `release_replies`.

- [ ] **Step 1: failing tests** (документ создаётся прямо через `ocr.create` + `ocr.kick`, API — в Task 5)

```python
def test_pages_are_recognized_in_order_with_the_cleaner_prompt(studio, caplog):
    # 2 страницы PDF "ink" → state done, scan_pages done с текстом ocr_reply; тела запросов:
    body = json.loads(studio.gw.ocr_requests[0].content)
    assert body["model"] == "tencent/HunyuanOCR" and body["max_tokens"] == 16384 and body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": ""}
    assert body["messages"][1]["content"][0]["type"] == "image_url" and body["messages"][1]["content"][1]["text"] == ocr.PROMPT
    # порядок путей: release картинок и gemma-abl → /pages/ocr ×2 → /pages/ocr-release
    # в журнале нет имени файла и текста страницы
def test_truncated_empty_and_layout_only_pages_fail_alone(studio):
    # ocr_replies: finish_reason length; content ""; content '[{"layout_type":"text","bbox":[1,2,3,4]}]' ; затем норма
    # → три страницы failed со словами, четвёртая done; документ done
def test_card_busy_waits_and_says_so(studio, monkeypatch):     # 503 ×3 → «жду карту · м:сс», затем done
def test_cancel_waits_for_the_page_and_releases(studio):       # Review Focus 5
    # ocr_hold → request_cancel → state cancelling; снять hold → cancelled, вторая страница не запрошена,
    # /pages/ocr-release после ответа страницы, ответ 200 (не 409)
def test_release_waits_for_the_last_scan_of_the_studio(studio):  # Review Focus 3
    # два пользователя, у второго документ ещё идёт → первый не зовёт release; кончился второй — один release
def test_queue_runs_one_at_a_time_and_caps_at_five(studio):
    # 1 идёт + 5 queued → шестой create → 409 busy; queued отменяется сразу (cancelled), без запроса к шлюзу
def test_blank_pages_skip_the_gateway(studio):
    # PDF из одних "blank" → done, страницы blank, ни free_card, ни /pages/ocr, ни release
def test_recover_marks_running_as_interrupted_and_fills_the_attachment(studio):
    # строки queued/working/cancelling → error INTERRUPTED; attachments.text = attachment_marker(INTERRUPTED)
def test_stop_user_stops_the_runner(studio):   # history.delete_all во время идущего документа — без висящих задач, JOBS пуст
```

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pytest -q -n0 tests/test_ocr.py`

- [ ] **Step 3: implement** `ocr.py` (часть 1), правки `main.py`, `history.py`, `fakes.py`. `recognize`: ответ 503 → `Busy`; иной не 200 → `OcrError(upstream.gateway_error(r, conn.name))`; `finish_reason == "length"` → `PageFailed("ответ модели обрезан — страница слишком плотная")`; пустой — `PageFailed("на странице не найден текст")`; разметка без текста — `PageFailed("модель вернула разметку без текста")` (проверка — как `_layout_only` в `cleaner/cleaner/local_llm.py:221-234`). Таймаут/сеть — `OcrError` словами, как `speech._post`.

- [ ] **Step 4: run, expect PASS**; затем `.venv/Scripts/python -m pytest -q` (быстрый набор целиком: `history`, `main`).

- [ ] **Step 5: commit** — `OCR: задания распознавания — очередь, карта, отмена, перезапуск`.

---

### Task 5: API раздела, выгрузки, «В работе», выгрузка истории

**Files:**
- Modify: `app/ocr.py` (часть 2: `router = APIRouter(prefix="/api/ocr")`), `app/main.py` (`include_router` после `transcripts`)
- Modify: `app/documents.py` (`read_uploads`), `app/exports.py`, `app/jobs.py`, `app/takeout.py`
- Test: `tests/test_ocr.py` (API), `tests/test_exports.py`, `tests/test_jobs.py`, `tests/test_takeout.py`

**Interfaces:**
- Consumes: Task 4 (`create`, `kick`, `request_cancel`, `attachment_text`, `JOBS`, `detail`).
- Produces:
```python
# documents.py
async def read_uploads(request, max_files: int) -> list[tuple[str, bytes]]   # все части `file` (1…max_files), сумма ≤ DOC_MAX, тело ≤ BODY_MAX; ошибки — DocumentError
# exports.py
def md_plain(md: str) -> str            # таблицы HTML → строки с табуляцией между ячейками, теги убраны, # ** __ ` сняты
def pages_txt(pages: list[dict]) -> str  # «Страница N» + текст; failed — «[не распознана: …]»; blank — «[пустая страница]»
def pages_md(pages: list[dict]) -> str   # «## Страница N» + Markdown модели как есть
def pages_docx(pages: list[dict], title: str) -> bytes   # как docx(): заголовок, «Страница N» жирным, абзацы md_plain
TYPES["md"] = "text/markdown; charset=utf-8"
```
- HTTP (ответы-«item»: `{id, name, state, detail, model, pages, done_pages, origin, created, finished}`; у GET одного — ещё `pages: [{n, state, text, detail, media_id}]`):
  - `POST /api/ocr` multipart, части `file`: один PDF или 1…50 фото → `202 item`. Отказы: смесь PDF и фото, два PDF, не PDF/фото — 415 «нужен один PDF или фото PNG, JPEG, WebP»; страниц > 50 — 422 «больше 50 страниц — разделите документ»; 413 — размер. Пара — `sections.resolve(uid, "ocr", x-studio-connection, x-studio-model)`. Имя — имя PDF или первого фото (`documents.clean_name`). Файлы — `media.save_async` (PDF — `source_media_id`; фото — `scan_pages.media_id` каждой страницы как есть). PDF в разделе — все страницы `waiting` (слой не читается).
  - `GET /api/ocr?before&before_id&limit` → `{"items": [...]}` (`db.page`, столбцы без `text`)
  - `GET /api/ocr/{sid}`; `POST /api/ocr/{sid}/cancel` → `{"state"}`; `DELETE /api/ocr/{sid}` → 204 (идущее — 409 `running`; файлы — `media.referenced` после удаления строки)
  - `GET /api/ocr/{sid}/export?format=txt|md|docx` (только `done`, иначе 409 `running`; имя — `<stem>.<format>`, `media.disposition`)
  - `POST /api/ocr/{sid}/attachments` → 201 вложение-документ `<stem>.md` (текст — `pages_md`, предел `TEXT_MAX_CHARS` — 413 словами; поля ответа — как `transcripts.to_chat`, без `source_transcript_id`)
- `jobs._ocr(uid, after)`: идущие — `RUNNING`, заголовок `f"Распознавание «{name}»"`, `detail` — `job.stage` идущего, `href = f"ocr?id={id}"`; в `list_jobs` — рядом с `_transcribe`.
- `takeout`: `SECTIONS` + `"ocr"`, `REFS["ocr"] = ("scans", ("source_media_id",), "")` и файлы страниц из `scan_pages` (своё условие по `user_id` — посмотреть, как `REFS` читается, и добавить вторую таблицу так же), `TABLES["ocr"] = ("scans", _scans)` — запись `{id, model, name, origin, pages, state, detail, created, finished, file, pages: [{n, state, text, detail, file}]}`; README выгрузки — строка `scans/`.

- [ ] **Step 1: failing tests**

```python
def test_upload_photos_make_one_document_in_order(studio):
    r = post_files(studio, [("a.jpg", photo()), ("b.png", photo("PNG"))])
    assert r.status_code == 202 and r.json()["pages"] == 2 and r.json()["name"] == "a.jpg"
    item = wait_ocr(studio, r.json()["id"], {"done"})
    assert [p["n"] for p in item["pages"]] == [1, 2] and all(p["media_id"] for p in item["pages"])
def test_pdf_in_the_section_recognizes_every_page(studio):   # текстовая страница тоже уходит в /pages/ocr
def test_refusals_are_words(studio):                         # HEIC, PDF+фото, 51 страница, 21 МиБ, без подключения раздела
def test_someone_else_cannot_see_the_scan_or_its_pages(studio):   # GET, export, attachments, /api/media/<id страницы> — 404
def test_export_txt_md_docx(studio):                          # имена файлов, «Страница 1», таблица — табуляцией в txt
def test_to_chat_attaches_markdown(studio):                   # 201, name «<stem>.md», text начинается с «## Страница 1»
def test_jobs_panel_lists_running_and_done_scans(studio)      # tests/test_jobs.py
def test_takeout_has_scans_and_their_page_files(studio)       # tests/test_takeout.py
def test_delete_running_is_409_and_done_removes_files(studio)
```

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pytest -q -n0 tests/test_ocr.py tests/test_exports.py tests/test_jobs.py tests/test_takeout.py`
- [ ] **Step 3: implement** часть 2 `ocr.py`, `read_uploads`, `exports`, `jobs`, `takeout`. Журнал — `ocr %s: принят, N стр.`, без имён.
- [ ] **Step 4: run, expect PASS**; быстрый набор целиком.
- [ ] **Step 5: commit** — `OCR: API раздела, выгрузки, «В работе», выгрузка истории`.

---

### Task 6: скан в чате — вложение и ход, который ждёт распознавания

**Files:**
- Modify: `app/chat.py` (`upload_attachment` `:1015-1044`, `create_turn` `:1159-1267`, `StreamTask` `:518-839`, `_message_out` `:280-290`, `_events` `:894-912`)
- Test: `tests/test_chat_ocr.py` (новый), не ломая `tests/test_chat.py`

**Interfaces:**
- Consumes: Task 3 (`documents.pdf_page_texts`, `ocr_render.has_layer`, `PAGES_MAX`), Task 4 (`ocr.create(origin="chat", attachment_id=…)`, `ocr.kick`, `ocr.request_cancel`, `ocr.RUNNING/TERMINAL`), `sections.default(uid, "ocr")`.
- Produces:
  - `POST /api/attachments` PDF: если есть страницы без слоя **и** `sections.default(uid, "ocr")` даёт подключение через сервер — документ `ocr` (`origin='chat'`, страницы со слоем — `layer` с текстом, без слоя — `waiting`; без слоя > `PAGES_MAX` — 422 «больше 50 страниц-сканов — распознайте его частями в разделе «Распознавание»»), вложение с `text NULL`, `chars 0`; ответ — прежние поля + `"ocr": {"id": sid, "pages": число страниц без слоя}`. Без подключения раздела — прежний `documents.extract`; его `SCAN` в чате — словами «это скан — подключите «Распознавание» в настройках». Пустые страницы определяет задание (рендер), не загрузка.
  - `create_turn`: `pending = [scan ids в RUNNING, чьё attachment_id ∈ вложения хода]`. `via == "browser"` и `pending` → 409 `{"code": "not_ready", "message": "скан ещё распознаётся"}` до `BEGIN`. `via == "server"` и `pending`: в транзакции — без `compose`/оценки токенов; ответ `streaming`, `StreamTask(..., messages=[], scans=pending, user_message_id=user_mid)`; `fill_photos`/`with_date` — тоже в `StreamTask`.
  - `StreamTask`: поля `scans: list[str]`, `user_message_id: str | None`, `scan_state: dict | None`. В `run()` до `_read()`: `error = await self._await_scans()`; ждёт раз в `SCAN_POLL_SEC = 1` (`clock.SLEEP`), шлёт `put("ocr", {"page": done, "pages": total})` при смене (сумма `done_pages`/`pages` своих документов — страниц без слоя); любой документ `error`/`cancelled` → итог-ошибка `f"скан не распознан: {detail}"` (модель не зовётся); готово → `self.messages = with_date(await fill_photos(chat_id, uid, compose(chat_id, user_message_id)))`, затем предел контекста (`self.limit`: сверх — ошибка словами `_too_big(...).detail["message"]`). `CancelledError` во время ожидания → `ocr.request_cancel` своих документов и дальше как сейчас (`stopped`/`error`). Срок ответа (`deadline`) ставит `_read` — ожидание его не съедает.
  - `_events`: `elif kind == "ocr": yield _sse("ocr", data)`; `_message_out`: `"ocr": live.scan_state if live else None`.
  - `delete_attachment` (`:1061`): неотправленное вложение с идущим документом (ход отклонили после загрузки, затем крестик) — `ocr.request_cancel` этого документа, затем как сейчас.

- [ ] **Step 1: failing tests** (`tests/test_chat_ocr.py`; помощники `scan_pdf` из `tests/scans.py`)

```python
def test_scan_pdf_attaches_and_the_turn_waits_for_it(studio):
    add_gateway(studio); chat = new_chat(studio)
    att = attach(studio, "скан.pdf", scan_pdf("ink", "ink")).json()
    assert att["chars"] == 0 and att["ocr"]["pages"] == 2
    events = turn(studio, chat, "о чём договор?", [att["id"]])     # читает SSE до done
    assert [e for e, _ in events if e == "ocr"] and events[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[-1]["messages"][-1]["content"]
    assert "Страница 1" in sent and "Текст страницы." in sent
    # запрос к модели — после последнего /pages/ocr и после /pages/ocr-release
def test_mixed_pdf_recognizes_only_pages_without_a_layer(studio):   # 1 текстовая + 1 "ink" → один /pages/ocr
def test_only_blank_scanned_pages_do_not_touch_the_card(studio):     # Review Focus 4
def test_without_an_ocr_connection_the_chat_works_as_before(studio):
    # sections unlink ocr → текстовый PDF с пустой страницей — текст сразу; чистый скан — 422 «это скан — подключите…»
def test_direct_connection_turn_is_not_ready_until_the_scan_is_done(studio)   # 409 not_ready, затем ход проходит
def test_failed_scan_is_an_error_answer_and_the_model_is_not_called(studio)
def test_stop_while_waiting_cancels_the_scan(studio)                 # POST /stop → ответ stopped, документ cancelled
def test_reload_shows_the_scan_progress(studio)                      # GET /api/chats/<id> во время ожидания → message.ocr == {"page": …, "pages": 2}
def test_restart_while_a_turn_waits_leaves_a_marker(studio)          # Review Focus 2: chat.recover()+ocr.recover() → ответ error, attachments.text — пометка, следующий ход шлёт её, а не "None"
def test_wait_does_not_eat_the_answer_deadline(studio, monkeypatch)  # ожидание > STREAM_TOTAL_SEC по часам → ответ done, не CUT_TIME
```

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pytest -q -n0 tests/test_chat_ocr.py`
- [ ] **Step 3: implement** по Interfaces. Порядок в `create_turn` не менять для хода без сканов (сторожат `tests/test_chat.py`, `test_chat_web.py`, `test_chat_reasoning.py`).
- [ ] **Step 4: run, expect PASS**; затем `.venv/Scripts/python -m pytest -q` целиком.
- [ ] **Step 5: commit** — `OCR: скан в чате — ход ждёт распознавания на сервере`.

---

### Task 7: страница раздела «Распознавание» — `ocr.js`

**Files:**
- Create: `app/static/js/ocr.js`
- Modify: `app/static/index.html:27-31` (вкладка `#/ocr` после «Расшифровки»), `app/static/js/app.js` (`HINTS` `:30-36`, `ICONS` `:37-44`, `SECTIONS` `:51-60`, комментарий `:4`), `app/static/js/connection-form.js:17-18` (`SECTION_NAMES`), `app/static/js/settings.js:299-300` (`EXPORT_SECTIONS`), `app/static/js/search.js:23-26`, `app/static/js/jobs.js:32-47` (`NOTICES`, `HREF` — добавить `ocr`, `ICONS`), `app/static/css/app.css` (классы `ocr-*`)
- Test: `tests/test_ui_browser.py` (`add_gateway()` `:1772-1776` — `'ocr'` в `sections`; `test_rail_labels_fit_their_tabs` `:2338-2351` — 6 подписей)

**Interfaces:**
- Consumes: Task 5 HTTP; `createPicker` (`feed.js:58-147`), `renderMarkdown` (`markdown.js:55`), `api`, `el`, `openDialog`, `words` (`ui.js`), `kick` (`jobs.js`).
- Produces: модуль `ocr.js` с `mount(view, ctx)`, `update(ctx)`, `unmount()` (как `transcribe.js:491-501`); `#/ocr?id=<id>` выбирает документ; `#/chat?ocr=<id>&ask=<discuss|summary|translate>` — переход «В чат ▾» (принимает Task 8).
- Подпись вкладки — «Сканы» (заголовок раздела и настройки — «Распознавание»): «Распознавание» на 10 px не влезает во вкладку телефона.
- Раскладка: слева — как «Расшифровка» (`split`, «+ Загрузить скан» — `input[type=file][multiple]`, `accept="application/pdf,.pdf,image/png,image/jpeg,image/webp"`; загрузка — `FormData` с несколькими `file`, ход в процентах через XHR, как `transcribe.js`), элементы — имя, «N стр.», `detail`. Справа — имя, «N страниц · дата», действия (`#ocr-copy`, `#ocr-download` ▾ txt/md/docx, `#ocr-chat` ▾ «Обсудить в чате»/«Кратко пересказать»/«Перевести», `#ocr-delete`), идущий — `detail` и `#ocr-cancel`; листалка `#ocr-prev` «‹», `#ocr-page` «страница N из M», `#ocr-next` «›»; ниже `.ocr-two`: `img.ocr-scan` (нажатие — своё окно `dialog.viewer` по образцу `photo.js:636-640,716-733`, id `ocr-viewer`) и `.ocr-text.md` (`renderMarkdown`). Страница `layer` — вместо картинки «страница взята из текстового слоя»; `failed` — слова причины; `blank` — «пустая страница». Ширина ≤ 760 px — картинка над текстом. Готовые страницы идущего документа листаются.
- Новые строки: `HINTS.ocr`, `NOTICES.ocr` (готово/ошибка), `search.js` — `['ocr', 'Распознавание']`.

- [ ] **Step 1: failing browser tests** (`-m integration`, `live.gw.ocr_reply` — Markdown с таблицей `<table><tr><td>Офис</td><td>38,5</td></tr></table>`)

```python
@pytest.mark.parametrize("width", [390, 1280])
def test_ocr_uploads_photos_and_pages_through_scan_and_text(browser, live, width):
    # signin → add_gateway() → #/ocr → два фото в input → ждать .ocr-text → «страница 1 из 2»
    # → .ocr-scan загружена (naturalWidth > 0) → #ocr-next → «страница 2 из 2»; таблица отрисована (td), скриптов нет
    # 390: .ocr-scan над .ocr-text (getBoundingClientRect().bottom <= text.top); NO_OVERFLOW
def test_ocr_download_and_to_chat(browser, live):   # «Скачать ▾» → 3 формата; «В чат ▾» → адрес #/chat, вложение «<stem>.md»
def test_rail_labels_fit_their_tabs(...)            # 6 подписей, 320 и 390 — влезают (Review Focus 1)
```

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pytest -q -m integration -n0 tests/test_ui_browser.py -k "ocr or rail_labels"`
- [ ] **Step 3: implement**. Если с шестой вкладкой «Расшифровка» не влезает на 320 px — правка CSS вкладок (`app.css:113-125`, меньше `letter-spacing`/отступы), а не подписи.
- [ ] **Step 4: run, expect PASS**; затем `-m integration tests/test_ui_browser.py -k "transcribe or settings or search or jobs"` — соседние сценарии целы.
- [ ] **Step 5: commit** — `OCR: раздел «Распознавание» — скан рядом с текстом`.

---

### Task 8: чат на странице — плашка распознавания, `not_ready`, «В чат» из раздела

**Files:**
- Modify: `app/static/js/chat.js` (`readServer` `:182-214`, `paintBadge` `:869-885`, `TOOL_WORDS`/`toolLine` `:82,170-175`, `submit` `:1148-1176`, `query` `:1346-1349`, `fromTranscript` `:751-775` — образец, `ASKS` `:72-76`)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: Task 6 (SSE `ocr {page, pages}`, `message.ocr`, 409 `not_ready`, ответ загрузки с `ocr`), Task 7 (`#/chat?ocr=<id>&ask=…`).
- Produces:
  - SSE `ocr` → `job.scan = data`, перерисовка; `paintBadge`: идущий ответ с `job.scan` (своя вкладка) или `m.ocr` (чужая, после `reload`) — `web-badge busy` с `span.spin` и текстом `📄 Распознаю скан · стр. ${page} из ${pages}`; пришла первая `delta` — плашка уходит.
  - `submit`: `err.code === 'not_ready'` → под полем «скан ещё распознаётся — отправлю, когда будет готов», повтор `submit(...)` через 3 с, пока не уйдёт или пока пользователь не уйдёт из диалога (прямые подключения).
  - `query()`: `ocr=<id>` → `fromScan(id, ask)` — как `fromTranscript`, но `POST /api/ocr/${id}/attachments`, без признака расшифровки (нет подтверждения внешней модели); `ASKS.translate = 'Переведи этот документ на русский'` (если `ASKS` не содержит).

- [ ] **Step 1: failing browser tests**

```python
def test_chat_scan_shows_progress_then_answers(browser, live):
    # live.gw.ocr_hold = True → прикрепить PDF-скан (2 стр.) → Отправить → ждать .web-badge.busy с «Распознаю скан · стр.»
    # → перейти в #/photo и обратно → плашка снова видна (m.ocr) → снять hold → ответ «Здравствуйте!»
def test_chat_from_scan_attaches_markdown(browser, live)   # #/chat?ocr=<id>&ask=summary → вложение «<stem>.md», в поле — вопрос
```

- [ ] **Step 2: run, expect FAIL** — `.venv/Scripts/python -m pytest -q -m integration -n0 tests/test_ui_browser.py -k "chat_scan or chat_from_scan"`
- [ ] **Step 3: implement**.
- [ ] **Step 4: run, expect PASS**; затем `-m integration tests/test_ui_browser.py -k chat` целиком.
- [ ] **Step 5: commit** — `OCR: плашка распознавания в чате и «В чат» из раздела`.

---

### Task 9: документы, живая приёмка, полный прогон

**Files:**
- Modify: `README.md` (раздел «Распознавание», скан в чате, пределы, подключения), `CLAUDE.md` (строки «Устройство»: `ocr`, `ocr_render`, таблицы, шаг схемы 16; «Ловушки»: освобождение `ocr` после документа, отмена дожидается страницы)
- Modify: `docs/superpowers/specs/2026-10-27-studio-ocr-design.md` (6.1 — путь `/pages/ocr-release`; 6.3 — `scan_pages`, вид файлов по MIME, без особой логики хранилища)
- Modify: `tests/test_studio_upload_live.py` (`manual`): одностраничный скан в разделе и в чате (`local-gemma-abl`); после — `/pages/ocr-release` 200 и ответ gemma-abl без ожидания аренды
- Test: весь быстрый набор, `-m integration`

- [ ] **Step 1:** README и CLAUDE.md — по коду задач 2–8 (имена, пределы, пути API); спека — две правки выше.
- [ ] **Step 2:** живая приёмка — сценарий в `test_studio_upload_live.py` (без запуска: требует выкладки и учётки, Task 10).
- [ ] **Step 3:** `.venv/Scripts/python -m pytest -q --durations=10` → зелёно, ≤ 60 с, ни одного теста > 1 с; `.venv/Scripts/python -m pytest -q -m integration` → зелёно.
- [ ] **Step 4: commit** — `OCR: README, CLAUDE.md, живая приёмка`.

---

### Task 10: выкладка — только по слову владельца

- [ ] llm_routers: `git push origin main`; `bash gateway/deploy/deploy.sh --dry-run`, затем `bash gateway/deploy/deploy.sh`; на srvllm `issue_keys.py --sync --apply --only owner-images`; проверка — ключом `owner-images` (`GATEWAY_KEY_OWNER_IMAGES` из vault, значение не печатать) страница на `/pages/ocr` → 200, `/pages/ocr-release` → 200.
- [ ] studio: `git push origin main`; `bash deploy/deploy.sh`; `https://studio.example.com/health`; на debian — `pypdfium2` в `.venv` (`install.sh` пересоберёт среду по `requirements.txt`), схема 16, журнал без предупреждений.
- [ ] Живая приёмка `-m manual` — по правилу CLAUDE.md студии (пароль из vault нерабочий — спросить владельца про учётку приёмки).
