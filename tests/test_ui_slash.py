"""Меню «/» и чип навыка поля ввода (slash.js) в настоящем браузере: отбор, выбор, клавиши, 320 px по-английски."""
import json

import pytest

from test_ui_browser import NO_OVERFLOW, READY, browser, live, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

ITEMS = [{"kind": "skill", "id": "s1", "name": "Разобрать по частям", "description": "План и части"},
         {"kind": "skill", "id": "s2", "name": "Н" * 80, "description": "о" * 200},          # пределы навыка
         {"kind": "project", "id": ".agents/skills/spec", "name": "spec", "description": "Подготовить ТЗ"},
         {"kind": "command", "id": "init", "name": "init", "description": ""}]

SCRIPT = """(async () => {
  const frames = (n = 3) => new Promise(r => { const step = () => (n-- > 0 ? requestAnimationFrame(step) : r()); step(); });
  const { slashMenu, skillChip } = await import('/static/js/slash.js');
  const { fieldBox, enterSends } = await import('/static/js/field.js');
  const { el } = await import('/static/js/ui.js');
  const ITEMS = %s;
  window.sent = 0;
  window.picked = null;
  const field = el('textarea', { id: 'probe-text', rows: '1' });
  const { box } = fieldBox(field, { mic: false });
  const form = el('form', { class: 'composer' }, box);
  document.querySelector('#view').prepend(form);
  enterSends(field, () => { window.sent += 1; });
  let changed = 0;
  const chip = skillChip(form, { changed: () => { changed += 1; } });
  const view = slashMenu(field, { items: async () => ITEMS, onPick: (item) => { window.picked = item; chip.set(item); } });
  const menu = document.querySelector('#slash-menu');
  const rows = () => [...menu.querySelectorAll('.slash-option')];
  const type = async (value) => {
    field.focus();
    field.value = value;
    field.setSelectionRange(value.length, value.length);
    field.dispatchEvent(new Event('input', { bubbles: true }));
    await frames();
  };
  const key = async (name) => {
    field.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true }));
    await frames();
  };
  const out = {};
  await type('привет'); out.plain = menu.hidden;
  await type('/'); out.open = [rows().length, field.getAttribute('aria-expanded')];
  out.kinds = rows().map(row => row.querySelector('.slash-kind').textContent);
  const inside = (r) => r.left >= 0 && r.right <= innerWidth;
  const long = rows()[1].querySelector('.slash-name');
  out.fits = !menu.hidden && inside(menu.getBoundingClientRect()) && rows().every(row => inside(row.getBoundingClientRect()))
    && (%s) && long.scrollWidth > long.clientWidth && getComputedStyle(long).textOverflow === 'ellipsis'
    && long.title === ITEMS[1].name;
  await type('/sp'); out.filtered = rows().map(row => row.querySelector('.slash-name').textContent);
  await type('текст /sp'); const middle = menu.hidden;
  await type('/sp текст'); out.middle = middle && menu.hidden;
  await type('/раз'); await key('Enter');
  out.picked = window.picked ? [window.picked.kind, window.picked.id] : null;
  out.after = field.value; out.sent = window.sent;
  out.chip = [chip.node.querySelector('.skill-chip-name').textContent, chip.value()?.id];
  chip.node.querySelector('button').click();
  out.cleared = [chip.node.hidden, chip.value()];
  out.changed = changed;
  await type('/ра'); await key('Escape');
  const escaped = [menu.hidden, field.value];
  await type('/раз'); out.escaped = [...escaped, menu.hidden];
  await type(''); await type('/'); out.reopened = !menu.hidden && rows().length === 4;
  view.unmount();
  out.removed = !document.querySelector('#slash-menu');
  // items() с задержкой: Enter до ответа — не отправка; после ответа — первая строка, а без строк — ничего
  let release;
  window.picked = null;
  const late = slashMenu(field, { items: () => new Promise(r => { release = () => r(ITEMS); }),
    onPick: (item) => { window.picked = item; } });
  const lateMenu = document.querySelector('#slash-menu');
  await type(''); await type('/раз'); await key('Enter');
  const waiting = [window.sent, window.picked, field.value, lateMenu.hidden];
  release(); await frames();
  out.late = [...waiting, window.picked ? [window.picked.kind, window.picked.id] : null, field.value, window.sent];
  window.picked = null;
  await type(''); await type('/zzz'); await key('Enter');
  release(); await frames();
  out.none = [window.sent, window.picked, field.value, lateMenu.hidden,
    lateMenu.querySelector('[role=status]').textContent.length > 0];
  late.unmount(); form.remove();
  return out; })()"""


@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])
def test_slash_menu_filters_picks_and_fits(browser, live, width, lang):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "v", SCRIPT % (json.dumps(ITEMS, ensure_ascii=False), NO_OVERFLOW))],
        width=width, mobile=width == 320, init=f"localStorage.setItem('studio-language', '{lang}')")
    assert not r.get("failed"), r
    v = r["values"]["v"]
    assert v["plain"] is True and v["open"] == [4, "true"], v
    assert v["kinds"] == (["skill", "skill", "project", "command"] if lang == "en" else ["навык", "навык", "проект", "команда"]), v
    assert v["filtered"] == ["spec"] and v["middle"] is True, v
    assert v["fits"] is True, v
    assert v["picked"] == ["skill", "s1"] and v["after"] == "" and v["sent"] == 0, v
    assert v["chip"] == [("Skill: " if lang == "en" else "Навык: ") + "Разобрать по частям", "s1"], v
    assert v["cleared"] == [True, None] and v["changed"] >= 1, v
    assert v["escaped"] == [True, "/ра", True] and v["reopened"] is True, v
    assert v["removed"] is True, v
    # Enter, пока items() не ответил: [отправлено, выбрано, поле, меню скрыто], затем выбор после ответа
    assert v["late"] == [0, None, "/раз", False, ["skill", "s1"], "", 0], v
    # после ответа строк нет: ни отправки, ни выбора, текст цел, меню открыто со словами «Ничего не найдено»
    assert v["none"] == [0, None, "/zzz", False, True], v
