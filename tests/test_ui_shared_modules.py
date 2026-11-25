"""Общие модули страницы «Агентов» и «Чата» в настоящем браузере: строка состояния, стрелки ленты, черновики диалогов."""
import pytest

from test_ui_browser import READY, browser, live, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: ждать n кадров отрисовки (ResizeObserver и rAF модулей)
FRAMES = "const frames = (n = 3) => new Promise(r => { const step = () => (n-- > 0 ? requestAnimationFrame(step) : r()); step(); });"


def probe(browser, live, script, width=1280):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("eval", "v", script)],
                      width=width, mobile=width == 320)
    assert not r.get("failed"), r
    return r["values"]["v"]


def test_status_bar_takes_ids_from_options_and_overflows(browser, live):
    v = probe(browser, live, "(async () => {" + FRAMES + """
      const { statusActions } = await import('/static/js/status-bar.js');
      const { el } = await import('/static/js/ui.js');
      const { icon } = await import('/static/js/icons.js');
      const host = el('div', { style: 'width:300px' });
      const bar = el('div', { class: 'code-status-bar' });
      const activity = el('p', { class: 'code-activity' }, 'Готов');
      const actions = el('div', { class: 'code-chat-actions' });
      for (let i = 1; i <= 6; i++) {
        const button = el('button', { type: 'button', class: 'icon-btn', 'aria-label': `Действие ${i}`, title: `Действие ${i}` });
        button.append(icon('more'));
        actions.append(button);
      }
      const ring = el('button', { type: 'button', class: 'icon-btn', 'data-status': 'ring', 'aria-label': 'Окно' },
        el('span', {}, '42%'));
      const fixed = el('div', { class: 'code-status-fixed' }, ring);
      host.append(bar);
      document.querySelector('#view').prepend(host);
      const view = statusActions(host, bar, activity, actions, fixed, { id: 'probe-status' });
      await frames(4);
      const more = document.querySelector('#probe-status-more');
      const menu = document.querySelector('#probe-status-menu');
      const moved = [...menu.querySelectorAll('button')];
      const controls = bar.querySelector('.code-status-controls');
      const shown = [...controls.querySelectorAll('button')].map(n => n.getBoundingClientRect()).filter(r => r.width > 0);
      const span = Math.max(...shown.map(r => r.right)) - Math.min(...shown.map(r => r.left));
      const box = ring.getBoundingClientRect();
      const out = { more: !!more && !more.hidden && more.getBoundingClientRect().width > 0,
        agent_ids: document.querySelectorAll('#code-status-more, #code-status-menu').length,
        moved: moved.length,
        labels: moved.length > 0 && moved.every(b => { const l = b.querySelector('.code-status-action-label');
          return !!l && !l.hidden && l.textContent === b.getAttribute('aria-label'); }),
        square: box.width > 0 && Math.abs(box.width - box.height) < 0.5,
        inside: span <= bar.getBoundingClientRect().width * 0.6 + 1,
        old_module: (await fetch('/static/js/code-status-bar.js')).status };
      view.unmount(); host.remove();
      return out; })()""")
    assert v["more"] is True and v["agent_ids"] == 0, v
    assert v["moved"] >= 1 and v["labels"] is True, v
    assert v["square"] is True and v["inside"] is True, v
    assert v["old_module"] == 404, v


