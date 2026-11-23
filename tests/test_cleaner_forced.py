"""Принудительный Cleaner (флажок администратора «Cleaner для всех внешних моделей», спека 2026-11-22, 6.3): внешние
подключения в «Фото», «Расшифровке», «Озвучке» и голосовом вводе и их новые связи — отказ словами; облачные роли srvllm
в «Чате» и «Переводе» — отказ, `local-*` — идут; внутренние подключения не меняются; общее внешнее — через Cleaner
с заблокированным переключателем. «Знания»: внешний источник векторов при флажке не используется вовсе, а диалог через
Cleaner не отдаёт ему ни запроса, ни кусков, ни своих больших вложений."""
import json

import httpx
import pytest

import cleaner_route
import knowledge
import sections
import security
import state
import translate_text
import upstream
from conftest import add_connection, add_gateway, make_user, signin
from fakes import FakeCleanerGateway, FakeGateway
from test_chat import attach, new_chat, sse, turn
from test_chat_pending import answer, pending_of
from test_chat_web import call
from test_cleaner_route_chat import ADDRESS, KEY, allow, cleaner_gw, settings  # noqa: F401 (фикстура)
from test_knowledge import base, big_text, find, gateway, settle, shared_source, upload, vector
from test_translate import post, wait_row

#: внешний провайдер — публичный IP из списка разрешённых: без флажка сервер ходил бы к нему сам
PROVIDER = "https://8.8.8.8/v1"
SHARED = "https://9.9.9.9/v1"
#: внешний источник «Знаний» — общее подключение администратора
EMBED = "https://8.8.4.4/v1"
ELSEWHERE = ("8.8.8.8:443", "9.9.9.9:443")
CLEANER_HOSTS = ("10.77.1.20:5464", "10.77.1.21:5464")
SECTIONS_OFF = ("photo", "transcribe", "speech", "dictation")


def owner_id() -> int:
    return state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]


def conn_of(cid: int):
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    return sections.connections.get(uid, cid)


def provider_trap(s) -> list:
    """Узлы провайдеров: любой запрос туда — провал теста (его адрес копится в списке)."""
    hits = []

    async def trap(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(500)
    for host in ELSEWHERE:
        s.gw.hosts[host] = trap
    return hits


def embed_source(s, kind="openai", *, s06=False) -> int:
    """Общее подключение администратора на внешнем адресе из списка и источник «Знаний» на нём; запросы —
    `s.embeds`, `s.reranks`."""
    s.embeds, s.reranks = [], []

    async def source(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            s.embeds.append(body)
            return httpx.Response(200, json={"data": [{"index": i, "embedding": vector(t)}
                                                      for i, t in enumerate(body["input"])]})
        s.reranks.append(body)
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": float("бирюз" in d)}
                                                     for i, d in enumerate(body["documents"])]})
    s.gw.hosts["8.8.4.4:443"] = source
    cid = state.DB.execute(
        "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, s06)"
        " VALUES (NULL, 'cloud embeddings', ?, ?, ?, '[]', 0, 1, ?)",
        (kind, EMBED, security.encrypt("embed-key"), s06)).lastrowid
    r = s.c.put("/api/admin/knowledge", json={"connection_id": cid, "embed_model": "embed-1", "rerank_model": "rerank-1"})
    assert r.status_code == 200, r.text
    return cid


@pytest.fixture
def forced_studio(studio):
    """Cleaner настроен, флажок включён. Внешнее подключение (`studio.ext`, PROVIDER из списка) связано с чатом и
    разделами звука и картинок до флажка, у каждого отмечена модель `m`; вход — владельцем-администратором. Узлы
    провайдеров и шлюз srvllm хода не получают — проверка после теста."""
    allow(*CLEANER_HOSTS, *ELSEWHERE, "8.8.4.4:443", "10.77.1.30:8000")
    studio.ext = add_connection(studio, base_url=PROVIDER, sections=["chat", *SECTIONS_OFF], chat_models=("gpt-x",))
    for section in SECTIONS_OFF:
        sections.set_models(owner_id(), section, studio.ext, ["m"])
    gw = FakeCleanerGateway(KEY)
    for host in CLEANER_HOSTS:
        studio.gw.hosts[host] = gw.handler
    studio.cleaner = gw
    hits = provider_trap(studio)
    settings(studio, forced=True)
    assert cleaner_route.forced()
    yield studio
    assert hits == []


