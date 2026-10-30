/* Ряд вкладок сессий открытого проекта Claude Code (спека 2026-10-29-studio-code-workspace-design, 6.4–6.5).
 *
 * tabBar(host, { aid, pid, pick, close, created, failed, read }) → { show({ tabs, active, info, loaded }), unmount() }:
 * - div#code-tabbar[role=tablist] — вкладки в порядке открытия: div.code-tab[data-chat][role=tab][aria-selected] —
 *   название сессии (span.code-tab-title: до списка пусто, нет в info — «Сессия», пустое — «Новый чат»; полное — подсказкой), метка
 *   «идёт ход» или «ждёт ответа» (span.code-tab-mark.running|.asking) и «×» (button.code-tab-close → close(chat):
 *   вкладка уходит, сессия на машине остаётся); в конце ряда — «+» (#code-tab-add);
 * - «+» — меню #code-tab-menu: «Новый чат» и сессии проекта без открытых вкладок (code-sessions.js; items — последний
 *   ответ GET …/chats у code.js; read() перечитывает его при открытии, открытое меню show() не пересобирает); выбор — pick(chat),
 *   «Новый чат» — created(chat), отказ — failed(err); меню закрывается выбором, нажатием вне и Escape;
 * - нажатие на вкладку — pick(chat); active — показанная (null — ни одна: «Файлы», «Git»); она прокручивается в
 *   видимую часть ряда (телефон листает ряд вбок).
 * Текст машины — только textContent. */
import { sessionList } from './code-sessions.js';
import { el } from './ui.js';

const UNTITLED = 'Новый чат';
const SESSION = 'Сессия';

export function tabBar(host, { aid, pid, pick, close, created, failed, read, codex = () => false }) {
  const bar = el('div', { id: 'code-tabbar', class: 'code-tabbar', role: 'tablist', 'aria-label': 'Сессии проекта' });
  const add = el('button', { type: 'button', id: 'code-tab-add', class: 'code-tab-add', 'aria-label': 'Сессии проекта',
    title: 'Сессии проекта', 'aria-haspopup': 'true', 'aria-expanded': 'false', 'aria-controls': 'code-tab-menu' }, '+');
  const menu = el('div', { id: 'code-tab-menu', class: 'code-tab-menu', hidden: true });
  const root = el('div', { class: 'code-tabs-row' }, bar, menu);
  host.append(root);
  let state = { tabs: [], active: null, info: new Map(), loaded: false };
  let list = null;           // сессии в меню

  function openMenu(open) {
    if (list) list.unmount();
    list = null;
    menu.hidden = !open;
    add.setAttribute('aria-expanded', String(open));
    if (!open) return;
    const done = (then) => (...args) => {
      openMenu(false);
      then(...args);
    };
    list = sessionList(menu, { aid, pid, items: read(), exclude: new Set(state.tabs), picked: done(pick),
      created: done(created), failed, codex: codex() });
  }

  function tabNode(chat) {
    const known = state.info.get(chat);
    const title = known ? `${known.engine === 'codex' ? 'Codex · ' : ''}${known.title || UNTITLED}` : state.loaded ? SESSION : '';
    let mark = null;
    if (known && known.asking) mark = el('span', { class: 'code-tab-mark asking', title: 'ждёт ответа' }, el('span', { class: 'sr' }, 'ждёт ответа'));
    else if (known && known.running) mark = el('span', { class: 'code-tab-mark running', title: 'идёт ход' }, el('span', { class: 'sr' }, 'идёт ход'));
    const shut = el('button', { type: 'button', tabindex: '-1', class: 'code-tab-close', 'aria-label': 'Закрыть вкладку', title: 'Закрыть вкладку' }, '×');
    const selected = chat === state.active;
    const stop = state.active || state.tabs[0];
    const node = el('div', { class: 'code-tab', role: 'tab', tabindex: chat === stop ? '0' : '-1', 'data-chat': chat,
      'aria-selected': String(selected) }, el('span', { class: 'code-tab-title', title }, title), mark, shut);
    node.addEventListener('click', (event) => {
      if (!event.target.closest('.code-tab-close')) pick(chat);
    });
    node.addEventListener('keydown', (event) => {
      if (event.target !== node) return;
      const i = state.tabs.indexOf(chat);
      const keys = { ArrowLeft: (i + state.tabs.length - 1) % state.tabs.length,
        ArrowRight: (i + 1) % state.tabs.length, Home: 0, End: state.tabs.length - 1 };
      if (Object.hasOwn(keys, event.key)) {
        event.preventDefault();
        bar.querySelectorAll('.code-tab')[keys[event.key]].focus();
      } else if (event.key === 'Delete') {
        event.preventDefault();
        close(chat);
      }
      if ((event.key === 'Enter' || event.key === ' ') && event.target === node) {
        event.preventDefault();
        pick(chat);
      }
    });
    shut.addEventListener('click', () => close(chat));
    return node;
  }

  /** Активная — в видимой части ряда («+» прилипает справа и её не закрывает). */
  function reveal() {
    const node = bar.querySelector('.code-tab[aria-selected="true"]');
    if (!node) return;
    const room = bar.clientWidth - add.offsetWidth;
    if (node.offsetLeft < bar.scrollLeft) bar.scrollLeft = node.offsetLeft;
    else if (node.offsetLeft + node.offsetWidth > bar.scrollLeft + room) bar.scrollLeft = node.offsetLeft + node.offsetWidth - room;
  }

  function onDoc(event) {
    if (!menu.hidden && !menu.contains(event.target) && !add.contains(event.target)) openMenu(false);
  }

  function onKey(event) {
    if (event.key === 'Escape' && !menu.hidden) {
      openMenu(false);
      add.focus();
    }
  }

  add.addEventListener('click', () => openMenu(menu.hidden));
  document.addEventListener('click', onDoc);
  document.addEventListener('keydown', onKey);

  return {
    show(next) {
      const focused = document.activeElement?.closest('.code-tab');
      const chat = focused && bar.contains(focused) ? focused.dataset.chat : null;
      const index = state.tabs.indexOf(chat);
      const adding = document.activeElement === add;
      state = next;
      // открытое меню не пересобирается (опрос меток, ответ рабочего места): пункт под рукой и идущий выбор —
      // те же; новый список — при следующем открытии
      bar.replaceChildren(...next.tabs.map(tabNode), add);
      if (chat) {
        const target = next.tabs.includes(chat) ? next.tabs.indexOf(chat) : Math.min(index, next.tabs.length - 1);
        (bar.querySelectorAll('.code-tab')[target] || add).focus({ preventScroll: true });
      } else if (adding) add.focus({ preventScroll: true });
      reveal();
    },
    unmount() {
      if (list) list.unmount();
      document.removeEventListener('click', onDoc);
      document.removeEventListener('keydown', onKey);
      root.remove();
    },
  };
}
