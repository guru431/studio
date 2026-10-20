# Подключения по разделам — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** у каждого из шести разделов (чат, фото, видео, расшифровка, озвучка, голосовой ввод) — свои подключения, отмеченные модели и одна модель по умолчанию; внешние адреса по имени (https) в списке разрешённых; настройки «вариант А»; чипы моделей разделов из отмеченного.

**Architecture:** второй из трёх планов спеки (этап 3 раздела 9). Связи «раздел ↔ подключение» и умолчания — новые таблицы `section_links`, `section_defaults` (шаг базы 4); логика — новый модуль `app/sections.py`, им пользуются `connections`, `chat`, `images`, `videos`, `profile`, `admin`. Граница SSRF для имён — в `app/upstream.py`: имя разрешается при каждом запросе, все адреса публичные, запрос идёт на проверенный IP с SNI и `Host` имени. Фронтенд — новый `app/static/js/settings.js` вместо `profile.js`.

**Tech Stack:** Python 3.13/3.14, FastAPI, SQLite (JSON1), httpx 0.28 / httpcore 1.0 (`sni_hostname`); ванильный JS (ES-модули); pytest, браузерные сценарии `tests/browser.py`.

**Spec:** [docs/superpowers/specs/2026-10-20-studio-sections-voice-design.md](../specs/2026-10-20-studio-sections-voice-design.md) — решения 4–8, 16; разделы 6.1, 6.2. Разделы «Расшифровка», «Озвучка», «Голосовой ввод» здесь получают только хранение и настройки; их работа — в плане «Речь» (`2026-10-20-studio-speech.md`).

## Global Constraints

- Шаг базы только добавляет (`db.STEPS`), `SCHEMA` — сразу последняя версия; старые столбцы `connections.sections`, `users.default_connection_id`, `users.default_model` остаются и держатся согласованными (откат `install.sh` на прежний код работает с новой базой).
- Журнал — только id: имена, адреса, модели, токены не пишутся.
- Разделы: `chat`, `photo`, `video`, `transcribe`, `speech`, `dictation`; подписи — Чат, Фото, Видео, Расшифровка, Озвучка, Голосовой ввод.
- Виды по разделам: `srvllm` — все шесть; `openai` — все, кроме `video` («видео — только через шлюз srvllm»).
- Фото, видео, расшифровка, озвучка, голосовой ввод — только через сервер: подключение вне списка разрешённых в этих разделах не сохраняется — «попросите администратора внести адрес в список разрешённых». Чат — как сейчас.
- Встроенный перечень srvllm (короткие имена студии; первое — по умолчанию): фото `lustify`, `flux`; видео `wan`; расшифровка `whisperx`; озвучка `qwen3-tts`; голосовой ввод `gigaam`, `whisperx`. Чат srvllm — из `GET /v1/models` шлюза; при подключении отмечается `local-gemma-abl` (`manage.DEFAULT_MODEL`).
- Имя в списке разрешённых — только для https; все адреса имени — `ipaddress.ip_address(a).is_global`; соединение — с проверенным адресом (SNI и `Host` — имя); переадресации не выполняются. IP-записи — как сейчас, с правилом путей `connections.PROXIED_PATHS`; адрес с именем — с любым путём.
- Модель — до 200 знаков, отмеченных у связи — до 200.
- Тест-команды: быстрый — `.venv/Scripts/python -m pytest -q`; точечный — `.venv/Scripts/python -m pytest -q tests/<файл>.py -k <имя>`; браузер — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k <имя>`.
- Коммит — сообщение файлом (`git commit -F`), в конце `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; без пуша.

## Review Focus

1. **Имя, которое разрешается во внутренний адрес** (или в публичный и частный сразу; или меняет ответ DNS между проверкой и запросом) → отказ до соединения / запрос уходит на проверенный адрес — тесты в задаче 1.
2. **Откат на прежний код после шага 4** → прежний код видит те же подключения в чате, фото и видео и ту же модель чата по умолчанию — тест в задаче 2.
3. **Снята галочка с модели по умолчанию или убрано подключение по умолчанию** → умолчание переходит к первой отмеченной модели первого подключения раздела, нет такой — раздел показывает подсказку — тест в задаче 3.
4. **Чужой `connection_id`** в запросах разделов, фото, видео, чата → 404 `gone`, ничего о чужом подключении в ответе — тесты в задачах 3 и 4.
5. **Удалено подключение, которое участвует в нескольких разделах** → связи и умолчания уходят каскадом, старые диалоги открываются, новый ход — понятный отказ — тест в задаче 2.

---

### Task 1: Имена в списке разрешённых (https, проверенный адрес)

**Files:**
- Modify: `app/upstream.py` (`parse_entry`, `_host`, `is_allowed`, `_prepare` → async, новые `RESOLVE`, `named`), `app/connections.py:168-188` (`check_base_url`), `app/admin.py:153-166` (текст отказа), `app/static/js/admin.js:154-155,210-211` (подсказка и пояснение списка)
- Test: `tests/test_connections.py` (заменить `test_allowlist_takes_ip_literals_only`)

