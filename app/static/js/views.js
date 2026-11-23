import { tr } from './i18n.js';
/* Вид ленты разделов «Фото» и «Видео»: превью (крупно, с промптом), плитка (сетка миниатюр) и
 * таблица (строка на запись). Кнопка с меню — в шапке раздела; выбор живёт в учётке (me.views, PATCH
 * /api/me) и одинаков на всех её устройствах. Вид ставится атрибутом data-view ленты, по нему и
 * раскладывает CSS. */
import { api, me } from './api.js';
import { timeOnly } from './time.js';
import { el } from './ui.js';
import { icon } from './icons.js';
import { menuItem, popupMenu } from './menu.js';

const VIEWS = [['preview', tr("Превью")], ['tiles', tr("Плитка")], ['table', tr("Таблица")]];   // значки — icons.js по имени вида

/** Кнопка вида раздела `section` с меню из трёх видов → { node, value }; onChange(вид) — после смены.
 * Меню — общий компонент menu.js: выбор, Escape (фокус — на кнопку) и нажатие вне закрывают его. */
export function viewSwitch(section, fallback, onChange) {
  const saved = me.views[section];
  let value = VIEWS.some((v) => v[0] === saved) ? saved : fallback;
  const toggle = el('button', { type: 'button', class: 'icon-btn view-toggle', id: `${section}-view` });
  const items = VIEWS.map(([name, label]) => menuItem(label, { radio: true, before: icon(name),
    attrs: { class: 'view-option', id: `${section}-view-${name}`, 'data-view': name },
    onSelect: () => {
      popup.close(true);
      if (name === value) return;
      value = name;
      me.views[section] = name;
      // вид уже сменён; без сети не запомнится — сохранится при следующем выборе
      api('PATCH', '/api/me', { views: { [section]: name } }).catch(() => {});
      paint();
      onChange(name);
    } }));
  const menu = el('div', { class: 'view-menu', id: `${section}-view-menu`, role: 'menu', 'aria-label': tr("Вид ленты") },
    ...items);
  const node = el('div', { class: 'view-pick' }, toggle, menu);
  const popup = popupMenu(toggle, menu, { align: 'end' });

  function paint() {
    const [, label] = VIEWS.find((v) => v[0] === value);
    toggle.replaceChildren(icon(value), icon('down', 'ico caret'));
    toggle.setAttribute('aria-label', tr("Вид ленты: {0}", label));
    toggle.title = tr("Вид ленты: {0}", label);
    for (const b of items) b.setAttribute('aria-checked', String(b.dataset.view === value));
  }
  paint();
  return { node, get value() { return value; } };
}

/** Чип модели в шапке ленты раздела `section` ('photo' | 'video') → { node, value, add(model), set(model) }:
 * «Все модели» и модели своей истории (GET /api/search/models), label(model) — подпись. Чип виден, когда моделей
 * больше одной или фильтр включён. onChange(модель | null) — выбор сменился (раздел перечитывает ленту с ?model=);
 * add — модель новой записи, set — сменить выбор без onChange. Выбор не запоминается: новый вход — все модели. */
export function modelFilter(section, label, onChange) {
  let value = null;
  let models = [];
  const name = el('span', { class: 'chip-name' });
  const toggle = el('button', { type: 'button', class: 'chip ctl-chip feed-model-toggle', id: `${section}-model-filter` },
    name, icon('down', 'ico caret'));
  const menu = el('div', { class: 'chip-menu', id: `${section}-model-filter-menu`, role: 'menu', 'aria-label': tr("Модель") });
  const node = el('div', { class: 'feed-model', hidden: true }, toggle, menu);
  const option = (model) => menuItem(model === null ? tr("Все модели") : label(model), { radio: true,
    checked: model === value, attrs: { 'data-model': model ?? '' }, onSelect: () => {
      popup.close(true);
      if (model === value) return;
      value = model;
      paint();
      onChange(value);
    } });
  const popup = popupMenu(toggle, menu, { align: 'end', fill: () => {
    const nodes = [null, ...models].map(option);
    return [nodes, nodes];
  } });

  function paint() {
    name.textContent = value === null ? tr("Все модели") : label(value);
    toggle.title = tr("Модель: {0}", name.textContent);
    toggle.classList.toggle('on', value !== null);
    node.hidden = models.length < 2 && value === null;
  }

  function add(model) {
    if (!model || models.includes(model)) return;
    models.push(model);
    paint();
  }

  api('GET', '/api/search/models').then(({ models: all }) => {
    for (const model of all[section] || []) add(model);
  }, () => {});                                       // чип — подсказка: без него лента та же
  paint();
  return { node, add, set(model) { value = model; paint(); }, get value() { return value; } };
}

/** Строка заголовков вида «Таблица» (над лентой, видна только в нём). */
export function tableHead(titles) {
  return el('div', { class: 'feed-head', 'aria-hidden': 'true' }, ...titles.map((t) => el('span', {}, t)));
}

/** Время записи «14:05» (день — в заголовке группы ленты). */
export function timeOf(created) {
  return timeOnly(created);
}
