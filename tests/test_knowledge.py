"""Знания (app/knowledge.py, спека 2026-11-21-studio-source-capabilities-design, п. 15): большие вложения и базы знаний
— куски с перекрытием, поиск BM25 (FTS5) и, при источнике эмбеддингов администратора, векторы, слияние RRF и
переранжирование. Тексты документов и запросов в журнал не пишутся."""
import json
import logging
import math
import re
from urllib.parse import quote

import httpx

import chat
import knowledge
import security
import state
from conftest import add_gateway, make_user, signin
from fakes import TOKEN
from test_chat import attach, messages, new_chat, sse, turn
from test_chat_web import call

#: слова, по которым поддельная модель эмбеддингов строит вектор: текст про собак и текст про кошек — разные оси
AXES = ("собак", "кошк", "космос", "пёс", "щен")


def vector(text: str) -> list[float]:
    low = text.lower().replace("пёс", "собак").replace("щен", "собак")
    return [low.count(axis) + 0.01 for axis in AXES]


def gateway(studio, *, rerank=None):
    """Поддельные `/v1/embeddings` и `/v1/rerank`; запросы — в `studio.embeds`, `studio.reranks`."""
    studio.embeds, studio.reranks = [], []

    async def embeddings(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        studio.embeds.append(body)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vector(t)}
                                                  for i, t in reversed(list(enumerate(body["input"])))]})

    async def reranking(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        studio.reranks.append(body)
        score = rerank or (lambda q, d: float("щен" in d))
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": score(body["query"], d)}
                                                     for i, d in enumerate(body["documents"])]})

    studio.gw.on["/v1/embeddings"] = embeddings
    studio.gw.on["/v1/rerank"] = reranking


def shared_source(studio, rerank_model="bge-reranker") -> int:
    """Общее подключение шлюза и источник знаний на нём (PUT /api/admin/knowledge)."""
    cid = state.DB.execute(
        "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only)"
        " VALUES (NULL, 'srvllm', 'srvllm', 'http://10.77.1.15:4000', ?, '[]', 0, 1)",
        (security.encrypt(TOKEN),)).lastrowid
    if not state.DB.execute("SELECT 1 FROM users WHERE login = 'admin'").fetchone():
        make_user(studio, "admin", is_admin=True)
    signin(studio, "admin")
    r = studio.c.put("/api/admin/knowledge", json={"connection_id": cid, "embed_model": "bge-m3",
                                                   "rerank_model": rerank_model})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == {"connection_id": cid, "embed_model": "bge-m3", "rerank_model": rerank_model}
    return cid


