# Речь: голосовой ввод, расшифровка, озвучка — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** микрофон в каждом поле промпта, вкладка «Расшифровка» (запись до 200 МиБ → текст по собеседникам, txt/docx/srt) и вкладка «Озвучка» (голос по своему образцу → mp3) — на моделях srvllm, с внешними сервисами OpenAI-стиля на подменах.

**Architecture:** третий из трёх планов спеки (этапы 4–6 раздела 9); опирается на план «Подключения по разделам» (`2026-10-20-studio-section-connections.md`): разделы `dictation`, `transcribe`, `speech`, `sections.resolve`, имена в списке разрешённых. Звук — `ffmpeg`/`ffprobe` debian через новый `app/audio.py`; запросы к шлюзу и внешним сервисам — `app/speech.py`; задания — `app/transcripts.py`, `app/speeches.py` (как `images.py`: фон, отмена, `recover()`); голоса — `app/voices.py`; выгрузки — `app/exports.py`. Шаг базы 5. Фронтенд — `mic.js`, `transcribe.js`, `speech.js`.

**Tech Stack:** Python 3.13/3.14, FastAPI, SQLite, httpx; ffmpeg 7.1 (libmp3lame) на debian; ванильный JS (`MediaRecorder`, `XMLHttpRequest` для хода загрузки); pytest.

**Spec:** [docs/superpowers/specs/2026-10-20-studio-sections-voice-design.md](../specs/2026-10-20-studio-sections-voice-design.md) — решения 1–3, 6, 7, 9, 10, 16; разделы 6.1 (маршруты речи, ключ шлюза), 6.3–6.5, 7, 8.

## Контракт шлюза (сверено с `llm_routers` 2026-10-20)

| Модель студии | Запрос | Поля | Ответ |
|---|---|---|---|
| `whisperx` (расшифровка) | `POST {base}/speech/asr?language=ru&diarize=true&word_timestamps=true&output_format=json&model=large-v3` | multipart **`audio_file`** | `{"segments": [{"start", "end", "text", "speaker"?}], "language", "text": [...]}` — текст собирать из `segments`; `speaker` может не быть |
| `whisperx` (голосовой ввод) | `POST {base}/speech/transcriptions` | `file`, `model=large-v3`, `language=ru`, `response_format=json` | `{"text"}` |
| `gigaam` | `POST {base}/speech/lite/transcriptions` | `file`, `response_format=json` (`model` не читается) | `{"text"}`; 32 МиБ, 600 с звука; принимает m4a/webm (ffmpeg у службы) |
| `qwen3-tts` | `POST {base}/speech/tts-qwen` (путь — сам маршрут, без `/clone`) | `text`, `language=Russian`, `ref_text`, `ref_audio` (WAV) | `audio/wav`; `503 busy` — повтор |
| openai, расшифровка / ввод | `POST {base}/audio/transcriptions` | `file`, `model`, `response_format` (`verbose_json` / `json`) | `{"text", "segments"?}` |
| openai, озвучка | `POST {base}/audio/speech` JSON `{"model","input","voice","response_format":"mp3"}` | — | mp3; ввод до 4096 знаков |

Карта: `asr` и `tts-qwen` не поднимутся, пока `comfy` (Lustify, Wan) держит аренду — шлюз ждёт `budget_s 300` и отвечает 503. Освобождения `asr`/`tts-qwen` нет. Перед расшифровкой и озвучкой srvllm — release `lustify-v10-krea2-turbo`, `flux.2-klein-4b` (`/slot/images/release`) и `gemma4-abl` (`/slot/images/descriptions/release`), итог не важен. Таймаут запроса к `/speech/asr` — 2460 с (LiteLLM), к `lite` — 180 с, к `tts-qwen` — 720 с.

## Global Constraints

