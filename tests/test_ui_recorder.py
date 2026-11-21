"""«Записать» в «Расшифровке» (п. 38 спеки 2026-11-21): длинная запись с микрофона — пауза, смена раздела её не
прерывает (точка у рельса), «Стоп и расшифровать» загружает тем же путём, что файл; перезагрузка посреди записи —
«Восстановить запись (мм:сс)» из IndexedDB (Chromium 320/1280).

Микрофон и MediaRecorder — поддельные (сценарий до скриптов каждой страницы): рекордер отдаёт куски WAV на 6 с по
2000 байт каждые 300 мс, на stop — остаток; склеенные куски — целый WAV, и сервер принимает запись."""
import httpx
import pytest

from test_ui_browser import NO_OVERFLOW, READY, add_gateway, browser, go, live, problems, signin  # noqa: F401
from test_ui_chat_focus import focus_browser  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

#: window.recorders — созданные рекордеры, recSlices — timeslice их start, stopped — отпущенные дорожки,
#: locks / unlocks — Wake Lock экрана
FAKE_RECORDER = """(() => {
  const rate = 8000, n = rate * 6, buf = new ArrayBuffer(44 + n), v = new DataView(buf);
  const put = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  put(0, 'RIFF'); v.setUint32(4, 36 + n, true); put(8, 'WAVEfmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate, true); v.setUint16(32, 1, true);
  v.setUint16(34, 8, true); put(36, 'data'); v.setUint32(40, n, true); new Uint8Array(buf, 44).fill(128);
  const wav = new Uint8Array(buf);
  window.recorders = []; window.recSlices = []; window.stopped = 0; window.locks = 0; window.unlocks = 0;
  if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = async () => {
    const track = { enabled: true, stop() { window.stopped += 1; } };
    return { getTracks: () => [track] };
  };
  Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: { request: async () => {
    window.locks += 1;
    const lock = { released: false, async release() { if (!lock.released) { lock.released = true; window.unlocks += 1; } } };
    return lock; } } });
  class FakeRecorder extends EventTarget {
    static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
    constructor(stream, options) { super(); this.mimeType = (options && options.mimeType) || ''; this.state = 'inactive';
      this.options = options; this.at = 0; window.recorders.push(this); }
    give(all) { const end = all ? wav.length : Math.min(wav.length, this.at + 2000);
      const event = new Event('dataavailable'); event.data = new Blob([wav.slice(this.at, end)], { type: 'audio/webm' });
      this.at = end; this.dispatchEvent(event); }
    start(slice) { window.recSlices.push(slice); this.state = 'recording';
      this.timer = setInterval(() => { if (this.state === 'recording') this.give(false); }, 300); }
    pause() { this.state = 'paused'; }
    resume() { this.state = 'recording'; }
    requestData() { if (this.state !== 'inactive') this.give(false); }
    stop() { clearInterval(this.timer); this.give(true); this.state = 'inactive';
      setTimeout(() => this.dispatchEvent(new Event('stop')), 0); }
  }
  window.MediaRecorder = FakeRecorder;
  window.wavSize = wav.length;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) { window.sentSize = body && body.size; return send.call(this, body); };
})();"""

RECORDER = "(await import('/static/js/recorder.js'))"
PENDING = f"(async () => {RECORDER}.pendingRecordings())()"
LAST = "window.recorders.at(-1)"
PANEL = "document.querySelector('#rec-panel')"
DOT = "document.querySelector('#rec-dot')"


def record(login):
    return [*signin(login), ("wait", READY), add_gateway(),
            *go("transcribe"), ("wait", "document.querySelector('#transcribe-record')?.offsetWidth > 0"),
            ("click", "#transcribe-record"),
            ("wait", f"{PANEL} && !{PANEL}.hidden && {LAST}?.state === 'recording'")]


