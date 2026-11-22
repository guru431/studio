"""«Защищённый источник» MCP (спека 2026-11-21-studio-source-capabilities-design, п. 14): после успешного вызова
инструмента такого общего сервера «Интернет» диалога выключается и больше не включается, веб-инструменты не
предлагаются, а аргументы со ссылками отклоняются. Данные источника не уходят из Студии и мимо хода: ссылки
«Поделиться», экспорт и архив такого диалога, ссылки «Моих файлов» на защищённом сервере закрыты."""
import json
from urllib.parse import urlsplit

import chat_tools
import mcp_client
import memory
import state
import web
from conftest import signin
from test_chat import messages, new_chat, sse, turn
from test_chat_pending import answer, pending_of
from test_chat_web import EXA_KEY, call
from test_mcp import URL as MCP_URL
from test_mcp_dialog import methods, name_of, offered, setup, tool, view
from test_takeout import download, unzip

ALL = ["web_search", "read_page", "youtube_subtitles"]


def names(request) -> list[str]:
    """Инструменты «Интернета» запроса — без MCP и встроенных."""
    return [t["function"]["name"] for t in request.get("tools", []) if t["function"]["name"] in web.NAMES]


def protect(studio, sid, on=True):
    """Флажок «Защищённый источник» общего сервера — администратором; вход — снова участником."""
    signin(studio, "admin")
    saved = studio.c.put(f"/api/admin/shared/mcp/{sid}", json={"name": "Shared tools", "url": MCP_URL, "protected": on})
    assert saved.status_code == 200, saved.text
    assert saved.json()["protected"] is on
    assert studio.c.get(f"/api/admin/shared/mcp/{sid}").json()["protected"] is on
    signin(studio, "member")


def web_dialog(studio) -> str:
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{cid}", json={"web": True}).json()["web"] is True
    return cid


def flags(cid) -> tuple[int, int]:
    row = state.DB.execute("SELECT protected_used, web FROM chats WHERE id = ?", (cid,)).fetchone()
    return row[0], row[1]


def test_protected_call_turns_web_off_and_locks_it(studio):
    sid, calls, _ = setup(studio)
    protect(studio, sid)
    # личному серверу флажок не положен
    personal = studio.c.post("/api/mcp", json={"name": "Mine", "url": MCP_URL, "protected": True})
    assert personal.status_code == 422 and personal.json()["detail"]["code"] == "invalid"
    cid = web_dialog(studio)
    assert view(studio, cid)["protected"] is False
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert "tools/call" in methods(calls)
    assert flags(cid) == (1, 0)
    shown = view(studio, cid)
    assert shown["protected"] is True and shown["web"] is False
    refused = studio.c.patch(f"/api/chats/{cid}", json={"web": True})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "protected"
    assert flags(cid) == (1, 0)
    assert studio.c.patch(f"/api/chats/{cid}", json={"web": False}).status_code == 200