- Звук, тексты расшифровок и озвучек, образцы голосов, продиктованное, имена файлов — ни в журнал, ни в адрес запроса (журнал Apache пишет строку запроса): имя файла едет заголовком `X-Studio-Name` (`encodeURIComponent`, до 255 знаков после разбора).
- Звук голосового ввода не хранится: в памяти запроса, после ответа — ничего.
- Временные файлы `ffmpeg` — `/var/lib/studio/tmp` (`config.tmp_dir` = `state_dir / "tmp"`), удаляются в `finally`; при старте службы каталог чистится.
- `ffmpeg`: argv-списком (`asyncio.create_subprocess_exec`, без оболочки), `-nostdin -hide_banner -loglevel error`; только звук (GPU не нужен: аппаратное ускорение — для видео). Запись перед распознаванием — всегда mp3 16 кГц моно 32 кбит/с (`-vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 32k`).
- Пределы: запись расшифровки — 200 МиБ тела и 60 мин звука; голосовой ввод — 2 мин и 8 МиБ; образец голоса — 5–15 с и 10 МиБ, его текст — до 500 знаков, имя — до 64; текст озвучки — до 2000 знаков.
- Одно идущее задание расшифровки и одно — озвучки на пользователя (409 `busy`).
- Карта занята (503 шлюза) — задание ждёт и повторяет раз в `RETRY_SEC = 15` до `WAIT_MAX_SEC = 1800`, состояние словами «жду карту · м:сс»; дальше — ошибка словами.
- Шаг базы только добавляет; «Удалить всю историю» и удаление учётки уносят расшифровки, озвучки, голоса и их файлы.
- Тест-команды — как в плане «Подключения по разделам»; тесты с настоящим `ffmpeg` — `@pytest.mark.integration` (`tests/test_audio.py`), быстрые — с подменой `audio` (фикстура `fake_audio`).
- Коммит — сообщение файлом, `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, без пуша. Коммиты в `llm_routers` — по его правилам (`CLAUDE.md` проекта).

## Review Focus

1. **iPhone пишет `audio/mp4`, Chrome — `audio/webm`, файл без `type`** → сервер берёт тип по расширению имени, неизвестное — 415 словами, а не 500 и не «пустой текст» — тесты в задачах 4 и 5.
2. **Карта занята чужими роликами 10 минут** → расшифровка показывает «жду карту · 9:40», «Отменить» работает в ожидании, через `WAIT_MAX_SEC` — ошибка словами — тест в задаче 5.
3. **Перезапуск службы посреди расшифровки или озвучки** → строка — ошибка «прервано перезапуском студии», временных файлов не остаётся — тест в задаче 5.
4. **Обрыв загрузки или запись больше 200 МиБ** → ни строки, ни файла на шаре, ни временного — тест в задаче 5.
5. **WhisperX без разделения собеседников** (нет `speaker` у сегментов) → текст есть, «собеседники не разделены», выгрузки без имён — тест в задаче 6.

---

### Task 1: Маршруты речи ключу шлюза `owner-images` (`llm_routers`)

**Files (репозиторий `C:/work/projects/llm_routers`):**
- Modify: `gateway/consumers.yaml` (запись `owner-images`, его `note:`), `gateway/README.md` (столбец «Кому открыт» строк `/speech/asr`, `/speech/transcriptions`, `/speech/lite/transcriptions`, `/speech/tts-qwen` и абзац «никому» о `/speech/tts-qwen`), `local-config/cron/llm-consumers.yaml` (описание ключа: речь студии)

- [ ] **Step 1:** в `routes` ключа `owner-images` добавить `"/speech/asr", "/speech/transcriptions", "/speech/lite/transcriptions", "/speech/tts-qwen"`; `note:` — строка о речи студии (расшифровка, голосовой ввод, озвучка по образцу).
- [ ] **Step 2:** README — «Кому открыт» этих маршрутов дополнить `owner-images`; о `/speech/tts-qwen` — «студия (`owner-images`), голос по образцу владельца».
- [ ] **Step 3: Проверка** — `cd C:/work/projects/llm_routers && python gateway/roles_lint.py && (cd gateway && python -m pytest -q) && python -m pytest -q tests/test_readme_contract.py` → всё зелёное.
- [ ] **Step 4: Commit** (в `llm_routers`) — «Open the speech routes to the studio key».
- [ ] **Step 5: Ключ на шлюзе** — сухой прогон, затем запись (мастер-ключ — в переменную, не на экран):
  `K=$(ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.15 "sudo grep ^LITELLM_MASTER_KEY= /opt/ai/gateway/.env | cut -d= -f2-")`;
  `LITELLM_MASTER_KEY="$K" GATEWAY_BASE=http://10.77.1.15:4000 python gateway/issue_keys.py --sync --only owner-images` (печатает тело), затем то же с `--apply` → код 0.
- [ ] **Step 6: Живая проверка** — ключ `GATEWAY_KEY_OWNER_IMAGES` из vault (`grep`, не печатать): `POST http://10.77.1.15:4000/speech/lite/transcriptions` с секундой тишины WAV (`file`, `response_format=json`) → 200 `{"text": …}`; без строки в `routes` было бы 403.

---

### Task 2: `audio.py` — ffmpeg и ffprobe

**Files:**
- Create: `app/audio.py`, `tests/test_audio.py`
- Modify: `app/config.py` (`tmp_dir`), `app/main.py` (`lifespan`: создать `tmp_dir`, удалить в нём оставшееся), `app/media.py:32-34` (`EXT`: `audio/mpeg .mp3`, `audio/mp4 .m4a`, `audio/x-m4a .m4a`, `audio/wav .wav`, `audio/x-wav .wav`, `audio/ogg .ogg`, `audio/webm .webm`, `audio/flac .flac`, `audio/aac .aac`, `video/webm .webm`, `video/quicktime .mov`, `application/json .json`), `deploy/install.sh` (до подмены кода: `command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || apt-get install -y --no-install-recommends ffmpeg`), `tests/conftest.py` (фикстура `fake_audio`)
- Test: `tests/test_audio.py` (integration), `tests/test_deploy.py`

