"""Chat and document controls keep their regular Studio affordances with MCP sources."""
import pytest

from fakes import PNG_REAL
from test_mcp_sections import service
from test_ui_browser import API, NO_OVERFLOW, browser, go, live, problems, put_files, set_value
from test_ui_section_limits import SERVICE, choose, ready, serve, start

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def test_chat_mcp_attachment_reason_and_translation_in_regular_history(browser, live, monkeypatch):
    login = "mcp-chat-translation-controls"
    live.add_user(login)
    calls = service(live.gw, catalogs=True, monkeypatch=monkeypatch)
    begin = start(login)
    result = browser.drive(live.url + "/", [
        *begin[:-1],
        ("eval", "chat", "(async () => { const sid = await " + begin[-1][2] + ";"
         f" const chat = await {API}.api('POST', '/api/chats', {{mcp_model_sid: sid, model:'chat-b'}});"
         " location.hash = '#/chat/' + chat.id; return chat.id; })()"),
        ("wait", "!document.querySelector('#chat-attachment-note')?.hidden && document.querySelector('#attach')?.disabled"),
        ("eval", "reason", "document.querySelector('#chat-attachment-note').textContent"),
        ("eval", "chatLower", "document.querySelector('.chat-history').getBoundingClientRect().bottom"
         " <= document.querySelector('#composer').getBoundingClientRect().top"),
        *go("translate"), ("wait", ready("text_translate")), choose("text_translate"),
        ("wait", "document.querySelector('#tl-target option[value=en]')"),
        set_value("#tl-source", "ru"), set_value("#tl-target", "en"),
        ("type", "#tl-input", "Текст для перевода"), ("click", "#tl-go"),
        ("wait", "document.querySelector('#tl-output')?.textContent === 'Translated text'"
         " && document.querySelector('.tl-row[data-state=done]')"
         " && document.querySelector('#tl-share')?.hidden === false"),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "translateLower", "document.querySelector('.tl-view').getBoundingClientRect().bottom"
         " <= document.querySelector('#tl-composer').getBoundingClientRect().top"),
        *go("chat"), ("wait", "document.querySelector('#chat-input')"),
        *go("translate"), ("wait", "document.querySelector('.tl-row[data-state=done] .tl-open')"),
        ("click", ".tl-row[data-state=done] .tl-open"),
        ("wait", "document.querySelector('#tl-output')?.textContent === 'Translated text'"),
        ("eval", "history", "[document.querySelectorAll('.tl-row').length, document.querySelector('#tl-share').hidden,"
         " document.querySelector('.mcp-history').hidden, document.querySelector('#tl-export').hidden]"),
    ], width=320, mobile=True)
    assert not result.get("failed"), result
    assert "без вложений" in result["values"]["reason"]
    assert result["values"]["fits"] and result["values"]["history"] == [1, False, True, False]
    assert result["values"]["chatLower"] and result["values"]["translateLower"]
    assert [args for tool, args in calls if tool == "text_translate"] == [
        {"text": "Текст для перевода", "source_lang": "ru", "target_lang": "en"}
    ]
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_scan_mcp_shows_options_and_refuses_nonimages_before_upload(browser, live, width):
    login = f"mcp-scan-options-{width}"
    live.add_user(login)
    scan = {"properties": {"artifact_id": {"type": "string"}, "engine": {"type": "string", "enum": ["default", "paddle"]}},
            "required": ["artifact_id"]}
    calls = serve(live.gw, {**SERVICE, "document_read": scan})
    result = browser.drive(live.url + "/", [
        *start(login), *go("ocr"), ("wait", ready("document_read")), choose("document_read"),
        ("wait", "!document.querySelector('#ocr-mcp-hint')?.hidden && document.querySelector('#mcp-field-engine')?.isConnected"),
        ("eval", "lower", """(() => {
          const dock = document.querySelector('#ocr-composer');
          return dock.contains(document.querySelector('#ocr-upload')) &&
            dock.contains(document.querySelector('#mcp-field-engine')) &&
            dock.getBoundingClientRect().top >= document.querySelector('.ocr-documents').getBoundingClientRect().bottom &&
            dock.getBoundingClientRect().bottom <= document.querySelector('.ocr .split-main').getBoundingClientRect().bottom;
        })()"""),
        set_value("#mcp-field-engine", "paddle"),
        put_files("#ocr-file", [("scan.pdf", b"test")], "application/pdf"),
        ("wait", "document.querySelector('.ocr .tr-error')?.textContent"),
        ("eval", "refusal", "document.querySelector('.ocr .tr-error').textContent"),
        put_files("#ocr-file", [("page.png", PNG_REAL)]),
        ("wait", "document.querySelector('#section-mcp-result pre')?.textContent === 'ok'"),
    ], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    assert "PNG, JPEG или WebP" in result["values"]["refusal"]
    assert result["values"]["lower"]
    assert [tool for tool, _ in calls] == ["artifact_upload", "document_read"]
    assert calls[-1][1]["engine"] == "paddle"
    assert not problems(result), result.get("log")