def use_section(s, section: str, cid: int) -> httpx.Response:
    if section == "photo":
        return s.c.post("/api/images", json={"prompt": "кот", "size": "square", "connection_id": cid, "model": "m"})
    if section == "transcribe":
        return s.c.post("/api/transcripts", content=b"\x00" * 64,
                        headers={"content-type": "audio/mp4", "x-studio-name": "a.m4a",
                                 "x-studio-connection": str(cid), "x-studio-model": "m"})
    if section == "speech":
        return s.c.post("/api/speeches", json={"text": "Привет.", "voice": "v", "connection_id": cid, "model": "m"})
    return s.c.post("/api/dictation", files={"audio": ("voice.m4a", b"\x00" * 128, "audio/mp4")})


def refused(response: httpx.Response, words: str) -> bool:
    return response.status_code == 409 and response.json()["detail"] == {"code": "cleaner_forced", "message": words}


@pytest.mark.parametrize("section", SECTIONS_OFF)
def test_forced_refuses_external_connections_outside_chat_and_translate(forced_studio, section):
    r = use_section(forced_studio, section, forced_studio.ext)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cleaner_forced"
    assert refused(r, cleaner_route.FORCED_SECTION)
    assert forced_studio.gw.requests == [] and forced_studio.gw.asr_requests == []
    settings(forced_studio, forced=False)                                   # флажок снят — раздел снова свой
    assert cleaner_route.forced_refusal(conn_of(forced_studio.ext), section, "m") is None


def test_forced_refuses_new_links_of_external_connections(forced_studio):
    s = forced_studio
    other = add_connection(s, base_url=SHARED, sections=["chat"])
    for section in SECTIONS_OFF:
        assert refused(s.c.post(f"/api/sections/{section}/links", json={"connection_id": other}),
                       cleaner_route.FORCED_SECTION), section
    assert refused(s.c.patch(f"/api/connections/{other}", json={"sections": ["chat", "photo"]}),
                   cleaner_route.FORCED_SECTION)
    body = {"name": "new", "kind": "openai", "base_url": SHARED, "token": "cloud-key", "sections": ["chat", "speech"]}
    assert refused(s.c.post("/api/connections", json=body), cleaner_route.FORCED_SECTION)
    assert state.DB.execute("SELECT count(*) FROM connections WHERE name = 'new'").fetchone()[0] == 0
    assert sections.sections_of(owner_id(), other) == ["chat"]
    # перевод через Cleaner — можно; прежние связи внешнего подключения остаются (выключение флажка их вернёт)
    assert s.c.post("/api/sections/translate/links", json={"connection_id": other}).status_code == 200
    assert s.c.patch(f"/api/connections/{s.ext}", json={"name": "renamed"}).status_code == 200
    assert set(sections.sections_of(owner_id(), s.ext)) == {"chat", *SECTIONS_OFF}