def base(studio, name="Справочник") -> str:
    r = studio.c.post("/api/knowledge", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(studio, kb_id: str, name: str, text: str, prefix="/api/knowledge") -> dict:
    r = studio.c.post(f"{prefix}/{kb_id}/documents", content=text.encode(),
                      headers={"x-studio-name": quote(name), "content-type": "text/plain"})
    assert r.status_code == 201, r.text
    return r.json()


def settle(studio):
    studio.c.portal.call(knowledge.settle)


def bases_of(studio) -> dict:
    return {b["id"]: b for b in studio.c.get("/api/knowledge").json()["items"]}


def find(studio, uid, query, **scope):
    return studio.c.portal.call(lambda: knowledge.search(uid, query, **scope))


def test_chunks_overlap_and_cover_text():
    text = "".join(chr(0x430 + i % 32) for i in range(4000))
    parts = knowledge.chunks(text)
    assert len(parts) == math.ceil((len(text) - knowledge.OVERLAP) / (knowledge.CHUNK_CHARS - knowledge.OVERLAP))
    assert all(len(p) <= knowledge.CHUNK_CHARS for p in parts) and len(parts[0]) == knowledge.CHUNK_CHARS
    assert all(a[-knowledge.OVERLAP:] == b[:knowledge.OVERLAP] for a, b in zip(parts, parts[1:]))
    assert parts[0] + "".join(p[knowledge.OVERLAP:] for p in parts[1:]) == text
    assert knowledge.chunks("коротко") == ["коротко"] and knowledge.chunks("") == []
    assert (knowledge.CHUNK_CHARS, knowledge.OVERLAP, knowledge.INLINE_TOKENS, knowledge.TOP_K) == (1500, 200, 30_000, 6)


def test_bm25_only_without_source(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    kb = base(studio)
    first = upload(studio, kb, "ель.txt", "Новогодняя ёлка стоит в зале. " * 3)
    upload(studio, kb, "море.txt", "Море тёплое, песок жёлтый. " * 3)
    assert first["state"] == "ready" and first["name"] == "ель.txt" and first["chars"] > 0
    assert [d["state"] for d in bases_of(studio)[kb]["documents"]] == ["ready", "ready"]
    assert not [r for r in studio.gw.requests if r.url.path.endswith("/embeddings")]
    for query in ("елка", "ёлка", "ЁЛКИ в зале"):                 # «ё» запроса сворачивается, как в индексе
        hits = find(studio, uid, query, kb_ids=(kb,))
        assert hits and {h.doc_name for h in hits} == {"ель.txt"}, query
        assert hits[0].n == 1 and "ёлка" in hits[0].text
    assert find(studio, uid, "песок", kb_ids=(kb,))[0].doc_name == "море.txt"
    assert find(studio, uid, "вулкан", kb_ids=(kb,)) == []
    assert find(studio, uid, 'ёлка" OR песок*', kb_ids=(kb,))        # синтаксис FTS5 запроса не проходит
    other = make_user(studio, "other")
    assert find(studio, other, "ёлка", kb_ids=(kb,)) == []           # чужая личная база — мимо


def test_embeddings_and_rerank_order_hits(studio):
    gateway(studio)
    shared_source(studio)
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    kb = base(studio)
    animals = "Про собак. " * 130 + "Щенок учится сидеть. " * 70 + "Кошка спит. " * 120
    doc = upload(studio, kb, "звери.txt", animals)
    space = upload(studio, kb, "небо.txt", "Космос далёк. " * 6500)   # больше 64 кусков — две пачки
    assert doc["state"] == "queued"
    settle(studio)
    assert {d["state"] for d in bases_of(studio)[kb]["documents"]} == {"ready"}
    assert all(r[0] is not None for r in state.DB.execute("SELECT vector FROM kb_chunks"))
    sizes = [len(b["input"]) for b in studio.embeds]
    assert max(sizes) == knowledge.EMBED_BATCH and sum(sizes) == state.DB.execute(
        "SELECT count(*) FROM kb_chunks").fetchone()[0]
    assert all(b["model"] == "bge-m3" for b in studio.embeds)
    studio.embeds.clear()
    # «пёс» в документах нет — BM25 молчит, вектор находит куски про собак; переранжирование ставит «щенка» первым
    hits = find(studio, uid, "пёс", kb_ids=(kb,))
    assert len(studio.embeds) == 1 and studio.embeds[0]["input"] == ["пёс"]
    assert studio.reranks and studio.reranks[-1]["model"] == "bge-reranker" and studio.reranks[-1]["query"] == "пёс"
    assert len(hits) == knowledge.TOP_K and hits[0].doc_name == "звери.txt" and "Щенок" in hits[0].text
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert all(h.doc_name == "звери.txt" for h in hits if h.score > 0)
    assert len(studio.reranks[-1]["documents"]) > knowledge.TOP_K  # переранжируются кандидаты слияния, не TOP_K
    assert find(studio, uid, "космос", kb_ids=(kb,))[0].doc_name == "небо.txt"
    # шлюз переранжирования упал — порядок слияния, без ошибки
    studio.gw.on["/v1/rerank"] = lambda request: _down(request)
    assert find(studio, uid, "пёс", kb_ids=(kb,))
    assert space["state"] == "queued"


async def _down(request):
    return httpx.Response(503, json={"error": {"message": "нет"}})


def big_text(secret: str) -> str:
    filler = "Обычная строка отчёта без важного. " * 1500
    return filler + secret + " " + filler


def test_big_attachment_sends_excerpts_not_whole_text(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    text = big_text("Пароль от сейфа — 4711.")
    assert chat.estimate_tokens([{"role": "user", "content": text}]) > knowledge.INLINE_TOKENS
    doc = attach(studio, "отчёт.txt", text.encode())
    small = attach(studio, "заметка.txt", "Короткая заметка.".encode())
    row = state.DB.execute("SELECT * FROM kb_documents WHERE attachment_id = ?", (doc["id"],)).fetchone()
    assert row is not None and row["kb_id"] is None and row["state"] == "ready" and row["chars"] == len(text)
    assert state.DB.execute("SELECT 1 FROM kb_documents WHERE attachment_id = ?", (small["id"],)).fetchone() is None
    sse(turn(studio, cid, "Какой пароль от сейфа?", attachment_ids=[doc["id"], small["id"]]))
    sent = [m for m in studio.gw.chat_requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert '<document name="отчёт.txt" excerpts="true">' in sent and "4711" in sent
    assert re.search(r"\[кусок \d+\]", sent)
    assert len(sent) < len(text) // 4 and sent.count("Обычная строка") < 300
    assert '<document name="заметка.txt">\nКороткая заметка.\n</document>' in sent
    # следующий ход ищет по своему сообщению: в выдержках — то, что спросили теперь
    sse(turn(studio, cid, "Что ещё в отчёте?"))
    again = [m for m in studio.gw.chat_requests[-1]["messages"] if m["role"] == "user"][0]["content"]
    assert 'excerpts="true"' in again and len(again) < len(text) // 4
    assert len(messages(studio, cid)) == 4


def test_shared_kb_visible_by_group_only_and_admin_always(studio):
    member, other = make_user(studio, "member"), make_user(studio, "other")
    make_user(studio, "admin", is_admin=True)
    signin(studio, "other")
    private = base(studio, "Личное другого")
    upload(studio, private, "личное.txt", "Секретный рецепт пирога.")
    signin(studio, "admin")
    r = studio.c.post("/api/admin/knowledge/bases", json={"name": "Регламенты"})
    assert r.status_code == 201
    kb = r.json()["id"]
    upload(studio, kb, "отпуск.txt", "Отпуск оформляют за две недели.", prefix="/api/admin/knowledge/bases")
    group = studio.c.post("/api/admin/groups", json={"name": "Кадры"}).json()["id"]
    assert studio.c.put(f"/api/admin/groups/{group}/members", json={"user_ids": [member]}).status_code == 200
    assert studio.c.put(f"/api/admin/knowledge/bases/{kb}/access", json={"groups": []}).status_code == 422
    assert studio.c.put(f"/api/admin/knowledge/bases/{kb}/access", json={"groups": [group]}).status_code == 200
    listed = studio.c.get("/api/admin/knowledge").json()["bases"]
    assert [(b["id"], b["groups"], [d["name"] for d in b["documents"]]) for b in listed] == [(kb, [group], ["отпуск.txt"])]
    assert set(bases_of(studio)) == {kb}                          # администратор — общие всегда, чужие личные — нет
    assert studio.c.delete(f"/api/knowledge/{private}").status_code == 404
    assert studio.c.delete(f"/api/knowledge/{kb}").status_code == 404     # общая — только из панели
    signin(studio, "member")
    assert bases_of(studio)[kb]["shared"] is True
    assert find(studio, member, "отпуск", kb_ids=(kb,))[0].doc_name == "отпуск.txt"
    signin(studio, "other")
    assert set(bases_of(studio)) == {private}
    assert find(studio, other, "отпуск", kb_ids=(kb,)) == []
    assert studio.c.get("/api/admin/knowledge").status_code == 403
    assert studio.c.post(f"/api/admin/knowledge/bases/{kb}/documents", content=b"x",
                         headers={"x-studio-name": "x.txt"}).status_code == 403
    signin(studio, "admin")
    assert studio.c.put(f"/api/admin/knowledge/bases/{kb}/access", json={"groups": None}).status_code == 200
    signin(studio, "other")
    assert set(bases_of(studio)) == {private, kb}                 # правил нет — всем


def test_chat_kb_block_and_tool(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    kb = base(studio)
    upload(studio, kb, "пароли.txt", "Кодовое слово отдела — бирюза. Его меняют весной.")
    cid = new_chat(studio)
    sse(turn(studio, cid, "Привет"))                              # базы не выбраны — блока и инструмента нет
    first = studio.gw.chat_requests[-1]
    assert "<knowledge>" not in json.dumps(first, ensure_ascii=False)
    assert "knowledge_search" in {t["function"]["name"] for t in first.get("tools", [])}   # база у учётки есть
    assert studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": ["nope"]}).status_code == 404
    r = studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]})
    assert r.status_code == 200 and r.json() == {"kb_ids": [kb]}
    assert studio.c.get(f"/api/chats/{cid}").json()["knowledge"] == [kb]
    sse(turn(studio, cid, "Какое кодовое слово?"))
    sent = studio.gw.chat_requests[-1]
    user = [m for m in sent["messages"] if m["role"] == "user"][-1]["content"]
    assert user.startswith("<knowledge>") and "бирюза" in user and 'source="пароли.txt"' in user
    assert user.rstrip().endswith("Какое кодовое слово?")
    assert "бирюза" not in json.dumps(messages(studio, cid), ensure_ascii=False)     # в историю блок не пишется
    studio.gw.chat_script = [{"tool_calls": [call("knowledge_search", query="весной меняют")]}, ["Весной."]]
    events = sse(turn(studio, cid, "Когда меняют?"))
    assert events[-1] == ("done", {"status": "done"})
    step = messages(studio, cid)[-1]["tools"]
    assert step == [{"kind": "knowledge", "query": "весной меняют", "found": 1, "sources": ["пароли.txt"]}]
    tool = [m for m in studio.gw.chat_requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "бирюза" in tool
    studio.gw.chat_script = [{"tool_calls": [call("knowledge_search", query="")]}, ["Ладно."]]
    sse(turn(studio, cid, "Ищи"))
    assert messages(studio, cid)[-1]["tools"][0]["error"]
    assert studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": []}).json() == {"kb_ids": []}
    other = make_user(studio, "other")
    signin(studio, "other")
    assert studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": []}).status_code == 404
    assert uid != other


def small_model(studio, cid, gw, context_length):
    """Модель диалога `tiny` с пределом контекста из списка моделей подключения."""
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "tiny"]})
    studio.gw.models.append({"id": "tiny", "context_length": context_length})
    assert studio.c.patch(f"/api/chats/{cid}", json={"model": "tiny"}).json()["model"] == "tiny"


