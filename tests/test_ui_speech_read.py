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


def answer_chat(live, uid) -> str:
    import db
    cid, now = uuid.uuid4().hex, time.time()
    conn = db.connect(live.db_path)
    try:
        conn.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, 'чтение', ?, ?)",
                     (cid, uid, now, now))
        for n, (role, content) in enumerate([("user", "вопрос"), ("assistant", "**Короткий** ответ модели.")]):
            conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                         " VALUES (?, ?, ?, ?, 'done', 'server', 'local-gemma-abl', ?)",
                         (uuid.uuid4().hex, cid, role, content, now + n))
    finally:
        conn.close()
    return cid


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