@pytest.mark.parametrize("width", [320, 1280])
def test_message_nav_walks_own_messages_and_toggles_steps(browser, live, width):
    v = probe(browser, live, "(async () => {" + FRAMES + """
      const { messageNav } = await import('/static/js/message-nav.js');
      const { el } = await import('/static/js/ui.js');
      const scroller = el('div', { style: 'height:300px; overflow:auto' });
      const feed = el('div');
      for (let n = 1; n <= 6; n++) feed.append(el('div', { class: 'msg user' }, `Вопрос ${n}`),
        el('div', { class: 'msg assistant', style: 'height:400px' },
          el('details', { class: 'web-steps' }, el('summary', {}, 'Шаги'), el('p', {}, 'шаг'))));
      scroller.append(feed);
      const calls = { act: 0, follow: [] };
      const stuck = () => scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 80;
      const nav = messageNav({ scroller, feed, prefix: 'chat', own: '.msg.user', steps: 'details.web-steps', stuck,
        act: () => { calls.act += 1; }, follow: (on) => calls.follow.push(on) });
      const history = el('div', { style: 'position:relative' }, scroller, nav.node);
      document.querySelector('#view').prepend(history);
      scroller.scrollTop = scroller.scrollHeight;
      nav.paint();
      await frames();
      const atTop = () => { const top = scroller.getBoundingClientRect().top;
        return [...feed.querySelectorAll('.msg.user')].find(n => Math.abs(n.getBoundingClientRect().top - top) <= 3)?.textContent ?? null; };
      const press = async (id) => { document.getElementById(id).click(); await frames(); };
      const out = { ids: [...nav.node.querySelectorAll('button')].map(b => b.id) };
      await press('chat-message-up'); out.up = atTop();
      await press('chat-message-up'); out.up2 = atTop();
      await press('chat-message-down'); out.down = atTop();
      const down = document.getElementById('chat-message-down');
      out.tail = [down.getAttribute('aria-label'), !down.disabled];
      await press('chat-message-down');
      out.end = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
      const toggle = document.getElementById('chat-toggle-steps');
      const opened = () => feed.querySelectorAll('details.web-steps[open]').length;
      await press('chat-toggle-steps'); out.steps = [opened(), toggle.getAttribute('aria-pressed')];
      await press('chat-toggle-steps'); out.steps.push(opened(), toggle.getAttribute('aria-pressed'));
      out.calls = calls;
      out.inside = !nav.node.hidden && nav.node.getBoundingClientRect().right <= innerWidth;
      nav.unmount(); history.remove();
      return out; })()""", width=width)
    assert v["ids"] == ["chat-message-up", "chat-toggle-steps", "chat-message-down"], v
    assert [v["up"], v["up2"], v["down"]] == ["Вопрос 6", "Вопрос 5", "Вопрос 6"], v
    assert v["tail"] == ["В конец чата", True] and v["end"] is True, v
    assert v["steps"] == [6, "true", 0, "false"], v
    assert v["calls"] == {"act": 6, "follow": [False, False, False, True]} and v["inside"] is True, v


def test_dialog_drafts_are_scoped_announced_and_cleared(browser, live):
    # Свой экземпляр модуля (адрес с запросом): «Чат» страницы уже открыл своё хранилище черновиков и прочёл сессию.
    v = probe(browser, live, """(async () => {
      const m = await import('/static/js/dialog-drafts.js?probe');
      const { me } = await import('/static/js/api.js');
      const events = [];
      document.addEventListener('studio-dialog-draft', ({ detail }) => events.push([detail.store, detail.id, detail.draft]));
      sessionStorage.setItem(m.CHAT_DRAFTS, JSON.stringify({ scope: 'другая сессия', items: { x: { text: 'чужое' } } }));
      const store = m.draftStore(m.CHAT_DRAFTS);
      const foreign = { ...store.saved() };
      const dropped = sessionStorage.getItem(m.CHAT_DRAFTS);
      store.save('c1', 'Черновик');
      const stored = JSON.parse(sessionStorage.getItem(m.CHAT_DRAFTS));
      const has = store.has('c1');
      store.save('c1', '');
      const out = { csrf: me.csrf, stored, foreign, dropped, has, events: [...events],
        same: m.draftStore(m.CHAT_DRAFTS) === store && m.draftStore(m.AGENT_DRAFTS) !== store };
      store.save('c2', 'ещё'); m.draftStore(m.AGENT_DRAFTS).save('1/a/b', 'задание');
      document.dispatchEvent(new Event('studio-locked'));
      out.locked = [sessionStorage.getItem(m.CHAT_DRAFTS), sessionStorage.getItem(m.AGENT_DRAFTS)];
      return out; })()""")
    assert v["stored"] == {"scope": v["csrf"], "items": {"c1": {"text": "Черновик"}}}, v
    assert v["has"] is True and v["dropped"] is None, v
    assert v["events"] == [["studio-chat-drafts", "c1", True], ["studio-chat-drafts", "c1", False]], v
    assert v["same"] is True and v["foreign"] == {}, v
    assert v["locked"] == [None, None], v
