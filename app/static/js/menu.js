/* Общий компонент меню студии (образец — code-picker.js и chat-share.js shareMenu): popover у кнопки, размещение
 * в видимой области (visualViewport, CSS zoom корня), стрелки ↑↓, Home/End, Escape и Tab — меню закрывается и
 * фокус возвращается на кнопку. Escape ловит само меню и дальше не пускает: закрывается только верхнее меню, а не
 * выдвижная панель или окно под ним (обработчики Escape разделов пропускают event.defaultPrevented). Нажатие вне
 * меню закрывает его средствами popover (light dismiss). Слушатели окна — только пока меню открыто: меню можно
 * заводить на каждую строку списка.
 *
 * Роли: у кнопки aria-haspopup="menu", aria-expanded, aria-controls; у меню role="menu"; пункты — menuitem или
 * menuitemradio с aria-checked (✓ рисует app.css). Форма в меню не кладётся — для неё popupPanel: тот же popover
 * без role="menu" (role="dialog", обычный Tab по полям). Пункт на сенсоре — не ниже 44 px (app.css). */
import { el } from './ui.js';

const GAP = 6;
const EDGE = 8;

/** Показать popover `menu` у `trigger`: side — 'below' | 'above' (куда предпочтительно), align — 'start' | 'end'
 * (край кнопки). Не помещается с предпочтительной стороны — с другой, иначе прижимается к краю видимой области. */
export function placePopup(menu, trigger, { side = 'below', align = 'start' } = {}) {
  const zoom = Number(getComputedStyle(document.documentElement).zoom) || 1;
  const viewport = window.visualViewport;
  const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
  const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
  menu.style.maxWidth = `${(width - 2 * EDGE) / zoom}px`;
  menu.style.maxHeight = `${(height - 2 * EDGE) / zoom}px`;
  menu.style.left = `${(left + EDGE) / zoom}px`;
  menu.style.top = `${(top + EDGE) / zoom}px`;
  menu.style.bottom = 'auto';
  if (!menu.matches(':popover-open')) {
    try { menu.showPopover({ source: trigger }); } catch { menu.showPopover(); }
  }
  const box = trigger.getBoundingClientRect(), size = menu.getBoundingClientRect();
  const x = align === 'end' ? box.right - size.width : box.left;
  const above = box.top - size.height - GAP, below = box.bottom + GAP;
  const fitsAbove = above >= top + EDGE, fitsBelow = below + size.height <= top + height - EDGE;
  const up = side === 'above' ? fitsAbove : !fitsBelow && fitsAbove;
  const y = up ? above : fitsBelow ? below : top + height - size.height - EDGE;
  menu.style.left = `${Math.max(left + EDGE, Math.min(x, left + width - size.width - EDGE)) / zoom}px`;
  if (up) {
    // над кнопкой — низом к ней: панель, что растёт (серия: ползунок «до»), растёт вверх, а не на кнопку
    menu.style.top = 'auto';
    const bottom = Math.min(box.top - GAP, top + height - EDGE);
    menu.style.bottom = `${(innerHeight - bottom) / zoom}px`;
  } else {
    menu.style.top = `${Math.max(top + EDGE, y) / zoom}px`;
  }
}

/** Popover у кнопки: общий каркас меню и панели — открыть/закрыть, aria-expanded, нажатие кнопки при открытом
 * закрывает, прокрутка страницы, смена размера и уход со страницы закрывают. */
function popover(trigger, node, { side, align, blocked = () => false, before = () => {}, after = () => {}, kind,
  followViewport = false }) {
  node.setAttribute('popover', 'auto');
  node.classList.add('popup-menu');
  if (!node.id && trigger.id) node.id = `${trigger.id}-menu`;
  trigger.setAttribute('aria-haspopup', kind);
  trigger.setAttribute('aria-expanded', 'false');
  if (node.id) trigger.setAttribute('aria-controls', node.id);
  let wasOpen = false;                     // кнопку нажали при открытом: light dismiss уже закрыл на pointerdown
  const isOpen = () => node.matches(':popover-open');
  const close = (focus = false) => {
    if (isOpen()) node.hidePopover();
    trigger.setAttribute('aria-expanded', 'false');
    if (focus && trigger.isConnected && !trigger.disabled) trigger.focus({ preventScroll: true });
  };
  // прокрутка закрывает, только если сдвинула кнопку: лента чата, что дописывается под открытым меню, — нет
  const scrolled = (event) => {
    const target = event.target;
    if (target === document || (target instanceof Node && !node.contains(target) && target.contains(trigger))) moved();
  };
  const hidden = () => { if (document.hidden) close(); };
  const moved = () => {
    if (followViewport && isOpen()) placePopup(node, trigger, { side, align });
    else close();
  };
  const listen = (on) => {
    const how = on ? 'addEventListener' : 'removeEventListener';
    window[how]('resize', moved);
    window[how]('scroll', scrolled, true);
    window.visualViewport?.[how]('resize', moved);
    if (followViewport) window.visualViewport?.[how]('scroll', moved);
    document[how]('visibilitychange', hidden);
  };
  const open = (...args) => {
    if (blocked() || isOpen() || !trigger.isConnected) return;
    before(...args);
    placePopup(node, trigger, { side, align });
    trigger.setAttribute('aria-expanded', 'true');
    after(...args);
  };
  trigger.addEventListener('pointerdown', () => { wasOpen = isOpen(); });
  trigger.addEventListener('click', () => {
    const was = wasOpen || isOpen();
    wasOpen = false;
    if (was) close();
    else open();
  });
  // beforetoggle — синхронно с открытием и закрытием (и light dismiss): aria-expanded без запаздывания
  node.addEventListener('beforetoggle', (event) => {
    const shown = event.newState === 'open';
    trigger.setAttribute('aria-expanded', String(shown));
    listen(shown);
  });
  return { open, close, isOpen };
}

