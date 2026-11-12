"""Опрос идущих заданий не пересоздаёт карточки и строки: фокус на «Отменить» карточки «Фото» и «Видео»
и на строке идущей записи «Расшифровки» и «Озвучки», на «Отменить» панели выбранной идущей записи «Расшифровки»,
«Озвучки» и «Сканов» переживает несколько опросов, нажатие доходит."""
import asyncio
import json
import time

import httpx
import pytest

from fakes import video_object
from test_ui_browser import (CLIP, LISTS, READY, add_gateway, browser, go, known_refusals, live,  # noqa: F401
                             photo_screen, png_of, problems, put_file, seed_list, signin, video_screen)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

#: пауза опроса раздела (ms) — 50 мс; window.polls — сколько GET по адресу pattern ушло
FAST_POLLS = """((ms, pattern) => { window.polls = 0; const wait = window.setTimeout.bind(window); const real = window.fetch;
  window.setTimeout = (f, t, ...a) => wait(f, t === ms ? 50 : t, ...a);
  window.fetch = (url, opts = {}) => {
    if ((opts.method || 'GET') === 'GET' && new RegExp(pattern).test(String(url))) window.polls += 1;
    return real(url, opts); };
  return true; })"""


def fast_polls(ms, pattern):
    return ("eval", "spy", f"{FAST_POLLS}({ms}, {json.dumps(pattern)})")


def hold(selector):
    """Фокус на узел selector; window.held — он, window.mark — опросов к этому мигу."""
    return ("eval", "focused", f"""(() => {{ const b = document.querySelector({json.dumps(selector)}); b.focus();
      window.held = b; window.mark = window.polls; return document.activeElement === b; }})()""")


AFTER_POLLS = "window.polls >= window.mark + 4"
KEPT = "[document.activeElement === window.held, window.held.isConnected]"


@pytest.mark.parametrize("width", [320, 1280])
def test_photo_cancel_keeps_focus_through_polls(browser, live, width):
    import images
    login = f"pfocus{width}"
    live.add_user(login)

    async def held(request):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(j.state == "cancelling" for j in images.JOBS.values()):
            await asyncio.sleep(0.01)
        return await live.gw.builtin(request)

    running = "#photo-feed .photo-card[data-state=working]"
    live.gw.on["/slot/images/generations"] = held
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), fast_polls(1500, r"^/api/images/[0-9a-f]+$"),
            ("type", "#photo-prompt", "долгий рассвет"), ("click", "#photo-go"),
            ("wait", f"document.querySelector('{running} .card-cancel')"),
            hold(f"{running} .card-cancel"), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
            ("click", f"{running} .card-cancel"),
            ("wait", "document.querySelector('#photo-feed .photo-card[data-state=cancelled]')")],
            width=width, mobile=width == 320, timeout=20000)
    finally:
        live.gw.on.pop("/slot/images/generations", None)
    assert not r.get("failed"), r
    assert r["values"]["focused"] is True and r["values"]["kept"] == [True, True]
    assert not problems(r), problems(r)


def test_video_cancel_keeps_focus_through_polls(browser, live):
    login = "vfocus"
    live.add_user(login)
    running_id = f"video_{len(live.gw.video_ids) + 1:024x}"
    progress = iter(range(5, 100))

    async def running(request):
        if request.method != "GET":
            return await live.gw.builtin(request)
        return httpx.Response(200, json=video_object(running_id, status="in_progress", progress=next(progress), seed=7))

    live.gw.on[f"/slot/videos/{running_id}"] = running
    busy = f"{CLIP}[data-state=in_progress]"
    try:
        r = browser.drive(live.url + "/", [*video_screen(login), fast_polls(3000, r"^/api/videos/[0-9a-f]+$"),
            put_file("#video-file", png_of(48, 64, "teal")),
            ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
            ("click", "#video-go"),
            ("wait", f"document.querySelector('{busy} .card-cancel')"),
            hold(f"{busy} .card-cancel"), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
            ("click", f"{busy} .card-cancel"),
            ("wait", f"document.querySelector('{CLIP}[data-state=cancelled]')")],
            width=1280, height=900, timeout=20000)
    finally:
        live.gw.on.pop(f"/slot/videos/{running_id}", None)
    assert not r.get("failed"), r
    assert r["values"]["focused"] is True and r["values"]["kept"] == [True, True]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("section", LISTS)