**Interfaces:**
- Produces (`app/audio.py`):
  - `class AudioError(Exception)` — слова для человека («не удалось прочитать запись»), без имён и путей.
  - `async def probe(path: Path) -> float` — длительность, с (`ffprobe -v error -show_entries format=duration -of csv=p=0`).
  - `async def compress(src: Path, dst: Path) -> None` — mp3 16 кГц моно 32 кбит/с (Global Constraints).
  - `async def to_wav(src: Path, dst: Path, *, max_sec: float) -> float` — WAV PCM 16 бит моно 24 кГц, не длиннее `max_sec`; возвращает длительность.
  - `async def mp3_of(parts: list[Path], dst: Path) -> None` — склейка (WAV или mp3) в один mp3 (`-c:a libmp3lame -q:a 4`, concat-фильтр).
  - `def temp(suffix: str) -> Path` — новое имя в `state.CFG.tmp_dir`.
  - `TIMEOUT_SEC = 900` на вызов; превышение — убить процесс, `AudioError`.
- `fake_audio` (conftest): `probe` → 61.0, `compress`/`to_wav`/`mp3_of` — копии байтов (склейка — конкатенация); пишет вызовы в `calls`.

- [ ] **Step 1: Write the failing tests** — `tests/test_audio.py` (`pytestmark = pytest.mark.integration`): тишина 2 с WAV (`wave` stdlib) → `probe` ≈ 2.0; `compress` → mp3, `probe` ≈ 2.0, `ffprobe` даёт `sample_rate=16000`, `channels=1`; `to_wav(max_sec=1)` → ≈ 1.0; `mp3_of([a, b])` → ≈ сумма; мусор вместо звука → `AudioError` без пути в тексте. `tests/test_deploy.py`: `test_install_brings_ffmpeg_before_the_code_swap` — строка `apt-get install` c `ffmpeg` стоит раньше `mv "$NEW/$p"`.
- [ ] **Step 2: Run** — `.venv/Scripts/python -m pytest -q -m integration tests/test_audio.py` и `.venv/Scripts/python -m pytest -q tests/test_deploy.py -k ffmpeg` → FAIL.
- [ ] **Step 3: Implement** по Interfaces. Шапка модуля: argv без оболочки, временные — в `tmp_dir`, в журнал — только код выхода.
- [ ] **Step 4: Run** — те же команды → PASS.
- [ ] **Step 5: Commit** — «Wrap ffmpeg and ffprobe for the studio's sound».

---

### Task 3: `speech.py` — распознавание и озвучка у шлюза и внешнего сервиса

**Files:**
- Create: `app/speech.py`
- Modify: `tests/fakes.py` (`FakeGateway`: `/speech/asr`, `/speech/transcriptions`, `/speech/lite/transcriptions`, `/speech/tts-qwen`; поля `asr_requests`, `asr_reply`, `tts_requests`, `tts_replies`)
- Test: `tests/test_speech.py` (новый)

**Interfaces:**
- Consumes: `sections.CATALOG` (имена студии), `upstream.request`, `images.release`, `images.release_description`.
- Produces (`app/speech.py`):
  - `GATEWAY = {"whisperx": "large-v3", "gigaam": "gigaam-v3"}`
  - `class Busy(Exception)` — 503 (карта занята, `busy` Qwen); `class SpeechError(Exception)` — прочие отказы словами (`upstream.gateway_error`).
  - `@dataclass class Segment: start: float; end: float; speaker: str | None; text: str`
  - `async def transcribe(conn, model: str, audio: Path, mime: str) -> list[Segment]` — расшифровка (srvllm — `/speech/asr` из таблицы контракта; openai — `verbose_json`, сегментов нет — один сегмент на весь текст, `speaker=None`).
  - `async def dictate(conn, model: str, audio: bytes, mime: str, name: str) -> str` — голосовой ввод (srvllm gigaam/whisperx, openai — `json`).
  - `async def say(conn, model: str, text: str, voice) -> tuple[bytes, str]` — одна фраза → (байты, mime): srvllm — `voice` = `(wav: bytes, ref_text: str)`, WAV; openai — `voice` = имя, mp3.
  - `async def free_card(conn) -> None` — release трёх моделей (srvllm; прочим — ничего), итог не важен.
  - `def speakers(segments) -> list[Segment]` — `SPEAKER_xx` → «Собеседник N» по первому появлению; соседние сегменты одного собеседника — склеены.

- [ ] **Step 1: Write the failing tests** (`tests/test_speech.py`, шлюз — `FakeGateway`, подключение — `add_gateway`):

