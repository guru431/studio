"""Язык, загрузка и запись в «Расшифровке» общие для Студии и инструмента MCP.
Поля инструмента доступны в отдельном ряду; итог сохраняет реплики и файлы."""
import httpx
import pytest

from fakes import WAV_FAKE
from test_speech_segments import TRANSCRIBE, UPLOADED, transcriber
from test_ui_browser import NO_OVERFLOW, add_gateway, problems, put_files, set_value, browser, live  # noqa: F401
from test_ui_section_limits import choose, ready, start

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

ARTIFACT = "art_" + "5" * 32
RESULT = {"text": "Speaker 1: Добрый день.\nSpeaker 2: Начнём.", "artifacts": [
    {"id": ARTIFACT, "mime_type": "text/markdown", "name": "protocol.md", "download_path": f"/artifacts/{ARTIFACT}/content"}]}
STATUS = "document.querySelector('.mcp-run .status').textContent"
UPLOAD = put_files("#transcribe-add input", [("talk.wav", WAV_FAKE)], mime="audio/wav")
LANGUAGE = "document.querySelector('#tr-language-pick').textContent"
BOTTOM = """(() => {
  const main = document.querySelector('.transcribe .split-main');
  const content = main.querySelector('.tr-content'), composer = main.querySelector('#tr-composer');
  const filler = document.createElement('div');
  filler.style.height = (content.clientHeight * 2 + 100) + 'px';
  content.append(filler);
  const bounds = composer.getBoundingClientRect(), margin = parseFloat(getComputedStyle(composer).marginBottom);
  const atBottom = Math.abs(main.getBoundingClientRect().bottom - bounds.bottom - margin) < 2;
  const inOrder = main.querySelector('.head').getBoundingClientRect().bottom <= content.getBoundingClientRect().top
    && content.getBoundingClientRect().bottom <= bounds.top;
  const toggle = main.querySelector('.head .split-toggle'), toggleBox = toggle.getBoundingClientRect();
  const listReachable = !toggleBox.width || toggle.contains(document.elementFromPoint(
    toggleBox.left + toggleBox.width / 2, toggleBox.top + toggleBox.height / 2));
  content.scrollTop = content.scrollHeight;
  const fixed = content.scrollTop > 0 && composer.getBoundingClientRect().top === bounds.top;
  filler.remove();
  content.scrollTop = 0;
  return atBottom && inOrder && fixed && listReachable;
})()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_tool_schema_fields_appear_and_reach_the_call(browser, live, width):
    login = f"fields{width}"
    live.add_user(login)
    props = {**TRANSCRIBE, "language": {"type": "string", "enum": ["Russian", "en-US"]},
             "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 5, "default": 0}}
    calls = transcriber(live.gw, RESULT, properties=props)

    async def protocol(request):     # итог раздела сохраняется в историю — файл скачивается со шлюза
        return httpx.Response(200, content="# Протокол".encode())
    live.gw.on[f"/artifacts/{ARTIFACT}/content"] = protocol
    r = browser.drive(live.url + "/", [*start(login), add_gateway(),
        ("eval", "go", "location.hash = '#/transcribe'"), ("wait", ready("audio_transcribe")),
        set_value("#tr-language", "ru"), ("eval", "nativeLanguage", LANGUAGE), ("eval", "nativeBottom", BOTTOM),
        choose("audio_transcribe"), ("wait", "!document.querySelector('#tr-options-toggle')?.hidden"),
        ("eval", "retainedLanguage", LANGUAGE),
        ("eval", "toolbar", """[...document.querySelectorAll('#tr-control-row button')].filter(e => e.offsetWidth > 0)
          .map(e => e.id)"""),
        ("eval", "collapsed", "document.querySelector('#tr-options').hidden"),
        ("click", "#tr-options-toggle"),
        ("eval", "shown", "[...document.querySelectorAll('#tr-options [data-field]')].map(e => e.dataset.field)"),
        ("eval", "names", "[document.querySelector('#mcp-field-protocol').textContent,"
                          " document.querySelector('#mcp-field-speakers').closest('label').textContent]"),
        ("click", "#mcp-field-protocol"), set_value("#mcp-field-speakers", "11"), set_value("#tr-language", "en-US"),
        set_value("#mcp-field-topic", "Планёрка"),
        ("eval", "language", LANGUAGE),
        ("click", "#tr-options-toggle"),
        UPLOAD, ("wait", f"{STATUS}.includes('1')"), ("eval", "refused", STATUS),
        ("click", "#tr-options-toggle"), set_value("#mcp-field-speakers", "3"), ("click", "#tr-options-toggle"),
        UPLOAD, ("wait", "document.querySelector('#section-mcp-turns')"),
        ("eval", "turns", "[...document.querySelectorAll('#section-mcp-turns .tr-turn')].map(t => [...t.children].map(c => c.textContent))"),
        ("eval", "files", "[...document.querySelectorAll('.mcp-files a')].map(a => [a.textContent, a.getAttribute('download')])"),
        ("eval", "mcpBottom", BOTTOM),
        set_value("#section-mcp-tool", ""), ("eval", "back", LANGUAGE),
        ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["shown"] == ["protocol", "speakers", "topic"]
    assert v["names"] == ["Протокол", "Говорящих"]
    assert v["collapsed"] and {"tr-language-pick", "transcribe-upload", "transcribe-record"} <= set(v["toolbar"])
    assert v["nativeBottom"] and v["mcpBottom"], v
    assert v["nativeLanguage"] == v["retainedLanguage"] == "Русский"
    assert v["language"] == v["back"] == "English"
    assert v["refused"] == "Значение «Говорящих» — от 1 до 10"               # вне границ — до вызова
    assert v["turns"] == [["Собеседник 1", "Добрый день."], ["Собеседник 2", "Начнём."]]   # без времени — без него
    assert v["files"] == [["protocol.md", "protocol.md"]] and v["fits"], v
    sent = [args for name, args in calls if name == "audio_transcribe"]
    assert sent == [{"protocol": True, "speakers": 3, "language": "en-US", "topic": "Планёрка", "artifact_id": UPLOADED}]
    assert not problems(r), problems(r)