def test_forced_refuses_srvllm_cloud_roles_but_not_local(forced_studio):
    s = forced_studio
    gw = add_gateway(s)
    uid = owner_id()
    sections.set_models(uid, "chat", gw, ["local-gemma-abl", "gpt-x"])
    sections.set_models(uid, "translate", gw, ["local-gemma-abl", "gpt-x"])
    # новый диалог с облачной ролью — отказ; диалог, начатый до флажка, — отказ хода до записи
    assert refused(s.c.post("/api/chats", json={"connection_id": gw, "model": "gpt-x"}), cleaner_route.FORCED_SRVLLM)
    cloud = new_chat(s, connection_id=gw, model="local-gemma-abl")
    state.DB.execute("UPDATE chats SET model = 'gpt-x' WHERE id = ?", (cloud,))
    assert refused(turn(s, cloud, "Секрет"), cleaner_route.FORCED_SRVLLM)
    assert s.gw.chat_requests == []
    assert state.DB.execute("SELECT count(*) FROM messages WHERE chat_id = ?", (cloud,)).fetchone()[0] == 0
    local = new_chat(s, connection_id=gw, model="local-gemma-abl")
    assert sse(turn(s, local, "Привет"))[-1] == ("done", {"status": "done"})
    assert len(s.gw.chat_requests) == 1 and s.gw.chat_requests[0]["model"] == "local-gemma-abl"
    # «Перевод»: облачная роль — отказ, local-* — идёт
    assert refused(post(s, "Hello.", connection_id=gw, model="gpt-x"), cleaner_route.FORCED_SRVLLM)
    r = post(s, "Hello.", connection_id=gw, model="local-gemma-abl")
    assert r.status_code == 202, r.text
    assert wait_row(s, r.json()["id"])["state"] == "done"
    assert all(body["model"] == "local-gemma-abl" for body in s.gw.chat_requests)


def test_pause_of_a_cloud_role_cannot_resume_once_forced(studio):
    gw = add_gateway(studio)
    sections.set_models(owner_id(), "chat", gw, ["local-gemma-abl", "gpt-x"])
    cid = new_chat(studio, connection_id=gw, model="gpt-x")
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Какой цвет?", options=["Красный"])]}, ["Ок."]]
    shown = pending_of(sse(turn(studio, cid, "Нарисуй", external_ok=True)))
    allow(*CLEANER_HOSTS)
    settings(studio, forced=True)
    sent = len(studio.gw.chat_requests)
    assert refused(answer(studio, cid, shown["id"], "answer", "Красный"), cleaner_route.FORCED_SRVLLM)
    assert len(studio.gw.chat_requests) == sent


def test_forced_leaves_internal_connections_alone(forced_studio):
    s = forced_studio
    uid = owner_id()
    gw = add_gateway(s)                                                    # srvllm во всех разделах — связи без отказа
    private = add_connection(s, base_url="https://10.77.1.30:8000/v1", sections=["chat"])
    for section in ("photo", "transcribe"):                                # частный адрес из списка — внутренний
        assert s.c.post(f"/api/sections/{section}/links", json={"connection_id": private}).status_code == 200
    sections.set_models(uid, "transcribe", private, ["whisper-1"])
    assert sections.resolve(uid, "transcribe", private, "whisper-1")[0].id == private
    r = s.c.post("/api/connections", json={"name": "s06", "kind": "openai", "base_url": "http://10.77.1.15:4000",
                                           "token": "s06-key", "s06": True, "sections": ["chat", "transcribe"]})
    assert r.status_code == 201, r.text
    s06 = r.json()["id"]
    assert sections.resolve(uid, "transcribe", s06, "whisperx")[0].id == s06
    items = {i["id"]: i for i in s.c.get("/api/connections").json()["items"]}
    for cid, section, model in ((gw, "photo", "flux"), (gw, "speech", "qwen3-tts"), (gw, "chat", "local-gemma-abl"),
                                (s06, "chat", "qwen-27b"), (private, "chat", "gpt-x"), (private, "photo", "m")):
        conn = conn_of(cid)
        assert cleaner_route.forced_refusal(conn, section, model) is None, (cid, section)
        assert not conn.routed and not items[cid]["cleaner_locked"] and not items[cid]["external"]
    assert items[s.ext]["cleaner_locked"] and items[s.ext]["routed"]