```python
def test_transcribe_asks_whisperx_with_diarization(studio, tmp_path):
    conn = conn_of(add_gateway(studio))
    studio.gw.asr_reply = {"language": "ru", "text": [], "segments": [
        {"start": 0.9, "end": 3.1, "speaker": "SPEAKER_01", "text": " Бюро переводов."},
        {"start": 3.2, "end": 4.0, "speaker": "SPEAKER_01", "text": "Здравствуйте."},
        {"start": 4.1, "end": 6.0, "speaker": "SPEAKER_00", "text": "У меня вопрос."}]}
    f = tmp_path / "a.mp3"; f.write_bytes(b"ID3fake")
    segs = speech.speakers(asyncio.run(speech.transcribe(conn, "whisperx", f, "audio/mpeg")))
    q = studio.gw.asr_requests[0]
    assert q.url.path == "/speech/asr" and dict(q.url.params) == {"language": "ru", "diarize": "true",
        "word_timestamps": "true", "output_format": "json", "model": "large-v3"}
    assert b'name="audio_file"' in q.content
    assert [(s.speaker, s.text) for s in segs] == [("Собеседник 1", "Бюро переводов. Здравствуйте."),
                                                   ("Собеседник 2", "У меня вопрос.")]

def test_dictation_goes_to_gigaam_and_keeps_the_phone_format(studio):
    conn = conn_of(add_gateway(studio))
    studio.gw.asr_reply = {"text": "кот на окне"}
    text = asyncio.run(speech.dictate(conn, "gigaam", b"\x00" * 64, "audio/mp4", "voice.m4a"))
    q = studio.gw.asr_requests[0]
    assert text == "кот на окне" and q.url.path == "/speech/lite/transcriptions"
    assert b'filename="voice.m4a"' in q.content and b"audio/mp4" in q.content

def test_qwen_busy_is_busy_and_wav_comes_back(studio):
    conn = conn_of(add_gateway(studio))
    studio.gw.tts_replies = [(503, {"detail": "busy: идёт другая генерация"}), (200, b"RIFFwav")]
    with pytest.raises(speech.Busy):
        asyncio.run(speech.say(conn, "qwen3-tts", "Привет.", (b"RIFFref", "образец")))
    data, mime = asyncio.run(speech.say(conn, "qwen3-tts", "Привет.", (b"RIFFref", "образец")))
    q = studio.gw.tts_requests[-1]
    assert (data, mime) == (b"RIFFwav", "audio/wav") and q.url.path == "/speech/tts-qwen"
    for field in (b'name="text"', b'name="language"', b"Russian", b'name="ref_text"', b'name="ref_audio"'):
        assert field in q.content

def test_free_card_releases_images_and_gemma(studio):
    conn = conn_of(add_gateway(studio))
    asyncio.run(speech.free_card(conn))
    assert sorted(m for m, _ in studio.gw.releases) == ["flux.2-klein-4b", "gemma4-abl", "lustify-v10-krea2-turbo"]
```

плюс внешний сервис (имя в списке, `RESOLVE` подменён, как в плане разделов): `transcribe` шлёт `response_format=verbose_json`, `say` — JSON `/audio/speech` с `voice`.

- [ ] **Step 2: Run** — `.venv/Scripts/python -m pytest -q tests/test_speech.py` → FAIL.
- [ ] **Step 3: Implement** по Interfaces и таблице контракта. 503 — `Busy`; прочее не-200 — `SpeechError(upstream.gateway_error(...))`; `UpstreamRefused` — `SpeechError("адрес подключения убран из списка администратора")`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — «Talk to WhisperX, GigaAM and Qwen3-TTS through the gateway».

---

### Task 4: Голосовой ввод — `POST /api/dictation` и микрофон в полях

**Files:**
- Create: `app/dictation.py`, `app/static/js/mic.js`
- Modify: `app/main.py` (роутер), `app/static/js/chat.js`, `photo.js`, `editor.js`, `video.js` (кнопка у поля), `app/static/css/app.css` (`.mic`)
- Test: `tests/test_dictation.py` (новый), `tests/test_ui_browser.py`

