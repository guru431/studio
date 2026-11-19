"""Оболочка страницы: адрес раздела, блокировка, несохранённое и слова интерфейса в настоящем браузере."""
import pytest

from test_ui_browser import READY, browser, go, live, open_new_chat, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: POST /api/chats ждёт `window.releaseChat()`: ответ создания диалога приходит, когда раздел уже другой
HOLD_CHAT = """(() => { const real = window.fetch; let release; const gate = new Promise(r => { release = r; });
  window.releaseChat = () => { release(); return true; };
  window.fetch = (url, options = {}) => String(url) === '/api/chats' && options.method === 'POST'
    ? gate.then(() => real(url, options)) : real(url, options);
  return true; })()"""


def test_chat_created_after_leaving_keeps_the_address_of_the_open_section(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("eval", "hold", HOLD_CHAT), ("type", "#chat-input", "Ответ дочитает «В работе»"), ("press", "Enter"),
        *go("photo"), ("wait", "document.querySelector('#rail [data-section=photo][aria-current=page]')"),
        ("eval", "release", "window.releaseChat()"),
        ("wait", "fetch('/api/chats').then(r => r.json()).then(j => j.items.length > 0)"),
        ("eval", "settled", "new Promise(r => setTimeout(() => r(location.hash), 500))")])
    assert not r.get("failed"), r
    assert r["values"]["settled"] == "#/photo"
    assert not problems(r), problems(r)


def test_agent_drafts_leave_the_tab_with_the_emergency_exit(browser, live):
    login = "guard-drafts"
    live.add_user(login)
    stored = "sessionStorage.getItem('studio-agent-drafts')"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "saved", "import('/static/js/code-drafts.js').then(m => { m.saveDraft('1/a/b', 'неотправленное задание');"
                          f" return {stored}.includes('неотправленное'); }})"),
        *go("settings/security"), ("wait", "document.querySelector('#emergency-password')"),
        set_value("#emergency-password", "password-123"), ("click", "#emergency-start"), ("click", "#emergency-confirm"),
        ("wait", "location.pathname === '/login'"), ("eval", "after", stored)])
    assert not r.get("failed"), r
    assert r["values"]["saved"] is True and r["values"]["after"] is None


def test_english_connection_history_names_the_devices_in_english(browser, live):
    from test_ui_browser import live_sql
    import time
    login = "guard-devices"
    uid = live.add_user(login)
    for i, device in enumerate(["браузер a1b2c3", "телефон 0f9e8d", "мост", "аварийный выход", "участник", "Ноутбук"]):
        live_sql(live, "INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
                       " VALUES (?, 7, 'PC', ?, 'open', ?, NULL)", (uid, time.time() - 100 + i, device))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/code"),
        ("wait", "document.querySelectorAll('#agent-events li').length === 6"),
        ("eval", "devices", "[...document.querySelectorAll('#agent-events li')].map(li => li.textContent.split(' · ').at(-1))")],
        init="localStorage.setItem('studio-language', 'en')")
    assert not r.get("failed"), r
    assert r["values"]["devices"] == ["Ноутбук", "member", "emergency sign-out", "bridge", "phone 0f9e8d", "browser a1b2c3"]


#: /api/jobs страницы отвечает строками сервера (jobs.py) — русские слова и имя файла
FAKE_JOBS = """localStorage.setItem('studio-language', 'en');
  const real = window.fetch;
  window.fetch = (url, options) => {
    if (!String(url).startsWith('/api/jobs')) return real(url, options);
    const now = Date.now() / 1000;
    const row = (section, id, title, detail) => ({ section, id, state: 'running', title, detail, started: now - 5,
      finished: null, href: section === 'chat' ? 'chat/c1' : `${section}?id=${id}`,
      cancel: section === 'chat' ? '/api/chats/c1/stop' : `/api/transcripts/${id}/cancel` });
    return Promise.resolve(new Response(JSON.stringify({ now, push: false, finished: [], running: [
      row('transcribe', 't1', 'Расшифровка «Встреча.m4a»', 'жду карту'),
      row('chat', 'm1', 'Ответ · новый диалог', 'пишет ответ')] }), { headers: { 'content-type': 'application/json' } }));
  };"""