/** Меню у кнопки. fill(close) → [узлы, пункты по порядку] — пересобирать при каждом открытии (выбор, ✓); без fill
 * пункты — [role^=menuitem] внутри меню. Ответ — { open(last), close(focus), isOpen(), unmount() }. */
export function popupMenu(trigger, menu, { blocked, fill = null, side = 'below', align = 'start' } = {}) {
  let items = [];
  if (!menu.getAttribute('role')) menu.setAttribute('role', 'menu');
  const live = () => items.filter((item) => !item.disabled && !item.hidden && item.isConnected);
  const focusItem = (item) => {
    if (!item) return;
    item.focus({ preventScroll: true });
    if (item.offsetTop < menu.scrollTop) menu.scrollTop = item.offsetTop;
    else if (item.offsetTop + item.offsetHeight > menu.scrollTop + menu.clientHeight) {
      menu.scrollTop = item.offsetTop + item.offsetHeight - menu.clientHeight;
    }
  };
  const box = popover(trigger, menu, { side, align, blocked, kind: 'menu',
    before: () => {
      if (fill) {
        const [nodes, list] = fill(close);
        menu.replaceChildren(...nodes);
        items = list;
      } else {
        items = [...menu.querySelectorAll('[role^="menuitem"]')];
      }
      for (const item of items) item.tabIndex = -1;
    },
    after: (last = false) => {
      const available = live();
      focusItem(last ? available.at(-1)
        : available.find((item) => item.getAttribute('aria-checked') === 'true') || available[0]);
    } });
  function close(focus = false) { box.close(focus); }
  trigger.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      box.open(event.key === 'ArrowUp');
    }
  });
  menu.addEventListener('keydown', (event) => {
    const available = live(), current = available.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (current + 1) % available.length;
    else if (event.key === 'ArrowUp') next = (current + available.length - 1) % available.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = available.length - 1;
    else if (event.key === 'Escape') {
      event.stopPropagation();             // только это меню: выдвижная панель и окно под ним остаются
      close(true);
    } else if (event.key === 'Tab') { close(true); return; }
    else if ((event.key === 'Enter' || event.key === ' ') && current >= 0 && available[current].tagName !== 'BUTTON') {
      available[current].click();          // пункт-label (выбор файла) клавиатурой
    } else return;
    event.preventDefault();
    if (next !== undefined && available.length) focusItem(available[next]);
  });
  return { open: box.open, close, isOpen: box.isOpen, unmount() { close(); menu.remove(); } };
}

/** Панель у кнопки (popover без role="menu"): для формы — серия картинок. Tab ходит по полям; фокус ушёл из панели —
 * она закрывается; Escape закрывает только её и возвращает фокус на кнопку. */
export function popupPanel(trigger, panel, { label, side = 'below', align = 'start', blocked,
  followViewport = false } = {}) {
  panel.setAttribute('role', 'dialog');
  if (label) panel.setAttribute('aria-label', label);
  const box = popover(trigger, panel, { side, align, blocked, kind: 'dialog', followViewport,
    after: () => (panel.querySelector('[aria-checked="true"]:not([hidden])')
      || panel.querySelector('button:not([disabled]):not([hidden]), input:not([disabled])'))?.focus({ preventScroll: true }) });
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation();
    box.close(true);
  });
  panel.addEventListener('focusout', (event) => {
    const to = event.relatedTarget;
    if (to && !panel.contains(to) && to !== trigger) box.close();
  });
  return { open: box.open, close: box.close, isOpen: box.isOpen };
}

/** Кнопка с текстом («Скачать ▾», «В чат ▾») и меню действий: entries — [[текст, атрибуты пункта, onSelect]].
 * Ответ — узел-обёртка с кнопкой и меню. */
export function actionMenu(label, { id = null, cls = '', entries }) {
  const trigger = el('button', { type: 'button', class: 'btn btn-sm', id }, label);
  const menu = el('div', { class: `tr-formats ${cls}`.trim(), role: 'menu', 'aria-label': label.replace(/\s*▾$/, '') });
  const popup = popupMenu(trigger, menu);
  menu.append(...entries.map(([text, attrs, onSelect]) => menuItem(text, { attrs, onSelect: () => {
    popup.close(true);
    onSelect();
  } })));
  return el('div', { class: 'tr-download-wrap' }, trigger, menu);
}

/** Пункт меню: radio — menuitemradio с aria-checked; onSelect(event) — по нажатию. attrs — прочие атрибуты (id,
 * data-*, class добавляется к menu-option). */
export function menuItem(label, { radio = false, checked = false, onSelect = null, attrs = {}, before = null } = {}) {
  const { class: cls, ...rest } = attrs;
  const item = el('button', { type: 'button', class: cls ? `menu-option ${cls}` : 'menu-option',
    role: radio ? 'menuitemradio' : 'menuitem', tabindex: '-1', ...(radio ? { 'aria-checked': String(checked) } : {}), ...rest },
  ...(before ? [before] : []), label);
  if (onSelect) item.addEventListener('click', onSelect);
  return item;
}
