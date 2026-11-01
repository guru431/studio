"""Browser acceptance with both real services and a synthetic cloud, desktop and narrow phone."""
import json

import httpx
import pytest

import db
import security
import state
from test_cleaner_contract import cleaner_server
from test_ui_browser import READY, browser, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_cleaner_review_edit_restore_and_copy(browser, live, cleaner_server, monkeypatch, width, tmp_path):
    url, real, _ = cleaner_server
    port = httpx.URL(url).port
    original = live.gw.handler

    def forward(request):
        if request.url.port != port:
            return original(request)
        response = real.request(request.method, str(request.url), headers=request.headers, content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    monkeypatch.setattr(state.HTTP._transport, "handler", forward)
    login = f"cleaner-{width}"
    uid = live.add_user(login)
    connection = db.connect(live.db_path)
    try:
        connection.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                           (json.dumps(["10.77.1.15:4000", f"127.0.0.1:{port}"]),))
        cid = connection.execute("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created,"
                                 " server_only, cleaner) VALUES (?, 'Cleaner', 'openai', ?, ?, '[]', 0, 1, 1)",
                                 (uid, url, security.encrypt("ck_" + "A" * 43))).lastrowid
        connection.execute("INSERT INTO section_links VALUES (?, 'chat', ?, '[\"protected-model\"]', 0)", (uid, cid))
        connection.execute("INSERT INTO section_defaults VALUES (?, 'chat', ?, 'protected-model')", (uid, cid))
    finally:
        connection.close()
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("wait", "document.querySelector('#chat-input') && !document.querySelector('#send').disabled"),
        set_value("#chat-input", "Почта ivan@contoso.ru, проект Северное сияние"), ("click", "#send"),
        ("wait", "document.querySelector('#cleaner-preview-dialog[open]')"),
        ("eval", "preview", "document.querySelector('.cleaner-preview-text').textContent"),
        ("eval", "notSent", "!document.querySelector('.msg.assistant')"),
        ("click", "#cleaner-edit summary"), set_value("#cleaner-fragment", "Северное сияние"),
        ("click", "#cleaner-mask"),
        ("wait", "document.querySelector('.cleaner-preview-text')?.textContent.includes('<TERM_1>')"),
        ("eval", "fixed", "document.querySelector('.cleaner-preview-text').textContent"),
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth && document.querySelector('#cleaner-preview-dialog').scrollWidth <= document.querySelector('#cleaner-preview-dialog').clientWidth"),
        ("screenshot", str(tmp_path / f"cleaner-review-{width}.png")), ("click", "#cleaner-confirm"),
        ("wait", "document.querySelector('.msg-cleaner-view')"),
        ("eval", "restored", "document.querySelector('.msg.assistant .md')?.textContent || document.querySelector('#messages').textContent"),
        ("click", ".msg-cleaner-view"),
        ("eval", "raw", "document.querySelector('#messages').textContent"),
        ("click", ".msg-cleaner-copy"), ("wait", "document.querySelector('#cleaner-copy-dialog[open]')"),
        ("eval", "copy", "document.querySelector('.cleaner-copy-text').value"),
    ], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert "ivan@contoso.ru" not in values["preview"] and "<EMAIL_1>" in values["preview"]
    assert values["notSent"] and values["fits"]
    assert "Северное сияние" not in values["fixed"] and "<TERM_1>" in values["fixed"]
    assert "Ответ ivan@contoso.ru" in values["restored"]
    assert "Ответ <EMAIL_1>" in values["raw"]
    assert "ivan@contoso.ru" not in values["copy"]
    assert not problems(result), problems(result)