**Interfaces:**
- Produces: `upstream.parse_entry(text) -> str` принимает и `имя:порт` (DNS-имя в нижнем регистре, ASCII/IDNA, с точкой, не из одних цифр); `upstream.named(url: httpx.URL) -> bool`; `upstream.RESOLVE: Callable[[str, int], Awaitable[list[str]]]` (по умолчанию `loop.getaddrinfo(host, port, type=SOCK_STREAM)` → адреса); `request`/`stream` — прежние сигнатуры.
- Отказ имени при запросе — `UpstreamRefused("подключение <id>: имя разрешается во внутренний адрес")`.

- [ ] **Step 1: Write the failing tests** (`tests/test_connections.py`)

```python
def named_list(studio, *entries):
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", (json.dumps(list(entries)),))

def test_allowlist_takes_names_for_https_only(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    r = studio.c.put("/api/admin/allowlist", json={"entries": ["API.OpenAI.com:443", "10.0.0.5:4000"]})
    assert r.status_code == 200 and r.json()["entries"] == ["api.openai.com:443", "10.0.0.5:4000"]
    for bad in ["localhost:4000", "llm:80", "1.2.3:80", "-x.test:443", "a..b:443"]:
        r = studio.c.put("/api/admin/allowlist", json={"entries": [bad]})
        assert r.status_code == 422 and code(r) == "entry", bad
    assert upstream.is_allowed("https://api.openai.com/v1") and upstream.is_allowed("https://API.openai.com:443/x")
    assert not upstream.is_allowed("http://api.openai.com:443/v1")          # имя — только https

def test_named_request_goes_to_the_checked_address(studio, monkeypatch):
    named_list(studio, "api.cloud.test:443")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(studio, base_url="https://api.cloud.test/v1", token="cloud-key-1")
    seen = []
    studio.gw.reply = lambda q: (seen.append(q), httpx.Response(200, json={"data": []}))[1]
    asyncio.run(upstream.request(conn_of(cid), "GET", "https://api.cloud.test/v1/models"))
    q, = seen
    assert q.url.host == "93.184.216.34" and q.url.path == "/v1/models" and q.url.scheme == "https"
    assert q.headers["host"] == "api.cloud.test" and q.extensions["sni_hostname"] == "api.cloud.test"
    assert q.headers["authorization"] == "Bearer cloud-key-1"

@pytest.mark.parametrize("addrs", [["10.0.0.7"], ["93.184.216.34", "127.0.0.1"], ["100.64.1.1"], ["169.254.1.1"],
                                   ["::1"], ["fd00::1"], []])
def test_named_host_that_resolves_inside_is_refused_before_any_io(studio, monkeypatch, addrs):
    named_list(studio, "api.cloud.test:443")
    async def resolve(host, port): return addrs
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(studio, base_url="https://api.cloud.test/v1", token="cloud-key-1")
    with pytest.raises(upstream.UpstreamRefused):
        asyncio.run(upstream.request(conn_of(cid), "GET", "https://api.cloud.test/v1/models"))
    with pytest.raises(upstream.UpstreamRefused):
        asyncio.run(streamed(conn_of(cid), "https://api.cloud.test/v1/models"))
    assert studio.gw.requests == []

def test_named_address_takes_any_path_but_ip_keeps_the_rule(studio):
    named_list(studio, "openrouter.ai:443", "10.77.1.15:4000")
    make_user(studio, "owner"); signin(studio, "owner")
    assert post(studio, base_url="https://openrouter.ai/api/v1").status_code == 201
    r = post(studio, base_url="http://10.77.1.15:4000/api/v1")
    assert r.status_code == 422 and code(r) == "path"
```

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m pytest -q tests/test_connections.py -k "names or named"` → FAIL (`parse_entry` отказывает именам).

- [ ] **Step 3: Implement**
  - `upstream._host`: IP — как сейчас; иначе имя: `host.encode("idna").decode()` в нижнем регистре, `re.fullmatch(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]", name)`, иначе ValueError. Докстринг модуля и `_host` — правило имён (только https, проверка адресов на каждом запросе, соединение с проверенным адресом).
  - `is_allowed`: запись по имени совпадает только у `https`.
  - `async def _prepare(conn, url, kw)`: для имени — `addrs = await RESOLVE(host, port)`; пусто или хоть один не `is_global` → `UpstreamRefused`; иначе `parsed.copy_with(host=addrs[0])`, заголовок `Host` — имя (с портом, если не 443), `kw["extensions"] = {**kw.get("extensions", {}), "sni_hostname": host}`. `request` и `stream` — `await _prepare(...)`.
  - `connections.check_base_url`: правило `PROXIED_PATHS` — только если хост адреса — IP (`not upstream.named(url)`).
  - `admin.put_allowlist`: отказ — «… — не IP:порт и не имя:порт (имя — только для https)». `admin.js`: placeholder «IP:порт или имя:порт, например 10.0.0.5:4000 или api.openai.com:443», пояснение — «Имя — только для https: студия при каждом запросе проверяет, что имя ведёт в интернет, а не во внутреннюю сеть».

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest -q tests/test_connections.py tests/test_profile_admin.py tests/test_chat.py` → PASS (прежние проверки IP-записей и переадресаций — без изменений).

- [ ] **Step 5: Commit** — «Let the allowlist take https names, pinned to a checked public address».

---

### Task 2: Связи разделов и умолчания (база, `sections.py`, совместимость подключений)

**Files:**
- Create: `app/sections.py`
- Modify: `app/db.py` (`VERSION = 4`, `SCHEMA`, `STEPS[4]`), `app/connections.py` (`KINDS`, `Connection.sections` из связей, `create`/`patch`/`delete`, `capabilities` → в `sections`), `app/profile.py` (`_me`: `capabilities` и `default_*` из `sections`; `MePatch` без `default_*`), `app/manage.py:85-106` (`_ensure_gateway` — связи во всех шести), `app/chat.py:114-127` (`_chat_connection`, `_default_connection` — по связям чата)
- Test: `tests/test_sections.py` (новый), `tests/test_foundation.py`, `tests/test_connections.py:178-188`, `tests/test_profile_admin.py` (`test_profile_defaults_take_only_ones_own_connection` → через разделы), `tests/test_deploy.py` (если сверяет `create-admin`)

**Interfaces:**
- Produces (`app/sections.py`):
  - `SECTIONS: tuple[str, ...]`, `NAMES: dict[str, str]`, `SERVER_ONLY: set[str]`, `ALLOWED: dict[str, set[str]]`, `CATALOG: dict[str, list[str]]` (Global Constraints), `CHAT_INITIAL = ["local-gemma-abl"]`.
  - `@dataclass(frozen=True) class Link: section: str; connection: Connection; models: tuple[str, ...]`
  - `links(user_id: int, section: str) -> list[Link]` — по `created`, затем `rowid`.
  - `sections_of(user_id: int, conn_id: int) -> list[str]` — в порядке `SECTIONS`.
  - `default(user_id: int, section: str) -> tuple[Connection, str] | None`.
  - `link(user_id, section, conn_id) -> None` — 404 `gone` чужому/неизвестному; 422 `kind` («видео — только через шлюз srvllm»); 422 `server` для `SERVER_ONLY` вне списка; уже есть — ничего. Отмеченные при создании: srvllm — `CATALOG[section]` (чат — `CHAT_INITIAL`), openai — `[]`.
  - `unlink(user_id, section, conn_id) -> None`; `set_models(user_id, section, conn_id, models: list[str]) -> None` (422 `model`: пусто/длиннее 200/повтор/больше 200; srvllm не-чат — только из `CATALOG`); `set_default(user_id, section, conn_id, model) -> None` (422 `model` — модель не отмечена).
  - `resolve(user_id, section, connection_id: int | None, model: str | None) -> tuple[Connection, str]`: оба None — умолчание; только модель — первая связь (умолчания — первой), где она отмечена; пара — связь с отмеченной моделью. Нет умолчания — 409 `{"code": "no_model", "message": "выберите модель в настройках раздела «<Имя>»"}`; пары нет — 422 `{"code": "model", "message": "этой модели нет среди отмеченных в настройках раздела"}`; чужое подключение — 404 `gone`; `SERVER_ONLY` вне списка — 409 `server`.
  - `capabilities(user_id) -> dict[str, bool]` — по всем шести: есть умолчание (а для `SERVER_ONLY` — его подключение `proxied`).
  - После каждой правки — `_settle(user_id, section)` (умолчание неверно → первая отмеченная модель первой связи или нет) и `_sync_legacy(user_id)` (`connections.sections` = связи ∩ {chat, photo, video} как JSON-список по алфавиту; `users.default_connection_id/default_model` = умолчание чата или NULL). Всё — в одной транзакции правки.
- `connections.KINDS` → `{"openai": ALLOWED["openai"], "srvllm": ALLOWED["srvllm"]}`; `ConnIn.sections` / `ConnPatch.sections` — необязательные (пусто — можно): POST связывает, PATCH приводит связи к списку. `out(conn)["sections"]` — из связей. PATCH, после которого подключение вне списка, а связано с `SERVER_ONLY`, — 422 `server`.

- [ ] **Step 1: Write the failing tests** — `tests/test_sections.py` (хелперы — `from conftest import add_connection, add_gateway, make_user, signin`, `from test_connections import conn_of`):

```python
def test_gateway_joins_every_section_with_its_catalog(studio):
    cid = add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    assert sections.sections_of(uid, cid) == list(sections.SECTIONS)
    assert [l.models for l in sections.links(uid, "photo")] == [("lustify", "flux")]
    assert sections.default(uid, "photo")[1] == "lustify" and sections.default(uid, "chat")[1] == "local-gemma-abl"
    assert sections.capabilities(uid) == dict.fromkeys(sections.SECTIONS, True)