def test_knowledge_search_result_fits_the_context(studio):
    """Найденное `knowledge_search` — через подгонку контекста, как результаты «Интернета» и MCP: модель с малым
    пределом получает обрезанный текст с пометкой chat.CUT_CONTEXT, а не запрос больше контекста (400 шлюза)."""
    gw = add_gateway(studio)
    kb = base(studio)
    upload(studio, kb, "бирюза.txt", "Кодовое слово отдела — бирюза. " * 600)    # ≈ 18 тыс. знаков, 14 кусков
    cid = new_chat(studio)
    small_model(studio, cid, gw, 6000)
    studio.gw.chat_script = [{"tool_calls": [call("knowledge_search", query="бирюза")]}, ["Бирюза."]]
    assert sse(turn(studio, cid, "Какое кодовое слово?"))[-1] == ("done", {"status": "done"})
    tool = [m for m in studio.gw.chat_requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert tool.endswith(chat.CUT_CONTEXT) and len(tool) < 6000
    assert chat.estimate_tokens(studio.gw.chat_requests[-1]["messages"]) <= 6000 - chat.RESERVE_TOKENS


def test_kb_delete_goes_to_trash_and_restores(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    kb = base(studio)
    doc = upload(studio, kb, "план.txt", "План работ на квартал: ремонт крыши.")
    keep = upload(studio, kb, "другое.txt", "Закупка бумаги.")
    assert studio.c.delete(f"/api/knowledge/{kb}/documents/{doc['id']}").status_code == 204
    assert [d["id"] for d in bases_of(studio)[kb]["documents"]] == [keep["id"]]
    assert find(studio, uid, "крыши", kb_ids=(kb,)) == []
    assert studio.c.delete(f"/api/knowledge/{kb}/documents/{doc['id']}").status_code == 404
    trash = {(i["kind"], i["id"]): i for i in studio.c.get("/api/trash").json()}
    assert trash[("kb_documents", doc["id"])]["title"] == "план.txt"
    assert studio.c.post(f"/api/trash/kb_documents/{doc['id']}/restore").status_code == 204
    assert find(studio, uid, "крыши", kb_ids=(kb,))[0].doc_name == "план.txt"
    assert studio.c.delete(f"/api/knowledge/{kb}").status_code == 204
    assert kb not in bases_of(studio) and find(studio, uid, "крыши", kb_ids=(kb,)) == []
    assert studio.c.post(f"/api/knowledge/{kb}/documents", content=b"x",
                         headers={"x-studio-name": "x.txt"}).status_code == 404
    listed = [(i["kind"], i["id"]) for i in studio.c.get("/api/trash").json()]
    assert listed == [("knowledge_bases", kb)]                    # документы базы — в её строке
    assert studio.c.post(f"/api/trash/knowledge_bases/{kb}/restore").status_code == 204
    assert len(bases_of(studio)[kb]["documents"]) == 2
    assert studio.c.delete(f"/api/knowledge/{kb}").status_code == 204
    assert studio.c.delete(f"/api/trash/knowledge_bases/{kb}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM kb_fts WHERE kb_fts MATCH 'крыши'").fetchone()[0] == 0


def test_texts_never_logged(studio, caplog):
    caplog.set_level(logging.DEBUG)
    gateway(studio)
    shared_source(studio)
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'admin'").fetchone()[0]
    kb = base(studio, "Тайная база")
    upload(studio, kb, "имя-файла-тайное.txt", "Тайный текст про щенка и собак.")
    settle(studio)
    find(studio, uid, "тайный запрос про пса", kb_ids=(kb,))
    cid = new_chat(studio)
    studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]})
    sse(turn(studio, cid, "вопрос про щенка"))
    doc = attach(studio, "большой-тайный.txt", big_text("секретная-фраза").encode())
    sse(turn(studio, cid, "где секретная-фраза", attachment_ids=[doc["id"]]))
    logged = caplog.text
    assert "знания" in logged
    for secret in ("Тайная база", "имя-файла-тайное", "Тайный текст", "тайный запрос", "щенка", "большой-тайный",
                   "секретная-фраза", "bge-m3"):
        assert secret not in logged, secret