def test_forced_refuses_s06_and_srvllm_on_a_listed_name(forced_studio, monkeypatch):
    """Флажок s06 и вид srvllm выбирает пользователь: на имени из списка такой шлюз — внешний. Через Cleaner его не
    провести (`routes` — только `openai`), поэтому при флажке — отказ во всех разделах, даже модели `local-*`."""
    s = forced_studio
    allow(*CLEANER_HOSTS, *ELSEWHERE, "gw.cloud.test:443")

    async def resolve(host, port):
        return ["8.8.8.8"]                                                # узел — ловушка провайдера
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    uid = owner_id()
    settings(s, forced=False)
    r = s.c.post("/api/connections", json={"name": "s06", "kind": "openai", "base_url": "https://gw.cloud.test",
                                           "token": "s06-key", "s06": True, "sections": ["chat", "transcribe"]})
    assert r.status_code == 201, r.text
    s06 = r.json()["id"]
    srv = add_connection(s, kind="srvllm", base_url="https://gw.cloud.test", token="srv-key", name="srv")
    sections.set_models(uid, "chat", s06, ["qwen-27b"])
    sections.set_models(uid, "transcribe", s06, ["whisperx"])
    sections.set_models(uid, "chat", srv, ["local-x"])
    sections.set_models(uid, "translate", srv, ["local-x"])
    sections.set_models(uid, "photo", srv, ["flux"])
    before = {cid: new_chat(s, connection_id=cid, model=model) for cid, model in ((s06, "qwen-27b"), (srv, "local-x"))}
    settings(s, forced=True)
    for cid, model in ((s06, "qwen-27b"), (srv, "local-x")):
        assert refused(turn(s, before[cid], "Секрет"), cleaner_route.FORCED_GATEWAY), cid
        assert refused(s.c.post("/api/chats", json={"connection_id": cid, "model": model}), cleaner_route.FORCED_GATEWAY)
        assert state.DB.execute("SELECT count(*) FROM messages WHERE chat_id = ?", (before[cid],)).fetchone()[0] == 0
    assert refused(post(s, "Hello.", connection_id=srv, model="local-x"), cleaner_route.FORCED_GATEWAY)
    assert refused(use_section(s, "photo", srv), cleaner_route.FORCED_SECTION)
    r = s.c.post("/api/transcripts", content=b"\x00" * 64,
                 headers={"content-type": "audio/mp4", "x-studio-name": "a.m4a", "x-studio-connection": str(s06),
                          "x-studio-model": "whisperx"})
    assert refused(r, cleaner_route.FORCED_SECTION)
    items = {i["id"]: i for i in s.c.get("/api/connections").json()["items"]}
    for cid in (s06, srv):                                                # переключателя у шлюзов нет
        assert items[cid]["external"] and not items[cid]["routed"]
        assert not items[cid]["cleaner_offer"] and not items[cid]["cleaner_locked"]
    assert s.gw.requests == [] and s.gw.chat_requests == [] and s.gw.asr_requests == [] and s.cleaner.turns == []