def test_openai_cannot_join_video_and_outside_the_list_only_chat(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    cloud = add_connection(studio, base_url="https://cloud.test/v1", sections=["chat"])
    for section, code_ in (("video", "kind"), ("photo", "server"), ("dictation", "server")):
        with pytest.raises(HTTPException) as e:
            sections.link(uid, section, cloud)
        assert e.value.detail["code"] == code_, section

def test_unchecking_the_default_moves_it_and_empty_section_has_no_default(studio):
    cid = add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    sections.set_models(uid, "photo", cid, ["flux"])
    assert sections.default(uid, "photo")[1] == "flux"
    sections.unlink(uid, "photo", cid)
    assert sections.default(uid, "photo") is None and sections.capabilities(uid)["photo"] is False
    with pytest.raises(HTTPException) as e:
        sections.resolve(uid, "photo", None, None)
    assert e.value.status_code == 409 and "настройках раздела «Фото»" in e.value.detail["message"]

def test_legacy_columns_follow_the_links(studio):
    """Прежний код (откат) читает connections.sections и users.default_*."""
    cid = add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    sections.unlink(uid, "video", cid); sections.unlink(uid, "dictation", cid)
    assert json.loads(state.DB.execute("SELECT sections FROM connections").fetchone()[0]) == ["chat", "photo"]
    assert state.DB.execute("SELECT default_connection_id, default_model FROM users").fetchone()[:] == (cid, "local-gemma-abl")
    sections.unlink(uid, "chat", cid)
    assert state.DB.execute("SELECT default_connection_id, default_model FROM users").fetchone()[:] == (None, None)

def test_deleting_a_connection_drops_its_links_and_defaults(studio):
    cid = add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    chat_id = studio.c.post("/api/chats", json={}).json()["id"]
    assert studio.c.delete(f"/api/connections/{cid}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM section_links").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM section_defaults").fetchone()[0] == 0
    assert studio.c.get(f"/api/chats/{chat_id}").status_code == 200
    r = studio.c.post(f"/api/chats/{chat_id}/turns", json={"text": "hi"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
```

`tests/test_foundation.py`: `db.VERSION == 4`; прежние тесты версий 1 и 2 дополнительно `DROP TABLE section_links; DROP TABLE section_defaults;`; новый `test_migrate_links_a_version_3_base_to_every_section` — база версии 3 с пользователем (`default_connection_id` = его srvllm, `default_model = 'gemma4-abl'`), srvllm (`sections = '["chat","photo","video"]'`) и openai (`'["chat"]'`) → после `migrate`: srvllm во всех шести (`photo` — `["lustify","flux"]`, `chat` — `["gemma4-abl"]`), openai только в чате с `[]`; умолчания: chat — (srvllm, `gemma4-abl`), photo `lustify`, video `wan`, transcribe `whisperx`, speech `qwen3-tts`, dictation `gigaam`.

`tests/test_connections.py:180-188`: набор отказов — `("openai", ["video"])`, `("srvllm", ["chat", "music"])`, `("nope", ["chat"])`; `("openai", [])` теперь 201.

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m pytest -q tests/test_sections.py tests/test_foundation.py` → FAIL (`sections` нет, версия 3).

- [ ] **Step 3: `db.py`** — версия 4; таблицы:
  `section_links(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, section TEXT NOT NULL, connection_id INTEGER NOT NULL REFERENCES connections ON DELETE CASCADE, models TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (user_id, section, connection_id))`,
  `section_defaults(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, section TEXT NOT NULL, connection_id INTEGER NOT NULL REFERENCES connections ON DELETE CASCADE, model TEXT NOT NULL, PRIMARY KEY (user_id, section))`.
  `STEPS[4]` — те же `CREATE TABLE` и перенос одним SQL (`INSERT … SELECT`, JSON1): srvllm — шесть строк на подключение (чат — `json_array(COALESCE(CASE WHEN u.default_connection_id = c.id THEN u.default_model END, 'local-gemma-abl'))`, прочие — перечень); openai с `"chat"` в `sections` — строка чата (`json_array(u.default_model)`, если это умолчание пользователя, иначе `'[]'`); умолчания — чат: подключение `users.default_connection_id`, если у него непустая связь чата, иначе наименьший id с непустой связью, модель — первая отмеченная; прочие разделы — наименьший id связи, модель — первая в перечне раздела.

- [ ] **Step 4: `sections.py`** по Interfaces. Шапка модуля: раздел → подключения → отмеченные модели → одно умолчание; встроенный перечень srvllm; кто ходит (`SERVER_ONLY`); прежние столбцы держатся согласованными для отката; журнал — только id. Только синхронное чтение и запись базы.

- [ ] **Step 5: Подключить** — `connections`: `_from_row` берёт разделы из `section_links` (`list_for` — одним запросом с `group_concat`), `create` после вставки — `sections.link` для каждого из `body.sections` в той же транзакции, `patch` — привести связи к `body.sections`, если он прислан, `delete` — строки уходят каскадом, затем `_sync_legacy`; `capabilities` переезжает в `sections`, `profile._me` берёт его и умолчание чата оттуда; `manage._ensure_gateway` — `sections.link` во всех шести на своём соединении (функциям `sections` — необязательный параметр `db=state.DB`); `chat._chat_connection` — подключение со связью чата, `_default_connection` — `sections.default(uid, "chat")`, нет — 409 `no_connection` «выберите модель в настройках раздела «Чат»».

- [ ] **Step 6: Run tests** — `.venv/Scripts/python -m pytest -q` → PASS (быстрый набор целиком: связи трогают чат, фото, видео и панель).

- [ ] **Step 7: Commit** — «Keep connections, checked models and a default per section».

---

### Task 3: API разделов (профиль и панель)

**Files:**
- Modify: `app/sections.py` (роутер `/api/sections`), `app/admin.py` (те же действия для `/api/admin/users/{uid}/sections…`), `app/main.py` (`include_router`)
- Test: `tests/test_sections.py`, `tests/test_auth.py` (маршруты под сессией — тест обхода маршрутов их увидит сам)

**Interfaces:**
- Produces (у профиля, у панели — с префиксом `/api/admin/users/{uid}`):
  - `GET /api/sections` → `{"sections": [{"id", "name", "links": [{"connection": connections.out(c), "models": [...], "catalog": [...] | null}], "default": {"connection_id", "model"} | null}]}` в порядке `SECTIONS`; `catalog` — перечень у srvllm не в чате.
  - `POST /api/sections/{section}/links` `{"connection_id"}` → 200, объект раздела.
  - `DELETE /api/sections/{section}/links/{conn_id}` → 204.
  - `PUT /api/sections/{section}/links/{conn_id}` `{"models": [...]}` → объект раздела.
  - `PUT /api/sections/{section}/default` `{"connection_id", "model"}` → объект раздела.
  - `GET /api/sections/{section}/links/{conn_id}/found` → `{"items": [...]}`: srvllm не-чат — перечень; srvllm чат и openai через сервер — `GET …/models` (ids); прямое — 409 `direct` (браузер берёт сам, как сейчас).
  - Неизвестный раздел — 404 `{"code": "section", "message": "такого раздела нет"}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_sections.py`)

```python
def test_sections_api_round_trip(studio):
    cid = add_gateway(studio)
    body = studio.c.get("/api/sections").json()["sections"]
    assert [s["id"] for s in body] == list(sections.SECTIONS)
    photo = body[1]
    assert photo["links"][0]["catalog"] == ["lustify", "flux"] and photo["default"] == {"connection_id": cid, "model": "lustify"}
    r = studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["lustify"]})
    assert r.status_code == 200 and r.json()["links"][0]["models"] == ["lustify"]
    assert studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["sdxl"]}).json()["detail"]["code"] == "model"
    assert studio.c.put("/api/sections/photo/default", json={"connection_id": cid, "model": "flux"}).status_code == 422
    found = studio.c.get(f"/api/sections/chat/links/{cid}/found").json()["items"]
    assert "local-gemma-abl" in found
    assert studio.c.get("/api/sections/music").status_code in (404, 405)

def test_foreign_connection_is_gone_everywhere(studio):
    mine = add_gateway(studio)
    make_user(studio, "bob"); bob_conn = state.DB.execute(
        "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created) "
        "SELECT id, 'b', 'srvllm', 'http://10.77.1.15:4000', x'00', '[]', 0 FROM users WHERE login = 'bob'").lastrowid
    for method, path, body in (("post", "/api/sections/photo/links", {"connection_id": bob_conn}),
                               ("put", f"/api/sections/photo/links/{bob_conn}", {"models": ["lustify"]}),
                               ("put", "/api/sections/photo/default", {"connection_id": bob_conn, "model": "lustify"}),
                               ("get", f"/api/sections/chat/links/{bob_conn}/found", None)):
        r = getattr(studio.c, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone", path
        assert "192.168" not in r.text

def test_admin_edits_a_users_sections(studio):
    add_gateway(studio)                                   # owner — администратор
    bob = make_user(studio, "bob")
    cid = studio.c.post(f"/api/admin/users/{bob}/connections", json={"name": "gw", "kind": "srvllm",
        "base_url": "http://10.77.1.15:4000", "token": "k", "sections": ["photo"]}).json()["id"]
    r = studio.c.put(f"/api/admin/users/{bob}/sections/photo/links/{cid}", json={"models": ["flux"]})
    assert r.status_code == 200 and r.json()["default"] == {"connection_id": cid, "model": "flux"}

def test_section_changes_log_only_ids(studio, caplog):
    caplog.set_level(logging.DEBUG)
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["flux"]})
    assert "flux" not in caplog.text and "192.168" not in caplog.text
```

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m pytest -q tests/test_sections.py -k "api or foreign or admin_edits or log"` → FAIL (404 маршрутов).

- [ ] **Step 3: Implement** — роутер в `sections.py` (тела — pydantic: `LinkIn{connection_id: int}`, `ModelsIn{models: list[str] = Field(max_length=200)}`, `DefaultIn{connection_id: int, model: str = Field(max_length=200)}`); функции-обработчики берут `user_id` параметром, профиль — из сессии, панель (`admin.py`) — `uid` пути после `_user_out(uid)`. `found` — `upstream.request(conn, "GET", connections.models_url(conn), timeout=10)`, отказ — 502 `upstream` словами `upstream.gateway_error`. Журнал: «пользователь %d: раздел %s, подключение %d — …» (имя раздела — из `SECTIONS`, не из запроса).

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest -q tests/test_sections.py tests/test_auth.py tests/test_profile_admin.py` → PASS.

- [ ] **Step 5: Commit** — «Serve sections: links, checked models, defaults and found models».

---

### Task 4: Чат, фото и видео берут подключение и модель из раздела

**Files:**
- Modify: `app/chat.py:474-483` (`create_chat` — `sections.resolve(uid, "chat", body.connection_id, body.model)`; `patch_chat` — та же проверка пары), `app/images.py` (`JobIn.model: str` до 200, `connection_id: int | None`; `create_job` — `sections.resolve(uid, "photo", …)`; `_photo_connection` → `_srvllm_photo(uid)` для «Описать» и «Освободить карту»), `app/videos.py` (`VideoIn.connection_id`, `model`; `_video_connection` → `sections.resolve(uid, "video", …)`)
- Test: `tests/test_images.py`, `tests/test_videos.py`, `tests/test_chat.py`

**Interfaces:**
- Consumes: `sections.resolve`, `sections.links` (задача 2).
- Produces: `POST /api/images` принимает `model` (короткое имя srvllm или id внешнего сервиса) и `connection_id`; `Job.model` — короткое имя, `Job.gateway_model` — id у шлюза (`MODELS[...]`) или id внешнего; `POST /api/videos` — необязательные `connection_id`, `model` (`wan` → `VIDEO_MODEL`). Отказ без умолчания — 409 `no_model` (задача 2).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_images.py
def test_unchecked_model_is_refused_and_default_is_used(studio):
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["flux"]})
    assert start(studio, model="lustify").status_code == 422
    job = wait_state(studio, start(studio).json()["id"], {"done"})
    assert job["model"] == "flux" and studio.gw.generations[-1][0]["model"] == FLUX

