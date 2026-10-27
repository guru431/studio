/* Вид ленты разделов «Фото» и «Видео»: превью (крупно, с промптом), плитка (сетка миниатюр) и
 * таблица (строка на запись). Кнопка с меню — в шапке раздела; выбор живёт в учётке (me.views, PATCH
 * /api/me) и одинаков на всех её устройствах. Вид ставится атрибутом data-view ленты, по нему и
 * раскладывает CSS. */
import { api, me } from './api.js';
import { el } from './ui.js';

const VIEWS = [
  ['preview', 'Превью', '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 16l5-5 4 4 3-3 6 6"/>'],
  ['tiles', 'Плитка', '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
    + '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>'],
  ['table', 'Таблица', '<path d="M10 6h10M10 12h10M10 18h10"/><rect x="3.5" y="4.5" width="3" height="3" rx=".8"/>'
    + '<rect x="3.5" y="10.5" width="3" height="3" rx=".8"/><rect x="3.5" y="16.5" width="3" height="3" rx=".8"/>'],
];

const svg = (path, cls = 'ico') => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
const CARET = '<path d="M6 9l6 6 6-6"/>';

/** Кнопка вида раздела `section` с меню из трёх видов → { node, value }; onChange(вид) — после смены.
 * Меню закрывают выбор, Escape (фокус — на кнопку) и нажатие вне; слушатели документа — только пока открыто. */
export function viewSwitch(section, fallback, onChange) {
  const saved = me.views[section];
  let value = VIEWS.some((v) => v[0] === saved) ? saved : fallback;
  const toggle = el('button', { type: 'button', class: 'icon-btn view-toggle', id: `${section}-view`, 'aria-haspopup': 'menu',
    'aria-expanded': 'false', 'aria-controls': `${section}-view-menu` });
  const items = VIEWS.map(([name, label, path]) => {
    const b = el('button', { type: 'button', class: 'chip-option view-option', id: `${section}-view-${name}`, role: 'menuitemradio',
      'data-view': name });
    b.innerHTML = svg(path);                                       // константы выше
    b.append(label);
    b.addEventListener('click', () => {
      open(false);
      toggle.focus();
      if (name === value) return;
      value = name;
      me.views[section] = name;
      // вид уже сменён; без сети не запомнится — сохранится при следующем выборе
      api('PATCH', '/api/me', { views: { [section]: name } }).catch(() => {});
      paint();
      onChange(name);
    });
    return b;
  });
  const menu = el('div', { class: 'view-menu', id: `${section}-view-menu`, role: 'menu', 'aria-label': 'Вид ленты', hidden: true },
    ...items);
  const node = el('div', { class: 'view-pick' }, toggle, menu);

  // нажатие вне — в фазе захвата: в режиме выделения лента глушит нажатие по карточке
  const onDoc = (event) => { if (!node.contains(event.target)) open(false); };
  const onKey = (event) => {
    if (event.key !== 'Escape') return;
    open(false);
    toggle.focus();
  };
  function open(on) {
    menu.hidden = !on;
    toggle.setAttribute('aria-expanded', String(on));
    const how = on ? 'addEventListener' : 'removeEventListener';
    document[how]('click', onDoc, true);
    document[how]('keydown', onKey);
  }
  toggle.addEventListener('click', () => open(menu.hidden));

  function paint() {
    const [, label, path] = VIEWS.find((v) => v[0] === value);
    toggle.innerHTML = svg(path) + svg(CARET, 'ico caret');       // константы выше
    toggle.setAttribute('aria-label', `Вид ленты: ${label}`);
    toggle.title = `Вид ленты: ${label}`;
    for (const b of items) b.setAttribute('aria-checked', String(b.dataset.view === value));
  }
  paint();
  return { node, get value() { return value; } };
}

/** Строка заголовков вида «Таблица» (над лентой, видна только в нём). */
export function tableHead(titles) {
  return el('div', { class: 'feed-head', 'aria-hidden': 'true' }, ...titles.map((t) => el('span', {}, t)));
}

/** Время записи «14:05» (день — в заголовке группы ленты). */
export function timeOf(created) {
  return new Date(created * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
