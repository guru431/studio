"""Страница шлюза Cleaner в настоящем браузере, 320 и 1280 px: блок «Cleaner» панели (адрес, служебный ключ не
показывается, флажок, проверка связи, предупреждения), переключатель «Через Cleaner» в меню «⋯» подключения
(включается и выключается, в принудительном режиме — заблокирован с пояснением), признак диалога, подпись ответа и
окно «Что получила модель» с запросом в заменах. Cleaner — `FakeCleanerGateway` на адресе из списка, узел
провайдера — AssertionError."""
import json

import pytest

import db
from fakes import FakeCleanerGateway
from test_ui_browser import (ANSWERED, API, LAST_ANSWER, NO_OVERFLOW, READY, browser, go, live, open_new_chat,  # noqa: F401
                             problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

ADDRESS = "https://10.77.1.20:5464"
KEY = "ck_" + "u" * 43
PROVIDER = "https://api.cloud.test/v1"


@pytest.fixture
def cleaner(live):
    """Cleaner в списке разрешённых и на подменённом узле; самопроверка без провайдера отвечает пунктами."""
    gw = FakeCleanerGateway(KEY)
    gw.masks = {"Ивану": "<PERSON_1>"}
    live.gw.hosts["10.77.1.20:5464"] = gw.handler
    live.gw.hosts["api.cloud.test:443"] = gw.provider
    connection = db.connect(live.db_path)
    connection.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                       (json.dumps(["10.77.1.15:4000", "10.77.1.20:5464"]),))
    connection.close()
    yield gw
    live.gw.hosts.pop("10.77.1.20:5464", None)
    live.gw.hosts.pop("api.cloud.test:443", None)


def call(name, body):
    """Шаг: запрос API от вошедшей учётки (CSRF — api.js), значение — ответ."""
    return ("eval", name, f"(async () => {{ const m = {API}; return m.api({body}); }})()")


def press(card, selector):
    """Шаг: нажатие на элемент своей карточки подключения (в разделе есть и шлюз srvllm)."""
    return ("eval", "press", f"{card}.querySelector({selector!r}).click()")


DIALOG = "document.querySelector('#cleaner-sent-dialog[open]')"


