"""«Чат»: перенос из «Агентов» (спека 2026-11-25-studio-skills-status-local-models, п. 18, 19, 21, 22, 24) — «Продолжить
иначе» у своего сообщения, стрелки по своим сообщениям и «Развернуть все шаги», черновик на каждый диалог, очередь
«После ответа» (до 5, в памяти страницы; ждущую карточку не обходит; закрытие страницы возвращает неотправленное в
черновик), вставка файлов из буфера и перетаскиванием тем же путём, что скрепка."""
import base64
import json

import pytest

from fakes import PNG_REAL
from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, add_gateway, browser, go, known_refusals,  # noqa: F401
                             live, open_new_chat, problems, signin)
from test_ui_chat_pending import ASK

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

PHOTO = base64.b64encode(PNG_REAL).decode()
USERS = "[...document.querySelectorAll('#messages .msg.user .bubble')].map(b => b.textContent)"
QUEUED = "[...document.querySelectorAll('#code-queue .code-queue-preview')].map(p => p.textContent)"
FIELD = "document.querySelector('#chat-input').value"
ERROR = "document.querySelector('#chat-error').textContent"
CHAT = "location.hash.split('/')[2]"
#: ответ идёт: «Остановить» и «После ответа» на виду
RUNNING = "!document.querySelector('#stop').hidden && !document.querySelector('#queue-add').hidden"
CARD = "document.querySelector('#messages .msg.assistant:last-child .msg-pending-card')"


def draft_of(expr: str) -> str:
    """Черновик диалога `expr` (выражение JS с id или 'new') в sessionStorage вкладки или null."""
    return (f"JSON.parse(sessionStorage.getItem('studio-chat-drafts') || 'null')?.items?.[{expr}]?.text ?? null")


def held_until(word: str):
    """Ответ шлюза «идёт» после первого куска, пока у кого-то нет диалога с названием `word`: удержание снимает
    сценарий страницы (`release`)."""
    import state
    return lambda: state.DB.execute("SELECT 1 FROM chats WHERE title = ?", (word,)).fetchone() is None


def release(word: str):
    return ("eval", f"release-{word}", f"""(async () => {{ const m = {API}; const c = await m.api('POST', '/api/chats', {{}});
      await m.api('PATCH', '/api/chats/' + c.id, {{ title: {word!r} }}); return true; }})()""")


