"""Вложения сообщения чата (спека 2026-11-21-studio-source-capabilities-design, п. 30): до `chat.ATTACH_MAX` разных
вложений, картинок модели — не больше предела подключения (`profiles.max_images`, прежние — пометкой `chat.DROPPED`),
MCP с согласием получает все; текстовые файлы и код — документы, код — блоком с языком."""
import base64
import io

from PIL import Image

import chat
import connections
import mcp_files
import profiles
import state
from conftest import add_gateway
from test_chat import attach, messages, new_chat, png, sse, turn
from test_mcp_chat_files import consent, send, setup, uploads

COLORS = ["red", "green", "blue", "yellow", "white"]


def user_parts(studio, n=-1) -> list:
    """Части последнего сообщения пользователя в запросе `n` к модели."""
    return [m for m in studio.gw.chat_requests[n]["messages"] if m["role"] == "user"][-1]["content"]


def conn_of(conn_id):
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (conn_id,)).fetchone()[0]
    return connections.get(uid, conn_id)


def color_of(part) -> tuple:
    data = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 0))


def test_ten_mixed_attachments_accepted_eleventh_refused(studio):
    add_gateway(studio); cid = new_chat(studio)
    pics = [attach(studio, f"p{i}.png", png(COLORS[i])) for i in range(5)]
    docs = [attach(studio, name, data) for name, data in (
        ("a.txt", b"text"), ("b.csv", b"x;y\n1;2"), ("c.json", b'{"a": 1}'), ("d.py", b"print(1)"), ("e.md", b"# M"))]
    extra = attach(studio, "f.log", b"line")
    assert chat.ATTACH_MAX == 10 and {d["kind"] for d in docs} == {"document"}
    ids = [a["id"] for a in pics + docs]
    r = turn(studio, cid, "Смотри", attachment_ids=[*ids, extra["id"]])
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "attachments", "message": "не больше 10 вложений в сообщении"}
    assert messages(studio, cid) == [] and not studio.gw.chat_requests
    sse(turn(studio, cid, "Смотри", attachment_ids=ids))
    assert len(messages(studio, cid)[0]["attachments"]) == 10
    parts = user_parts(studio)
    assert sum(p["type"] == "image_url" for p in parts) == profiles.DEFAULT_MAX_IMAGES
    assert '<document name="b.csv">' in parts[0]["text"] and '<document name="d.py">' in parts[0]["text"]


def test_five_photos_with_limit_four_newest_four_reach_model(studio):
    add_gateway(studio); cid = new_chat(studio)
    pics = [attach(studio, f"p{i}.png", png(COLORS[i])) for i in range(5)]
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[p["id"] for p in pics]))
    parts = user_parts(studio)
    dropped = chat.DROPPED.format(4)
    assert parts[0] == {"type": "text", "text": "Что на фото?"}
    assert parts[1] == {"type": "text", "text": dropped}
    assert [p["type"] for p in parts[2:]] == ["image_url"] * 4
    expected = [Image.new("RGB", (1, 1), c).getpixel((0, 0)) for c in COLORS[1:]]
    assert [color_of(p) for p in parts[2:]] == expected
    assert sum(p.get("text") == dropped for p in parts) == 1
    assert len(messages(studio, cid)[0]["attachments"]) == 5      # в истории — все


def test_profile_max_images_overrides_default_and_stays_out_of_request(studio):
    conn_id = add_gateway(studio); cid = new_chat(studio)
    url = f"/api/connections/{conn_id}/profile"
    assert profiles.max_images(conn_of(conn_id)) == profiles.DEFAULT_MAX_IMAGES == 4
    for bad in (0, 33, 1.5, True, "2"):
        assert studio.c.put(url, json={"params": {"max_images": bad}}).status_code == 422, bad
    r = studio.c.put(url, json={"params": {"max_images": 2, "temperature": 0.5}})
    assert r.status_code == 200 and r.json()["params"] == {"max_images": 2, "temperature": 0.5}
    conn = conn_of(conn_id)
    assert profiles.max_images(conn) == 2 and profiles.request_params(conn) == {"temperature": 0.5}
    pics = [attach(studio, f"p{i}.png", png(COLORS[i])) for i in range(3)]
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[p["id"] for p in pics]))
    body = studio.gw.chat_requests[-1]
    assert "max_images" not in body and body["temperature"] == 0.5
    parts = user_parts(studio)
    assert [p["type"] for p in parts] == ["text", "text", "image_url", "image_url"]
    assert parts[1]["text"] == chat.DROPPED.format(2)


def test_mcp_with_consent_gets_all_photos(studio, monkeypatch):
    # паузы пингов потока двигают часы теста на PING_SEC, пока идут загрузки: срок хода здесь не проверяется
    monkeypatch.setattr(mcp_files, "SYNC_SEC", 10 ** 6)
    sid, calls = setup(studio)
    cid = new_chat(studio)
    consent(studio, cid, sid)
    assert mcp_files.PER_TURN == 10
    pics = [attach(studio, f"p{i}.png", png((i * 20, 0, 0))) for i in range(10)]
    send(studio, cid, "Что на фото?", attachment_ids=[p["id"] for p in pics])
    assert sorted(u["name"] for u in uploads(calls)) == sorted(f"p{i}.png" for i in range(10))
    assert sum(p["type"] == "image_url" for p in user_parts(studio)) == profiles.DEFAULT_MAX_IMAGES


def test_code_file_becomes_fenced_document(studio):
    add_gateway(studio); cid = new_chat(studio)
    code = "def f():\n    '''```не конец```'''\n    return 1\n"
    att = attach(studio, "main.py", code.encode())
    assert att["kind"] == "document" and att["chars"] == len(code)
    sse(turn(studio, cid, "Что делает?", attachment_ids=[att["id"]]))
    sent = user_parts(studio)
    assert sent == f'<document name="main.py">\n````python\n{code}\n````\n</document>\n\nЧто делает?'
    assert documents_lang(".ps1") == "powershell" and documents_lang(".csv") is None


def documents_lang(suffix):
    import documents
    return documents.LANG.get(suffix)


def test_cp1251_text_file_decoded(studio):
    add_gateway(studio); cid = new_chat(studio)
    text = "Имя;Сумма\nИванов;100"
    att = attach(studio, "отчёт.csv", text.encode("cp1251"))
    assert att["kind"] == "document" and att["chars"] == len(text)
    sse(turn(studio, cid, "Итог?", attachment_ids=[att["id"]]))
    assert user_parts(studio) == f'<document name="отчёт.csv">\n{text}\n</document>\n\nИтог?'
    assert chat.DOC_MIME[".csv"] == "text/csv"