def test_protected_error_result_locks_too(studio):
    """Срабатывание — любой вызов, дошедший до сервера: ответ-ошибка тоже несёт его данные модели
    («SIM 79… не найдена»)."""
    sid, calls, _ = setup(studio, tools=[tool("gone")])
    protect(studio, sid)
    cid = web_dialog(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert "tools/call" in methods(calls)
    assert "error" in messages(studio, cid)[-1]["tools"][0]
    assert flags(cid) == (1, 0)


def test_refused_before_the_call_changes_nothing(studio):
    """Отказ самой студии до запроса к серверу (неверные аргументы) — не срабатывание: «Интернет» не трогает."""
    sid, calls, _ = setup(studio)
    protect(studio, sid)
    cid = web_dialog(studio)
    studio.gw.chat_script = [{"tool_calls": [(name_of(sid), "[1]")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert "tools/call" not in methods(calls)
    assert messages(studio, cid)[-1]["tools"][0]["error"] == "Неверные аргументы инструмента MCP"
    assert flags(cid) == (0, 1)


def test_after_protected_web_and_open_world_tools_not_offered(studio):
    sid, _, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid)
    cid = web_dialog(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    first, second = studio.gw.chat_requests
    assert names(first) == ALL and offered(first) == ["Shared tools: lookup", "Shared tools: search"]
    # тот же ответ после срабатывания: ни «Интернета», ни веб-инструментов MCP
    assert names(second) == [] and offered(second) == ["Shared tools: lookup"]
    studio.gw.chat_script = [["Ещё"]]
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})
    third = studio.gw.chat_requests[-1]
    assert names(third) == [] and offered(third) == ["Shared tools: lookup"]
    # «Интернет» включили в обход страницы — следующий ход всё равно без веб-инструментов
    state.DB.execute("UPDATE chats SET web = 1 WHERE id = ?", (cid,))
    studio.gw.chat_script = [["Снова"]]
    sse(turn(studio, cid, "Снова"))
    last = studio.gw.chat_requests[-1]
    assert names(last) == [] and offered(last) == ["Shared tools: lookup"]


def test_url_in_arguments_refused_after_protected(studio):
    sid, calls, _ = setup(studio)
    protect(studio, sid)
    cid = new_chat(studio)
    # ссылка до срабатывания — можно (это и есть вызов защищённого сервера); после — отказ без вызова
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="https://before.example")]},
                             {"tool_calls": [call(name_of(sid), q="see https://after.example/x"),
                                             call(name_of(sid), deep={"list": ["x", "www.after.example"]}),
                                             call("ask_user", question="Открыть http://after.example?")]},
                             ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert [p["arguments"] for m, p in calls if m == "tools/call"] == [{"q": "https://before.example"}]
    steps = messages(studio, cid)[-1]["tools"]
    assert [s.get("error") for s in steps] == [None] + [mcp_client.URL_REFUSED] * 3
    assert steps[1] == {"kind": "mcp", "server": "Shared tools", "tool": "lookup", "error": mcp_client.URL_REFUSED}
    sent = [m["content"] for m in studio.gw.chat_requests[-1]["messages"] if m["role"] == "tool"]
    assert all(mcp_client.URL_REFUSED in text for text in sent[1:])
    # и в следующих ходах — по отметке диалога, регистр не спасает; без ссылки — вызов идёт
    calls.clear()
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="HTTP://X.example"), call(name_of(sid), q="2")]},
                             ["Готово"]]
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})
    assert [p["arguments"] for m, p in calls if m == "tools/call"] == [{"q": "2"}]