@pytest.mark.parametrize("width", [320, 1280])
def test_cleaner_gateway_page(browser, live, cleaner, width):
    masked = cleaner._masked

    def tool_history(body):
        sent = masked(body)
        sent["messages"].extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "function": {"name": "lookup", "arguments": '{"q":"<PERSON_1>"}'}}]},
            {"role": "tool", "content": "Готово", "tool_call_id": "call-1", "name": "lookup"},
        ])
        return sent

    cleaner._masked = tool_history
    mobile = width == 320
    card = f"document.querySelector('.link[data-conn=\"' + sessionStorage.cid + '\"]')"
    toggle = f"{card}.querySelector('.link-cleaner')"
    steps = [*signin(), ("wait", READY),
        # Cleaner не настроен: переключателя у внешнего подключения нет
        call("reset", "'PUT', '/api/admin/cleaner', { base_url: '', key: null, forced: false }"),
        ("eval", "cid", f"""(async () => {{ const m = {API};
          const c = await m.api('POST', '/api/connections', {{ name: 'Облако {width}', kind: 'openai',
            base_url: {PROVIDER!r}, token: 'cloud-key', sections: ['chat'] }});
          await m.api('PUT', '/api/sections/chat/links/' + c.id, {{ models: ['gpt-x'] }});
          await m.loadMe(); sessionStorage.cid = c.id; return c.id; }})()"""),
        # блок «Cleaner» панели: адрес и ключ, ключ после сохранения не показывается
        *go("admin"), ("wait", "document.querySelector('#cleaner-key')?.placeholder"),
        ("eval", "forcedOff", "document.querySelector('#cleaner-forced').disabled"),
        ("eval", "notes", "[document.querySelector('#cleaner-private-note').textContent,"
                          " document.querySelector('#cleaner-forced-note').textContent]"),
        set_value("#cleaner-url", ADDRESS), set_value("#cleaner-key", KEY),
        ("eval", "forcedReady", "!document.querySelector('#cleaner-forced').disabled"),
        ("click", "#cleaner-save"), ("wait", "document.querySelector('#cleaner-status').classList.contains('ok')"),
        ("eval", "afterSave", "[document.querySelector('#cleaner-url').value, document.querySelector('#cleaner-key').value,"
                              f" document.body.innerHTML.includes({KEY!r})]"),
        ("click", "#cleaner-check"), ("wait", "document.querySelector('#cleaner-checks li')"),
        ("eval", "checked", "[document.querySelector('#cleaner-status').className,"
                            " document.querySelector('#cleaner-checks').textContent]"),
        ("click", "#cleaner-forced"), ("click", "#cleaner-save"),
        ("wait", "document.querySelector('#cleaner-status').classList.contains('ok')"
                 " && document.querySelector('#cleaner-forced').checked"),
        ("eval", "saved", "fetch('/api/admin/cleaner').then((r) => r.text())"),
        ("eval", "adminFits", NO_OVERFLOW),
        # принудительный режим: в меню «⋯» переключатель включён и заблокирован, с пояснением
        *go("settings/chat"), ("wait", f"{card}?.querySelector('.link-menu')"),
        press(card, '.link-menu'),
        ("wait", f"{toggle} && !{card}.querySelector('.link-actions').hidden"),
        ("eval", "locked", f"[{toggle}.checked, {toggle}.disabled, {card}.querySelector('.link-cleaner-note').textContent,"
                           f" !!{card}.querySelector('.link-routed')]"),
        ("eval", "settingsFits", NO_OVERFLOW),
        # флажок снят: переключатель свободен — включить и выключить
        call("unforce", f"'PUT', '/api/admin/cleaner', {{ base_url: {ADDRESS!r}, key: null, forced: false }}"),
        ("eval", "reload", "location.reload()"), ("wait", READY),
        ("wait", f"{card}?.querySelector('.link-menu')"), press(card, '.link-menu'),
        ("wait", f"{toggle} && !{toggle}.disabled"),
        ("eval", "free", f"[{toggle}.checked, !!{card}.querySelector('.link-routed')]"),
        press(card, '.link-cleaner'),
        ("wait", f"{card}?.querySelector('.link-routed')"),
        ("eval", "on", "fetch('/api/connections').then((r) => r.json()).then((r) => r.items.map((c) => c.via_cleaner))"),
        press(card, '.link-menu'), ("wait", f"{toggle}?.checked && !{toggle}.disabled"),
        press(card, '.link-cleaner'),
        ("wait", f"{card} && !{card}.querySelector('.link-routed')"),
        ("eval", "off", "fetch('/api/connections').then((r) => r.json()).then((r) => r.items.map((c) => c.via_cleaner))"),
        call("again", "'PATCH', '/api/connections/' + sessionStorage.cid, { via_cleaner: true }"),
        # диалог через Cleaner: признак в шапке, подпись ответа, окно «Что получила модель»
        *open_new_chat(mobile), ("click", "#model-chip"),
        ("wait", "document.querySelector('.model-option[data-model=\"gpt-x\"]')"),
        ("click", ".model-option[data-model=\"gpt-x\"]"),
        ("wait", "document.querySelector('#model-chip').textContent.includes('gpt-x')"),
        ("eval", "draftTag", "!document.querySelector('#chat-routed').hidden"),
        ("type", "#chat-input", "Позвони Ивану"), ("press", "Enter"),
        ("wait", ANSWERED), ("wait", f"{LAST_ANSWER}.querySelector('.msg-cleaner-sent')"),
        ("eval", "label", f"{LAST_ANSWER}.querySelector('.msg-model').textContent"),
        ("eval", "tag", "[!document.querySelector('#chat-routed').hidden, document.querySelector('#chat-routed').title]"),
        ("click", "#messages .msg.assistant:last-child .msg-cleaner-sent"), ("wait", DIALOG),
        ("eval", "sent", f"[{DIALOG}.querySelector('.cleaner-sent-text').textContent,"
                         f" {DIALOG}.querySelector('.cleaner-sent-text').innerHTML.includes('&lt;PERSON_1&gt;'),"
                         f" {DIALOG}.textContent.includes('Люди: 1')]"),
        ("eval", "collapsed", f"!{DIALOG}.querySelector('.cleaner-sent-calls').open"),
        ("click", ".cleaner-sent-calls summary"), ("wait", f"{DIALOG}.querySelector('.cleaner-sent-calls[open]')"),
        ("eval", "toolCalls", f"JSON.parse({DIALOG}.querySelector('.cleaner-sent-calls pre').textContent)"),
        ("eval", "chatFits", NO_OVERFLOW),
        ("press", "Escape"), ("wait", f"!{DIALOG}"),
        call("cleanup", "'DELETE', '/api/connections/' + sessionStorage.cid"),
        call("unset", "'PUT', '/api/admin/cleaner', { base_url: '', key: null, forced: false }"),
    ]
    result = browser.drive(live.url + "/", steps, width=width, mobile=mobile)
    assert not result.get("failed"), result
    v = result["values"]
    assert v["forcedOff"] is True and v["forcedReady"] is True
    assert "частные адреса" in v["notes"][0].lower() and "Open WebUI" in v["notes"][1]
    assert v["afterSave"] == [ADDRESS, "", False]
    assert "ok" in v["checked"][0] and "ключ действует" in v["checked"][1]
    assert json.loads(v["saved"]) == {"base_url": ADDRESS, "key_set": True, "forced": True, "reenter": False}
    assert v["locked"][:2] == [True, True] and "заблокирован" in v["locked"][2] and v["locked"][3] is True
    assert v["free"] == [False, False] and True in v["on"] and True not in v["off"]
    assert v["draftTag"] is True and v["tag"] == [True, "Диалог идёт через Cleaner"]
    assert v["label"] == "gpt-x · через Cleaner"
    text, escaped, counts = v["sent"]
    assert "Позвони <PERSON_1>" in text and "Ивану" not in text and escaped and counts
    assert v["collapsed"] and "Идентификатор вызова: call-1" in text and "Инструмент (lookup)" in text
    assert json.loads(v["toolCalls"][0]["function"]["arguments"]) == {"q": "<PERSON_1>"}
    assert v["adminFits"] and v["settingsFits"] and v["chatFits"]
    assert cleaner.turns and cleaner.sent_reads and cleaner.checked
    assert all(r.headers["x-cleaner-key"] == KEY and "authorization" not in r.headers for r in cleaner.sent_reads)
    assert not problems(result), problems(result)
