/* Лента карточек по дням («Сегодня», «Вчера», «15 октября») с подгрузкой при прокрутке —
 * общая для разделов «Фото» и «Видео».
 *
 * createFeed(el, { load, render, key, empty, head }): el — прокручиваемый контейнер ленты; load(before) —
 * Promise страницы записей новее-сверху, следующая — before = created последней (секунды unix),
 * первая — before undefined, пустая страница — конец; render(item) — узел карточки; key(item) —
 * id записи; empty — текст пустой ленты, head — узел над днями (оба необязательны). Записи несут `created`.
 * Ответ — { prepend(item), update(item), remove(id), reload(), redraw() }; reload() — Promise первой
 * страницы, redraw() — карточки заново тем же render (сменился вид ленты), записи и прокрутка — на месте.
 * Состояние — el.dataset.feed: loading | ready | end | error (по нему ждут тесты). */
import { button, el as make, words } from './ui.js';

const NEAR_PX = 600;             // ближе к низу — подгрузить следующую страницу

function dayKey(created) {
  const d = new Date(created * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function dayTitle(created) {
  const d = new Date(created * 1000);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const start = new Date(d);
  start.setHours(0, 0, 0, 0);
  const days = Math.round((today - start) / 86400000);
  if (days <= 0) return 'Сегодня';
  if (days === 1) return 'Вчера';
  const opts = { day: 'numeric', month: 'long' };
  if (d.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString('ru-RU', opts);
}

export function createFeed(el, { load, render, key, empty = '', head = null }) {
  const items = [];              // записи по порядку ленты
  const nodes = new Map();       // id → узел карточки
  const groups = new Map();      // день → { box, grid }
  const days = make('div', { class: 'feed-days' });
  const more = make('div', { class: 'feed-more', 'aria-live': 'polite' });
  let loading = false;
  let done = false;
  let generation = 0;            // reload обнуляет: запоздалая страница прежней ленты не ляжет

  el.replaceChildren(...(head ? [head] : []), days, more);

  function setState(name) {
    el.dataset.feed = name;
    const none = !items.length;
    if (name === 'loading') more.replaceChildren(make('p', { class: 'muted small' }, 'Загружаю…'));
    else if (name === 'error') return;
    else more.replaceChildren(...(none && empty ? [make('p', { class: 'muted feed-empty' }, empty)] : []));
  }

  function group(created, atTop) {
    const k = dayKey(created);
    let g = groups.get(k);
    if (!g) {
      const grid = make('div', { class: 'feed-grid' });
      g = { box: make('section', { class: 'feed-day', 'data-day': k }, make('h2', { class: 'feed-day-title' }, dayTitle(created)), grid),
        grid };
      groups.set(k, g);
      if (atTop) days.prepend(g.box);
      else days.append(g.box);
    }
    return g;
  }

  function place(item, atTop) {
    const id = key(item);
    if (nodes.has(id)) return false;
    const node = render(item);
    nodes.set(id, node);
    const g = group(item.created, atTop);
    if (atTop) g.grid.prepend(node);
    else g.grid.append(node);
    return true;
  }

  function near() {
    return el.scrollTop + el.clientHeight >= el.scrollHeight - NEAR_PX;
  }

  async function page() {
    if (loading || done) return;
    loading = true;
    const mine = generation;
    setState('loading');
    let got;
    try {
      got = await load(items.length ? items[items.length - 1].created : undefined);
    } catch (err) {
      if (mine !== generation) return;
      loading = false;
      el.dataset.feed = 'error';
      more.replaceChildren(make('p', { class: 'error small' }, words(err)),
        button('Повторить', { class: 'btn btn-sm' }, () => page()));
      return;
    }
    if (mine !== generation) return;
    loading = false;
    for (const item of got) {
      if (place(item, false)) items.push(item);
    }
    done = !got.length;
    setState(done ? 'end' : 'ready');
    if (!done && near()) page();      // страница не заполнила экран — следующую сразу
  }

  el.addEventListener('scroll', () => { if (near()) page(); }, { passive: true });
  new ResizeObserver(() => { if (near()) page(); }).observe(el);

  function prepend(item) {
    if (!place(item, true)) return;
    items.unshift(item);
    if (el.dataset.feed !== 'loading') setState(done ? 'end' : 'ready');
  }

  function update(item) {
    const id = key(item);
    const old = nodes.get(id);
    if (!old) return;
    const node = render(item);
    old.replaceWith(node);
    nodes.set(id, node);
    const i = items.findIndex((x) => key(x) === id);
    if (i >= 0) items[i] = { ...item, created: items[i].created };
  }

  function remove(id) {
    const node = nodes.get(id);
    if (!node) return;
    const grid = node.parentElement;
    node.remove();
    nodes.delete(id);
    const i = items.findIndex((x) => key(x) === id);
    if (i >= 0) items.splice(i, 1);
    if (grid && !grid.children.length) {
      const box = grid.parentElement;
      groups.delete(box.dataset.day);
      box.remove();
    }
    if (el.dataset.feed !== 'loading') setState(done ? 'end' : 'ready');
  }

  function redraw() {
    for (const item of items) {
      const id = key(item);
      const node = render(item);
      nodes.get(id).replaceWith(node);
      nodes.set(id, node);
    }
    if (near()) page();
  }

  function reload() {
    generation += 1;
    loading = false;
    done = false;
    items.length = 0;
    nodes.clear();
    groups.clear();
    days.replaceChildren();
    el.scrollTop = 0;
    return page();
  }

  page();
  return { prepend, update, remove, reload, redraw };
}
