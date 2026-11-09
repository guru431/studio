"""Регрессии рабочего места, вопросов, push и геометрии просмотра скана."""
import json

import pytest

from test_ui_browser import (CODE_ACTIVE_TAB, CODE_CHAT_A, CODE_CHAT_B, CODE_IDLE_AFTER, CODE_TABS, PNG_REAL, READY,
                             browser, code_chat, code_free_machine, code_online, code_requests, code_send,
                             code_session, done_cards, go, known_refusals, live, nth, ocr_reply, photo_screen,
                             picked, problems, push_init, push_keys, recognized, selecting, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_submit_missing_chat_closes_its_tab(browser, live):
    login = "submit-missing-chat"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A"), CODE_CHAT_B: code_session("Чат B")})
    aid, server = code_online(live, login, machine)
    missing = """(() => {
      const original = window.fetch;
      window.fetch = (url, options) => {
        if (String(url).endsWith('/chats/%s') && (!options?.method || options.method === 'GET')) {
          window.fetch = original;
          window.missingSeen = true;
          return Promise.resolve(new Response(JSON.stringify({detail: {code: 'not_found', message: 'нет такого чата'}}),
            {status: 404, headers: {'content-type': 'application/json'}}));
        }
        return original(url, options);
      };
      return true;
    })()""" % CODE_CHAT_A
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            *code_chat(aid, CODE_CHAT_B), *code_chat(aid, CODE_CHAT_A), ("eval", "missing", missing),
            *code_send("Задание удалённому чату"),
            ("wait", f"window.missingSeen && (!document.querySelector('#code-send')?.disabled || {CODE_ACTIVE_TAB} === '{CODE_CHAT_B}')"),
            ("eval", "tabs", CODE_TABS)])
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["tabs"] == [[CODE_CHAT_B, "Чат B", "true"]]
    assert not code_requests(machine, "turn")
    assert not known_refusals(problems(result), 404), problems(result)


@pytest.mark.parametrize("payload", [{}, None])
def test_invalid_push_status_preserves_device_flag_and_reports_error(browser, live, payload):
    login = "push-invalid-" + ("object" if payload == {} else "null")
    live.add_user(login)
    init = push_init(push_keys()) + """(() => {
      localStorage.setItem('studio-push', '1');
      const original = window.fetch;
      window.fetch = (url, options) => String(url) === '/api/me/push' && (!options?.method || options.method === 'GET')
        ? Promise.resolve(new Response(JSON.stringify(%s), {headers: {'content-type': 'application/json'}}))
        : original(url, options);
    })();""" % json.dumps(payload)
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/notifications"),
        ("wait", "document.querySelector('#push-enabled') && (!document.querySelector('#push-enabled').disabled"
                 " || document.querySelector('#push-status').textContent)"),
        ("eval", "push", "[document.querySelector('#push-enabled').disabled, document.querySelector('#push-status').textContent,"
                         " localStorage.getItem('studio-push'), window.pushCalls]")], init=init)
    assert not result.get("failed"), result
    disabled, message, flag, calls = result["values"]["push"]
    assert disabled and "подписки push" in message and flag == "1" and calls == []
    assert not problems(result), problems(result)


@pytest.mark.parametrize("zoom", [1, .7])
def test_ocr_zoom_retains_point_inside_offset_stage(browser, live, ocr_reply, zoom):
    from scans import photo

    login = "ocr-point-" + str(zoom)
    live.add_user(login)
    point = """(() => {
      const dialog = document.querySelector('#ocr-viewer');
      const stage = dialog.querySelector('.viewer-stage');
      const img = stage.querySelector('img');
      stage.style.marginLeft = '60px';
      stage.style.marginTop = '40px';
      dialog.style.zoom = '%s';
      const before = img.getBoundingClientRect();
      const x = Math.round(before.left + before.width * .5);
      const y = Math.round(before.top + before.height * .5);
      const fx = (x - before.left) / before.width;
      const fy = (y - before.top) / before.height;
      img.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: x, clientY: y}));
      const after = img.getBoundingClientRect();
      return [Math.abs(after.left + after.width * fx - x), Math.abs(after.top + after.height * fy - y)];
    })()""" % zoom
    result = browser.drive(live.url + "/", [*recognized(login, [("Скан.png", photo("PNG", (2800, 2200)))]),
        ("wait", "document.querySelector('img.ocr-scan')?.naturalWidth > 0"), ("click", "img.ocr-scan"),
        ("wait", "document.querySelector('#ocr-viewer img')?.naturalWidth > 0"), ("eval", "point", point)])
    assert not result.get("failed"), result
    assert max(result["values"]["point"]) < 2, result["values"]
    assert not problems(result), problems(result)


