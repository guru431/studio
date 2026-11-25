"""«Цитировать» и меню сообщения (п. 38 спеки 2026-11-25-studio-skills-status-local-models): цитата — чипом над полем,
при отправке — строками «> …» в начале поля; меню правой кнопкой и долгим нажатием повторяет кнопки сообщения;
«Чат», «Агенты» и Open WebUI."""
import pytest

from test_ui_browser import (ANSWERED, CODE_CHAT_A, READY, add_gateway, browser, code_chat, code_free_machine,  # noqa: F401
                             code_online, code_requests, code_session, go, live, open_new_chat, problems, signin)
from fake_owui import FakeOwui  # noqa: F401
from test_ui_owui import connected, fake, fits, opened, owui_browser, session, talk, until  # noqa: F401
from test_ui_shared_modules import FRAMES, probe
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

MODULES = "(async () => {" + FRAMES + """
  const q = await import('/static/js/quote.js');
  const mm = await import('/static/js/message-menu.js');
  const { el } = await import('/static/js/ui.js');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = {};
  out.quoted = [q.quoted('а\\nб'), q.quoted('я'.repeat(5000)).replace(/^> /, '').replace(/…$/, '').length];
  const field = el('textarea');
  const form = el('form', {}, el('div', { class: 'field-box' }, field));
  document.body.append(form);
  let inputs = 0;
  field.addEventListener('input', () => inputs++);
  const chip = q.quoteChip(form, field, { id: 'probe-quote' });
  out.placed = chip.node.nextElementSibling === form.querySelector('.field-box');
  chip.set('первая строка');
  out.chip = [chip.node.hidden, chip.node.textContent.trim()];
  chip.set('цитата');
  field.value = 'вопрос';
  out.fold = [chip.fold(), field.value, chip.node.hidden, inputs];
  out.empty = chip.fold();

  const scroller = el('div', { style: 'height:300px; overflow:auto' });
  let clicked = 0, quotedText = null;
  const edit = el('button', { type: 'button', class: 'btn btn-sm' }, 'Изменить');
  edit.addEventListener('click', () => { clicked++; });
  const gone = el('button', { type: 'button', class: 'btn btn-sm', hidden: true }, 'Скрытая');
  const link = el('a', { href: '#nowhere' }, 'ссылка');
  const msg = el('div', { class: 'msg user' }, el('div', { class: 'bubble' }, 'Текст ', link),
    el('div', { class: 'msg-actions' }, edit, gone, el('button', { type: 'button', class: 'btn btn-sm msg-copy' }, 'Копировать')));
  scroller.append(msg);
  document.body.append(scroller);
  const menu = mm.messageMenu(scroller, { message: '.msg', text: (n) => n.querySelector('.bubble').textContent,
    quote: (t) => { quotedText = t; } });
  const box = msg.querySelector('.bubble').getBoundingClientRect();
  const x = box.left + 5, y = box.top + 5;
  const context = (target) => { const e = new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: x, clientY: y });
    target.dispatchEvent(e); return e.defaultPrevented; };
  const pop = [...document.querySelectorAll('[id=msg-menu]')].at(-1);   // у раздела «Чат» — своё меню
  const items = () => [...pop.querySelectorAll('[role=menuitem]')];
  context(msg.querySelector('.bubble'));
  out.menu = items().map((b) => b.textContent);
  const r = pop.getBoundingClientRect();
  out.inside = pop.matches(':popover-open') && r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;
  items().find((b) => b.textContent === 'Изменить').click();
  out.clicked = clicked;
  out.closed = !pop.matches(':popover-open');
  context(msg);
  items().find((b) => b.textContent === 'Цитировать').click();
  out.quote = quotedText;
  out.link = context(link) || pop.matches(':popover-open');
  out.anchor = !!document.querySelector('.msg-menu-anchor');
  if (window.Touch && window.TouchEvent) {
    const touch = (type) => { const t = new Touch({ identifier: 1, target: msg, clientX: x, clientY: y });
      msg.dispatchEvent(new TouchEvent(type, { bubbles: true, cancelable: true, touches: type === 'touchend' ? [] : [t],
        changedTouches: [t] })); };
    touch('touchstart'); await sleep(700); touch('touchend');
    const long = pop.matches(':popover-open');
    if (long) pop.hidePopover();
    touch('touchstart'); await sleep(150); touch('touchend');
    out.long = [long, pop.matches(':popover-open')];
  } else out.long = null;
  menu.unmount();
  out.unmounted = !pop.isConnected;
  return out;
})()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_quote_chip_folds_into_the_field_and_the_menu_mirrors_actions(browser, live, width):
    v = probe(browser, live, MODULES, width=width)
    assert v["quoted"] == ["> а\n> б", 4000], v
    assert v["placed"] is True and v["chip"] == [False, "Цитата: первая строка"], v
    assert v["fold"] == [True, "> цитата\n\nвопрос", True, 1] and v["empty"] is False, v
    assert v["menu"] == ["Копировать", "Цитировать", "Изменить"] and v["clicked"] == 1 and v["closed"] is True, v
    assert v["quote"] == "Текст ссылка" and v["anchor"] is True and v["unmounted"] is True, v
    assert v["link"] is False and v["inside"] is True, v
    if width == 320:                                       # сенсор: 550 мс — меню, короткое касание — нет
        assert v["long"] == [True, False], v


ZOOMED = "(async () => {" + FRAMES + """
  const q = await import('/static/js/quote.js');
  const mm = await import('/static/js/message-menu.js');
  const { el } = await import('/static/js/ui.js');
  document.documentElement.style.zoom = '0.8';                  // «Масштаб интерфейса» 80 %
  const text = el('div', { class: 'md' }, 'выделенный текст ответа');
  const msg = el('div', { class: 'msg assistant', style: 'margin:200px 0 0 300px; width:400px' }, text);
  const scroller = el('div', {}, msg);
  document.body.append(scroller);
  const sel = q.selectionQuote(scroller, { texts: '.md', quote: () => {} });
  const menu = mm.messageMenu(scroller, { message: '.msg', text: (n) => n.textContent });
  const range = document.createRange(); range.selectNodeContents(text);
  getSelection().removeAllRanges(); getSelection().addRange(range);
  await frames();
  const end = [...range.getClientRects()].at(-1);
  const button = [...document.querySelectorAll('#quote-float')].at(-1).getBoundingClientRect();   // у «Чата» — своя
  const out = { float: [Math.round(button.left + button.width / 2 - end.right), Math.round(button.top - end.bottom)] };
  getSelection().removeAllRanges();
  const box = text.getBoundingClientRect();
  const x = Math.round(box.left + 20), y = Math.round(box.top + 5);
  text.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: x, clientY: y }));
  await frames();
  const anchor = [...document.querySelectorAll('.msg-menu-anchor')].at(-1).getBoundingClientRect();
  out.anchor = [Math.round(anchor.left - x), Math.round(anchor.top - y)];
  sel.unmount(); menu.unmount(); document.documentElement.style.zoom = '';
  return out;
})()"""


def test_quote_button_and_menu_follow_the_pointer_at_80_percent(browser, live):
    """Масштаб интерфейса — CSS zoom корня: координаты экрана делятся на него (menu.rootZoom), иначе при 80 % кнопка
    у выделения и меню уезжают на пятую часть расстояния от угла."""
    v = probe(browser, live, ZOOMED)
    assert abs(v["float"][0]) <= 2 and 0 <= v["float"][1] <= 12, v
    assert [abs(n) <= 1 for n in v["anchor"]] == [True, True], v


SELECT_ANSWER = """(() => { const md = document.querySelector('#messages .msg.assistant .md');
  const range = document.createRange(); range.selectNodeContents(md);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range); return sel.toString(); })()"""


def test_quote_reaches_the_request_in_chat(browser, live):
    login = "quote-chat"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("type", "#chat-input", "Привет"), ("press", "Enter"), ("wait", ANSWERED),
        ("eval", "selected", SELECT_ANSWER),
        ("wait", "!document.querySelector('#quote-float').hidden"), ("click", "#quote-float"),
        ("wait", "!document.querySelector('#chat-quote').hidden"),
        ("eval", "chip", "document.querySelector('#chat-quote').textContent.trim()"),
        ("type", "#chat-input", "Почему?"), ("press", "Enter"),
        ("wait", "document.querySelectorAll('#messages .msg.assistant').length === 2 && " + ANSWERED),
        ("eval", "after", "[document.querySelector('#chat-input').value, document.querySelector('#chat-quote').hidden]")],
        width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["chip"] == "Цитата: Здравствуйте!", v
    assert live.gw.chat_requests[-1]["messages"][-1]["content"] == "> Здравствуйте!\n\nПочему?"
    assert v["after"] == ["", True], v
    assert not problems(r), problems(r)


def test_quote_reaches_the_request_in_agents(browser, live):
    login = "quote-agents"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Цитаты", messages=[
        {"id": "m-0", "role": "user", "text": "Что в проекте?", "steps": [], "time": 0},
        {"id": "m-1", "role": "assistant", "text": "Три файла.", "steps": [], "time": 0}])})
    machine.script = [{"k": "text", "text": "Понял"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    menu_on = """(() => { const m = [...document.querySelectorAll('#code-feed .code-msg.assistant')].at(-1);
      const b = m.getBoundingClientRect(); m.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true,
        clientX: b.left + 10, clientY: b.top + 10 }));
      return [...document.querySelectorAll('#msg-menu [role=menuitem]')].map((i) => i.textContent); })()"""
    pick = """(() => { [...document.querySelectorAll('#msg-menu [role=menuitem]')].find((i) => i.textContent === 'Цитировать')
      .click(); return !document.querySelector('#code-quote').hidden; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "document.querySelector('#code-feed').textContent.includes('Три файла.')"),
            ("eval", "menu", menu_on), ("eval", "picked", pick),
            ("type", "#code-text", "Каких?"), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Понял')"),
            ("eval", "agents_field", "document.querySelector('#code-text').value")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["menu"][:2] == ["Копировать", "Цитировать"] and v["picked"] is True, v
    [turn] = code_requests(machine, "turn")
    assert turn["text"] == "> Три файла.\n\nКаких?" and v["agents_field"] == "", (turn, v)
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_quote_and_copy_in_open_webui(owui_browser, live, fake, width):
    """Open WebUI: меню `.owui-msg` → «Цитировать», отправка — текст с цитатой у эталона; «Копировать» ответа. На 320 px —
    по-английски (Review Focus 5)."""
    english = width == 320
    cookie = session(live, f"quote-owui-{owui_browser.browser_type.name}-{width}")
    fake.add_model("m1", "Model One")
    chat = fake.add_chat("Boats", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi there"}]}]}))
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        page.add_init_script(("localStorage.setItem('studio-language', 'en');" if english else "")
                             + "window.copied = []; navigator.clipboard.writeText = (t) => { window.copied.push(t);"
                             " return Promise.resolve(); };")
        page.reload()
        page.locator("html[data-ready='1']").wait_for(state="attached")
        answer = page.locator(".owui-msg.assistant").last
        pw.expect(answer.locator(".md")).to_contain_text("hi there")
        copy = answer.locator(".msg-copy")
        pw.expect(copy).to_have_text("Copy" if english else "Копировать")
        copy.click()
        answer.dispatch_event("contextmenu")
        items = page.locator("#msg-menu [role=menuitem]")
        pw.expect(items).to_have_text(["Copy", "Quote"] if english else ["Копировать", "Цитировать"])
        items.nth(1).click()
        chip = page.locator("#owui-quote")
        pw.expect(chip).to_have_text("Quote: hi there" if english else "Цитата: hi there")
        assert fits(page)
        page.locator("#owui-input").fill("Why?")
        page.locator("#owui-send").click()
        until(page, lambda: fake.completions)
        sent = fake.completions[-1]["user_message"]["content"]
        assert sent == "> hi there\n\nWhy?", sent
        pw.expect(chip).to_be_hidden()
        assert page.evaluate("window.copied") == ["hi there"]
        assert not page.problems, page.problems