**Interfaces:**
- Produces: `POST /api/dictation` — multipart `audio` (файл), до 8 МиБ тела; ответ `{"text": str}`; модель — `sections.resolve(uid, "dictation", None, None)`; 409 `no_model` (подсказка «выберите модель в настройках раздела «Голосовой ввод»»); 503 `busy` «модель голосового ввода занята — попробуйте ещё раз» (без ожидания: ввод — секунды); 415 `format` — не звук; журнал — «пользователь %d: голосовой ввод» и код итога.
- `mic.js`: `export function attachMic(field: HTMLTextAreaElement | HTMLInputElement, host: HTMLElement): HTMLButtonElement` — кнопка `button.mic` (`aria-label` «Голосовой ввод» / «Остановить запись»), запись `MediaRecorder` (тип по `isTypeSupported`: `audio/mp4`, затем `audio/webm`), таймер в кнопке, конец — повторное нажатие или 120 с; ответ вставляется на место курсора (пробел между словами, если нужен), поле получает событие `input`, отправки нет; отказ (нет микрофона, нет `MediaRecorder`, отказ сервера) — `p.mic-error` словами рядом с полем. Шапка модуля — где хранится звук (нигде).
- `export async function recordAudio(maxSec: number, onTick?): Promise<Blob>` — общий рекордер (им же пишется образец голоса, задача 8).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dictation.py
def test_dictation_returns_text_and_keeps_nothing(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    studio.gw.asr_reply = {"text": "тайная диктовка"}
    media0 = state.DB.execute("SELECT count(*) FROM media").fetchone()[0]
    r = studio.c.post("/api/dictation", files={"audio": ("voice.m4a", b"\x00" * 128, "audio/mp4")})
    assert r.status_code == 200 and r.json() == {"text": "тайная диктовка"}
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == media0
    assert "тайная диктовка" not in caplog.text and "voice.m4a" not in caplog.text

def test_dictation_refuses_non_audio_and_too_big(studio):
    add_gateway(studio)
    assert studio.c.post("/api/dictation", files={"audio": ("x.txt", b"hi", "text/plain")}).status_code == 415
    big = b"\x00" * (8 * 1024 * 1024 + 1)
    assert studio.c.post("/api/dictation", files={"audio": ("v.webm", big, "audio/webm")}).status_code == 413
```

`tests/test_ui_browser.py` — `test_mic_inserts_text_at_the_cursor` (параметр `width` 390/1280): подменить `navigator.mediaDevices.getUserMedia` и `MediaRecorder` (запись отдаёт `Blob` 1 КиБ `audio/webm`), шлюз отвечает «кот», поле фото содержит «рыжий |на окне» (курсор после «рыжий »); два нажатия на `#photo-composer .mic` → поле «рыжий кот на окне», запрос к `/api/images` не ушёл; у `#chat-input`, `#video-prompt` и в окне «Изменить» кнопка `.mic` есть.

- [ ] **Step 2: Run** — `.venv/Scripts/python -m pytest -q tests/test_dictation.py` и `-m integration … -k mic` → FAIL.
- [ ] **Step 3: Implement** по Interfaces. Разбор формы — новая `forms.audio_form(raw: bytes, content_type: str, *, max_bytes: int) -> tuple[dict[str, str], bytes, str, str]` (поля, звук из части `audio`, её тип, имя файла) тем же разбором `email.parser.BytesParser`, что `image_upload.decode_form` (`python-multipart` в зависимостях нет); тип — по `Content-Type` части, пустой или `application/octet-stream` — по расширению имени (`forms.AUDIO_EXT`: `.mp3 .m4a .mp4 .wav .ogg .opus .webm .flac .aac .mov`), иначе 415 `format`. Ею же разбирается образец голоса (задача 8).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — «Dictate into every prompt field».

---

### Task 5: Расшифровка — служба (загрузка потоком, задание, отмена, перезапуск)

**Files:**
- Create: `app/transcripts.py`
- Modify: `app/db.py` (`VERSION = 5`, таблицы `transcripts`, `voices`, `speeches` — все три здесь, одним шагом), `app/main.py` (роутер, `transcripts.recover()`), `app/history.py` (стоп и удаление), `app/media.py` (`referenced` — и новые таблицы), `deploy/apache-vhost.conf` (`<LocationMatch "^/api/transcripts$"> LimitRequestBody 220200960 </LocationMatch>`)
- Test: `tests/test_transcripts.py` (новый), `tests/test_deploy.py`, `tests/test_foundation.py`

**Interfaces:**
- Таблицы (шаг 5):
  `transcripts(id TEXT PRIMARY KEY, user_id … ON DELETE CASCADE, connection_id INTEGER, model TEXT NOT NULL, name TEXT NOT NULL, source_media_id TEXT NOT NULL, result_media_id TEXT, duration REAL, speakers INTEGER, state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL)`;
  `voices(id TEXT PRIMARY KEY, user_id … ON DELETE CASCADE, name TEXT NOT NULL, ref_text TEXT NOT NULL, media_id TEXT NOT NULL, duration REAL NOT NULL, created REAL NOT NULL)`;
  `speeches(id TEXT PRIMARY KEY, user_id … ON DELETE CASCADE, connection_id INTEGER, model TEXT NOT NULL, voice TEXT NOT NULL, text TEXT NOT NULL, media_id TEXT, duration REAL, state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL)`; индексы `(user_id, created)`.
- `POST /api/transcripts` — тело — сам файл (`Content-Type` — тип записи), `X-Studio-Name` — имя; потоком `media.save_stream` (предел `UPLOAD_MAX = 200 * 1024 * 1024`), без `Content-Length` или больше предела — 413 и ни строки, ни файла; тип — `Content-Type`, иначе по расширению имени (`.mp3 .m4a .wav .ogg .opus .webm .flac .aac .mp4 .mov`), иначе 415 `format`; занят — 409 `busy` до чтения тела; модель — `sections.resolve(uid, "transcribe", …)` (параметры — заголовки `X-Studio-Connection`, `X-Studio-Model`, необязательные). Ответ 202 — запись ленты.
- Состояния: `working` (detail: «готовлю запись», «жду карту · м:сс», «распознаю · м:сс»), `cancelling`, `done`, `error`, `cancelled`. Ход: `probe` (больше 60 мин — ошибка «запись длиннее 60 минут — разделите её»), `compress` во временный mp3, `speech.free_card` (srvllm), `speech.transcribe` с повтором на `Busy` (Global Constraints), `speech.speakers`, итог — JSON `{"segments": [{"speaker", "start", "end", "text"}]}` в media `application/json` (`result_media_id`), `speakers` — число различных (0 — не разделены).
- `GET /api/transcripts?before=&limit=` (лента), `GET /api/transcripts/{id}` (+ `segments` у готовой), `POST /api/transcripts/{id}/cancel`, `DELETE /api/transcripts/{id}` (409 `running` у идущей), `transcripts.recover()`, `transcripts.stop_user(uid)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_transcripts.py`, фикстуры `studio`, `fake_audio`):
  - `test_upload_streams_to_the_share_and_reads_by_speakers` — тело 1 МиБ `audio/mp4`, имя «Звонок Ивану.m4a» в `X-Studio-Name` → 202; после хода: `state == "done"`, `duration == 61.0`, `speakers == 2`, сегменты «Собеседник 1/2»; шлюз получил mp3 (`fake_audio.calls` — `compress`), release трёх моделей был до `/speech/asr`; имени нет в журнале.
  - `test_card_busy_waits_and_says_so` — шлюз 503 трижды, затем ответ; часы `clock.NOW` двигаются паузами `clock.SLEEP`: detail в ожидании начинается с «жду карту ·», итог `done`; `WAIT_MAX_SEC` исчерпан — `error` со словами «карта занята».
  - `test_cancel_while_waiting_for_the_card` → `cancelled`, временный каталог пуст.
  - `test_too_big_or_unknown_upload_leaves_nothing` — тело больше `UPLOAD_MAX` (подменить предел на 1 КиБ) → 413; `text/plain` без расширения звука → 415; ни строк `transcripts`, ни `media`, ни файлов на шаре и во временном.
  - `test_restart_marks_running_as_interrupted` — строка `working` → `recover()` → `error`, «прервано перезапуском студии».
  - `test_one_running_per_user_and_history_wipe_takes_all` — вторая загрузка — 409 `busy`; `DELETE /api/history` уносит строки и файлы.
  - `tests/test_deploy.py`: `test_apache_lets_a_recording_through_only_on_its_route` — `LocationMatch "^/api/transcripts$"` с `LimitRequestBody` ≥ `transcripts.UPLOAD_MAX`, общий предел — прежний.
  - `tests/test_foundation.py`: версия 5, три таблицы есть; база версии 4 переходит, строки на месте.