def test_running_row_keeps_focus_through_list_polls(browser, live, section):
    """Строка идущей записи и «Показать ещё» — те же узлы после опросов первой страницы; нажатие по
    строке выбирает её."""
    login = f"lfocus{section}"
    ids = seed_list(live, section, live.add_user(login), 3, running_newest=True)
    url = LISTS[section][1]
    row = f".tr-item[data-id='{ids[-1]}']"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"),
        fast_polls(3000, rf"^/api/{url}\?"), *go(section),
        ("wait", f"document.querySelector(\"{row}[data-state=working]\")"),
        hold(row), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
        ("eval", "rows", "[...document.querySelectorAll('.tr-list .tr-item')].map(e => e.dataset.id)"),
        ("click", row), ("wait", f"document.querySelector(\"{row}[aria-current=true]\")"),
        ("eval", "still", "window.held.isConnected")], width=1280, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["focused"] is True and v["kept"] == [True, True]
    assert v["rows"] == ids[::-1] and v["still"] is True
    assert not known_refusals(problems(r), 404), problems(r)


#: опрос раздела (3 с) — 50 мс; ответы GET списка и записи id несут detail «этап <номер опроса списка>»: у идущей
#: записи меняется detail, и каждый опрос перечитывает выбранную; window.picks — её GET, window.cancels — POST отмены
STAGES = """((url, id) => { window.polls = 0; window.picks = 0; window.cancels = 0;
  const wait = window.setTimeout.bind(window); const real = window.fetch;
  window.setTimeout = (f, t, ...a) => wait(f, t === 3000 ? 50 : t, ...a);
  window.fetch = async (path, opts = {}) => {
    const u = String(path), method = opts.method || 'GET';
    if (method === 'POST' && u === `/api/${url}/${id}/cancel`) window.cancels += 1;
    const list = u.startsWith(`/api/${url}?`), one = u === `/api/${url}/${id}`;
    if (method !== 'GET' || !(list || one)) return real(path, opts);
    if (list) window.polls += 1; else window.picks += 1;
    const res = await real(path, opts);
    const body = await res.json();
    for (const item of body.items || [body]) if (item.id === id) item.detail = `этап ${window.polls}`;
    return new Response(JSON.stringify(body), { status: res.status, headers: { 'content-type': 'application/json' } });
  };
  return true; })"""


def seed_scan(live, uid) -> str:
    """Идущий документ «Сканов» из двух страниц, первая распознана (без файлов: раннера у него нет)."""
    import db
    scan_id = f"{uid:08x}{0:024x}"
    conn = db.connect(live.db_path)
    try:
        conn.execute("INSERT INTO scans(id, user_id, model, name, origin, pages, state, detail, created)"
                     " VALUES (?, ?, 'ocr', 'Договор', 'section', 2, 'working', 'ждёт', ?)", (scan_id, uid, time.time()))
        conn.execute("INSERT INTO scan_pages(scan_id, user_id, n, state, text) VALUES (?, ?, 1, 'done', 'первая')",
                     (scan_id, uid))
        conn.execute("INSERT INTO scan_pages(scan_id, user_id, n, state) VALUES (?, ?, 2, 'waiting')", (scan_id, uid))
    finally:
        conn.close()
    return scan_id


PANELS = {"transcribe": ("transcripts", "#tr-cancel"), "speech": ("speeches", "#sp-cancel"), "ocr": ("ocr", "#ocr-cancel")}