def test_photo_without_a_checked_model_points_to_settings(studio):
    cid = add_gateway(studio)
    studio.c.delete(f"/api/sections/photo/links/{cid}")
    r = start(studio)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"

# tests/test_chat.py
def test_new_chat_takes_the_sections_default(studio):
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{cid}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": "gemma4-abl"})
    assert studio.c.post("/api/chats", json={}).json()["model"] == "gemma4-abl"
    assert studio.c.post("/api/chats", json={"connection_id": cid, "model": "other"}).status_code == 422

# tests/test_videos.py
def test_video_needs_a_linked_gateway(studio):
    cid = add_gateway(studio)
    studio.c.delete(f"/api/sections/video/links/{cid}")
    r = create_video(studio, prompt=PROMPT)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"
```

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m pytest -q tests/test_images.py tests/test_videos.py tests/test_chat.py -k "checked or sections or linked"` → FAIL.

- [ ] **Step 3: Implement** — по Interfaces. Повтор картинки (только `model`, без `connection_id`) находит связь, где модель отмечена. Существующие диалоги: ход — как сейчас (пара диалога не перепроверяется), подключение удалено — 404 `gone`.

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest -q tests/test_images.py tests/test_videos.py tests/test_chat.py tests/test_chains.py` → PASS.

- [ ] **Step 5: Commit** — «Take connection and model from the section in chat, photo and video».

---

### Task 5: Фото через внешний сервис (`/images/generations`, `/images/edits`)

**Files:**
- Modify: `app/images.py` (`generate` — ветка `conn.kind == "openai"`), шапка модуля
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes: `Job.conn.kind`, `Job.gateway_model` (задача 4).
- Внешний запрос: генерация — `POST <base>/images/generations` JSON `{"model", "prompt", "n": 1, "size", "response_format": "b64_json"}`; правка — `POST <base>/images/edits` multipart `image` (PNG), `model`, `prompt`, `n`, `size`, `response_format`. Размеры студии → `portrait 1024x1536`, `square 1024x1024`, `landscape 1536x1024`, `original` — без `size`. Без `seed` и `strength`. Ответ — только `data[0].b64_json`; `url` вместо картинки — ошибка «сервис вернул ссылку, а не картинку» (по ссылке сервер не ходит). Кисть, «Описать», «Освободить карту» и release перед запросом — только srvllm: маска при внешнем подключении — 422 `{"code": "brush", "message": "правка кистью — только через шлюз srvllm"}`.

- [ ] **Step 1: Write the failing tests**

```python
def cloud_photo(studio, monkeypatch):
    state.DB.execute("UPDATE settings SET value = '[\"10.77.1.15:4000\", \"img.cloud.test:443\"]' WHERE key = 'allowlist'")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(studio, base_url="https://img.cloud.test/v1", token="cloud-key-1", sections=["photo"])
    studio.c.put(f"/api/sections/photo/links/{cid}", json={"models": ["gpt-image-1"]})
    return cid

