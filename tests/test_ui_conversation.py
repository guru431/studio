"""«Разговор» в чате (п. 13 спеки 2026-11-21): фраза в поддельный микрофон → распознавание → обычный ход → озвучка
ответа по предложениям → снова слушает; диктовка в это время не стартует, Escape останавливает (Chromium 320/1280).

Микрофон — настоящий MediaStream из генератора Web Audio: `window.talk(true)` — «говорит», `false` — тишина; конец фразы
страница находит сама по энергии (chat-voice.js)."""
import pytest

from test_ui_browser import (NO_OVERFLOW, READY, add_gateway, add_voice, browser, go, live,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

TALK = "#talk-toggle"
STATE = f"document.querySelector('{TALK}').dataset.state"

#: микрофон — генератор Web Audio; window.states — смены состояния кнопки, window.stopped — отпущенные дорожки
FAKE_TALKER = f"""(() => {{
  window.states = []; window.mics = 0; window.stopped = 0;
  let gain = null;
  window.talk = (on) => {{ gain.gain.value = on ? 0.5 : 0; return true; }};
  navigator.mediaDevices.getUserMedia = async () => {{
    window.mics += 1;
    const ctx = new AudioContext(); await ctx.resume();
    const osc = ctx.createOscillator(); gain = ctx.createGain(); gain.gain.value = 0;
    const dest = ctx.createMediaStreamDestination();
    osc.connect(gain); gain.connect(dest); osc.start();
    for (const track of dest.stream.getTracks()) {{
      const stop = track.stop.bind(track); track.stop = () => {{ window.stopped += 1; stop(); }};
    }}
    return dest.stream;
  }};
  const button = document.querySelector('{TALK}');
  new MutationObserver(() => {{ const s = button.dataset.state; if (window.states.at(-1) !== s) window.states.push(s); }})
    .observe(button, {{ attributes: true, attributeFilter: ['data-state'] }});
  return true; }})()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_conversation_hears_a_phrase_answers_aloud_and_listens_again(browser, live, width):
    login = f"talk{width}"
    live.add_user(login)
    saved = live.gw.asr_reply, live.gw.chat_reply, len(live.gw.tts_requests)
    live.gw.asr_reply = {"text": "Как дела?"}
    live.gw.chat_reply = ["Хорошо, спасибо. ", "А у вас как?"]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), add_voice(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *go("chat"), ("wait", f"document.querySelector('{TALK}') && document.querySelector('#model-chip')"),
            ("eval", "fake", FAKE_TALKER),
            ("eval", "fits", NO_OVERFLOW),
            ("click", TALK), ("wait", f"{STATE} === 'listening'"),
            ("eval", "pressed", f"document.querySelector('{TALK}').getAttribute('aria-pressed')"),
            ("eval", "on", "window.talk(true)"), ("wait", f"{STATE} === 'hearing'"),
            ("eval", "off", "window.talk(false)"),
            ("wait", f"window.states.includes('speaking') && {STATE} === 'listening'"
                     " && document.querySelectorAll('#messages .msg.assistant').length === 1"),
            ("eval", "states", "window.states.join(',')"),
            ("eval", "user", "document.querySelector('#messages .msg.user').textContent"),
            ("eval", "answer", "document.querySelector('#messages .msg.assistant').textContent"),
            ("eval", "field", "document.querySelector('#chat-input').value"),
            # диктовка, пока идёт «Разговор», не стартует: микрофон у него
            ("click", "#composer .mic"),
            ("wait", "document.querySelector('#composer .mic-error').textContent.length > 0"),
            ("eval", "busy", "document.querySelector('#composer .mic-error').textContent"),
            ("press", "Escape"), ("wait", f"{STATE} === 'off' && window.stopped === 1"),
            ("eval", "after", f"document.querySelector('{TALK}').getAttribute('aria-pressed')"),
            ("eval", "mics", "window.mics")],
            width=width, height=900, mobile=width == 320, timeout=30000)
    finally:
        live.gw.asr_reply, live.gw.chat_reply = saved[0], saved[1]
    assert not r.get("failed"), r
    v = r["values"]
    assert v["fits"] and v["pressed"] == "true" and v["after"] == "false" and v["mics"] == 1, v
    assert v["states"].startswith("listening,hearing,thinking,speaking") and v["states"].endswith("listening"), v["states"]
    assert "Как дела?" in v["user"] and "А у вас как?" in v["answer"] and v["field"] == "", v
    assert "Разговор" in v["busy"], v["busy"]
    assert r["sent"].count("POST /api/dictation") == 1, r["sent"]
    assert r["sent"].count("POST /api/speech/say") == 2, r["sent"]   # два предложения — две фразы по порядку
    assert len(live.gw.tts_requests) - saved[2] == 2
    assert not problems(r), problems(r)
