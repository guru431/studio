/* Ряд вкладок сессий открытого проекта Claude Code (спека 2026-10-29-studio-code-workspace-design, 6.4–6.5).
 *
 * tabBar(host, { pick, close, add, move }) → { show({ tabs, active, info, loaded, choosing, drafts, offline }), unmount() }:
 * - div#code-tabbar[role=tablist] — вкладки в порядке открытия: div.code-tab[data-chat][role=tab][aria-selected] —
 *   название сессии (span.code-tab-title: до списка пусто, нет в info — «Сессия», пустое — «Новый чат»; полное — подсказкой), метка
 *   «идёт ход» или «ждёт ответа» (span.code-tab-mark.running|.asking) и «×» (button.code-tab-close → close(chat):
 *   вкладка уходит, сессия на машине остаётся); в конце ряда — «+» (#code-tab-add);
 * - «+» — add(): экран новой вкладки в основной области проекта (code.js), без всплывающего меню;
 * - нажатие на вкладку — pick(chat); active — показанная (null — ни одна: «Файлы», «Git»); она прокручивается в
 *   видимую часть ряда (телефон листает ряд вбок).
 * - признаки (D2): ✎ (span.code-tab-draft) — у чата в памяти страницы есть неотправленный текст или вложения
 *   (show({ drafts: Set })); точка span.code-tab-offline — компьютер не на связи (show({ offline: true }));
 * - порядок: перетаскивание мышью (после сдвига на 6 px) и пальцем (после удержания LONG_PRESS_MS, иначе — прокрутка
 *   ряда), с клавиатуры — Shift+←/→ у вкладки в фокусе; итог — move(chat, index) (code.js → act('move')).
 * Текст машины — только textContent. */
import { ENGINE_NAMES } from './code-options.js';
import { icon } from './icons.js';
import { el } from './ui.js';

const UNTITLED = 'Новый чат';
const SESSION = 'Сессия';
const LONG_PRESS_MS = 400;
const DRAG_MOUSE_PX = 6;
const DRAG_TOUCH_SLOP_PX = 8;