def test_english_jobs_panel_translates_server_words_but_not_names(browser, live):
    rows = "[...document.querySelectorAll('#jobs-running .jobs-row')].map(r => [r.querySelector('.jobs-title').textContent," \
           " r.querySelector('.jobs-detail').textContent.split(' · ')[0]])"
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("click", "#jobs"),
        ("wait", "document.querySelectorAll('#jobs-running .jobs-row').length === 2"), ("eval", "rows", rows)],
        init=FAKE_JOBS)
    assert not r.get("failed"), r
    assert sorted(r["values"]["rows"]) == [["Response · new conversation", "writing response"],
                                           ["Transcription “Встреча.m4a”", "waiting for GPU"]]


#: плеер на странице и «картинка в картинке», выход из которой виден по window.leftPip
PLAYER = """(() => { const v = document.createElement('video'); v.id = 'guard-video'; v.muted = true;
  document.body.append(v);
  Object.defineProperty(document, 'pictureInPictureElement', { configurable: true, get: () => window.leftPip ? null : v });
  document.exitPictureInPicture = () => { window.leftPip = true; return Promise.resolve(); };
  return true; })()"""
PLAY = """(() => { const v = document.querySelector('#guard-video'); v.play().catch(() => {});
  return new Promise(r => setTimeout(() => r(v.paused), 300)); })()"""


def test_locked_studio_leaves_picture_in_picture_and_pauses_any_play(browser, live):
    from test_ui_browser import API, LOCK_OPEN, hide, show
    login = "guard-player"
    live.add_user(login)
    configure = f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/me/screen-lock', {{enabled:true, timeout:0}});
      await m.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "configure", configure),
        ("eval", "player", PLAYER), hide(), ("wait", LOCK_OPEN), show(),
        ("eval", "pip", "window.leftPip === true"), ("eval", "lockedPaused", PLAY),
        ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"), ("wait", f"!{LOCK_OPEN}"),
        ("eval", "openPaused", PLAY)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pip"] is True and v["lockedPaused"] is True and v["openPaused"] is False