- [ ] **Step 2: Run** — `.venv/Scripts/python -m pytest -q tests/test_transcripts.py tests/test_deploy.py tests/test_foundation.py` → FAIL.
- [ ] **Step 3: Implement** по Interfaces, по образцу `images.py` (задания в памяти + строка базы, база — только с цикла событий, файлы и ffmpeg — в потоке/подпроцессе). Шапка модуля — ход задания, ожидание карты, что где лежит, что в журнал (только id).
- [ ] **Step 4: Run** — те же → PASS; затем `.venv/Scripts/python -m pytest -q` → PASS.
- [ ] **Step 5: Commit** — «Transcribe recordings by speakers in the background».

---

### Task 6: Выгрузки расшифровки — txt, docx, srt

**Files:**
- Create: `app/exports.py`
- Modify: `app/transcripts.py` (`GET /api/transcripts/{id}/export?format=txt|docx|srt`)
- Test: `tests/test_exports.py` (новый)

**Interfaces:**
- `def txt(segments: list[dict]) -> str` — строки «Собеседник 1 · 00:12» и текст, пустая строка между репликами; без собеседников — «00:12» и текст.
- `def srt(segments: list[dict]) -> str` — номер, `00:00:00,900 --> 00:00:03,100`, «Собеседник 1: текст».
- `def docx(segments: list[dict], title: str) -> bytes` — минимальный OOXML (`zipfile`: `[Content_Types].xml`, `_rels/.rels`, `word/document.xml`; заголовок — имя записи, абзац на реплику, имя собеседника и время — жирным), без новых зависимостей; текст — XML-экранирован.
- Ответ выгрузки — `Content-Disposition` с именем «<имя записи без расширения>.txt|docx|srt» (`media._disposition`), типы `text/plain; charset=utf-8`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/x-subrip`.

- [ ] **Step 1: Write the failing tests** — `txt`/`srt` точными строками на двух репликах; `docx` — `zipfile` открывает, `document.xml` разбирается `xml.etree`, в нём «Собеседник 2» и текст с `<&>` экранированным; сегменты без собеседника — без «Собеседник»; выгрузка чужой и идущей расшифровки — 404 / 409.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — «Export transcripts as txt, docx and srt».

---

### Task 7: Вкладка «Расшифровка» (раскладка Б)

**Files:**
- Create: `app/static/js/transcribe.js`
- Modify: `app/static/index.html` (вкладка `#/transcribe` в рельсе, значок), `app/static/js/app.js` (`SECTIONS`, `HINTS`, маршрут `transcribe`), `app/static/css/app.css` (раскладка; нижние вкладки телефона — на пять разделов сразу, «Озвучка» добавит задача 10)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: API задач 5–6.
- Разметка: `#transcribe-add` (`input[type=file] accept="audio/*,video/*"`), ход загрузки `#transcribe-progress` (XHR, проценты), список `.tr-list` по дням, пункт `.tr-item[data-id][data-state]` (имя, длительность, состояние), выбранная — `.tr-view`: `.tr-meta` (длительность, собеседников, время), `#tr-copy`, `#tr-download` → меню `txt`/`docx`/`srt` (`?format=` → `<a download>`), `#tr-delete` (окно подтверждения), `audio.tr-audio` (исходная запись, `/api/media/<id>`), реплики `.tr-turn` («Собеседник N · мм:сс» + текст); идущая — состояние словами и `#tr-cancel`. Телефон: список — выезжающая панель, как список диалогов чата. Опрос идущей — раз в 3 с.

