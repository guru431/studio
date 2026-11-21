"""«Прочитать» у ответа чата (п. 12 спеки 2026-11-21): проигрыватель под ответом играет озвучку источником «Озвучки»
по умолчанию и скачивает её одним mp3; счётчик «Озвучки» говорит, сколько частей даст длинный текст (Chromium 320/1280)."""
import time
import uuid

import pytest

from test_ui_browser import (NO_OVERFLOW, READY, add_gateway, add_voice, browser, go, live,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PLAYER = ".msg.assistant .msg-read .read-player"
LONG = "\\n\\n".join(f"Абзац {n}. " + "Слово номер такое. " * 80 for n in range(3))


def answer_chat(live, uid, text="**Короткий** ответ модели.") -> str:
    import db
    cid, now = uuid.uuid4().hex, time.time()
    conn = db.connect(live.db_path)
    try:
        conn.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, 'чтение', ?, ?)",
                     (cid, uid, now, now))
        for n, (role, content) in enumerate([("user", "вопрос"), ("assistant", text)]):
            conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                         " VALUES (?, ?, ?, ?, 'done', 'server', 'local-gemma-abl', ?)",
                         (uuid.uuid4().hex, cid, role, content, now + n))
    finally:
        conn.close()
    return cid


@pytest.mark.parametrize("width", [320, 1280])
def test_long_read_is_cancelled_as_a_whole(browser, live, width):
    """C-I3: «Отменить» в проигрывателе «Прочитать» отменяет весь чат; повторное «Прочитать» отменяет прежний чат;
    «Отменить» в «Озвучке» — весь чат из нескольких записей."""
    from fakes import WAV_FAKE

    login = f"readcancel{width}"
    cid = answer_chat(live, live.add_user(login), LONG.replace("\\n", "\n"))
    gw = live.gw
    gw.tts_replies = [(503, {"detail": "busy"})]             # первая часть ждёт карту — чат идёт
    cancel = f"{PLAYER} .read-cancel"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), add_voice(),
            *go(f"chat/{cid}"), ("wait", "document.querySelector('.msg.assistant .msg-read')"),
            ("click", ".msg.assistant .msg-read"),
            ("wait", f"document.querySelector('{cancel}') && !document.querySelector('{cancel}').hidden"),
            ("click", cancel),
            ("wait", f"document.querySelector('{cancel}').hidden"),
            ("click", ".msg.assistant .msg-read"),
            ("wait", f"document.querySelector('{cancel}') && !document.querySelector('{cancel}').hidden"),
            ("click", ".msg.assistant .msg-read"),                 # заменяет прежний — его чат отменяется
            ("wait", f"document.querySelector('{cancel}') && !document.querySelector('{cancel}').hidden"),
            *go("speech"), ("wait", "document.querySelector('.sp-item')"),
            *([("click", ".split-toggle")] if width == 320 else []), ("click", ".sp-item"),
            ("wait", "!document.querySelector('#sp-cancel-chat').hidden"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#sp-cancel-chat"),
            ("wait", "document.querySelector('#sp-cancel-chat').hidden"),
            ("eval", "chats", "fetch('/api/speeches/chats').then(r => r.json()).then(b => b.items.map(c => c.state))")],
            width=width, height=900, mobile=width == 320, timeout=30000)
    finally:
        gw.tts_replies = [(200, WAV_FAKE)]
    assert not r.get("failed"), r
    cancels = [s for s in r["sent"] if s.startswith("POST /api/speeches/chats/") and s.endswith("/cancel")]
    assert len(cancels) == 3 and len(set(cancels)) == 3, r["sent"]
    assert r["values"]["chats"] == ["cancelled"] * 3 and r["values"]["fits"], r["values"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_read_answer_plays_and_downloads(browser, live, width):
    login = f"read{width}"
    cid = answer_chat(live, live.add_user(login))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), add_voice(),
        *go(f"chat/{cid}"), ("wait", "document.querySelector('.msg.assistant .msg-read')"),
        ("click", ".msg.assistant .msg-read"),
        ("wait", f"document.querySelector('{PLAYER} audio')?.getAttribute('src')"),
        ("wait", f"!document.querySelector('{PLAYER} .read-download').hidden"),
        ("eval", "src", f"document.querySelector('{PLAYER} audio').getAttribute('src')"),
        ("eval", "file", f"fetch(document.querySelector('{PLAYER} .read-download').href).then(r =>"
                         " [r.status, r.headers.get('content-type'), r.headers.get('content-disposition')])"),
        ("eval", "fits", NO_OVERFLOW),
        *go("speech"), ("wait", "document.querySelector('#chip-voice option')"),
        ("eval", "fill", f"(() => {{ const t = document.querySelector('#speech-text'); t.value = '{LONG}';"
                         " t.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"),
        ("eval", "count", "document.querySelector('#speech-count').textContent"),
        ("eval", "language", "!document.querySelector('#chip-language').closest('.pick').hidden"),
        ("eval", "fitsSpeech", NO_OVERFLOW)],
        width=width, height=900, mobile=width == 320, timeout=30000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["src"].startswith("/api/media/") and v["fits"] and v["fitsSpeech"], v
    status, mime, disposition = v["file"]
    assert status == 200 and mime == "audio/mpeg" and "filename*=UTF-8''" in disposition, v["file"]
    assert any(s == "POST /api/speeches/read" for s in r["sent"]), r["sent"]
    assert v["count"].endswith("· будет 3 части") and v["language"] is True, v
    assert not problems(r), problems(r)