def start(login: str, mobile: bool = False):
    return [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')")]


def pause(ms: int):
    return ("eval", f"pause-{ms}", f"new Promise(r => setTimeout(() => r(true), {ms}))")


def test_queue_sends_once_after_the_answer(browser, live):                 # Review Focus 4
    live.add_user("queue-once")
    before = len(live.gw.chat_requests)
    live.gw.chat_hold = held_until("go-once")
    try:
        r = browser.drive(live.url + "/", [*start("queue-once"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("type", "#chat-input", "Второй"), ("press", "Enter"),
            ("wait", f"{QUEUED}.length === 1"),
            ("eval", "queued", QUEUED), ("eval", "field", FIELD),
            ("eval", "later", "!document.querySelector('#queue-add').hidden"),
            # поле пусто, ответ идёт через сервер — автоперезагрузку запрещает сама очередь
            ("eval", "veto", "!document.dispatchEvent(new Event('studio-before-auto-refresh', { cancelable: true }))"),
            ("eval", "words", "(() => { const q = document.querySelector('#code-queue'); return [q.getAttribute('aria-label'),"
                              " q.querySelector('.small').textContent,"
                              " ...[...q.querySelectorAll('.code-queue-actions button')].map(b => b.getAttribute('aria-label'))]; })()"),
            *[step for n in ("3", "4", "5", "6") for step in (
                ("type", "#chat-input", n), ("click", "#queue-add"), ("wait", f"{QUEUED}.at(-1) === {n!r}"))],
            ("type", "#chat-input", "Шестое"), ("press", "Enter"),
            ("wait", ERROR),
            ("eval", "sixth", f"[{ERROR}, {FIELD}]"),
            ("eval", "clear", "(() => { const f = document.querySelector('#chat-input'); f.value = '';"
                              " f.dispatchEvent(new Event('input', { bubbles: true })); return true; })()"),
            release("go-once"),
            ("wait", f"{USERS}.length === 6 && {ANSWERED} && {QUEUED}.length === 0"),
            pause(1500),
            ("eval", "users", USERS),
            ("eval", "unveto", "!document.dispatchEvent(new Event('studio-before-auto-refresh', { cancelable: true }))")])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["queued"] == ["Второй"] and v["field"] == "" and v["later"] is True
    assert v["veto"] is True and v["unveto"] is False            # очередь пуста — перезагрузке не мешает
    # слова «Чата», а не «Агентов» (queueView, `labels`)
    assert v["words"] == ["Очередь сообщений", "После ответа", "Изменить ожидающее сообщение", "Удалить ожидающее сообщение"]
    assert v["sixth"] == ["В очереди не больше 5 сообщений", "Шестое"]
    assert v["users"] == ["Первый", "Второй", "3", "4", "5", "6"]
    assert len(live.gw.chat_requests) == before + 6
    assert not problems(r), problems(r)


def test_queue_survives_a_turn_from_another_device(browser, live):         # Review Focus 4: 409 busy
    """Перед POST хода «Второй» из очереди тот же API начинает удержанный ход «с телефона»: отказ 409 `busy` — элемент
    остаётся в очереди и уходит один раз, когда ответ «телефона» кончился."""
    import state
    live.add_user("queue-busy")
    released = lambda word: state.DB.execute("SELECT 1 FROM chats WHERE title = ?", (word,)).fetchone() is not None  # noqa: E731
    live.gw.chat_hold = lambda: (not released("go-busy-1")
                                 or ("с телефона" in json.dumps(live.gw.chat_requests[-1], ensure_ascii=False)
                                     and not released("go-busy-2")))
    phone = f"""(() => {{ const real = window.fetch; let once = true;
      window.fetch = async (url, opts) => {{
        if (once && opts?.method === 'POST' && /\\/turns$/.test(String(url)) && String(opts.body).includes('"Второй"')) {{
          once = false;
          const r = await {API}.api('POST', '/api/chats/' + location.hash.split('/')[2] + '/turns', {{ text: 'с телефона' }});
          window.held = r.body.getReader(); await window.held.read();
        }}
        return real(url, opts); }};
      return true; }})()"""
    saved = f"""(async () => (await {API}.api('GET', '/api/chats/' + {CHAT})).messages
      .filter(m => m.role === 'user').map(m => m.content))()"""
    try:
        r = browser.drive(live.url + "/", [*start("queue-busy"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("eval", "phone", phone),
            ("type", "#chat-input", "Второй"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            release("go-busy-1"),
            ("wait", f"{ERROR} && {USERS}.length === 2"),
            ("eval", "held", QUEUED), ("eval", "error", ERROR),
            release("go-busy-2"),
            ("wait", f"{USERS}.length === 3 && {ANSWERED} && {QUEUED}.length === 0"),
            pause(1500),
            ("eval", "users", USERS), ("eval", "saved", saved)])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["held"] == ["Второй"] and v["error"] == "Ответ ещё идёт — дождитесь или остановите"
    assert v["users"] == ["Первый", "с телефона", "Второй"] and v["saved"].count("Второй") == 1
    assert not known_refusals(problems(r), 409), problems(r)


def test_queue_pauses_when_the_send_is_uncertain(browser, live):           # Review Focus 4: исход неизвестен
    """POST хода «Второй» из очереди обрывается ошибкой сети до сервера: ушло ли — неизвестно. Сообщение снова в руках
    человека (не «уже отправлено»), очередь на паузе со словами; «Продолжить очередь» отправляет его ровно один раз."""
    live.add_user("queue-unsure")
    before = len(live.gw.chat_requests)
    live.gw.chat_hold = held_until("go-unsure")
    cut = """(() => { const real = window.fetch; let once = true;
      window.fetch = async (url, opts) => {
        if (once && opts?.method === 'POST' && /\\/turns$/.test(String(url)) && String(opts.body).includes('"Второй"')) {
          once = false; throw new TypeError('Failed to fetch');
        }
        return real(url, opts); };
      return true; })()"""
    saved = f"""(async () => (await {API}.api('GET', '/api/chats/' + {CHAT})).messages
      .filter(m => m.role === 'user').map(m => m.content))()"""
    resume = "[...document.querySelectorAll('#code-queue > button')].find(b => b.textContent === 'Продолжить очередь')"
    try:
        r = browser.drive(live.url + "/", [*start("queue-unsure"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("eval", "cut", cut),
            ("type", "#chat-input", "Второй"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            release("go-unsure"),
            ("wait", f"{ANSWERED} && {resume} && {ERROR}.startsWith('Не удалось узнать')"),
            pause(2500),                                   # квитанция: сервер хода не принимал — слова остаются
            ("eval", "paused", f"[{QUEUED}, {ERROR}, {USERS}, !{resume}.disabled]"),
            ("eval", "buttons", "[...document.querySelectorAll('#code-queue .code-queue-actions button')].every(b => !b.disabled)"),
            ("eval", "resume", f"{resume}.click(); true"),
            ("wait", f"{USERS}.length === 2 && {ANSWERED} && {QUEUED}.length === 0"),
            pause(1500),
            ("eval", "users", USERS), ("eval", "saved", saved)])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    words = "Не удалось узнать, ушло ли сообщение — «Продолжить очередь» повторит его без задвоения"
    assert v["paused"] == [["Второй"], words, ["Первый"], True] and v["buttons"] is True
    assert v["users"] == ["Первый", "Второй"] and v["saved"] == ["Первый", "Второй"]
    assert len(live.gw.chat_requests) == before + 2
    assert not problems(r), problems(r)


def test_cleaner_preview_return_pauses_the_queue_with_words(browser, live):
    """Диалог с предпросмотром Cleaner: сообщение очереди открывает «Что получит модель»; «Вернуться» — очередь на
    паузе со словами, сообщение остаётся в очереди, ход не уходит. Признак `cleaner` диалога и предпросмотр — подменой
    ответов в странице (сам Cleaner здесь не нужен)."""
    live.add_user("queue-cleaner")
    before = len(live.gw.chat_requests)
    live.gw.chat_hold = held_until("go-cleaner")
    fake = """(() => { const real = window.fetch;
      window.fetch = async (url, opts) => {
        const path = String(url);
        if (window.fakeCleaner && /\\/cleaner-preview$/.test(path)) {
          window.previews = (window.previews || 0) + 1;
          return new Response(JSON.stringify({ messages: [{ role: 'user', content: 'Второй' }], counts: {},
            deliverable: true, ticket: 't', destination: null }), { headers: { 'content-type': 'application/json' } });
        }
        const response = await real(url, opts);
        if (window.fakeCleaner && (!opts?.method || opts.method === 'GET') && /\\/api\\/chats\\/[0-9a-f]+$/.test(path)
            && response.ok) {
          const data = await response.json();
          data.chat.cleaner = true;
          return new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } });
        }
        return response; };
      return true; })()"""
    resume = "[...document.querySelectorAll('#code-queue > button')].find(b => b.textContent === 'Продолжить очередь')"
    back = ("[...document.querySelectorAll('#cleaner-preview-dialog button')]"
            ".find(b => b.textContent === 'Вернуться')")
    try:
        r = browser.drive(live.url + "/", [*start("queue-cleaner"),
            ("eval", "fake", fake),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("type", "#chat-input", "Второй"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            ("eval", "on", "window.fakeCleaner = true"),
            release("go-cleaner"),
            ("wait", "document.querySelector('#cleaner-preview-dialog[open]')"),
            ("eval", "back", f"{back}.click(); true"),
            ("wait", f"{resume} && {ERROR}"),
            pause(500),
            ("eval", "paused", f"[{QUEUED}, {ERROR}, {USERS}, window.previews]")])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    words = "Сообщение из очереди не отправлено — «Продолжить очередь» снова покажет, что получит модель"
    assert v["paused"] == [["Второй"], words, ["Первый"], 1]
    assert len(live.gw.chat_requests) == before + 1
    assert not problems(r), problems(r)


def test_uncertain_message_returns_to_the_draft_with_its_receipt(browser, live):   # Review Focus 4
    """Закрытие страницы при сообщении очереди с неизвестным исходом: текст — в черновик с квитанцией (id сообщения);
    после перезагрузки Enter отправляет его тем же `request_id` — сервер принимает его один раз."""
    live.add_user("queue-receipt")
    before = len(live.gw.chat_requests)
    live.gw.chat_hold = held_until("go-receipt")
    cut = """(() => { const real = window.fetch; let once = true;
      window.fetch = async (url, opts) => {
        if (once && opts?.method === 'POST' && /\\/turns$/.test(String(url)) && String(opts.body).includes('"Второй"')) {
          once = false; window.cutId = JSON.parse(opts.body).request_id; throw new TypeError('Failed to fetch');
        }
        return real(url, opts); };
      return true; })()"""
    stored = f"JSON.parse(sessionStorage.getItem('studio-chat-drafts') || 'null')?.items?.[{CHAT}]"
    sent_id = """(() => { const real = window.fetch;
      window.fetch = async (url, opts) => {
        if (opts?.method === 'POST' && /\\/turns$/.test(String(url))) window.sentId = JSON.parse(opts.body).request_id;
        return real(url, opts); };
      return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*start("queue-receipt"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("eval", "cut", cut),
            ("type", "#chat-input", "Второй"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            release("go-receipt"),
            ("wait", f"{ANSWERED} && {ERROR}.startsWith('Не удалось узнать')"),
            ("eval", "hide", "window.dispatchEvent(new Event('pagehide')); true"),
            ("eval", "draft", f"[{stored}?.text, {stored}?.receipt === window.cutId]"),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"
                     f" && {USERS}.length === 1 && {FIELD} === 'Второй'"),
            ("eval", "receipt", f"{stored}?.receipt"),
            ("eval", "spy", sent_id),
            ("press", "Enter"), ("wait", f"{USERS}.length === 2 && {ANSWERED}"),
            ("eval", "same", "window.sentId"),
            ("eval", "users", USERS)])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["draft"] == ["Второй", True] and v["same"] == v["receipt"] and v["receipt"]
    assert v["users"] == ["Первый", "Второй"] and len(live.gw.chat_requests) == before + 2
    assert not problems(r), problems(r)


def test_queue_waits_for_the_pending_card(browser, live):                  # Review Focus 4: ждущая карточка
    live.add_user("queue-card")
    before = len(live.gw.chat_requests)
    live.gw.chat_script = [{"text": ["Уточню."], "tool_calls": [("ask_user", ASK)]}, ["Выбран красный."], ["Третий ответ."]]
    live.gw.chat_hold = held_until("go-card")
    try:
        r = browser.drive(live.url + "/", [*start("queue-card"),
            ("type", "#chat-input", "Нарисуй"), ("press", "Enter"), ("wait", RUNNING),
            ("type", "#chat-input", "Потом"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            release("go-card"),
            ("wait", f"{CARD} && !document.querySelector('#send').hidden"),
            pause(1500),
            ("eval", "waiting", f"[{QUEUED}, {USERS}.length, !!{CARD}]"),
            ("click", ".msg-pending-card .pending-option"),
            ("wait", f"{USERS}.length === 2 && {ANSWERED} && {QUEUED}.length === 0"
                     " && document.querySelector('#messages .msg.assistant:last-child .md')?.textContent.includes('Третий')"),
            ("eval", "after", USERS)])
    finally:
        live.gw.chat_hold = False
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["waiting"] == [["Потом"], 1, True]
    assert v["after"] == ["Нарисуй", "Потом"] and len(live.gw.chat_requests) == before + 3
    assert not problems(r), problems(r)


def test_closed_page_returns_the_queue_to_the_draft(browser, live):        # Review Focus 4: закрытие страницы
    live.add_user("queue-close")
    live.gw.chat_hold = held_until("go-close")
    try:
        r = browser.drive(live.url + "/", [*start("queue-close"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("type", "#chat-input", "Вернётся"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            ("type", "#chat-input", "Набранное"),
            ("eval", "hide", "window.dispatchEvent(new Event('pagehide')); true"),
            # возврат из bfcache: поле показывает черновик с очередью, и правка его не затирает
            ("eval", "back", "window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));"
                             f" {FIELD}"),
            ("eval", "edit", "(() => { const f = document.querySelector('#chat-input'); f.value += '!';"
                             " f.dispatchEvent(new Event('input', { bubbles: true })); return true; })()"),
            ("eval", "kept", draft_of(CHAT)),
            ("eval", "id", CHAT),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"
                     f" && {USERS}.length === 1 && {FIELD}"),
            ("eval", "stored", draft_of(CHAT)), ("eval", "field", FIELD),
            release("go-close"),
            ("wait", ANSWERED), pause(1500),
            ("eval", "users", USERS), ("eval", "queued", QUEUED)])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["back"] == "Набранное\n\nВернётся" and v["kept"] == "Набранное\n\nВернётся!"
    assert v["stored"] == "Набранное\n\nВернётся!" == v["field"]
    assert v["users"] == ["Первый"] and v["queued"] == []
    assert not problems(r), problems(r)


def test_locked_page_keeps_the_queue_out_of_the_drafts(browser, live):
    """Блокировка стирает черновики: закрытие запертой страницы очередь в хранилище не пишет; после отпирания (с
    <html> снят `shrouded`) — снова пишет."""
    live.add_user("queue-lock")
    live.gw.chat_hold = held_until("go-lock")
    hide = ("eval", "hide", "window.dispatchEvent(new Event('pagehide')); true")
    try:
        r = browser.drive(live.url + "/", [*start("queue-lock"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"), ("wait", RUNNING),
            ("type", "#chat-input", "Тайное"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            ("eval", "lock", "document.dispatchEvent(new Event('studio-locked'));"
                             " document.documentElement.classList.add('shrouded'); true"),
            hide, pause(100),
            ("eval", "locked", f"[{draft_of(CHAT)}, sessionStorage.getItem('studio-chat-drafts')]"),
            ("eval", "unlock", "document.documentElement.classList.remove('shrouded'); true"), pause(100),
            ("type", "#chat-input", "Открытое"), ("press", "Enter"), ("wait", f"{QUEUED}.length === 1"),
            hide,
            ("eval", "opened", draft_of(CHAT)),
            release("go-lock"), ("wait", ANSWERED)])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["locked"] == [None, None] and v["opened"] == "Открытое"
    assert not problems(r), problems(r)


def test_draft_stays_with_its_dialog(browser, live):
    live.add_user("drafts")
    def to(expr):
        return ("eval", "to", f"location.hash = '#/chat/' + {expr}")
    loaded = lambda word: ("wait", f"document.querySelector('#messages .msg.user .bubble')?.textContent === {word!r}")  # noqa: E731
    r = browser.drive(live.url + "/", [*start("drafts"),
        ("type", "#chat-input", "Диалог А"), ("press", "Enter"), ("wait", ANSWERED),
        ("eval", "a", f"window.a = {CHAT}"),
        ("click", "#chat-new"), ("wait", "location.hash === '#/chat' && !document.querySelector('#messages .msg')"),
        ("type", "#chat-input", "Диалог Б"), ("press", "Enter"), ("wait", ANSWERED),
        ("eval", "b", f"window.b = {CHAT}"),
        to("window.a"), loaded("Диалог А"),
        ("type", "#chat-input", "Для А"),
        to("window.b"), loaded("Диалог Б"),
        ("eval", "in_b", FIELD),
        to("window.a"), loaded("Диалог А"),
        ("eval", "in_a", FIELD),
        ("click", "#chat-new"), ("wait", "location.hash === '#/chat' && !document.querySelector('#messages .msg')"),
        ("type", "#chat-input", "Для нового"),
        to("window.a"), loaded("Диалог А"),
        ("eval", "fresh", "location.hash = '#/chat'"),
        ("wait", "!document.querySelector('#messages .msg')"),
        ("eval", "new", FIELD),
        to("window.a"), loaded("Диалог А"),
        ("eval", "id", CHAT),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        loaded("Диалог А"),
        ("eval", "reloaded", FIELD),
        ("press", "Enter"), ("wait", f"{USERS}.length === 2 && {ANSWERED}"),
        ("eval", "sent", f"[{draft_of(CHAT)}, {FIELD}]"),
        ("type", "#chat-input", "Ещё"),
        ("eval", "typed", draft_of(CHAT)),
        ("eval", "lock", "document.dispatchEvent(new Event('studio-locked')); true"),
        ("eval", "locked", draft_of(CHAT))])
    assert not r.get("failed"), r
    v = r["values"]
    assert [v["in_b"], v["in_a"]] == ["", "Для А"] and v["new"] == "Для нового"
    assert v["reloaded"] == "Для А" and v["sent"] == [None, ""]
    assert v["typed"] == "Ещё" and v["locked"] is None
    assert not problems(r), problems(r)


NAV = "document.querySelector('.chat-history .code-message-nav')"
#: свои сообщения и ответы — левее стрелок: последнее сообщение не лежит под ними
CLEAR = f"""(() => {{ const nav = {NAV}.getBoundingClientRect();
  return [...document.querySelectorAll('#messages .msg')].every(m => m.getBoundingClientRect().right <= nav.left + 0.5); }})()"""
AT_TOP = """(() => { const top = document.querySelector('#messages').getBoundingClientRect().top;
  const u = [...document.querySelectorAll('#messages .msg.user')].find(m => Math.abs(m.getBoundingClientRect().top - top) < 3);
  return u ? u.querySelector('.bubble').textContent : null; })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_arrows_expand_all_and_fork_button(browser, live, width):
    login = f"arrows-{width}"
    live.add_user(login)
    mobile = width == 320
    long = "".join(f"Строка ответа {i}.\n\n" for i in range(30))
    live.gw.chat_script = [[f"Ответ {n}.\n\n{long}"] for n in (1, 2, 3)]
    turns = [step for n in (1, 2, 3) for step in (
        ("type", "#chat-input", f"Вопрос {n}"), ("press", "Enter"), ("wait", f"{USERS}.length === {n} && {ANSWERED}"))]
    try:
        r = browser.drive(live.url + "/", [*start(login, mobile), *turns,
            ("wait", f"{NAV} && !{NAV}.hidden"),
            ("eval", "ids", f"[...{NAV}.querySelectorAll('button')].map(b => b.id)"),
            ("eval", "clear", CLEAR), ("eval", "fits", NO_OVERFLOW),
            ("click", "#chat-message-up"), ("wait", f"{AT_TOP} === 'Вопрос 3'"),
            ("eval", "up", AT_TOP),
            ("eval", "old", f"window.oldChat = {CHAT}"),
            ("eval", "fork", "document.querySelectorAll('#messages .msg.user')[1].querySelector('.msg-fork').click(); true"),
            ("wait", f"{CHAT} && {CHAT} !== window.oldChat && {USERS}.length === 1 && {FIELD} === 'Вопрос 2'"),
            ("eval", "forked", f"[{USERS}, {FIELD}, {ERROR}]"),
            # текст ветки (openWith) — и в черновике: возврат из bfcache его не стирает
            ("eval", "bfcache", "window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));"
                                " window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));"
                                f" {FIELD}")],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["ids"] == ["chat-message-up", "chat-toggle-steps", "chat-message-down"]
    assert v["up"] == "Вопрос 3" and v["clear"] is True and v["fits"] is True
    assert v["forked"] == [["Вопрос 1"], "Вопрос 2", ""] and v["bfcache"] == "Вопрос 2"
    assert not problems(r), problems(r)


def file_js(name: str, mime: str) -> str:
    if name.endswith(".png"):
        return f"new File([Uint8Array.from(atob({PHOTO!r}), c => c.charCodeAt(0))], {name!r}, {{ type: {mime!r} }})"
    return f"new File(['line of {name}\\n'], {name!r}, {{ type: {mime!r} }})"


def transfer(*files) -> str:
    return ("(() => { const dt = new DataTransfer(); for (const f of [" + ", ".join(file_js(*f) for f in files)
            + "]) dt.items.add(f); return dt; })()")


def test_pasted_and_dropped_files_become_attachments(browser, live):
    live.add_user("paste")
    chips = "[...document.querySelectorAll('#composer .att-chip .att-name')].map(n => n.textContent)"
    r = browser.drive(live.url + "/", [*start("paste"),
        ("eval", "pasted", f"""(() => {{ const ev = new ClipboardEvent('paste', {{ clipboardData: {transfer(("p.png", "image/png"))},
          bubbles: true, cancelable: true }}); document.querySelector('#chat-input').dispatchEvent(ev); return ev.defaultPrevented; }})()"""),
        ("eval", "over", f"""(() => {{ const ev = new DragEvent('dragover', {{ dataTransfer: {transfer(("notes.txt", "text/plain"))},
          bubbles: true, cancelable: true }}); document.querySelector('#composer').dispatchEvent(ev); return ev.defaultPrevented; }})()"""),
        ("eval", "dropped", f"""(() => {{ const ev = new DragEvent('drop', {{ dataTransfer: {transfer(("notes.txt", "text/plain"))},
          bubbles: true, cancelable: true }}); document.querySelector('#composer').dispatchEvent(ev); return ev.defaultPrevented; }})()"""),
        ("wait", f"{chips}.length === 2"),
        ("eval", "chips", chips),
        ("eval", "bad", f"""(() => {{ const ev = new DragEvent('drop', {{ dataTransfer: {transfer(("tool.exe", "application/x-msdownload"))},
          bubbles: true, cancelable: true }}); document.querySelector('#composer').dispatchEvent(ev); return true; }})()"""),
        ("wait", "document.querySelector('#composer .att-chip.bad')"),
        ("eval", "unfit", "document.querySelector('#composer .att-chip.bad .att-state').textContent"),
        ("eval", "text_paste", """(() => { const dt = new DataTransfer(); dt.setData('text/plain', 'просто текст');
          const field = document.querySelector('#chat-input'); field.focus();
          const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
          field.dispatchEvent(ev);
          if (!ev.defaultPrevented) document.execCommand('insertText', false, dt.getData('text/plain'));
          return [ev.defaultPrevented, field.value]; })()"""),
        ("eval", "count", "document.querySelectorAll('#composer .att-chip').length"),
        # отправка — как со скрепкой; ветка от сообщения с вложениями: их нет в ветке — слова в #chat-error
        ("click", "#composer .att-chip.bad .att-remove"), ("wait", "!document.querySelector('#composer .att-chip.bad')"),
        ("click", "#send"), ("wait", ANSWERED),
        ("eval", "sent", "[...document.querySelectorAll('#messages .msg.user .att')].map(a => a.textContent.includes('notes.txt')"
                         " || !!a.querySelector('img'))"),
        ("eval", "old", f"window.oldChat = {CHAT}"),
        ("click", "#messages .msg.user .msg-fork"),
        ("wait", f"{CHAT} !== window.oldChat && {FIELD} === 'просто текст' && {ERROR}"),
        ("eval", "forked", f"[{ERROR}, document.querySelectorAll('#messages .msg').length]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pasted"] is True and v["over"] is True and v["dropped"] is True
    assert v["chips"] == ["p.png", "notes.txt"]
    # слова — как у скрепки (test_ui_browser: «02:F11»), с заглавной буквы чипа
    assert v["unfit"] == "Поддерживаются PDF, DOCX, текстовые файлы и код, фото PNG, JPEG, WebP"
    assert v["text_paste"] == [False, "просто текст"] and v["count"] == 3
    assert v["sent"] == [True, True]
    assert v["forked"] == ["Вложения сообщения в новый диалог не перенесены", 0]
    sent = live.gw.chat_requests[-1]["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" for p in sent) and any("line of notes.txt" in p.get("text", "") for p in sent)
    assert not problems(r), problems(r)
