/* Ряд вкладок сессий открытого проекта Claude Code (спека 2026-10-29-studio-code-workspace-design, 6.4–6.5).
 *
 * tabBar(host, { pick, close, add }) → { show({ tabs, active, info, loaded, choosing }), unmount() }:
 * - div#code-tabbar[role=tablist] — вкладки в порядке открытия: div.code-tab[data-chat][role=tab][aria-selected] —
 *   название сессии (span.code-tab-title: до списка пусто, нет в info — «Сессия», пустое — «Новый чат»; полное — подсказкой), метка
 *   «идёт ход» или «ждёт ответа» (span.code-tab-mark.running|.asking) и «×» (button.code-tab-close → close(chat):
 *   вкладка уходит, сессия на машине остаётся); в конце ряда — «+» (#code-tab-add);
 * - «+» — add(): экран новой вкладки в основной области проекта (code.js), без всплывающего меню;
 * - нажатие на вкладку — pick(chat); active — показанная (null — ни одна: «Файлы», «Git»); она прокручивается в
 *   видимую часть ряда (телефон листает ряд вбок).
 * Текст машины — только textContent. */
import { ENGINE_NAMES } from './code-options.js';
import { el } from './ui.js';

const UNTITLED = 'Новый чат';
const SESSION = 'Сессия';

export function tabBar(host, { pick, close, add: choose }) {
  const bar = el('div', { id: 'code-tabbar', class: 'code-tabbar', role: 'tablist', 'aria-label': 'Сессии проекта' });
  const add = el('button', { type: 'button', id: 'code-tab-add', class: 'code-tab-add', 'aria-label': 'Сессии проекта',
    title: 'Сессии проекта', 'aria-expanded': 'false', 'aria-controls': 'code-tab-menu' }, '+');
  bar.append(add);
  const root = el('div', { class: 'code-tabs-row' }, bar);
  host.append(root);
  let state = { tabs: [], active: null, info: new Map(), loaded: false };
  const nodes = new Map();
  const signatures = new Map();

  function tabNode(chat) {
    const known = state.info.get(chat);
    const title = known ? known.title || UNTITLED : state.loaded ? SESSION : '';
    const engine = known ? el('span', { class: 'tag code-tab-engine' }, ENGINE_NAMES[known.engine || 'claude']) : null;
    let mark = null;
    if (known && known.asking) mark = el('span', { class: 'code-tab-mark asking', title: 'ждёт ответа' }, el('span', { class: 'sr' }, 'ждёт ответа'));
    else if (known && known.running) mark = el('span', { class: 'code-tab-mark running', title: 'идёт ход' }, el('span', { class: 'sr' }, 'идёт ход'));
    const shut = el('button', { type: 'button', tabindex: '-1', class: 'code-tab-close', 'aria-label': 'Закрыть вкладку', title: 'Закрыть вкладку' }, '×');
    const selected = chat === state.active;
    const stop = state.active || state.tabs[0];
    const node = el('div', { class: 'code-tab', role: 'tab', tabindex: chat === stop ? '0' : '-1', 'data-chat': chat,
      'aria-selected': String(selected) }, engine, el('span', { class: 'code-tab-title', title }, title), mark, shut);
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

  add.addEventListener('click', choose);

  return {
    show(next) {
      const focused = document.activeElement?.closest('.code-tab');
      const chat = focused && bar.contains(focused) ? focused.dataset.chat : null;
      const index = state.tabs.indexOf(chat);
      const adding = document.activeElement === add;
      state = next;
      add.setAttribute('aria-expanded', String(!!next.choosing));
      const orderChanged = [...bar.querySelectorAll('.code-tab')].map((n) => n.dataset.chat).join(',') !== next.tabs.join(',');
      for (const id of [...nodes.keys()]) {
        if (!next.tabs.includes(id)) { nodes.get(id).remove(); nodes.delete(id); signatures.delete(id); }
      }
      for (const id of next.tabs) {
        const info = next.info.get(id);
        const signature = JSON.stringify([next.loaded, !!info, info?.title, info?.engine, !!info?.asking, !!info?.running]);
        if (!nodes.has(id) || signatures.get(id) !== signature) {
          const node = tabNode(id);
          if (nodes.has(id)) nodes.get(id).replaceWith(node);
          nodes.set(id, node);
          signatures.set(id, signature);
        }
        const node = nodes.get(id);
        node.setAttribute('aria-selected', String(id === next.active));
        node.tabIndex = id === (next.active || next.tabs[0]) ? 0 : -1;
      }
      if (orderChanged) bar.replaceChildren(...next.tabs.map((id) => nodes.get(id)), add);
      if (chat) {
        const target = next.tabs.includes(chat) ? next.tabs.indexOf(chat) : Math.min(index, next.tabs.length - 1);
        (bar.querySelectorAll('.code-tab')[target] || add).focus({ preventScroll: true });
      } else if (adding) add.focus({ preventScroll: true });
      reveal();
    },
    unmount() {
      root.remove();
    },
  };
}
