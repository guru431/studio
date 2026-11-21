"""Поле ввода чата (спека 2026-11-21-studio-source-capabilities-design, п. 30): до 10 вложений в сообщении — фото,
текстовые файлы и код вместе; 11-е — словами, диалог не создаётся."""
import base64

import pytest

from fakes import PNG_REAL
from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, attach, browser, live,  # noqa: F401
                             open_new_chat, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PHOTO = base64.b64encode(PNG_REAL).decode()
CHIPS = "document.querySelectorAll('#composer .att-chip').length"


def files(names) -> str:
    """Выражение JS со списком File: `*.png` — настоящая картинка, прочие — строка текста."""
    made = [f"new File([Uint8Array.from(atob({PHOTO!r}), c => c.charCodeAt(0))], '{n}', {{ type: 'image/png' }})"
            if n.endswith(".png") else f"new File(['line of {n}\\n'], '{n}')" for n in names]
    return "[" + ", ".join(made) + "]"


@pytest.mark.parametrize("width", [320, 1280])
def test_ten_attachments_pass_eleventh_is_refused_with_words(browser, live, width):
    ten = [f"p{i}.png" for i in range(5)] + ["main.py", "data.csv", "conf.json", "notes.txt", "readme.md"]
    before = len(live.gw.chat_requests)
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(width == 320),
        ("eval", "accept", "document.querySelector('#attach').accept"),
        attach(files(ten)), ("wait", f"{CHIPS} === 10"),
        ("eval", "quiet", "document.querySelector('#chat-error').textContent"),
        attach(files(["extra.log"])), ("wait", f"{CHIPS} === 11 && document.querySelector('#chat-error').textContent"),
        ("eval", "picked", "document.querySelector('#chat-error').textContent"),
        ("eval", "fits", NO_OVERFLOW),
        ("type", "#chat-input", "Что здесь?"), ("click", "#send"), ("idle",),
        ("eval", "refused", f"[document.querySelector('#chat-error').textContent, location.hash, {CHIPS}]"),
        ("click", "#composer .att-chip:last-child .att-remove"), ("wait", f"{CHIPS} === 10"),
        ("click", "#send"), ("wait", ANSWERED),
        ("eval", "sent", f"""(async () => {{ const m = {API};
          const d = await m.api('GET', '/api/chats/' + location.hash.split('/')[2]);
          return d.messages[0].attachments.map(a => a.name).sort(); }})()""")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert ".py" in v["accept"].split(",") and ".csv" in v["accept"].split(",") and "image/*" in v["accept"]
    assert v["quiet"] == ""
    assert v["picked"] == "не больше 10 вложений в сообщении" and v["fits"] is True
    assert v["refused"] == ["не больше 10 вложений в сообщении", "#/chat", 11]
    assert v["sent"] == sorted(ten)
    assert not [s for s in r["sent"] if s == "POST /api/chats"][1:], r["sent"]   # один диалог — под отправленный ход
    assert len(live.gw.chat_requests) == before + 1
    parts = [m for m in live.gw.chat_requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert sum(p["type"] == "image_url" for p in parts) == 4
    assert '<document name="main.py">\n```python\n' in parts[0]["text"]
    assert not problems(r), problems(r)