@pytest.mark.parametrize("section", PANELS)
def test_panel_cancel_keeps_focus_through_detail_changes(browser, live, section):
    """Панель выбранной идущей записи: смена detail при опросе меняет только текст блока «идёт» — «Отменить»
    тот же узел, фокус на нём, нажатие доходит; страницы скана на месте."""
    login = f"pnfocus{section}"
    uid = live.add_user(login)
    record = seed_scan(live, uid) if section == "ocr" else seed_list(live, section, uid, 1, running_newest=True)[0]
    url, cancel = PANELS[section]
    row = f".tr-item[data-id='{record}']"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"),
        ("eval", "spy", f"{STAGES}({json.dumps(url)}, {json.dumps(record)})"), *go(section),
        ("wait", f"document.querySelector(\"{row}\")"), ("click", row),
        ("wait", f"document.querySelector('{cancel}')"),
        hold(cancel), ("eval", "picked", "window.picks"), ("wait", AFTER_POLLS), ("eval", "kept", KEPT),
        ("eval", "repainted", "window.picks"),
        ("eval", "detail", f"document.querySelector('{cancel}').parentElement.querySelector('p').textContent"),
        ("eval", "pages", "document.querySelector('#ocr-page')?.textContent ?? null"),
        ("click", cancel), ("wait", "window.cancels === 1")], width=1280, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["focused"] is True and v["kept"] == [True, True]
    assert v["repainted"] >= v["picked"] + 3, v               # панель перерисована несколько раз
    assert v["detail"].startswith("этап ") and v["detail"] != "этап 0", v
    assert v["pages"] == ("страница 1 из 2" if section == "ocr" else None), v
    assert not known_refusals(problems(r), 404), problems(r)


#: опрос диалога (2 с) — 50 мс; ответ GET диалога cid несёт у идущего сообщения mid текст «часть <номер опроса>»
GROWING = """((cid, mid) => { window.polls = 0; const wait = window.setTimeout.bind(window); const real = window.fetch;
  window.setTimeout = (f, t, ...a) => wait(f, t === 2000 ? 50 : t, ...a);
  window.fetch = async (path, opts = {}) => {
    if ((opts.method || 'GET') !== 'GET' || String(path) !== `/api/chats/${cid}`) return real(path, opts);
    window.polls += 1;
    const res = await real(path, opts);
    const body = await res.json();
    for (const m of body.messages) if (m.id === mid) m.content = `часть ${window.polls}`;
    return new Response(JSON.stringify(body), { status: res.status, headers: { 'content-type': 'application/json' } });
  };
  return true; })"""


def test_chat_poll_keeps_settled_messages(browser, live):
    """Ответ идёт не в этой вкладке — диалог перечитывается раз в 2 с: прежние сообщения — те же узлы (фокус на
    «Копировать» остаётся, нажатие доходит), меняется только идущее."""
    import uuid
    import db
    uid = live.add_user("cfocus")
    cid, now = uuid.uuid4().hex, time.time()
    ids = [uuid.uuid4().hex for _ in range(4)]
    conn = db.connect(live.db_path)
    try:
        conn.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, 'опрос', ?, ?)",
                     (cid, uid, now, now))
        for n, (role, content, status) in enumerate([("user", "вопрос", "done"), ("assistant", "первый ответ", "done"),
                                                     ("user", "ещё вопрос", "done"), ("assistant", "", "streaming")]):
            conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                         " VALUES (?, ?, ?, ?, ?, 'server', 'local-gemma-abl', ?)", (ids[n], cid, role, content, status, now + n))
    finally:
        conn.close()
    copy = f".msg.assistant[data-id='{ids[1]}'] .msg-copy"
    streaming = f"document.querySelector(\".msg[data-id='{ids[3]}'] .md\").textContent"
    r = browser.drive(live.url + "/", [*signin("cfocus"), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"),
        ("eval", "spy", f"{GROWING}({json.dumps(cid)}, {json.dumps(ids[3])})"), *go(f"chat/{cid}"),
        ("wait", f"document.querySelector(\"{copy}\")"),
        hold(copy), ("eval", "bubble", f"window.bubble = document.querySelector(\".msg[data-id='{ids[0]}']\"); true"),
        ("wait", AFTER_POLLS), ("eval", "kept", KEPT), ("eval", "user", "window.bubble.isConnected"),
        ("eval", "streaming", streaming),
        ("click", copy), ("wait", "window.held.textContent !== 'Копировать'")], width=1280, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["focused"] is True and v["kept"] == [True, True] and v["user"] is True, v
    assert v["streaming"].startswith("часть ") and v["streaming"] != "часть 1", v
    assert not problems(r), problems(r)
