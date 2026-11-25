"""Звуковые сигналы (п. 37 спеки 2026-11-25-studio-skills-status-local-models): по умолчанию выключены, настройка
устройства «Настройки → Уведомления», три сигнала синтезом без файлов, тот же ответ — один раз, на скрытой странице —
тишина. AudioContext подменён: частоты тонов — в `window.tones`."""
import pytest

from test_ui_browser import ANSWERED, READY, add_gateway, browser, go, live, open_new_chat, problems, signin  # noqa: F401
from test_ui_chat_carryover import pause
from test_ui_chat_pending import ASK

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

FAKE_AUDIO = """window.tones = [];
window.AudioContext = class {
  constructor() { this.state = 'running'; this.currentTime = 0; this.destination = {}; }
  resume() { this.state = 'running'; return Promise.resolve(); }
  createOscillator() { return { type: '', frequency: { value: 0 }, connect: (g) => g,
    start() { window.tones.push(this.frequency.value); }, stop() {} }; }
  createGain() { return { gain: { setValueAtTime() {}, linearRampToValueAtTime() {} }, connect: (d) => d }; }
};"""
TONES = "window.tones.slice()"
CLEAR = ("eval", "clear", "(() => { window.tones.length = 0; return true; })()")


def start(login):
    return [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')")]


def ask(text):
    return [("type", "#chat-input", text), ("press", "Enter"), ("wait", ANSWERED)]


def test_sound_is_off_by_default_and_plays_three_signals_once(browser, live):
    live.add_user("sound")
    r = browser.drive(live.url + "/", [*start("sound"), *ask("Раз"), pause(300),
        ("eval", "default", f"[localStorage.getItem('studio-sound'), {TONES}]"),
        *go("settings/notifications"), ("wait", "document.querySelector('#sound-on')"),
        ("eval", "words", "[document.querySelector('#sound-settings').textContent]"),
        ("click", "#sound-on"), ("wait", "window.tones.length >= 2"),
        ("eval", "on", f"[localStorage.getItem('studio-sound'), {TONES}]"),
        pause(1600), CLEAR, *open_new_chat(), *ask("Два"), ("wait", "window.tones.length >= 2"),
        ("eval", "done", TONES), pause(6000), ("eval", "after_poll", TONES),
        pause(1600), CLEAR,
        ("eval", "hidden", """(async () => { const s = await import('/static/js/sound.js');
          Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
          s.signal('done', 'hidden-probe'); delete document.visibilityState; return window.tones.slice(); })()"""),
        ("eval", "agents", """(async () => { const s = await import('/static/js/sound.js'); const seen = new Map();
          return [s.agentSignals(seen, 7, [{ chat: 'c1', running: true }]),
                  s.agentSignals(seen, 7, [{ chat: 'c1', running: true, asking: true }]),
                  s.agentSignals(seen, 7, [])]; })()"""),
        ("eval", "file", "fetch('/static/js/sound.js').then(r => r.status)")], init=FAKE_AUDIO)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["default"] == [None, []], v
    assert "Звуковые сигналы" in v["words"][0] and "Только на этом устройстве и пока Студия на экране" in v["words"][0]
    assert v["on"] == ["1", [660, 880]], v
    assert v["done"] == [660, 880] and v["after_poll"] == [660, 880], v
    assert v["hidden"] == [], v
    assert v["agents"] == [[], ["ask"], ["done"]], v
    assert not problems(r), problems(r)


SUSPENDED_AUDIO = """window.audio = { made: 0, resumed: 0, last: null };
window.AudioContext = class {
  constructor() { this.state = 'suspended'; this.currentTime = 0; this.destination = {}; window.audio.made++;
    window.audio.last = this; }
  resume() { window.audio.resumed++; this.state = 'running'; return Promise.resolve(); }
  createOscillator() { return { type: '', frequency: { value: 0 }, connect: (g) => g, start() {}, stop() {} }; }
  createGain() { return { gain: { setValueAtTime() {}, linearRampToValueAtTime() {} }, connect: (d) => d }; }
};"""


def test_every_gesture_wakes_a_suspended_context(browser, live):
    """Контекст будит каждое отпускание пальца и клавиша, пока он не `running` (касание даёт активацию на `pointerup`,
    iOS приостанавливает контекст после фона); при выключенном звуке контекста нет, отменённое задание молчит."""
    login = "sound-wake"
    live.add_user(login)
    gesture = """(async () => { await import('/static/js/sound.js');
      document.body.dispatchEvent(new PointerEvent('%s', { bubbles: true, pointerType: 'touch' }));
      await Promise.resolve(); return [window.audio.made, window.audio.resumed, window.audio.last?.state ?? null]; })()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "off", gesture % "pointerup"),
        ("eval", "on", "(() => { localStorage.setItem('studio-sound', '1'); return true; })()"),
        ("eval", "down", gesture % "pointerdown"),
        ("eval", "up", gesture % "pointerup"),
        ("eval", "again", gesture % "pointerup"),
        ("eval", "background", "(() => { window.audio.last.state = 'suspended'; return true; })()"),
        ("eval", "back", gesture % "pointerup"),
        ("eval", "cancelled", """(async () => { const s = await import('/static/js/sound.js');
          return s.jobSignal([{ id: 'j1', state: 'cancelled' }]); })()""")], init=SUSPENDED_AUDIO, width=320, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["off"] == [0, 0, None] and v["down"] == [0, 0, None], v
    assert v["up"] == [1, 1, "running"] and v["again"] == [1, 1, "running"], v
    assert v["back"] == [1, 2, "running"] and v["cancelled"] is False, v
    assert not problems(r), problems(r)


def test_agents_signal_through_the_snapshot_event(browser, live):
    """Снимки «Агентов» — событием `studio-code-snapshot`: первый снимок компьютера молчит, «ждёт ответа» — `ask`,
    конец хода — `done`."""
    login = "sound-agents"
    live.add_user(login)
    snap = """(() => { window.tones.length = 0; document.dispatchEvent(new CustomEvent('studio-code-snapshot',
      { detail: { aid: 'a9', items: %s } })); return window.tones.slice(); })()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "load", """import('/static/js/sound.js').then(() => {          // жест создаёт контекст звука
          document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Shift', bubbles: true })); return true; })"""),
        ("eval", "first", snap % "[{ chat: 'c1', running: true }]"),
        ("eval", "asking", snap % "[{ chat: 'c1', running: true, asking: true }]"),
        pause(1600),
        ("eval", "done", snap % "[]")], init="localStorage.setItem('studio-sound', '1');" + FAKE_AUDIO)
    assert not r.get("failed"), r
    v = r["values"]
    assert [v["first"], v["asking"], v["done"]] == [[], [880, 880], [660, 880]], v
    assert not problems(r), problems(r)


@pytest.mark.parametrize("kind", ["error", "ask"])
def test_error_and_pause_have_their_own_signals(browser, live, kind):
    login = f"sound-{kind}"
    live.add_user(login)
    if kind == "error":
        live.gw.chat_error = (500, {"error": {"message": "boom"}})
        wait = ("wait", "document.querySelector('#messages .msg.assistant .msg-meta.error') && window.tones.length >= 2")
    else:
        live.gw.chat_script = [{"text": ["Уточню."], "tool_calls": [("ask_user", ASK)]}]
        wait = ("wait", "document.querySelector('.msg-pending-card') && window.tones.length >= 2")
    try:
        r = browser.drive(live.url + "/", [*start(login), CLEAR,
            ("type", "#chat-input", "Вопрос"), ("press", "Enter"), wait, pause(300), ("eval", "tones", TONES)],
            init="localStorage.setItem('studio-sound', '1');" + FAKE_AUDIO)
    finally:
        live.gw.chat_error = None
        live.gw.chat_script = []
    assert not r.get("failed"), r
    assert r["values"]["tones"] == ([330, 220] if kind == "error" else [880, 880]), r["values"]
