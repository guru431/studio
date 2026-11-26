"""Чтение Open WebUI через Студию: дерево, страницы чатов, чаты папки, поиск (запрос — телом), модели, чат в виде Студии,
файл потоком (`no-store`), 502 `owui_shape` на неожиданную форму, 503 `owui_offline` при молчащем сервере."""
import logging
import time

import httpx
import pytest
from fastapi import HTTPException

import owui
from conftest import make_user, signin
from fake_owui import FakeOwui, attach

BASE = "/api/owui/accounts"


@pytest.fixture
def setup(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    fake = attach(studio)
    r = studio.c.post(BASE, json={"label": "s06", "url": FakeOwui.URL, "email": FakeOwui.EMAIL,
                                  "password": FakeOwui.PASSWORD})
    assert r.status_code == 201, r.text
    return studio, fake, r.json()["id"]


def msg(mid, parent, role, text, **kw):
    return {"id": mid, "parentId": parent, "role": role, "content": text, "timestamp": 100, **kw}


def talk(fake, title="Talk", **kw):
    return fake.add_chat(title, messages=[
        msg("u1", None, "user", "hello", files=[{"type": "file", "id": "f1", "name": "doc.txt"}]),
        msg("a1", "u1", "assistant", "hi there", model="m1", done=True, output=[
            {"type": "reasoning", "id": "r1", "content": [{"type": "output_text", "text": "hmm"}], "duration": 2},
            {"type": "message", "id": "msg1", "content": [{"type": "output_text", "text": "hi there"}]}])],
        models=["m1"], **kw)


def test_tree_lists_pinned_folders_and_root_chats(setup):
    s, fake, aid = setup
    folder = fake.add_folder("Work")
    fake.add_chat("pinned one", pinned=True, updated_at=30)
    fake.add_chat("in folder", folder_id=folder, updated_at=20)
    fake.add_chat("old", updated_at=10)
    fake.add_chat("busy", updated_at=40, active=True)
    fake.add_chat("waiting", updated_at=50, pending_approval=True)
    r = s.c.get(f"{BASE}/{aid}/tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["title"] for c in body["pinned"]] == ["pinned one"]
    assert body["folders"] == [{"id": folder, "name": "Work", "parent_id": None}]
    assert [c["title"] for c in body["chats"]] == ["waiting", "busy", "old"]          # по свежести, без папок и закреплённых
    assert [(c["active"], c["pending"]) for c in body["chats"]] == [(False, True), (True, False), (False, False)]
    assert set(body["chats"][0]) == {"id", "title", "updated", "folder_id", "active", "pending"}
    fake.folders_enabled = False                                         # папки выключены сервером — дерево без них
    assert s.c.get(f"{BASE}/{aid}/tree").json()["folders"] == []


def test_chat_management_updates_remote_chat_without_replacing_history(setup):
    s, fake, aid = setup
    chat = talk(fake, updated_at=30)
    url = f"{BASE}/{aid}/chats/{chat}"
    assert s.c.patch(url, json={"title": "  Updated  "}).json()["title"] == "Updated"
    assert s.c.patch(url, json={"title": " "}).status_code == 422
    for desired in (True, True, False, True):
        assert s.c.put(url + "/pin", json={"pinned": desired}).json() == {"pinned": desired}
    pin_requests = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pin")]
    assert len(pin_requests) == 3
    view = s.c.get(url).json()
    assert (view["title"], view["updated"], view["pinned"]) == ("Updated", 30, True)
    assert [m["text"] for m in view["messages"]] == ["hello", "hi there"]
    shared = s.c.post(url + "/share", json={"pin": "4387", "days": 7})
    assert shared.status_code == 201, shared.text
    assert s.c.delete(url).status_code == 204
    assert s.c.get(url).status_code == 404
    assert s.c.get(url + "/share").json() == {"enabled": False}


def test_chat_pages_and_folder_chats(setup):
    s, fake, aid = setup
    for n in range(65):
        fake.add_chat(f"root {n}", updated_at=1000 - n)
    folder = fake.add_folder("Big")
    for n in range(23):
        fake.add_chat(f"in {n}", folder_id=folder, updated_at=500 - n)
    first, second = (s.c.get(f"{BASE}/{aid}/chats", params={"page": p}).json()["chats"] for p in (1, 2))
    assert (len(first), len(second)) == (60, 5) and second[0]["title"] == "root 60"
    inside = s.c.get(f"{BASE}/{aid}/folders/{folder}/chats").json()["chats"]
    assert len(inside) == 23 and len({c["id"] for c in inside}) == 23          # страницы Open WebUI по 10 склеены
    assert s.c.get(f"{BASE}/{aid}/folders/nope/chats").json() == {"chats": []}
    for bad in ("a%3Fb", "a%20b", "a%25b", "x" * 200):                    # в путь Open WebUI — только простые id
        assert s.c.get(f"{BASE}/{aid}/folders/{bad}/chats").status_code == 404
    for dots in ("..", ".", "a/b", ""):                                    # клиент теста сам схлопывает `..` в адресе
        with pytest.raises(HTTPException):
            owui._owui_id(dots, owui.NO_FOLDER)
    assert s.c.get(f"{BASE}/{aid}/chats", params={"page": 0}).status_code == 422


def test_models_hide_agent_models_and_pick_last_chat_model(setup):
    s, fake, aid = setup
    fake.add_model("m1", "Model One")
    fake.add_model("m2", "Model Two", vision=False)
    fake.add_model("agent:terminal", "Terminal agent")
    r = s.c.get(f"{BASE}/{aid}/models")
    assert r.status_code == 200, r.text
    assert r.json() == {"models": [{"id": "m1", "name": "Model One", "vision": True, "file_upload": True},
                                   {"id": "m2", "name": "Model Two", "vision": False, "file_upload": True}],
                        "default": "m1"}                                  # чатов нет — первая
    fake.add_chat("older", models=["m1"], updated_at=10, messages=[msg("u", None, "user", "x")])
    fake.add_chat("latest", models=["m2"], updated_at=20, messages=[msg("u", None, "user", "x")])
    assert s.c.get(f"{BASE}/{aid}/models").json()["default"] == "m2"        # модель последнего чата
    fake.chats = {k: v for k, v in fake.chats.items() if v["title"] != "latest"}
    fake.add_chat("agent chat", models=["agent:terminal"], updated_at=30, messages=[msg("u", None, "user", "x")])
    assert s.c.get(f"{BASE}/{aid}/models").json()["default"] == "m1"        # модели последнего чата нет в списке — первая
    fake.models.clear()
    assert s.c.get(f"{BASE}/{aid}/models").json() == {"models": [], "default": None}


def test_search_query_travels_in_body_only(setup):
    s, fake, aid = setup
    fake.add_chat("Alpha plan", updated_at=2)
    fake.add_chat("Beta", updated_at=1)
    r = s.c.post(f"{BASE}/{aid}/search", json={"text": "  alp "})
    assert r.status_code == 200 and [c["title"] for c in r.json()["chats"]] == ["Alpha plan"]
    assert r.request.url.query == b"" and "alp" not in str(r.request.url)       # строку запроса пишет журнал Apache
    (sent,) = [q for q in fake.requests if q.url.path == "/api/v1/chats/search"]
    assert sent.url.params["text"] == "alp"
    before = len(fake.requests)
    assert s.c.post(f"{BASE}/{aid}/search", json={"text": "   "}).json() == {"chats": []}
    assert len(fake.requests) == before                                         # пустой запрос — без обращения к серверу
    assert s.c.post(f"{BASE}/{aid}/search", json={"text": "x" * 300}).status_code == 422


def test_chat_view_is_normalized(setup):
    s, fake, aid = setup
    chat_id = talk(fake, active=True)
    r = s.c.get(f"{BASE}/{aid}/chats/{chat_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["title"], body["models"], body["active"]) == (chat_id, "Talk", ["m1"], True)
    user, answer = body["messages"]
    assert user["files"] == [{"id": "f1", "name": "doc.txt", "kind": "file"}]
    assert (answer["text"], answer["reasoning"], answer["model"]) == ("hi there", {"text": "hmm", "seconds": 2}, "m1")
    other = talk(fake, "Quiet")
    assert s.c.get(f"{BASE}/{aid}/chats/{other}").json()["active"] is False
    assert s.c.get(f"{BASE}/{aid}/chats/missing").json()["detail"]["code"] == "not_found"      # Open WebUI отвечает 401
    assert s.c.get(f"{BASE}/{aid}/chats/a%3Fb").status_code == 404
    # чужая учётка Студии видит одинаковый 404
    make_user(s, "other")
    signin(s, "other")
    assert s.c.get(f"{BASE}/{aid}/chats/{chat_id}").status_code == 404
    assert s.c.get(f"{BASE}/{aid}/tree").status_code == 404


def test_file_proxy_is_no_store(setup):
    s, fake, aid = setup
    fake.add_file("pic1", b"\x89PNG-bytes", "image/png")
    fake.add_file("page1", b"<script>alert(1)</script>", "text/html")
    r = s.c.get(f"{BASE}/{aid}/files/pic1")
    assert r.status_code == 200 and r.content == b"\x89PNG-bytes"
    assert r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"
    assert "content-disposition" not in r.headers and r.headers["x-content-type-options"] == "nosniff"
    page = s.c.get(f"{BASE}/{aid}/files/page1")
    assert page.headers["content-type"].startswith("text/html") and page.headers["content-disposition"] == "attachment"
    assert "sandbox" in page.headers["content-security-policy"]
    assert s.c.get(f"{BASE}/{aid}/files/nope").status_code == 404
    assert s.c.get(f"{BASE}/{aid}/files/a%3Fb").status_code == 404
    fake.expire_tokens()                       # рабочий токен отвергнут — одно обновление и повтор того же запроса
    assert s.c.get(f"{BASE}/{aid}/files/pic1").content == b"\x89PNG-bytes" and fake.refreshes == 1


def test_unexpected_shape_is_502_with_version(setup, caplog):
    s, fake, aid = setup
    caplog.set_level(logging.INFO, logger="studio")
    chat_id = fake.add_chat("Odd")
    fake.raw_chat[chat_id] = ["not", "a", "chat"]
    r = s.c.get(f"{BASE}/{aid}/chats/{chat_id}")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "owui_shape"
    assert "0.13.4" in r.json()["detail"]["message"]
    broken = fake.add_chat("Broken")
    fake.raw_chat[broken] = {"id": broken, "chat": {"history": {"messages": []}}}
    assert s.c.get(f"{BASE}/{aid}/chats/{broken}").json()["detail"]["code"] == "owui_shape"
    fake.models.append({"name": "no id"})
    assert s.c.get(f"{BASE}/{aid}/models").json()["detail"]["code"] == "owui_shape"
    assert "Odd" not in caplog.text and chat_id not in caplog.text            # журнал — без названий и id чатов


def test_offline_server_answers_503_quickly_and_studio_still_works(setup):
    s, fake, aid = setup
    fake.down = True
    started = time.perf_counter()
    for path in ("tree", "models", "chats", "chats/c1", "files/f1", "folders/f/chats"):
        r = s.c.get(f"{BASE}/{aid}/{path}")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "owui_offline", path
    assert s.c.post(f"{BASE}/{aid}/search", json={"text": "a"}).status_code == 503
    assert time.perf_counter() - started < owui.TIMEOUT_SEC
    assert s.c.get("/api/me").status_code == 200
    assert s.c.get(BASE).status_code == 200


def test_missing_chat_does_not_spend_token_rotation(setup):
    """Ревью I1: Open WebUI 0.13.4 на чужой и несуществующий чат отвечает 401 — это не отказ токену: Студия сверяет
    тот же токен `GET /api/v1/auths/` и пару не обновляет (ротация и предел сессий Open WebUI на адрес целы)."""
    s, fake, aid = setup
    for _ in range(10):
        r = s.c.get(f"{BASE}/{aid}/chats/missing")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    assert fake.refreshes == 0
    fake.expire_tokens()                       # а отвергнутый токен — ровно одно обновление
    assert s.c.get(f"{BASE}/{aid}/chats/missing").status_code == 404 and fake.refreshes == 1


def test_silent_server_ends_in_503_within_request_limit(setup):
    """Ревью M3: молчащий Open WebUI — каждый запрос со сроком не больше `owui.TIMEOUT_SEC`, истечение — 503
    `owui_offline` (MockTransport сроки не соблюдает: молчание — его исключение)."""
    s, fake, aid = setup
    limits = []

    async def silent(request):
        limits.append(request.extensions["timeout"])
        raise httpx.ReadTimeout("timed out", request=request)

    s.gw.hosts[FakeOwui.ENTRY] = silent
    for path in ("tree", "models", "chats", "chats/c1", "files/f1", "folders/f/chats"):
        r = s.c.get(f"{BASE}/{aid}/{path}")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "owui_offline", path
    assert limits and all(0 < value <= owui.TIMEOUT_SEC for limit in limits for value in limit.values())