@pytest.mark.parametrize("width", [320, 1280])
def test_record_pause_leave_and_stop_uploads_the_recording(browser, live, width):
    login = f"rec{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*record(login),
        ("wait", f"(async () => {{ const p = await {RECORDER}.pendingRecordings(); return p.length === 1 && p[0].seconds > 0; }})()"),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "slice", "window.recSlices[0]"),
        ("eval", "type", f"{LAST}.mimeType"),
        ("eval", "bits", f"{LAST}.options?.audioBitsPerSecond ?? null"),
        ("click", "#rec-pause"), ("wait", f"{LAST}.state === 'paused' && {PANEL}.dataset.state === 'paused'"),
        ("eval", "paused", "document.querySelector('#rec-pause').textContent"),
        ("eval", "unlocked", "window.unlocks"),
        # диктовка, пока идёт запись встречи, не стартует: микрофон у неё
        ("eval", "dictation", "(async () => { const m = await import('/static/js/mic.js');"
                              " try { await m.startRecording(10); return 'started'; } catch (e) { return e.message; } })()"),
        ("click", "#rec-pause"), ("wait", f"{LAST}.state === 'recording' && {PANEL}.dataset.state === 'recording'"),
        # смена раздела запись не прерывает: точка у рельса, запись идёт
        *go("chat"), ("wait", "document.querySelector('#chat-input')"),
        ("wait", f"{DOT} && !{DOT}.hidden && /\\d\\d:\\d\\d/.test({DOT}.textContent)"),
        ("eval", "dot", f"{DOT}.textContent"),
        ("eval", "dotFits", NO_OVERFLOW),
        ("eval", "still", f"{LAST}.state"),
        ("click", "#rec-dot"), ("wait", f"location.hash === '#/transcribe' && {PANEL} && !{PANEL}.hidden"),
        ("click", "#rec-stop"),
        ("wait", "document.querySelector('.tr-item')"),
        ("wait", f"(async () => (await {RECORDER}.pendingRecordings()).length === 0)()"),
        ("wait", f"{DOT}.hidden && {PANEL}.hidden"),
        ("eval", "item", "document.querySelector('.tr-item').textContent"),
        ("eval", "whole", "[window.sentSize, window.wavSize]"),
        ("eval", "released", "[window.stopped, window.locks, window.unlocks]")],
        width=width, height=900, mobile=width == 320, init=FAKE_RECORDER, timeout=30000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["fits"] and v["dotFits"], v
    assert v["slice"] == 10000 and v["type"] == "audio/webm;codecs=opus", v      # кусок каждые 10 с
    # ревью C-M5: речь — 32 кбит/с (сервер всё равно сжимает до 32k); 4 ч AAC по умолчанию браузера — больше 200 МиБ
    assert v["bits"] == 32000, v
    assert v["paused"] == "Продолжить" and v["unlocked"] >= 1, v                 # на паузе экран может гаснуть
    assert "запись" in v["dictation"] and v["dictation"] != "started", v
    assert "Запись" in v["dot"] and v["still"] == "recording", v
    assert v["item"].startswith("Запись "), v["item"]
    sent, wav = v["whole"]
    assert sent == wav, v["whole"]                    # куски, уже лежащие в IndexedDB, — из неё, запись целиком
    stopped, locks, unlocks = v["released"]
    assert stopped == 1 and locks >= 2 and unlocks == locks, v["released"]
    assert r["sent"].count("POST /api/transcripts") == 1, r["sent"]
    assert not problems(r), problems(r)


def test_without_device_storage_the_recording_still_uploads_whole(browser, live):
    """Ревью C-M5: записанные в IndexedDB куски в памяти не держатся; когда хранилища нет, они остаются в памяти, и
    «Стоп и расшифровать» всё равно отправляет запись целиком (и предупреждает, что перезагрузка её сотрёт)."""
    login = "recnodb"
    live.add_user(login)
    no_db = "indexedDB.open = () => { throw new DOMException('blocked', 'SecurityError'); };"
    r = browser.drive(live.url + "/", [*record(login),
        ("wait", f"{LAST}.at > 2000"),
        ("eval", "note", "document.querySelector('#rec-note').textContent"),
        ("click", "#rec-stop"),
        ("wait", "document.querySelector('.tr-item')"),
        ("eval", "whole", "[window.sentSize, window.wavSize]")],
        init=FAKE_RECORDER.replace("window.MediaRecorder = FakeRecorder;", f"window.MediaRecorder = FakeRecorder; {no_db}"))
    assert not r.get("failed"), r
    v = r["values"]
    assert "перезагрузка её сотрёт" in v["note"], v
    sent, wav = v["whole"]
    assert sent == wav, v["whole"]
    assert r["sent"].count("POST /api/transcripts") == 1, r["sent"]