- [ ] **Step 1: Write the failing test** — `test_transcribe_uploads_and_reads_by_speakers` (390 и 1280): WAV 1 с тишины (`wave`) через `put_file('#transcribe-add input', …)`, шлюз отвечает двумя собеседниками, ожидание `.tr-item[data-state=done]`, реплики «Собеседник 1 · 00:00» и «Собеседник 2 · 00:03»; `#tr-copy` кладёт текст в буфер (подмена `navigator.clipboard.writeText`); «Скачать» → `docx` нажимает ссылку с `format=docx` (`CLICKS`); нет горизонтальной прокрутки; журнал браузера без ошибок. Живой студии в тесте нужен настоящий ffmpeg (есть на машине разработки) — тест `integration`, как все браузерные.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** по Interfaces. **Step 4: Run** — `-k "transcribe or layout or login_shell"` → PASS.
- [ ] **Step 5: Commit** — «Add the Transcription tab».

---

### Task 8: Голоса по образцу (служба и настройки «Озвучки»)

**Files:**
- Create: `app/voices.py`
- Modify: `app/main.py`, `app/history.py`, `app/static/js/settings.js` (раздел «Озвучка»: блок «Голоса»)
- Test: `tests/test_voices.py` (новый), `tests/test_ui_browser.py`

**Interfaces:**
- `POST /api/voices` — multipart `audio` (до 10 МиБ), `name` (1–64), `text` (1–500): `audio.to_wav(max_sec=15.5)`; короче 4.5 с — 422 `short` «образец — 5–15 секунд»; WAV — в media `audio/wav`; ответ — голос `{id, name, duration, created}` (текст образца не отдаётся списком — только в окне правки: `GET /api/voices/{id}` → `+ text`).
- `GET /api/voices`, `GET /api/voices/{id}`, `DELETE /api/voices/{id}` (204; файл — тоже).
- `voices.reference(user_id, voice_id) -> tuple[bytes, str]` — WAV и текст образца (для озвучки).
- Настройки → «Озвучка»: `#voice-add` → окно `#voice-dialog` (имя, текст образца, файл или «Записать» — `recordAudio(15)` из `mic.js`), список `.voice[data-id]` с «Удалить».

- [ ] **Step 1: Write the failing tests** — создание (fake_audio `to_wav` → 8.0 с) и список без текста; короткий (3.0 с) — 422 `short`; чужой голос — 404; удаление уносит файл; «Удалить всю историю» — голоса тоже; в журнале нет имени и текста. Браузер: `test_voice_is_added_in_settings` — файл WAV 6 с через `#voice-dialog`, голос появился в `.voice`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — «Keep voice samples for speech».

---

### Task 9: Озвучка — служба (фразы, склейка, mp3)

**Files:**
- Create: `app/speeches.py`
- Modify: `app/main.py` (роутер, `recover`), `app/history.py`
- Test: `tests/test_speeches.py` (новый)

