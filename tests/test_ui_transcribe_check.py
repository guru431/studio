"""«Расшифровка»: проверка записи до загрузки по пределам источника и язык распознавания у загрузки."""
import httpx
import pytest

from test_ui_browser import READY, NO_OVERFLOW, add_gateway, go, problems, set_value, signin, browser, live  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: загрузка не уходит в сеть: заголовки XHR записи — в window.sent
SPY = """(() => { const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send,
  header = XMLHttpRequest.prototype.setRequestHeader; window.sent = {};
  XMLHttpRequest.prototype.open = function (method, url, ...rest) { this.target = url; return open.call(this, method, url, ...rest); };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this.target === '/api/transcripts') window.sent[k.toLowerCase()] = v; return header.call(this, k, v); };
  XMLHttpRequest.prototype.send = function (body) {
    if (this.target !== '/api/transcripts') return send.call(this, body);
    window.sent.body = body.name;
  };
  const fetch = window.fetch;
  window.fetch = async (url, init) => {
    if (String(url).endsWith('/api/transcripts/limits')) await new Promise(resolve => { window.releaseLimits = resolve; });
    return fetch(url, init);
  };
  return true; })()"""

#: WAV 8 кГц 8 бит моно на 150 с (≈1,2 МБ) строится в странице — в поле файла «Расшифровки»
LONG_WAV = """(() => { const rate = 8000, n = rate * 150, buf = new ArrayBuffer(44 + n), v = new DataView(buf);
  const put = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  put(0, 'RIFF'); v.setUint32(4, 36 + n, true); put(8, 'WAVEfmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate, true); v.setUint16(32, 1, true);
  v.setUint16(34, 8, true); put(36, 'data'); v.setUint32(40, n, true); new Uint8Array(buf, 44).fill(128);
  const dt = new DataTransfer(); dt.items.add(new File([buf], 'Планёрка.wav', { type: 'audio/wav' }));
  const input = document.querySelector('#transcribe-add input'); input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true })); return true; })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_check_before_upload_tells_about_parts_and_sends_the_language(browser, live, width):
    """Источник берёт по 60 с: запись на 150 с — «будет расшифрована 3 частями», уходит с выбранным языком."""
    login = f"trcheck{width}"
    live.add_user(login)

    async def limits(request):
        return httpx.Response(200, json={"max_seconds": 60, "max_bytes": 25 << 20})
    live.gw.on["/speech/limits"] = limits
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        *go("transcribe"), ("wait", "document.querySelector('#tr-language-pick')?.offsetWidth > 0"),
        set_value("#tr-language", "en"),
        ("eval", "chip", "document.querySelector('#tr-language-pick').textContent"),
        ("eval", "spy", SPY), ("eval", "file", LONG_WAV),
        ("wait", "window.releaseLimits"),
        ("eval", "preventReload", "!document.dispatchEvent(new Event('studio-before-auto-refresh', { cancelable: true }))"),
        ("eval", "release", "window.releaseLimits(); true"),
        ("wait", "window.sent.body"),
        ("eval", "note", "document.querySelector('#transcribe-check').textContent"),
        ("eval", "sent", "[window.sent.body, window.sent['x-studio-language']]"),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "go", "location.hash = '#/settings/input'"), ("wait", "document.querySelector('#dictation-language')"),
        set_value("#dictation-language", "ru"),
        ("eval", "device", "localStorage.getItem('studio-dictation-language')"),
        ("eval", "settingsFit", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["chip"] == "English"
    assert v["preventReload"]
    assert v["note"] == "«Планёрка.wav» будет расшифрована 3 частями"
    assert v["sent"] == ["Планёрка.wav", "en"] and v["fits"]
    assert v["device"] == "ru" and v["settingsFit"]            # язык голосового ввода — выбор устройства
    assert not problems(r), problems(r)