def test_cancel_erases_the_recording_after_a_question(browser, live):
    login = "reccancel"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*record(login),
        ("wait", f"(async () => (await {RECORDER}.pendingRecordings()).length === 1)()"),
        ("click", "#rec-cancel"), ("wait", "document.querySelector('#rec-drop-dialog[open]')"),
        ("click", "#rec-drop-confirm"),
        ("wait", f"{PANEL}.hidden && {DOT}.hidden && !document.querySelector('#rec-drop-dialog[open]')"),
        ("wait", f"(async () => (await {RECORDER}.pendingRecordings()).length === 0)()"),
        ("eval", "restore", "document.querySelector('#rec-restore-bar').hidden"),
        ("eval", "stopped", "window.stopped")], init=FAKE_RECORDER)
    assert not r.get("failed"), r
    assert r["values"] == {**r["values"], "restore": True, "stopped": 1}, r["values"]
    assert "POST /api/transcripts" not in r["sent"], r["sent"]
    assert not problems(r), problems(r)


def test_another_tab_cannot_recover_or_delete_an_active_recording(focus_browser, live):
    login = f"recshared-{focus_browser.browser_type.name}"
    live.add_user(login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with focus_browser.new_context(extra_http_headers={"Cookie": cookie}) as context:
        if focus_browser.browser_type.name == "firefox":
            context.add_cookies([{"name": key, "value": value, "url": live.url, "secure": True,
                                  "httpOnly": True, "sameSite": "Lax"} for key, value in signed.cookies.items()])
        context.add_init_script(FAKE_RECORDER)
        first = context.new_page()
        first.goto(live.url + "/f")
        if not first.evaluate("() => !!navigator.locks && !!navigator.mediaDevices"):
            pytest.skip("нет Web Locks или микрофона на HTTP localhost")
        rid = first.evaluate(f"async () => (await {RECORDER}.startMeeting()).id")
        first.wait_for_function(f"async () => (await {PENDING}).length === 1")
        second = context.new_page()
        second.goto(live.url + "/#/transcribe")
        second.locator("#rec-restore-bar").wait_for(state="attached")
        assert second.evaluate(f"async () => await {RECORDER}.pendingRecordings({{ inactiveOnly: true }})") == []
        assert second.evaluate(f"""async () => {{
          try {{ await {RECORDER}.discard({rid!r}); return false; }} catch {{ return true; }}
        }}""")
        assert not second.locator('.rec-restore').count()
        first.close()
        second.reload()
        second.locator(f'.rec-restore[data-id="{rid}"]').wait_for()


@pytest.mark.parametrize("width", [320, 1280])
def test_reload_in_the_middle_offers_to_restore_the_recording(browser, live, width):
    login = f"recback{width}"
    live.add_user(login)
    restore = "document.querySelector('.rec-restore')"
    r = browser.drive(live.url + "/", [*record(login),
        ("wait", f"(async () => {{ const p = await {RECORDER}.pendingRecordings(); return p.length === 1 && p[0].seconds >= 1; }})()"),
        ("eval", "reload", "location.reload(); true"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", f"{restore}?.offsetWidth > 0"),
        ("eval", "label", f"{restore}.textContent"),
        ("eval", "panel", f"{PANEL}.hidden"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", ".rec-restore"),
        ("wait", "document.querySelector('.tr-item')"),
        ("wait", f"(async () => (await {RECORDER}.pendingRecordings()).length === 0)() && !{restore}"),
        ("eval", "item", "document.querySelector('.tr-item').textContent")],
        width=width, height=900, mobile=width == 320, init=FAKE_RECORDER, timeout=30000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["label"].startswith("Восстановить запись (00:0"), v["label"]
    assert v["panel"] and v["fits"], v
    assert v["item"].startswith("Запись "), v["item"]
    assert r["sent"].count("POST /api/transcripts") == 1, r["sent"]
    assert not problems(r), problems(r)


def test_meeting_pauses_before_the_limit_and_is_still_accepted(browser, live):
    """Ревью C-I1: запись встаёт на паузу до 4 ч с запасом, проверка страницы — с допуском сервера; прежде она
    вставала на 14400,0–14400,25 с, а страница и сервер отказывали всему длиннее 14400 с — запись застревала."""
    login = "reclimit"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*record(login),
        # часы страницы — на 5 с до предела: запись «длилась» почти 4 ч
        ("eval", "shift", "(() => { const real = performance.now.bind(performance);"
                          " performance.now = () => real() + (4 * 3600 - 5) * 1000; return true; })()"),
        ("wait", f"{LAST}.state === 'paused' && {PANEL}.dataset.state === 'paused'"),
        ("eval", "seconds", f"(async () => {RECORDER}.meeting().seconds)()"),
        ("eval", "note", "document.querySelector('#rec-note').textContent"),
        ("click", "#rec-stop"),
        ("wait", "document.querySelector('.tr-item') || document.querySelector('.tr-error')?.textContent"),
        ("eval", "error", "document.querySelector('.tr-error').textContent"),
        ("wait", f"(async () => (await {RECORDER}.pendingRecordings()).length === 0)()")],
        init=FAKE_RECORDER, timeout=30000)
    assert not r.get("failed"), r
    v = r["values"]
    assert 4 * 3600 - 60 < v["seconds"] < 4 * 3600, v
    assert "Запись достигла 4 ч" in v["note"] and not v["error"], v
    assert r["sent"].count("POST /api/transcripts") == 1, r["sent"]
    assert not problems(r), problems(r)


def test_left_recording_can_be_downloaded(browser, live):
    """Ревью C-I1: запись, оставшаяся на устройстве (перезагрузка, отказ загрузки), — «Скачать запись»: её можно
    снять с устройства, даже если расшифровать её не выйдет; куски остаются до «Восстановить» или «Удалить»."""
    login = "recsave"
    live.add_user(login)
    save = "document.querySelector('.rec-save')"
    r = browser.drive(live.url + "/", [*record(login),
        ("wait", f"(async () => {{ const p = await {RECORDER}.pendingRecordings(); return p.length === 1 && p[0].seconds >= 1; }})()"),
        ("eval", "reload", "location.reload(); true"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", f"{save}?.offsetWidth > 0"),
        ("eval", "label", f"{save}.textContent"),
        ("eval", "fits", NO_OVERFLOW),
        # blob: URL CSP читать не даёт (connect-src) — Blob берётся у createObjectURL
        ("eval", "spy", "(() => { window.clicked = []; const make = URL.createObjectURL;"
                        " URL.createObjectURL = (blob) => { window.made = blob; return make(blob); };"
                        " HTMLAnchorElement.prototype.click = function () {"
                        " window.clicked.push([this.getAttribute('href'), this.getAttribute('download')]); }; return true; })()"),
        ("click", ".rec-save"),
        ("wait", "window.clicked.length === 1"),
        ("eval", "file", "(() => { const [href, name] = window.clicked[0];"
                         " return [href.slice(0, 5), name, window.made.size, window.made.type]; })()"),
        ("eval", "left", f"(async () => (await {RECORDER}.pendingRecordings()).length)()")],
        width=320, height=900, mobile=True, init=FAKE_RECORDER, timeout=30000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["label"] == "Скачать запись" and v["fits"], v
    href, name, size, kind = v["file"]
    assert href == "blob:" and name.startswith("Запись ") and name.endswith(".webm") and size > 0, v["file"]
    assert kind == "audio/webm" and v["left"] == 1, v
    assert "POST /api/transcripts" not in r["sent"], r["sent"]
    assert not problems(r), problems(r)