def test_memory_not_written_after_protected(studio):
    """Личная память идёт в системное сообщение всех диалогов (и с «Интернетом», и прямых облачных): после
    защищённого источника `memory_save` — шаг-отказ chat_tools.MEMORY_PROTECTED, в этом ходе и в следующих."""
    sid, calls, _ = setup(studio)
    protect(studio, sid)
    assert studio.c.patch("/api/me", json={"memory_on": True}).json()["memory_on"] is True
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'member'").fetchone()[0]
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="79001234567")]},
                             {"tool_calls": [call("memory_save", text="SIM 79001234567, тариф X")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Проверь номер"))[-1] == ("done", {"status": "done"})
    steps = messages(studio, cid)[-1]["tools"]
    assert steps[1] == {"kind": "memory", "action": "save", "error": chat_tools.MEMORY_PROTECTED}
    assert memory.list_for(uid) == []
    studio.gw.chat_script = [{"tool_calls": [call("memory_save", text="SIM 79001234567")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Запомни"))[-1] == ("done", {"status": "done"})
    assert messages(studio, cid)[-1]["tools"][0]["error"] == chat_tools.MEMORY_PROTECTED
    assert memory.list_for(uid) == []
    other = new_chat(studio)                        # в обычном диалоге память пишется
    studio.gw.chat_script = [{"tool_calls": [call("memory_save", text="любит краткость")]}, ["Готово"]]
    shown = pending_of(sse(turn(studio, other, "Запомни")))
    assert memory.list_for(uid) == []
    assert sse(answer(studio, other, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    assert [i["text"] for i in memory.list_for(uid)] == ["любит краткость"]


def protected_dialog(studio, sid, cid=None) -> str:
    """Диалог, где вызов дошёл до защищённого сервера (`chats.protected_used`)."""
    cid = cid or new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="79001234567")]}, ["Номер найден"]]
    assert sse(turn(studio, cid, "Проверь номер"))[-1] == ("done", {"status": "done"})
    assert flags(cid)[0] == 1
    return cid


def code_of(response, status=409) -> str:
    assert response.status_code == status, response.text
    return response.json()["detail"]["code"]


def test_protected_chat_cannot_be_shared_or_exported(studio):
    """Данные источника не уходят из Студии и мимо хода: ссылка «Поделиться» — 409, прежняя ссылка — «недоступна»
    с того же мгновения и навсегда (как у чата в корзине), Markdown, PDF, выделенное и архив ZIP — 409."""
    sid, _, _ = setup(studio)
    protect(studio, sid)
    cid = new_chat(studio)
    studio.gw.chat_script = [["Обычный ответ"]]
    assert sse(turn(studio, cid, "Привет"))[-1] == ("done", {"status": "done"})
    made = studio.c.post(f"/api/chats/{cid}/share", json={"pin": "0123"})
    assert made.status_code == 201, made.text
    token = urlsplit(made.json()["url"]).fragment
    opened = studio.c.post("/share/unlock", json={"token": token, "pin": "0123"})
    assert opened.status_code == 200
    access = opened.json()["access"]
    early = studio.c.post(f"/api/chats/{cid}/archive", json={})
    assert early.status_code == 200
    protected_dialog(studio, sid, cid)
    gone = studio.c.post("/share/unlock", json={"token": token, "pin": "0123"})
    assert code_of(gone, 404) == "share_gone"
    assert code_of(studio.c.post("/share/check", json={"access": access}), 404) == "share_gone"
    shares = studio.c.get("/api/me/access").json()["shares"]
    assert [(s["protected"], s["trashed"]) for s in shares] == [(True, False)]       # остаётся только отключить
    assert code_of(studio.c.post(f"/api/chats/{cid}/share", json={"pin": "0123"})) == "protected"
    for body in ({"format": "md"}, {"format": "pdf"}, {"format": "pdf", "selection": "<p>Номер найден</p>"}):
        assert code_of(studio.c.post(f"/api/chats/{cid}/export", json=body)) == "protected"
    assert code_of(studio.c.post(f"/api/chats/{cid}/archive", json={})) == "protected"
    assert code_of(studio.c.get(f"/api/chats/archive/{early.json()['token']}")) == "protected"
    assert [c["protected"] for c in studio.c.get("/api/chats").json()["items"]] == [True]
    # навсегда: и «Вернуть» из корзины ссылку не оживит
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204
    assert studio.c.post(f"/api/trash/chats/{cid}/restore").status_code == 204
    assert code_of(studio.c.post("/share/unlock", json={"token": token, "pin": "0123"}), 404) == "share_gone"
    # обычный диалог той же учётки — как прежде
    other = new_chat(studio)
    assert studio.c.post(f"/api/chats/{other}/export", json={"format": "md"}).status_code == 200
    assert studio.c.post(f"/api/chats/{other}/share", json={"pin": "0123"}).status_code == 201


def test_history_export_keeps_only_metadata_of_protected_chat(studio):
    sid, _, _ = setup(studio)
    protect(studio, sid)
    plain = new_chat(studio)
    studio.gw.chat_script = [["Обычный ответ"]]
    assert sse(turn(studio, plain, "Привет"))[-1] == ("done", {"status": "done"})
    cid = protected_dialog(studio, sid)
    _, z = download(studio, ["chat"])
    zf = unzip(z)
    kept = json.loads(zf.read(f"chats/{cid}.json"))
    assert kept["protected"] is True and kept["messages"] == [] and kept["media"] == []
    assert {"id", "title", "model", "created", "updated"} <= kept.keys()
    assert "79001234567" not in z.content.decode("utf-8", "replace")
    assert "Номер найден" not in z.content.decode("utf-8", "replace")
    assert [m["content"] for m in json.loads(zf.read(f"chats/{plain}.json"))["messages"]] == ["Привет", "Обычный ответ"]
    assert "защищённых источников MCP не выгружаются" in zf.read("README.txt").decode("utf-8")


def test_non_protected_dialog_unchanged(studio):
    sid, calls, _ = setup(studio)
    cid = web_dialog(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="https://ok.example")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert [p["arguments"] for m, p in calls if m == "tools/call"] == [{"q": "https://ok.example"}]
    assert flags(cid) == (0, 1)
    assert names(studio.gw.chat_requests[1]) == ALL
    shown = view(studio, cid)
    assert shown["protected"] is False and shown["web"] is True
    signin(studio, "admin")
    assert studio.c.get(f"/api/admin/shared/mcp/{sid}").json()["protected"] is False
