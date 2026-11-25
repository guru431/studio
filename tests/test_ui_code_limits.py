"""Вложения «Агентов» по пределам моста (`hello.limits`) в настоящем браузере: мост 0.48.0 — 10 скриншотов одним
сообщением, мост 0.47 — отказ словами с его числом до отправки."""
import base64
import json

import pytest

from fakes import PNG_REAL
from test_ui_browser import (CODE_CHAT_A, NO_OVERFLOW, READY, browser, code_chat, code_free_machine, code_online,
                             code_requests, code_send, code_session, live, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: `window.shots(n)` — DataTransfer с n файлами PNG; `window.pasteShots(n)` — их вставка в поле
SHOTS = """(() => { const bytes = Uint8Array.from(atob(%s), (c) => c.charCodeAt(0));
  window.shots = (n) => { const dt = new DataTransfer();
    for (let i = 0; i < n; i += 1) dt.items.add(new File([bytes], `screen-${i}.png`, { type: 'image/png' }));
    return dt; };
  window.pasteShots = (n) => document.querySelector('#code-text').dispatchEvent(new ClipboardEvent('paste',
    { clipboardData: shots(n), bubbles: true, cancelable: true }));
  return true; })()""" % json.dumps(base64.b64encode(PNG_REAL).decode())
PREVIEWS = "document.querySelectorAll('#code-images img').length"


#: строки окна «Сессия и контекст»: время хода, время сессии, скорость
TIMES = "['#code-turn-time', '#code-session-time', '#code-turn-speed'].map((s) => document.querySelector(s)?.textContent)"
OPEN_CONTEXT = [("click", "#code-context"), ("wait", "document.querySelector('#code-context-dialog').open")]


@pytest.mark.parametrize("width", [320, 1280])
def test_ten_screenshots_in_one_message_and_turn_time(browser, live, width):
    """Мост 0.48.0 объявил 12 и 12: одна вставка 10 файлов — 10 превью, все уходят одним ходом; окно «Сессия и
    контекст» — время хода и сессии от моста и скорость «выходные токены ÷ время хода»."""
    login = f"limits-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Скриншоты")})
    machine.limits = {"images": 12, "attachments": 12}
    machine.attachments = machine.large_attachments = machine.timing = True
    machine.context = {"used": 10, "limit": 100, "percent": 10, "usage": {"total": 100, "input": 80, "output": 20}}
    machine.script = [{"k": "context", "used": 20, "limit": 100, "usage": {"total": 700, "input": 180, "output": 520}},
                      {"k": "text", "text": "Вижу"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "document.querySelector('#code-context').dataset.used === '10'"),
            *OPEN_CONTEXT, ("eval", "before", TIMES), ("eval", "close", "document.querySelector('#code-context-dialog').close()"),
            ("eval", "setup", SHOTS), ("eval", "paste", "pasteShots(10)"), ("wait", PREVIEWS + " === 10"),
            ("eval", "hint", "document.querySelector('#code-attach').title"),
            ("click", "#code-send"),
            ("wait", "document.querySelector('#code-stop').hidden"
                     " && document.querySelector('#code-feed').textContent.includes('Вижу')"),
            *OPEN_CONTEXT, ("eval", "times", TIMES),
            ("eval", "fits", NO_OVERFLOW + " && (() => { const r = document.querySelector('#code-context-dialog')"
                             ".getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })()")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert "до 12 файлов" in values["hint"] and values["fits"], values
    assert values["before"] == ["нет данных"] * 3, values
    assert values["times"] == ["1 с", "1 с", "500 ток/с"], values
    [turn] = code_requests(machine, "turn")
    assert len(turn["images"]) == 10
    assert not problems(r), problems(r)


def test_zero_output_tokens_is_no_speed_data(browser, live):
    """Спека п. 8: прирост выходных токенов хода 0 — «нет данных», не «0 ток/с»; время хода при этом есть."""
    login = "limits-zero"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Без токенов")})
    machine.timing = True
    usage = {"total": 100, "input": 80, "output": 20}
    machine.context = {"used": 10, "limit": 100, "percent": 10, "usage": usage}
    machine.script = [{"k": "context", "used": 12, "limit": 100, "usage": usage},
                      {"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "document.querySelector('#code-context').dataset.used === '10'"),
            *code_send("Сделай"),
            ("wait", "document.querySelector('#code-stop').hidden"
                     " && document.querySelector('#code-feed').textContent.includes('Готово')"),
            *OPEN_CONTEXT, ("eval", "times", TIMES)])
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["times"] == ["1 с", "1 с", "нет данных"], r["values"]
    assert not problems(r), problems(r)


def test_old_bridge_refuses_the_fifth_image_before_sending(browser, live):
    """Review Focus 1: мост 0.47 без `hello.limits` — прежние 4 и 8, отказ словами до отправки."""
    login = "limits-old"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Скриншоты")})
    machine.attachments = True
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "setup", SHOTS), ("eval", "paste", "pasteShots(5)"),
            ("wait", "document.querySelector('#code-status').textContent.length > 0"),
            ("eval", "status", "document.querySelector('#code-status').textContent"),
            ("eval", "none", PREVIEWS),
            ("eval", "paste4", "pasteShots(4)"), ("wait", PREVIEWS + " === 4"),
            ("eval", "hint", "document.querySelector('#code-attach').title"),
            *OPEN_CONTEXT, ("eval", "rows", TIMES),
            ("eval", "fits", NO_OVERFLOW)])
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert values["status"] == "Можно прикрепить до 4 изображений к сообщению" and values["none"] == 0, values
    assert "до 8 файлов" in values["hint"] and values["fits"], values
    assert values["rows"] == ["нет данных"] * 3, values      # мост 0.47 времени не сообщает
    assert not code_requests(machine, "turn")
    assert not problems(r), problems(r)