def test_external_photo_generation_takes_b64_only(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    seen = []
    def reply(q):
        seen.append(q)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_REAL).decode()}]})
    studio.gw.on["/v1/images/generations"] = lambda q: asyncio.sleep(0, reply(q))
    job = wait_state(studio, start(studio, model="gpt-image-1", connection_id=cid, size="portrait").json()["id"], {"done"})
    body = json.loads(seen[0].content)
    assert body == {"model": "gpt-image-1", "prompt": PROMPT, "n": 1, "size": "1024x1536", "response_format": "b64_json"}
    assert seen[0].headers["host"] == "img.cloud.test" and job["media_id"]
    assert studio.gw.releases == []                       # release — только шлюзу

def test_external_photo_url_answer_is_an_error_and_not_fetched(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    studio.gw.on["/v1/images/generations"] = lambda q: asyncio.sleep(0, httpx.Response(200, json={"data": [{"url": "http://10.0.0.1/x.png"}]}))
    job = wait_state(studio, start(studio, model="gpt-image-1", connection_id=cid).json()["id"], {"error"})
    assert "ссылку" in job["detail"] and not any(q.url.host == "10.0.0.1" for q in studio.gw.requests)

def test_brush_is_srvllm_only(studio, monkeypatch):
    add_gateway(studio); cid = cloud_photo(studio, monkeypatch)
    mask = Image.new("L", (48, 32), 0); mask.paste(255, (0, 0, 8, 8))
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "original", "model": "gpt-image-1", "connection_id": str(cid)},
                      files={"image": ("p.png", photo(), "image/png"), "mask": ("m.png", png(mask), "image/png")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "brush"
```

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m pytest -q tests/test_images.py -k external_photo or brush_is` → FAIL.

- [ ] **Step 3: Implement** — `generate`: srvllm — как сейчас; openai — запрос по Interfaces через `upstream.request` (таймаут `GENERATE_TIMEOUT_SEC`), отказ — словами `upstream.gateway_error`. Правило кисти — в `create_job` до записи файлов. Шапка модуля — пункт о внешнем сервисе.

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest -q tests/test_images.py` → PASS.

- [ ] **Step 5: Commit** — «Generate and edit photos through an external OpenAI-style service».

---

### Task 6: Настройки «вариант А» (`settings.js`) и панель администратора

**Files:**
- Create: `app/static/js/settings.js`
- Delete: `app/static/js/profile.js` (пароль, история, `modelsOf`, `CORS` — переезжают в `settings.js`)
- Modify: `app/static/js/app.js` (маршрут `#/settings[/<раздел|other>]`, `#/profile` → `#/settings`, подсказки разделов), `app/static/index.html` (пункт меню «Настройки», `href="#/settings"`), `app/static/js/connection-form.js` (форма без галочек разделов; `SECTION_NAMES` — шесть), `app/static/js/admin.js` («Подключения» → окно с настройками пользователя), `app/static/js/chat.js:19` (импорт `CORS` из `settings.js`), `app/static/css/app.css`
- Test: `tests/test_ui_browser.py`, `tests/test_static.py`

**Interfaces:**
- Consumes: API задачи 3.
- Produces: `export function settingsView(host: HTMLElement, { sections: string, connections: string, section: string | null, onNavigate?: (id) => void }): HTMLElement` — `sections` / `connections` — базы API (`/api/sections`, `/api/connections` или панельные); `export async function modelsOf(conn)`, `export const CORS` (как сейчас в `profile.js`).
- Разметка (селекторы тестов): `.settings-nav a[data-section]` (шесть разделов и `other`), `.section-panel[data-section]`, карточка `.link[data-conn]` с `.link-name`, `.link-url`, `.link-hint`, `.link-refresh`, `.link-menu` → `.link-edit`, `.link-remove`, `.link-delete`; модели — `.model-search` (при > 8 моделях), `input.model-check[value]`, `input.model-default[value]` (радио, имя — `default-<раздел>`), `.model-missing` («не найдена»); `#link-add` → окно `#link-dialog` (`select[name=existing]` с пунктом «Новый адрес…» и форма подключения), `#link-save`; «Прочее» — `#pw-*`, `#password-save`, `#history-wipe` (как сейчас).
- Поведение: галочка и радио сохраняются сразу (`PUT …/links/<id>`, `PUT …/default`), отказ — словами в строке статуса раздела; «Обновить модели» — `found` (прямое подключение — `modelsOf` из браузера), найденные — все, отмеченные сверху, отмеченная, но не найденная — с `.model-missing`; удаление подключения — окно «Подключение участвует в разделах: …». Телефон (≤ 760 px): `#/settings` — только список разделов, `#/settings/<id>` — экран раздела со ссылкой «‹ Настройки».
- Подсказка раздела без модели (`app.js`): «Выберите модель в настройках раздела «<Имя>»», кнопка — `#/settings/<раздел>`.

- [ ] **Step 1: Write the failing tests** (`tests/test_ui_browser.py`)

```python
@pytest.mark.parametrize("width", [390, 1280])
def test_settings_photo_unchecks_flux_and_the_chip_keeps_lustify(browser, live, width):
    """Критерий успеха 1 спеки."""
    login = f"sect{width}"
    live.add_user(login)
    panel = ".section-panel[data-section=photo]"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        ("eval", "go", "location.hash = '#/settings/photo'"), ("wait", f"document.querySelector('{panel} .link')"),
        ("click", f"{panel} .link-refresh"),
        ("wait", f"document.querySelectorAll('{panel} input.model-check').length === 2"),
        ("click", f"{panel} input.model-check[value=flux]"),
        ("wait", f"!document.querySelector('{panel} input.model-check[value=flux]').checked"), ("idle",),
        *go("photo"), ("wait", FEED_READY), ("click", "#chip-model"),
        ("eval", "options", "[...document.querySelectorAll('#chip-model-menu .chip-option')].map(o => o.textContent)")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert r["values"]["options"] == ["Lustify"]
    assert not problems(r), problems(r)

def test_settings_menu_and_old_profile_route(browser, live):
    live.add_user("menuer")
    r = browser.drive(live.url + "/", [*signin("menuer"), ("wait", READY),
        ("click", "#avatar"), ("eval", "item", "document.querySelector('#menu a[href=\"#/settings\"]').textContent"),
        ("eval", "go", "location.hash = '#/profile'"), ("wait", "location.hash.startsWith('#/settings')"),
        ("eval", "nav", "[...document.querySelectorAll('.settings-nav a')].map(a => a.dataset.section)")])
    assert not r.get("failed"), r
    assert r["values"]["item"] == "Настройки"
    assert r["values"]["nav"] == ["chat", "photo", "video", "transcribe", "speech", "dictation", "other"]
```

Прежние сценарии профиля (`test_profile_adds_a_cloud_connection`, `test_profile_lists_gateway_models_and_changes_the_password`, `test_phone_avatar_never_covers_scrolled_profile_and_admin`, сценарии прямого облака и `test_admin_manages_users_connections_and_the_allowlist`, пункты меню `["Профиль", …]`) перевести на `#/settings/<раздел>`, «Настройки» и `#link-add`; облако после добавления отмечает модель `cloud-chat` (`PUT /api/sections/chat/links/<id>`) в хелпере `add_cloud`.

`tests/test_static.py`: `test_profile_module_is_gone` — `not (STATIC / "js" / "profile.js").exists()` и `'href="#/settings"'` в `index.html`.

- [ ] **Step 2: Run to verify they fail** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "settings"` → FAIL.

- [ ] **Step 3: Implement** — по Interfaces; разметка — `el()`/`textContent`, сохранение — сразу по изменению, без кнопки «Сохранить» у моделей. Шапка `settings.js` — раскладка А, кто ходит за моделями, поведение на телефоне.

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest -q tests/test_static.py` и `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "settings or profile or admin or avatar or cloud or direct or hint"` → PASS.

- [ ] **Step 5: Commit** — «Settings by section: connections, checked models and a default each».

---

### Task 7: Чипы «модель» из отмеченного (чат, фото, видео)

**Files:**
- Modify: `app/static/js/chat.js:360-430` (меню моделей — отмеченные модели чата по подключениям из `GET /api/sections`, без запроса списков при открытии), `app/static/js/photo.js` (чип модели — отмеченные модели фото, значение — пара; тело задания — `connection_id`; «Изменить» с кистью, «Описать», «Освободить карту» — только у srvllm), `app/static/js/editor.js` (кисть скрыта у внешнего подключения), `app/static/js/video.js` (чип модели, если вариантов > 1; `connection_id` в форме)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: `GET /api/sections` (задача 3), тела `POST /api/images`, `/api/videos`, `/api/chats` (задача 4).
- Подписи моделей srvllm: `lustify` — Lustify, `flux` — FLUX, `wan` — Wan; прочие — id. Группы меню — по имени подключения (`.model-group-name`), если подключений в разделе больше одного.

- [ ] **Step 1: Write the failing test**

```python
def test_chat_chip_lists_only_checked_models_grouped_by_connection(browser, live):
    login = "chipper"
    live.add_user(login)
    check = f"""(async () => {{ const m = {API}; const s = (await m.api('GET', '/api/sections')).sections[0];
      const id = s.links[0].connection.id;
      await m.api('PUT', '/api/sections/chat/links/' + id, {{ models: ['local-gemma-abl', 'gemma4-abl'] }});
      return true; }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), ("eval", "check", check),
        *open_new_chat(), ("click", "#chip-model"), ("wait", "document.querySelector('.model-option')"),
        ("eval", "models", "[...document.querySelectorAll('.model-option')].map(o => o.dataset.model)")])
    assert not r.get("failed"), r
    assert r["values"]["models"] == ["local-gemma-abl", "gemma4-abl"]
    assert not problems(r), problems(r)
```

- [ ] **Step 2: Run to verify it fails** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k chip_lists` → FAIL (меню берёт весь `/v1/models`).

- [ ] **Step 3: Implement** — по Interfaces.

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "chat or photo or video or chip"` → PASS.

- [ ] **Step 5: Commit** — «Offer only the section's checked models in the model chips».

---

### Task 8: Документы, выкладка с переносом базы, проверка

**Files:**
- Modify: `README.md` (раздел «Подключения» — разделы, отмеченные модели, умолчания, имена в списке разрешённых; «Профиль» → «Настройки»), `CLAUDE.md` (устройство: `sections.py`; правило имён в списке разрешённых; подключения — общий адрес с ключом, разделы ссылаются), `local-config/wiki/projects/studio/` — страница `architecture-sections.md` (раздел → подключения → модели, имена в списке, ≥ 2 `[[wikilinks]]`)

- [ ] **Step 1: Документы** — по списку.
- [ ] **Step 2: Полный быстрый набор** — `.venv/Scripts/python -m pytest -q` → PASS, ≤ 60 с.
- [ ] **Step 3: Браузерный набор** — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py` → PASS.
- [ ] **Step 4: Commit** — «Document sections, named allowlist entries and the settings».
- [ ] **Step 5: Копия базы и выкладка** — `ssh … "sudo systemctl start studio-backup.service && sudo journalctl -u studio-backup -n 3 --no-pager"` → «копия базы: …»; затем `bash deploy/deploy.sh`; `user_version` прода — 4; `SELECT section, models FROM section_links` — шесть строк у `srvllm` владельца; смоук `/health`.
- [ ] **Step 6: Владелец** — критерий успеха 1: «Настройки» → «Фото» → «Обновить модели» → снять FLUX → в чипе только Lustify.