def test_expired_partial_lock_forgets_jobs_of_protected_sections_in_an_open_section(browser, live):
    """«Озвучка» под защитой с таймером 0, открыт «Чат»: уход с экрана истекает срок — строка озвучки уходит из
    открытой панели «В работе», хотя сервер её уже не отдаёт, а страница держала бы её в памяти час."""
    import time
    from test_ui_browser import API, hide, live_sql, show
    login = "guard-partial"
    uid = live.add_user(login)
    live_sql(live, "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created, finished)"
                   " VALUES ('sp-guard', ?, 'tts', 'v', 'Тайный текст озвучки', 'done', '', ?, ?)",
             (uid, time.time() - 60, time.time() - 30))
    configure = f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/me/screen-lock', {{enabled:true, sections:['speech'], timeout:0}});
      await m.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"""
    speech_row = "document.querySelector('#jobs-finished .jobs-row[data-section=speech]')"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "configure", configure),
        ("click", "#jobs"), ("wait", f"!!{speech_row}"),
        ("eval", "title", f"{speech_row}.querySelector('.jobs-title').textContent"),
        hide(), show(), ("wait", f"!{speech_row}"),
        ("eval", "later", f"new Promise(r => setTimeout(() => r(!!{speech_row}), 1500))"),
        ("eval", "hash", "location.hash")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["title"].startswith("Озвучка · Тайный") and v["later"] is False and v["hash"] == "#/chat"


def test_unsaved_transcript_edit_asks_before_another_record_or_section(browser, live):
    from test_ui_browser import seed_list, transcribed
    login = "guard-transcript"
    other = seed_list(live, "transcribe", live.add_user(login), 1)[0]
    heading = "document.querySelector('.split-main h1').textContent"
    second = ".tr-turn[data-index='1'] .tr-edit-text"
    asked = "!!document.querySelector('#tr-unsaved')?.open"
    r = browser.drive(live.url + "/", [*transcribed(login),
        ("eval", "id", "window.trId = document.querySelector('.tr-item[aria-current=true]').dataset.id"),
        ("click", "#tr-edit"), ("wait", "document.querySelectorAll('.tr-edit-text').length === 2"),
        set_value(second, "Изменено."), ("click", f".tr-item[data-id='{other}']"), ("wait", asked),
        ("click", "#tr-unsaved .dialog-close"),
        ("eval", "stayed", f"[{heading}, document.querySelector({second!r}).value]"),
        ("click", f".tr-item[data-id='{other}']"), ("wait", asked), ("click", "#tr-unsaved-drop"),
        ("wait", f"{heading} === 'Запись 0'"),
        ("click", f".tr-item:not([data-id='{other}'])"), ("wait", f"{heading} === 'Звонок.wav'"),
        ("click", "#tr-edit"), ("wait", "document.querySelectorAll('.tr-edit-text').length === 2"),
        set_value(second, "Сохранено."), *go("photo"), ("wait", f"{asked} && location.hash === '#/transcribe'"),
        ("click", "#tr-unsaved-save"), ("wait", "location.hash === '#/photo'"),
        ("wait", "document.querySelector('#rail [data-section=photo][aria-current=page]')"),
        ("eval", "saved", "fetch('/api/transcripts/' + window.trId).then(r => r.json()).then(j => j.segments.map(s => s.text))")],
        timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["stayed"] == ["Звонок.wav", "Изменено."] and v["saved"] == ["Здравствуйте.", "Сохранено."]


def test_chat_tells_the_screen_reader_when_the_answer_starts_and_is_ready(browser, live):
    status = "document.querySelector('#chat-status')"
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("eval", "role", f"[{status}.getAttribute('role'), {status}.classList.contains('sr')]"),
        ("eval", "spy", f"(() => {{ window.said = []; new MutationObserver(() => window.said.push({status}.textContent))"
                        f".observe({status}, {{ childList: true, characterData: true, subtree: true }}); return true; }})()"),
        ("type", "#chat-input", "Привет"), ("press", "Enter"),
        ("wait", f"{status}.textContent === 'Ответ готов'"), ("eval", "said", "window.said")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["role"] == ["status", True] and v["said"][0] == "Модель отвечает…" and v["said"][-1] == "Ответ готов"
    assert "Здравствуйте" not in "".join(v["said"])


#: сбой связи раздела (refresh.js) с короткой первой паузой
FAILURE = """import('/static/js/refresh.js').then(m => { m.refreshAfterFailure({ code: 'network' }); return true; })"""
QUICK = """const delay = window.setTimeout;
  window.setTimeout = (fn, ms, ...args) => delay(fn, ms === 5000 ? 300 : ms, ...args);"""


def test_unsent_chat_text_turns_the_automatic_refresh_into_an_offer(browser, live):
    navigation = "performance.getEntriesByType('navigation')[0].type"
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *go("chat"),
        ("wait", "document.querySelector('#chat-input')"), ("type", "#chat-input", "Не потерять этот текст"),
        ("eval", "failed", FAILURE), ("wait", "document.querySelector('#studio-refresh-offer')"),
        ("eval", "kept", f"new Promise(r => setTimeout(() => r([{navigation},"
                         " document.querySelector('#chat-input').value]), 1000))"),
        ("click", "#studio-refresh-go"), ("wait", f"{navigation} === 'reload' && {READY}"),
        ("eval", "offer", "!!document.querySelector('#studio-refresh-offer')")], init=QUICK)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kept"] == ["navigate", "Не потерять этот текст"] and v["offer"] is False