def test_shared_external_connection_is_routed_and_locked(forced_studio):
    s = forced_studio
    r = s.c.post("/api/admin/shared/connections", json={"name": "Shared cloud", "kind": "openai", "base_url": SHARED,
                                                       "token": "shared-key", "sections": ["chat"]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert s.c.put(f"/api/admin/shared/sections/chat/links/{cid}", json={"models": ["gpt-x"]}).status_code == 200
    for value in (True, False):                                           # администратору переключатель тоже заблокирован
        response = s.c.patch(f"/api/admin/shared/connections/{cid}", json={"via_cleaner": value})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "via_cleaner"
    make_user(s, "member")
    signin(s, "member")
    item = next(i for i in s.c.get("/api/connections").json()["items"] if i["id"] == cid)
    assert item["shared"] and item["routed"] and item["cleaner_locked"] and item["external"] and not item["via_cleaner"]
    chat_id = new_chat(s, connection_id=cid, model="gpt-x")
    assert sse(turn(s, chat_id, "Привет"))[-1] == ("done", {"status": "done"})
    sent = s.cleaner.turns[0]
    assert sent.headers["x-cleaner-upstream"] == SHARED and sent.headers["authorization"] == "Bearer shared-key"
    assert s.cleaner.bodies[0]["messages"][-1] == {"role": "user", "content": "Привет"}


@pytest.mark.parametrize(("kind", "s06"), [("openai", False), ("srvllm", False), ("openai", True)])
def test_external_knowledge_source_is_off_when_forced(forced_studio, kind, s06):
    s = forced_studio
    settings(s, forced=False)
    embed_source(s, kind, s06=s06)
    kb = base(s)
    upload(s, kb, "пароли.txt", "Кодовое слово отдела — бирюза. Его меняют весной.")
    settle(s)
    assert len(s.embeds) == 1                                             # до флажка источник работал
    settings(s, forced=True)
    s.embeds.clear()
    assert s.c.get("/api/admin/knowledge").json()["source_off"] is True
    doc = upload(s, kb, "море.txt", "Море тёплое, песок жёлтый, бирюза волн.")
    assert doc["state"] == "ready"                                        # индексация — только BM25, без очереди
    settle(s)
    hits = find(s, owner_id(), "бирюза отдела", kb_ids=(kb,))
    assert {h.doc_name for h in hits} == {"пароли.txt", "море.txt"}
    assert s.embeds == [] and s.reranks == []                             # ни вектора запроса, ни переранжирования
    assert state.DB.execute("SELECT count(*) FROM kb_chunks WHERE vector IS NOT NULL").fetchone()[0] == 1
    chat_id = new_chat(s, connection_id=s.ext, model="gpt-x")
    attachment = attach(s, "отчёт.txt", big_text("Пароль от сейфа.").encode(), "text/plain")
    assert sse(turn(s, chat_id, "Что в отчёте?", attachment_ids=[attachment["id"]]))[-1][0] == "done"
    assert s.embeds == []
    settings(s, forced=False)
    assert s.c.get("/api/admin/knowledge").json()["source_off"] is False
    settle(s)
    assert len(s.embeds) == 1
    assert state.DB.execute("SELECT count(*) FROM kb_chunks WHERE vector IS NOT NULL").fetchone()[0] == 2


def test_routed_dialog_skips_an_external_knowledge_source(cleaner_gw):
    s, gw = cleaner_gw
    allow(*CLEANER_HOSTS, "8.8.4.4:443")
    embed_source(s)
    kb = base(s)
    upload(s, kb, "пароли.txt", "Кодовое слово отдела — бирюза. Его меняют весной.")
    settle(s)
    assert len(s.embeds) == 1                                             # индексация базы — не текст диалога
    s.embeds.clear()
    chat_id = new_chat(s, connection_id=gw.conn_id, model="gpt-x")
    assert s.c.put(f"/api/chats/{chat_id}/knowledge", json={"kb_ids": [kb]}).status_code == 200
    gw.script = [{"tool_calls": [call("knowledge_search", query="кодовое слово")]}, ["Бирюза."]]
    assert sse(turn(s, chat_id, "Какое кодовое слово отдела?"))[-1] == ("done", {"status": "done"})
    asked = [m for m in gw.bodies[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert asked.startswith("<knowledge>") and "бирюза" in asked             # блок баз — BM25
    tool = [m for m in gw.bodies[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "бирюза" in tool
    assert s.embeds == [] and s.reranks == []
    assert find(s, owner_id(), "кодовое слово", kb_ids=(kb,)) and s.embeds    # другим путям источник открыт


def test_big_attachment_of_a_routed_dialog_is_indexed_without_vectors(cleaner_gw):
    s, gw = cleaner_gw
    allow(*CLEANER_HOSTS, "8.8.4.4:443")
    source = embed_source(s)
    chat_id = new_chat(s, connection_id=gw.conn_id, model="gpt-x")
    doc = attach(s, "отчёт.txt", big_text("Пароль от сейфа — 4711.").encode(), "text/plain")
    assert sse(turn(s, chat_id, "Какой пароль от сейфа?", attachment_ids=[doc["id"]]))[-1][0] == "done"
    asked = gw.bodies[0]["messages"][-1]["content"]
    assert '<document name="отчёт.txt" excerpts="true">' in asked and "4711" in asked
    row = state.DB.execute("SELECT id, state FROM kb_documents WHERE attachment_id = ?", (doc["id"],)).fetchone()
    assert row["state"] == "ready"
    settle(s)
    # Даже после смены подключения чата повторный индекс не отправляет старое вложение.
    assert s.c.patch(f"/api/connections/{gw.conn_id}", json={"via_cleaner": False}).status_code == 200
    assert not s.c.get(f"/api/chats/{chat_id}").json()["chat"]["routed"]
    r = s.c.put("/api/admin/knowledge", json={"connection_id": source, "embed_model": "embed-2", "rerank_model": None})
    assert r.status_code == 200, r.text
    settle(s)
    assert s.embeds == [] and s.reranks == []
    assert state.DB.execute("SELECT state FROM kb_documents WHERE id = ?", (row["id"],)).fetchone()[0] == "ready"
    assert state.DB.execute("SELECT count(*) FROM kb_chunks WHERE vector IS NOT NULL").fetchone()[0] == 0


def test_internal_knowledge_source_still_works_when_forced(forced_studio):
    s = forced_studio
    gateway(s)
    shared_source(s)
    assert s.c.get("/api/admin/knowledge").json()["source_off"] is False
    uid = make_user(s, "reader")
    signin(s, "reader")
    kb = base(s)
    doc = upload(s, kb, "звери.txt", "Про собак. " * 130 + "Щенок учится сидеть. " * 70)
    assert doc["state"] == "queued"
    settle(s)
    assert s.embeds
    s.embeds.clear()
    hits = find(s, uid, "пёс", kb_ids=(kb,))
    assert hits and len(s.embeds) == 1 and s.reranks
    assert knowledge.vectors_for(conn_of(s.ext))                          # внутренний источник — и диалогу через Cleaner


# ---- флажок и переключатель действуют на идущую работу ------------------------------------------

@pytest.fixture
def direct_studio(studio):
    """Внешнее подключение на публичном IP из списка (`studio.ext`, ключ FakeGateway — сервер ходит к нему сам) в
    «Чате» и «Переводе» с моделью gpt-x; Cleaner настроен, флажок выключен. `studio.flip()` — первый запрос
    провайдеру включает флажок (администратор — посреди работы) и отвечает как обычно."""
    allow(*CLEANER_HOSTS, *ELSEWHERE)
    studio.ext = add_connection(studio, base_url=PROVIDER, token=FakeGateway.TOKEN, sections=["chat", "translate"],
                                chat_models=("gpt-x",))
    sections.set_models(owner_id(), "translate", studio.ext, ["gpt-x"])
    gw = FakeCleanerGateway(KEY)
    for host in CLEANER_HOSTS:
        studio.gw.hosts[host] = gw.handler
    studio.cleaner = gw
    settings(studio)

    def flip():
        async def chat(request: httpx.Request) -> httpx.Response:
            if not cleaner_route.forced():
                cleaner_route.save_settings(cleaner_route.SettingsIn(base_url=ADDRESS, forced=True))
            return await studio.gw.builtin(request)
        studio.gw.on["/v1/chat/completions"] = chat
    studio.flip = flip
    yield studio
    assert gw.turns == []                                                 # снимок до флажка в Cleaner тоже не пошёл


def test_flag_stops_a_running_translation_before_the_next_chunk(direct_studio, monkeypatch):
    s = direct_studio
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    s.gw.chat_script = [["Первый."], ["Второй."], ["Третий."]]
    s.flip()
    r = post(s, "First one is here.\n\nSecond one is here.\n\nThird one is here.", connection_id=s.ext, model="gpt-x")
    assert r.status_code == 202, r.text
    row = wait_row(s, r.json()["id"])
    assert row["state"] == "error" and "через Cleaner изменился" in row["detail"], row["detail"]
    assert [b["messages"][-1]["content"] for b in s.gw.chat_requests] == ["First one is here."]   # второго куска нет


def test_flag_stops_the_next_tool_round_of_a_running_turn(direct_studio):
    s = direct_studio
    chat_id = new_chat(s, connection_id=s.ext, model="gpt-x")
    s.gw.chat_script = [{"tool_calls": [call("todo_write", items=[{"text": "Шаг", "status": "pending"}])]}, ["Готово"]]
    s.flip()
    events = sse(turn(s, chat_id, "Секрет", external_ok=True))
    assert events[-1][0] == "error" and "через Cleaner изменился" in json.dumps(events[-1][1], ensure_ascii=False), events
    assert len(s.gw.chat_requests) == 1                                   # результат инструмента провайдеру не ушёл


@pytest.fixture
def named_gateway(studio, monkeypatch):
    """Шлюз srvllm на имени из списка (внешний по правилу C1, сервер ходит к нему сам), Cleaner настроен, флажок
    выключен. `studio.flip(path)` — первый запрос шлюзу по `path` включает флажок и отвечает как обычно."""
    allow(*CLEANER_HOSTS, "gw.cloud.test:443")

    async def resolve(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    studio.srv = add_gateway(studio)
    state.DB.execute("UPDATE connections SET base_url = 'https://gw.cloud.test' WHERE id = ?", (studio.srv,))
    gw = FakeCleanerGateway(KEY)
    for host in CLEANER_HOSTS:
        studio.gw.hosts[host] = gw.handler
    settings(studio)

    def flip(path):
        async def hook(request: httpx.Request) -> httpx.Response:
            if not cleaner_route.forced():
                cleaner_route.save_settings(cleaner_route.SettingsIn(base_url=ADDRESS, forced=True))
            return await studio.gw.builtin(request)
        studio.gw.on[path] = hook
    studio.flip = flip
    yield studio
    assert gw.turns == []


def test_flag_stops_a_running_translation_on_an_external_gateway(named_gateway, monkeypatch):
    s = named_gateway
    monkeypatch.setattr(translate_text, "CHUNK_CHARS", 20)
    s.gw.chat_script = [["Первый."], ["Второй."], ["Третий."]]
    s.flip("/v1/chat/completions")
    r = post(s, "First one is here.\n\nSecond one is here.\n\nThird one is here.", connection_id=s.srv,
             model="local-gemma-abl")
    assert r.status_code == 202, r.text
    row = wait_row(s, r.json()["id"])
    assert row["state"] == "error" and "через Cleaner изменился" in row["detail"], row["detail"]
    assert [b["messages"][-1]["content"] for b in s.gw.chat_requests] == ["First one is here."]


def test_flag_stops_a_queued_speech_on_an_external_gateway(named_gateway, fake_audio):
    from test_speeches import LONG_A, LONG_B, voice, wait_sp
    s = named_gateway
    vid = voice(s, fake_audio)
    s.flip("/speech/tts-qwen")
    r = s.c.post("/api/speeches", json={"text": f"{LONG_A} {LONG_B}", "voice": vid, "connection_id": s.srv,
                                        "model": "qwen3-tts"})
    assert r.status_code == 202, r.text
    item = wait_sp(s, r.json()["id"], {"done", "error"})
    assert item["state"] == "error" and len(s.gw.tts_requests) == 1, item     # вторая фраза шлюзу не ушла