export function tabBar(host, { pick, close, add: choose, move = () => {} }) {
  const bar = el('div', { id: 'code-tabbar', class: 'code-tabbar', role: 'tablist', 'aria-label': 'Сессии проекта' });
  const add = el('button', { type: 'button', id: 'code-tab-add', class: 'code-tab-add', 'aria-label': 'Сессии проекта',
    title: 'Сессии проекта', 'aria-expanded': 'false', 'aria-controls': 'code-tab-menu' }, '+');
  bar.append(add);
  const root = el('div', { class: 'code-tabs-row' }, bar);
  host.append(root);
  let state = { tabs: [], active: null, info: new Map(), loaded: false, drafts: new Set(), offline: false };
  const nodes = new Map();
  const signatures = new Map();

  let pendingFocus = null;
  function closeFocused(chat) {
    const index = state.tabs.indexOf(chat);
    pendingFocus = state.tabs[index + 1] || state.tabs[index - 1] || null;
    close(chat);
  }
  function tabNode(chat) {
    const known = state.info.get(chat);
    const title = known ? known.title || UNTITLED : state.loaded ? SESSION : '';
    const engine = known ? el('span', { class: 'tag code-tab-engine' }, ENGINE_NAMES[known.engine || 'claude']) : null;
    let mark = null;
    if (known && known.asking) mark = el('span', { class: 'code-tab-mark asking', title: 'ждёт ответа' }, el('span', { class: 'sr' }, 'ждёт ответа'));
    else if (known && known.running) mark = el('span', { class: 'code-tab-mark running', title: 'идёт ход' }, el('span', { class: 'sr' }, 'идёт ход'));
    const shut = el('button', { type: 'button', tabindex: '-1', class: 'code-tab-close', 'aria-label': 'Закрыть вкладку', title: 'Закрыть вкладку' }, icon('x'));
    const draft = state.drafts?.has(chat)
      ? el('span', { class: 'code-tab-draft', title: 'Есть неотправленный черновик' }, el('span', { 'aria-hidden': 'true' }, '✎'),
        el('span', { class: 'sr' }, 'есть черновик')) : null;
    const offline = state.offline
      ? el('span', { class: 'code-tab-offline', title: 'Компьютер не на связи' }, el('span', { class: 'sr' }, 'не на связи')) : null;
    const selected = chat === state.active;
    const stop = state.active || state.tabs[0];
    const node = el('div', { class: 'code-tab', role: 'tab', tabindex: chat === stop ? '0' : '-1', 'data-chat': chat,
      'aria-selected': String(selected), 'aria-keyshortcuts': 'Shift+ArrowLeft Shift+ArrowRight' },
      offline, engine, el('span', { class: 'code-tab-title', title }, title), draft, mark, shut);
    node.addEventListener('click', (event) => {
      if (!event.target.closest('.code-tab-close')) pick(chat);
    });
    node.addEventListener('keydown', (event) => {
      if (event.target !== node) return;
      const i = state.tabs.indexOf(chat);
      if (event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey
        && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        const to = i + (event.key === 'ArrowLeft' ? -1 : 1);
        if (to >= 0 && to < state.tabs.length) move(chat, to);
        return;
      }
      const keys = { ArrowLeft: (i + state.tabs.length - 1) % state.tabs.length,
        ArrowRight: (i + 1) % state.tabs.length, Home: 0, End: state.tabs.length - 1 };
      if (Object.hasOwn(keys, event.key)) {
        event.preventDefault();
        bar.querySelectorAll('.code-tab')[keys[event.key]].focus();
      } else if (event.key === 'Delete') {
        event.preventDefault();
        closeFocused(chat);
      }
      if ((event.key === 'Enter' || event.key === ' ') && event.target === node) {
        event.preventDefault();
        pendingFocus = chat;
        pick(chat, true);
      }
    });
    shut.addEventListener('click', () => closeFocused(chat));
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

  /* ---- перетаскивание: узел едет по ряду сразу, порядок уходит в рабочее место при отпускании ---- */
  let drag = null;          // { chat, node, id, x, y, touch, timer, active }
  let swallowClick = false; // отпускание после перетаскивания — не выбор вкладки
  let deferred = null;      // show() во время перетаскивания
  function begin() {
    if (!drag) return;
    clearTimeout(drag.timer);
    drag.active = true;
    drag.node.classList.add('dragging');
    bar.classList.add('reordering');
    try { drag.node.setPointerCapture(drag.id); } catch { /* указатель уже отпущен */ }
  }
  function finish(commit) {
    const current = drag;
    drag = null;
    if (!current) return;
    clearTimeout(current.timer);
    bar.classList.remove('reordering');
    current.node.classList.remove('dragging');
    if (!current.active) return;
    swallowClick = true;
    setTimeout(() => { swallowClick = false; }, 0);
    const order = [...bar.querySelectorAll('.code-tab')].map((n) => n.dataset.chat);
    const index = order.indexOf(current.chat);
    const later = deferred;               // смена рабочего места во время перетаскивания — после него
    deferred = null;
    bar.replaceChildren(...state.tabs.map((id) => nodes.get(id)).filter(Boolean), add);
    if (later) show(later);
    if (commit && index !== state.tabs.indexOf(current.chat)) move(current.chat, index);
  }
  bar.addEventListener('pointerdown', (event) => {
    const node = event.target.closest('.code-tab');
    if (!node || event.target.closest('.code-tab-close') || event.button !== 0 || state.tabs.length < 2) return;
    finish(false);
    drag = { chat: node.dataset.chat, node, id: event.pointerId, x: event.clientX, y: event.clientY,
      touch: event.pointerType !== 'mouse', active: false, timer: null };
    if (drag.touch) drag.timer = setTimeout(begin, LONG_PRESS_MS);
  });
  bar.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const far = Math.hypot(event.clientX - drag.x, event.clientY - drag.y);
    if (!drag.active) {
      if (!drag.touch && far >= DRAG_MOUSE_PX) begin();
      else if (drag.touch && far > DRAG_TOUCH_SLOP_PX) finish(false);   // палец повёл раньше удержания — прокрутка ряда
      if (!drag?.active) return;
    }
    const box = bar.getBoundingClientRect();
    if (event.clientX < box.left + 24) bar.scrollLeft -= 8;
    else if (event.clientX > box.right - add.offsetWidth - 24) bar.scrollLeft += 8;
    const others = [...bar.querySelectorAll('.code-tab')].filter((n) => n !== drag.node);
    const before = others.find((n) => {
      const r = n.getBoundingClientRect();
      return event.clientX < r.left + r.width / 2;
    }) || add;
    if (drag.node.nextElementSibling !== before) bar.insertBefore(drag.node, before);
  });
  bar.addEventListener('pointerup', (event) => { if (drag && event.pointerId === drag.id) finish(true); });
  bar.addEventListener('pointercancel', (event) => { if (drag && event.pointerId === drag.id) finish(false); });
  // Пока палец тащит вкладку, ряд не прокручивается и страница не листается (слушатель не пассивный).
  bar.addEventListener('touchmove', (event) => { if (drag?.active) event.preventDefault(); }, { passive: false });
  bar.addEventListener('contextmenu', (event) => { if (drag?.touch) event.preventDefault(); });
  bar.addEventListener('click', (event) => {
    if (!swallowClick) return;
    swallowClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);

  return {
    restoreFocus(chat) {
      const active = document.activeElement;
      if (pendingFocus === chat && (!active || active === document.body || active === document.documentElement)) {
        nodes.get(chat)?.focus({ preventScroll: true });
      }
      pendingFocus = null;
    },
    show,
    unmount() {
      root.remove();
    },
  };

  function show(next) {
    if (drag?.active) { deferred = next; return; }
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
      const signature = JSON.stringify([next.loaded, !!info, info?.title, info?.engine, !!info?.asking, !!info?.running,
        !!next.drafts?.has(id), !!next.offline]);
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
  }
}
