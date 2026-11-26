"""Статистика учётки и файлы диалога «Чата» (app/chat_info.py; спека 2026-11-25-studio-skills-status-local-models,
п. 14 и 15): ответы, токены и время генерации по дням пояса учётки и моделям за `chat_info.STATS_DAYS` дней; вложения,
медиа диалога и файлы результатов MCP одним списком со ссылками на прежние маршруты. Чужое — мимо."""
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import media
import state
from conftest import add_gateway, make_user, signin
from test_chat import attach, messages, new_chat, png, sse, turn


def answer(chat_id, created, model, *, usage=None, gen_ms=None, unchosen=0, status="done", tools=None) -> str:
    """Ответ прямой строкой `messages`: числа задаются тестом, а не записью хода."""
    mid = uuid.uuid4().hex
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, usage, gen_ms,"
                     " unchosen, tools) VALUES (?, ?, 'assistant', 'ответ', ?, 'server', ?, ?, ?, ?, ?, ?)",
                     (mid, chat_id, status, model, created, json.dumps(usage) if usage else None, gen_ms, unchosen,
                      json.dumps(tools) if tools else None))
    return mid


def test_stats_group_answers_by_account_day_and_model(studio):
    add_gateway(studio)
    now = studio.clock[0]
    cid, trashed = new_chat(studio), new_chat(studio)
    state.DB.execute("UPDATE chats SET deleted = ? WHERE id = ?", (now, trashed))
    other = make_user(studio, "other")
    foreign = uuid.uuid4().hex
    state.DB.execute("INSERT INTO chats(id, user_id, created, updated) VALUES (?, ?, ?, ?)", (foreign, other, now, now))
    today = datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow")).date()
    answer(cid, now, "m1", usage={"in": 10, "out": 4}, gen_ms=400)
    answer(cid, now, "m1", usage={"in": 20, "out": 8, "cached": 5}, gen_ms=500, unchosen=1)   # вариант тоже считается
    answer(cid, now, "m2")
    answer(cid, now - 30 * 86400, "m1")
    answer(cid, now - 31 * 86400, "m1")                                   # 31-й день назад — мимо окна
    answer(cid, now, "m1", status="streaming")                            # идущий — мимо
    answer(trashed, now, "m1")                                            # диалог в корзине — мимо
    answer(foreign, now, "m1")                                            # чужой диалог — мимо

    def row(day, model, n, i, o, g):
        return {"date": day.isoformat(), "model": model, "answers": n, "in": i, "out": o, "gen_ms": g}

    assert studio.c.get("/api/chats/stats").json()["days"] == [
        row(today, "m1", 2, 30, 12, 900), row(today, "m2", 1, None, None, None),
        row(today - timedelta(days=30), "m1", 1, None, None, None)]
    assert len(studio.c.get("/api/chats/stats?days=1").json()["days"]) == 2
    for bad in ("32", "0", "x"):
        assert studio.c.get(f"/api/chats/stats?days={bad}").status_code == 422
    far = datetime.fromtimestamp(now, ZoneInfo("Pacific/Pago_Pago")).date()
    assert far != today
    state.DB.execute("UPDATE users SET timezone = 'Pacific/Pago_Pago'")
    assert studio.c.get("/api/chats/stats?days=1").json()["days"][0]["date"] == far.isoformat()
    signin(studio, "other")
    assert studio.c.get("/api/chats/stats").json()["days"] == [row(far, "m1", 1, None, None, None)]   # только свой


def test_files_list_attachments_media_and_mcp_results(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    txt = attach(studio, "notes.txt", b"word " * 10, "text/plain")
    pic = attach(studio, "p.png", png(), "image/png")
    sse(turn(studio, cid, "Посмотри", attachment_ids=[txt["id"], pic["id"]]))
    uid = state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    now = studio.clock[0]
    mp3 = media.save(uid, b"ID3" + b"\0" * 64, "audio/mpeg", "call.mp3")
    gone = media.save(uid, b"ID3" + b"\0" * 32, "audio/mpeg", "old.mp3")
    for name, media_id, deleted in (("call.mp3", mp3, None), ("old.mp3", gone, now)):
        state.DB.execute("INSERT INTO chat_media(id, chat_id, user_id, name, mime, size, media_id, created, deleted)"
                         " VALUES (?, ?, ?, ?, 'audio/mpeg', 67, ?, ?, ?)",
                         (uuid.uuid4().hex, cid, uid, name, media_id, now, deleted))
    step = {"kind": "mcp", "server": "Shared tools", "tool": "plot", "sid": 1, "stamp": "s",
            "files": [{"aid": "art_1", "mime": "image/png", "name": "plot.png"}]}
    mid = messages(studio, cid)[-1]["id"]
    state.DB.execute("UPDATE messages SET tools = ? WHERE id = ?", (json.dumps([step]), mid))
    answer(cid, now + 1, "local-gemma-abl", unchosen=1, tools=[step])    # невыбранный вариант — мимо списка
    media_of = {a["name"]: a["media_id"] for a in messages(studio, cid)[0]["attachments"]}

    items = studio.c.get(f"/api/chats/{cid}/files").json()["items"]
    assert studio.c.get(f"/api/chats/{cid}").json()["file_count"] == len(items)
    assert [(i["kind"], i["name"], i["mime"], i["size"] is None) for i in items] == [
        ("attachment", "notes.txt", "text/plain", False), ("attachment", "p.png", "image/png", False),
        ("media", "call.mp3", "audio/mpeg", False), ("result", "plot.png", "image/png", True)]
    assert items[2]["size"] == 67
    assert [i["url"] for i in items] == [
        f"/api/media/{media_of['notes.txt']}?download=1", f"/api/media/{media_of['p.png']}",
        f"/api/media/{mp3}?download=1", f"/api/chats/{cid}/messages/{mid}/files/0/0"]
    for item in items[:3]:                                                 # существующие маршруты и их проверки
        assert studio.c.get(item["url"]).status_code == 200
    make_user(studio, "other")
    signin(studio, "other")
    assert studio.c.get(f"/api/chats/{cid}/files").status_code == 404
    assert studio.c.get(items[0]["url"]).status_code == 404
