"""Поля формы раздела по схеме инструмента MCP (спека 2026-11-21-studio-source-capabilities-design, п. 11, 26, 37):
у `audio_transcribe` с `protocol`/`speakers`/`language` в ряду «Расшифровки» появляются «Протокол», «Говорящих», «Язык»,
значения уходят в вызов; итог с метками собеседников — видом расшифровки, файлы итога — списком скачивания."""
import pytest

from fakes import WAV_FAKE
from test_speech_segments import UPLOADED, transcriber
from test_ui_browser import READY, NO_OVERFLOW, problems, put_files, set_value, browser, live  # noqa: F401
from test_ui_section_limits import start

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

ARTIFACT = "art_" + "5" * 32
RESULT = {"text": "Speaker 1: Добрый день.\nSpeaker 2: Начнём.", "artifacts": [
    {"id": ARTIFACT, "mime_type": "text/markdown", "name": "protocol.md", "download_path": f"/artifacts/{ARTIFACT}/content"}]}
FIELDS = "document.querySelector('#mcp-field-protocol') && document.querySelector('#mcp-field-language-pick')"
STATUS = "document.querySelector('.mcp-run .status').textContent"
#: высоты чипов полей и выбора инструмента в одном ряду (образец размеров — выбор, как у «Агентов»)
HEIGHTS = """(() => { const h = e => Math.round(e.getBoundingClientRect().height);
  return [h(document.querySelector('#section-mcp-tool-pick')),
    ...[...document.querySelectorAll('.split-toolbar .mcp-fields :is(.ctl-chip, .pick-trigger)')].map(h)]; })()"""
UPLOAD = put_files("#transcribe-add input", [("talk.wav", WAV_FAKE)], mime="audio/wav")


@pytest.mark.parametrize("width", [320, 1280])
def test_tool_schema_fields_appear_and_reach_the_call(browser, live, width):
    login = f"fields{width}"
    live.add_user(login)
    calls = transcriber(live.gw, RESULT)
    r = browser.drive(live.url + "/", [*start(login),
        ("eval", "go", "location.hash = '#/transcribe'"), ("wait", FIELDS),
        ("eval", "shown", "[...document.querySelectorAll('.split-toolbar .mcp-fields [data-field]')].map(e => e.dataset.field)"),
        ("eval", "names", "[document.querySelector('#mcp-field-protocol').textContent,"
                          " document.querySelector('#mcp-field-speakers').closest('label').textContent,"
                          " document.querySelector('#mcp-field-language-pick').textContent]"),
        ("eval", "heights", HEIGHTS),
        ("click", "#mcp-field-protocol"), set_value("#mcp-field-speakers", "11"), set_value("#mcp-field-language", "en"),
        set_value("#mcp-field-topic", "Планёрка"),
        ("eval", "language", "document.querySelector('#mcp-field-language-pick').textContent"),
        UPLOAD, ("wait", f"{STATUS}.includes('1')"), ("eval", "refused", STATUS),
        set_value("#mcp-field-speakers", "3"), UPLOAD, ("wait", "document.querySelector('#section-mcp-turns')"),
        ("eval", "turns", "[...document.querySelectorAll('#section-mcp-turns .tr-turn')].map(t => [...t.children].map(c => c.textContent))"),
        ("eval", "files", "[...document.querySelectorAll('.mcp-files a')].map(a => [a.textContent, a.getAttribute('download')])"),
        ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["shown"] == ["protocol", "speakers", "language", "topic"]        # artifact_id — файл раздела, не поле
    assert v["names"] == ["Протокол", "Говорящих", "Язык"]
    assert len(set(v["heights"])) == 1, v["heights"]
    assert v["language"] == "English"
    assert v["refused"] == "Значение «Говорящих» — от 1 до 10"               # вне границ — до вызова
    assert v["turns"] == [["Собеседник 1", "Добрый день."], ["Собеседник 2", "Начнём."]]   # без времени — без него
    assert v["files"] == [["protocol.md", "protocol.md"]] and v["fits"]
    sent = [args for name, args in calls if name == "audio_transcribe"]
    assert sent == [{"protocol": True, "speakers": 3, "language": "en", "topic": "Планёрка", "artifact_id": UPLOADED}]
    assert not problems(r), problems(r)
