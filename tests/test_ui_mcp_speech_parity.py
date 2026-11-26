"""Speech keeps standard controls with MCP and plays every part of one request."""
import pytest

from test_mcp_speech_batch import PARTS, TEXT, serve_speech
from test_ui_browser import NO_OVERFLOW, add_gateway, browser, go, live, problems, set_value  # noqa: F401
from test_ui_section_limits import choose, ready, start

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_speech_common_model_voice_language_and_long_text_on_mobile(browser, live):
    login = "speech-parity"
    live.add_user(login)
    calls = serve_speech(live.gw)
    tool = "speech_synthesize"
    result = browser.drive(live.url + "/", [*start(login), add_gateway(), *go("speech"),
        ("wait", ready(tool)), choose(tool), set_value("#chip-mcp-model", "second"),
        set_value("#chip-voice", "nova"), set_value("#chip-language", "en"),
        set_value("#speech-text", TEXT),
        ("eval", "count", "document.querySelector('#speech-count').textContent"),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "transport", "!!document.querySelector('#mcp-field-wait_seconds')"),
        ("click", "#speech-go"),
        ("wait", "document.querySelectorAll('#section-mcp-result audio').length === 3")],
        width=320, mobile=True, timeout=40000)
    assert not result.get("failed"), result
    assert "200 000" in result["values"]["count"] and "3 части" in result["values"]["count"]
    assert result["values"]["fits"] and not result["values"]["transport"]
    assert calls == [{"text": part, "voice": "nova", "model": "second", "language": "en"} for part in PARTS]
    assert not problems(result), problems(result)