def test_question_ids_distinguish_equal_question_text(browser, live):
    login = "question-ids"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Вопросы")})
    machine.script = [{"k": "ask", "request_id": "q-ids", "kind": "question", "questions": [
        {"id": key, "question": "Какой вариант?", "options": [{"label": value}], "multi": False}
        for key, value in (("__proto__", "SQLite"), ("language", "Python"))]},
        {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Выбери варианты"), ("wait", "document.querySelectorAll('.code-ask-question').length === 2"),
            ("click", ".code-ask input[value='SQLite']"), ("click", ".code-ask input[value='Python']"),
            ("click", ".code-ask-answer"), ("wait", CODE_IDLE_AFTER.format(n=1))])
    finally:
        server.stop()
    assert not result.get("failed"), result
    answer, = code_requests(machine, "answer")
    assert answer["answers"] == {"__proto__": "SQLite", "language": "Python"}
    assert not known_refusals(problems(result), 409), problems(result)


def test_steer_rejection_keeps_text_and_attachments(browser, live):
    import base64

    login = "steer-keeps-draft"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Работа")})
    machine.attachments = True
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, server = code_online(live, login, machine)
    files = """(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['Уточнение'], 'Заметка.txt', {type: 'text/plain'}));
      dt.items.add(new File([Uint8Array.from(atob(%s), c => c.charCodeAt(0))], 'Снимок.png', {type: 'image/png'}));
      const input = document.querySelector('#code-image-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
      const original = window.fetch;
      window.fetch = (url, options) => String(url).endsWith('/steer')
        ? Promise.resolve(new Response(JSON.stringify({detail: {code: 'invalid', message: 'Уточнение отклонено'}}),
            {status: 422, headers: {'content-type': 'application/json'}}))
        : original(url, options);
      return true;
    })()""" % json.dumps(base64.b64encode(PNG_REAL).decode())
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Начни работу"), ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "files", files), ("wait", "document.querySelectorAll('#code-images .code-image').length === 2"),
            set_value("#code-text", "Уточняю задачу"), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-status').textContent === 'Уточнение отклонено'"),
            ("eval", "draft", "[document.querySelector('#code-text').value,"
                              " [...document.querySelectorAll('#code-images .code-image')].map(n => n.title)]")])
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["draft"] == ["Уточняю задачу", ["Заметка.txt", "Снимок.png"]]
    assert not known_refusals(problems(result), 422), problems(result)


def test_long_press_without_trailing_click_leaves_next_tap_available(browser, live):
    login = "long-press-no-click"
    live.add_user(login)
    hold = """(async () => {
      const card = document.querySelector('#photo-feed .card[data-pickable]');
      const at = {bubbles: true, cancelable: true, pointerType: 'touch', isPrimary: true, pointerId: 7};
      card.dispatchEvent(new PointerEvent('pointerdown', at));
      await new Promise(resolve => setTimeout(resolve, 650));
      card.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
      card.dispatchEvent(new PointerEvent('pointerup', at));
      return true;
    })()"""
    result = browser.drive(live.url + "/", [*photo_screen(login, cards=2), ("wait", done_cards(2)),
        ("eval", "hold", hold), ("wait", selecting()), ("eval", "first", picked()),
        ("click", nth(2)), ("eval", "both", picked())], width=390, mobile=True)
    assert not result.get("failed"), result
    assert len(result["values"]["first"]) == 1 and len(result["values"]["both"]) == 2
    assert not problems(result), problems(result)