**Interfaces:**
- `def phrases(text: str, limit: int = 220) -> list[str]` — по концам предложений (`.!?…` и переводы строк), короткие соседние — вместе до `limit`, длинное — по запятым, затем по пробелам; пустые — прочь.
- `POST /api/speeches` JSON `{"text": 1–2000, "connection_id"?, "model"?, "voice"}` — `voice`: id голоса (srvllm) или имя голоса сервиса (openai, до 64 знаков); модель — `sections.resolve(uid, "speech", …)`; 409 `busy`, 409 `no_model`, 404 `gone` (чужой голос). Ход: `free_card` (srvllm), фразы по одной через `speech.say` с повтором на `Busy` (detail «озвучиваю 3 из 7», «жду карту · м:сс»), части — во временные файлы, `audio.mp3_of` → media `audio/mpeg` (`media_id`), `duration` — `probe`; у openai текст до 4096 — одним запросом.
- `GET /api/speeches`, `GET /api/speeches/{id}`, `POST …/cancel`, `DELETE …`, `recover()`, `stop_user(uid)`.

- [ ] **Step 1: Write the failing tests** — `phrases` на трёх предложениях и на одном длинном без точек (все ≤ 220, склейка = исходник без лишних пробелов); озвучка двух фраз голосом srvllm: шлюз получил две `/speech/tts-qwen` с `ref_text` голоса, `mp3_of` — два файла, `done` с `media_id`; `busy` у второй фразы → повтор → `done`; отмена между фразами → `cancelled`, временных нет; внешний сервис — один запрос `/audio/speech` с `voice: "alloy"`; текст 2001 — 422; в журнале нет текста.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — «Speak text phrase by phrase in a cloned voice».

---

### Task 10: Вкладка «Озвучка»

**Files:**
- Create: `app/static/js/speech.js`
- Modify: `app/static/index.html` (вкладка `#/speech`), `app/static/js/app.js` (`SECTIONS`, `HINTS`, маршрут `speech`), `app/static/css/app.css`
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Раскладка — как «Расшифровка»: список `.sp-list` по дням (`.sp-item[data-id][data-state]` — начало текста, длительность), выбранная `.sp-view`: текст, `audio.sp-audio`, `#sp-download` (`?download=1`, mp3), `#sp-delete`; внизу `#speech-composer`: `textarea#speech-text` (до 2000, счётчик `#speech-count`) с `attachMic`, чипы `#chip-model` (отмеченные модели раздела) и `#chip-voice` (srvllm — голоса пользователя; openai — поле с подсказками `alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`), `#speech-go` «Озвучить». Нет голосов у srvllm — подсказка «добавьте голос в настройках раздела «Озвучка»» со ссылкой.

- [ ] **Step 1: Write the failing test** — `test_speech_speaks_with_a_voice_and_plays` (390 и 1280): голос заведён через API (WAV 6 с), текст «Привет. Как дела?», «Озвучить» → `.sp-item[data-state=done]`, `audio.sp-audio` с `src` `/api/media/…`, «Скачать» — ссылка `?download=1`; без голосов — подсказка; нет горизонтальной прокрутки.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — «Add the Speech tab».

---

### Task 11: Документы, живая приёмка, выкладка

**Files:**
- Modify: `README.md` (голосовой ввод, «Расшифровка», «Озвучка», голоса, пределы, ffmpeg на debian), `CLAUDE.md` (устройство: `audio`, `speech`, `dictation`, `transcripts`, `exports`, `voices`, `speeches`; временный каталог; `LocationMatch` загрузки записи; ловушка: освобождения `asr`/`tts-qwen` нет, перед речью — release картинок и gemma), `tests/test_studio_upload_live.py` (приёмка: голосовой ввод фразы WAV, расшифровка 10 с, озвучка одной фразы голосом из образца — с уборкой своего), `local-config/wiki/projects/studio/architecture-speech.md` (≥ 2 `[[wikilinks]]`)

- [ ] **Step 1: Документы** — по списку.
- [ ] **Step 2: Быстрый набор** — `.venv/Scripts/python -m pytest -q` → PASS, ≤ 60 с.
- [ ] **Step 3: Integration** — `.venv/Scripts/python -m pytest -q -m integration` → PASS (браузер, ffmpeg).
- [ ] **Step 4: Commit** — «Document speech: dictation, transcription and voices».
- [ ] **Step 5: Копия базы и выкладка** — `studio-backup.service` вручную, затем `bash deploy/deploy.sh`; `user_version` — 5; `curl -sI` загрузки на 30 МиБ без входа → 401 (Apache пропустил тело до службы), смоук `/health`.
- [ ] **Step 6: Живая приёмка** — с разрешения владельца (вход учёткой администратора — см. `CLAUDE.md`, «пароль восстановления»): `.venv/Scripts/python -m pytest -q -m manual`.
- [ ] **Step 7: Владелец** — критерии успеха 2, 3, 8 спеки: запись звонка 15 мин, диктовка в фото и чат, голос по образцу и абзац в mp3.
