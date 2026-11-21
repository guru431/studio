"""Длинный текст «Озвучки» (п. 27 спеки 2026-11-21): части одного чата по порядку, очередь по чатам, один mp3
готовых частей; язык озвучки шлюза (п. 26)."""
import re

import speeches
import state
from conftest import add_gateway
from fakes import WAV_FAKE
from test_speeches import voice, wait_sp


def paragraph(n: int, size: int = 1500) -> str:
    words, text = [], f"Абзац {n}."
    while len(text) < size:
        text += f" Слово{n} номер {len(words)}."
        words.append(text)
    return text


def chat_rows(cid: str):
    return state.DB.execute("SELECT * FROM speeches WHERE chat_id = ? ORDER BY created, rowid", (cid,)).fetchall()


def test_long_text_split_into_parts_of_one_chat_in_order(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    text = "\n\n".join(paragraph(n) for n in range(3))
    assert len(text) > speeches.TEXT_MAX
    r = studio.c.post("/api/speeches", json={"text": text, "voice": vid})
    assert r.status_code == 202, r.text
    cid = r.json()["chat_id"]
    rows = chat_rows(cid)
    assert [row["text"] for row in rows] == speeches.split_text(text, speeches.TEXT_MAX)
    assert len(rows) == 3 and all(len(row["text"]) <= speeches.TEXT_MAX for row in rows)
    assert {row["chat_created"] for row in rows} == {rows[0]["chat_created"]}
    assert [wait_sp(studio, row["id"], {"done", "error"})["state"] for row in rows] == ["done"] * 3
    # части исполнялись по порядку: первая фраза каждой части ушла на шлюз раньше следующей
    sent = [q.content.decode("utf-8", "replace") for q in studio.gw.tts_requests]
    firsts = [next(i for i, body in enumerate(sent) if f"Абзац {n}." in body) for n in range(3)]
    assert firsts == sorted(firsts)
    over = studio.c.post("/api/speeches", json={"text": "я " * (speeches.LONG_MAX // 2 + 1), "voice": vid})
    assert over.status_code == 422 and over.json()["detail"]["code"] == "text"


def test_split_never_exceeds_limit_and_keeps_all_words():
    text = ("Первое предложение абзаца. " * 40 + "\n\n" + "Без точек " * 300 + "\n" + "слово" * 90 + " конец.")
    for limit in (speeches.TEXT_MAX, speeches.EXTERNAL_MAX, 120):
        parts = speeches.split_text(text, limit)
        assert parts and all(0 < len(p) <= limit for p in parts)
        assert "".join("".join(parts).split()) == "".join(text.split())
    plain = "Раз два три. Четыре пять!\n\nШесть семь восемь девять десять."
    parts = speeches.split_text(plain, 20)
    assert " ".join(parts).split() == plain.split() and all(len(p) <= 20 for p in parts)
    assert speeches.split_text("Короткий текст.", 100) == ["Короткий текст."]
    # по абзацам раньше, чем по предложениям
    assert speeches.split_text("Абзац один.\n\nАбзац два.", 15) == ["Абзац один.", "Абзац два."]


def test_queue_counts_chats_not_parts(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.tts_replies = [(503, {"detail": "busy"})]                 # первая часть ждёт карту
    text = "\n\n".join(paragraph(n) for n in range(3))
    first = studio.c.post("/api/speeches", json={"text": text, "voice": vid}).json()
    assert len(chat_rows(first["chat_id"])) == 3
    others = [studio.c.post("/api/speeches", json={"text": f"Фраза {n}.", "voice": vid})
              for n in range(speeches.QUEUE_MAX - 1)]
    assert all(r.status_code == 202 for r in others), [r.text for r in others]
    extra = studio.c.post("/api/speeches", json={"text": "Лишняя.", "voice": vid})
    assert extra.status_code == 409 and extra.json()["detail"]["code"] == "busy"
    # ещё запись в чат, который уже в очереди, нового чата не добавляет
    more = studio.c.post("/api/speeches", json={"text": "Ещё.", "voice": vid, "chat_id": first["chat_id"]})
    assert more.status_code == 202, more.text


def test_chat_audio_concatenates_done_parts(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    text = "\n\n".join(paragraph(n) for n in range(2))
    cid = studio.c.post("/api/speeches", json={"text": text, "voice": vid}).json()["chat_id"]
    rows = chat_rows(cid)
    assert len(rows) == 2
    for row in rows:
        wait_sp(studio, row["id"], {"done"})
    fake_audio.calls.clear()
    r = studio.c.get(f"/api/speeches/chats/{cid}/audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    medias = [state.DB.execute("SELECT media_id FROM speeches WHERE id = ?", (row["id"],)).fetchone()[0] for row in rows]
    expected = b"".join(studio.c.get(f"/api/media/{m}").content for m in medias)
    assert r.content == expected and ("mp3_of", 2) in fake_audio.calls
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "filename*=UTF-8''" in disposition
    assert re.search(r"filename\*=UTF-8''%D0%90%D0%B1%D0%B7%D0%B0%D1%86", disposition)       # «Абзац…»
    assert studio.c.get(f"/api/speeches/chats/{'0' * 32}/audio").status_code == 404


def test_chat_audio_without_ready_parts_is_refused(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.tts_replies = [(503, {"detail": "busy"})]
    cid = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()["chat_id"]
    r = studio.c.get(f"/api/speeches/chats/{cid}/audio")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_ready"


def test_tts_language_sent_to_gateway(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    studio.gw.tts_replies = [(200, WAV_FAKE)]
    plain = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()
    wait_sp(studio, plain["id"], {"done"})
    english = studio.c.post("/api/speeches", json={"text": "Hello.", "voice": vid, "language": "en",
                                                   "model": "f5-tts"}).json()
    assert wait_sp(studio, english["id"], {"done"})["language"] == "en"
    first, second = studio.gw.tts_requests
    assert b'name="language"\r\n\r\nRussian' in first.content
    assert b'name="language"\r\n\r\nEnglish' in second.content
    assert state.DB.execute("SELECT language FROM speeches WHERE id = ?", (english["id"],)).fetchone()[0] == "en"
    assert studio.c.post("/api/speeches", json={"text": "Hi.", "voice": vid, "language": "de"}).status_code == 422
