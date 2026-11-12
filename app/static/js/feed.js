/* Лента карточек по дням («Сегодня», «Вчера», «15 октября») с подгрузкой при прокрутке —
 * общая для разделов «Фото» и «Видео».
 *
 * createFeed(el, { load, render, key, quiet, empty, head, onChange, dayTools }): el — прокручиваемый
 * контейнер ленты; load(last) — Promise страницы записей новее-сверху, следующая — после last,
 * последней записи ленты (запрос — pageQuery(last)), первая — last undefined, пустая страница — конец;
 * render(item) — узел карточки; key(item) — id записи; quiet — поля, смена которых карточку не меняет
 * (их ведёт свой таймер раздела): update с теми же прочими полями узел не заменяет (опрос не сбивает
 * фокус и нажатие), без quiet — заменяет всегда; empty — текст пустой ленты, head — узел над
 * днями; onChange() — после каждой смены показанных записей (страница, prepend, update, remove,
 * redraw; reload — когда легла его первая страница); dayTools(день) — узел в начало заголовка дня
 * (выделение, select.js). Необязательны все шесть. Записи несут `created` (секунды unix) и `id`.
 * Ответ — { prepend(item), update(item), remove(id), reload(), redraw(), items() }; reload() — Promise
 * первой страницы, redraw() — карточки заново тем же render (сменился вид ленты), записи и прокрутка —
 * на месте; items() — копия записей в порядке ленты.
 * Состояние — el.dataset.feed: loading | ready | end | error (по нему ждут тесты).
 *
 * pageQuery и withFirstPage — те же страницы для списков «Расшифровки» и «Озвучки», createPicker —
 * их список с выбором, pickerList — его отрисовка на месте, keep и busyBlock — панель выбранной записи на месте;
 * dayTitle — и подпись дня этих списков и «Сканов» (год — если не текущий). */
import { api } from './api.js';
import { calendarDay, dateOnly } from './time.js';
import { button, el as make, words } from './ui.js';

const NEAR_PX = 600;             // ближе к низу — подгрузить следующую страницу

export function dayKey(created) {
  const d = calendarDay(created);
  return `${d.year}-${d.month - 1}-${d.day}`;
}

export function dayTitle(created) {
  const d = calendarDay(created);
  const today = calendarDay(Date.now() / 1000);
  const days = today.number - d.number;
  if (days <= 0) return 'Сегодня';
  if (days === 1) return 'Вчера';
  const opts = { day: 'numeric', month: 'long' };
  if (d.year !== today.year) opts.year = 'numeric';
  return dateOnly(created, opts);
}

/** Курсор страницы истории после записи last (её created и id): у записей бывает одно время, и по
 * одному created записи того же времени за краем страницы пропали бы. Первая страница — ''. */
export function pageQuery(last) {
  return last === undefined ? '' : `&before=${last.created}&before_id=${encodeURIComponent(last.id)}`;
}

/** Свежая первая страница (size записей) поверх подгруженного списка items: подгруженные старше её
 * края остаются, как и порядок ленты — created, затем id по убыванию. */
export function withFirstPage(first, items, size) {
  if (first.length < size) return first;                  // вся история — на первой странице
  const edge = first[first.length - 1];
  const ids = new Set(first.map((x) => x.id));
  return [...first, ...items.filter((x) => !ids.has(x.id)
    && (x.created < edge.created || (x.created === edge.created && x.id < edge.id)))];
}

/** Список с выбором «Расшифровки» и «Озвучки»: записи url страницами по size, старше — more();
 * идущие (running(item)) — опрос первой страницы раз в pollMs, подгруженные остаются; выбранная —
 * GET url/<id> (select(id, quiet): quiet — отказ молча, запись по ссылке могли удалить).
 * Номера чтений и выбора: запоздалый ответ прежнего не ляжет и вид не займёт.
 * paint() — перерисовать список, show() — выбор занял вид (current), fail(err) — ошибка.
 * polled — выбранную идущую запись ведёт свой опрос раздела («Перевод»): опрос ленты её не перечитывает.
 * Ответ — { items, current, done, paging, load, more, refresh, select, deselect, forget(id), stop };
 * deselect() — вид убрали сами: выбор в пути (и перечитывание refresh) его не займёт;
 * forget(id) — запись удалена на сервере (дальше — вид и load()). */
export function createPicker({ url, size, pollMs, running, paint, show, fail, polled = false }) {
  const s = { items: [], current: null, done: false, paging: false, load, more, refresh, select, deselect, forget, stop };
  let alive = true;
  let poll = null;
  let listing = 0;             // номер чтения первой страницы: запоздавшее прежнее не ляжет
  let removals = 0;            // удаления: страница, начатая до удаления, не вернёт удалённую
  let picking = 0;             // номер выбора: запоздалый ответ прежнего вид не займёт
  let wanted = null;           // id последнего выбора

  async function load() {
    const mine = ++listing;
    try {
      const first = (await api('GET', `${url}?limit=${size}`)).items;
      if (mine !== listing) return;
      s.items = withFirstPage(first, s.items, size);
      if (first.length < size) s.done = true;
    } catch (err) {
      if (mine !== listing) return;
      fail(err);
    }
    paint();
    clearTimeout(poll);
    if (alive && s.items.some(running)) poll = setTimeout(refresh, pollMs);
  }

  async function more() {
    if (s.paging || s.done || !s.items.length) return;
    s.paging = true;
    const removed = removals;
    paint();
    try {
      const got = (await api('GET', `${url}?limit=${size}${pageQuery(s.items[s.items.length - 1])}`)).items;
      if (removed === removals) {
        const ids = new Set(s.items.map((x) => x.id));
        s.items = [...s.items, ...got.filter((x) => !ids.has(x.id))];
        s.done = got.length < size;
      }
    } catch (err) {
      fail(err);
    }
    s.paging = false;
    if (alive) paint();
  }

  async function refresh() {
    await load();
    const { current } = s;
    if (current && wanted === current.id && !(polled && running(current))) {   // выбор другой в пути — не перебивать
      const fresh = s.items.find((i) => i.id === current.id);
      if (fresh && (fresh.state !== current.state || fresh.detail !== current.detail)) await select(current.id);
    }
  }

  async function select(id, quiet = false) {
    const mine = ++picking;
    wanted = id;
    let got;
    try {
      got = await api('GET', `${url}/${encodeURIComponent(id)}`);
    } catch (err) {
      if (mine === picking && !quiet) fail(err);
      return;
    }
    if (!alive || mine !== picking) return;
    s.current = got;
    show();
  }

  function deselect() {
    picking += 1;                                     // выбор, начатый раньше, вид не займёт
    wanted = null;
    s.current = null;
  }

  function forget(id) {
    removals += 1;
    s.items = s.items.filter((x) => x.id !== id);
    deselect();
  }

  function stop() {
    alive = false;
    clearTimeout(poll);
  }

  return s;
}

/** Список createPicker на месте («Расшифровка», «Озвучка», «Сканы»): строка записи — кнопка .tr-item cls,
 * один и тот же узел, пока запись в списке, — меняются только её имя, слова состояния, data-state и
 * aria-current у выбранной; подписи дней и «Показать ещё» (id moreId) — тоже те же узлы; новые — на своё
 * место, пропавшие — прочь (опрос не сбивает фокус и нажатие). name(item), state(item) — тексты строки,
 * pick(id) — нажатие, empty — текст пустого списка. Ответ — paint(picker). */
export function pickerList(list, { cls, name, state, pick, empty, moreId }) {
  const rows = new Map();              // id → { node, title, words }
  const days = new Map();              // подпись дня → узел
  const none = make('p', { class: 'muted small' }, empty);
  const more = make('button', { type: 'button', class: 'btn btn-sm tr-more', id: moreId });
  let picker = null;
  more.addEventListener('click', () => picker.more());

  function text(node, value) {
    const shown = value ?? '';
    if (node.textContent !== shown) node.textContent = shown;
  }

  return function paint(p) {
    picker = p;
    const { items, current } = p;
    const nodes = [];
    const seen = new Set();
    const shownDays = new Set();
    let last = '';
    for (const item of items) {
      const label = dayTitle(item.created);
      if (label !== last) {
        if (!days.has(label)) days.set(label, make('p', { class: 'tr-group' }, label));
        nodes.push(days.get(label));
        shownDays.add(label);
        last = label;
      }
      const { id } = item;
      let r = rows.get(id);
      if (!r) {
        r = { title: make('span', { class: 'tr-name' }), words: make('span', { class: 'tr-state' }) };
        r.node = make('button', { type: 'button', class: `tr-item ${cls}`.trim(), 'data-id': id }, r.title, r.words);
        r.node.addEventListener('click', () => pick(id));
        rows.set(id, r);
      }
      seen.add(id);
      text(r.title, name(item));
      text(r.words, state(item));
      r.node.dataset.state = item.state;
      if (current && current.id === id) r.node.setAttribute('aria-current', 'true');
      else r.node.removeAttribute('aria-current');
      nodes.push(r.node);
    }
    for (const id of [...rows.keys()]) if (!seen.has(id)) rows.delete(id);
    for (const label of [...days.keys()]) if (!shownDays.has(label)) days.delete(label);
    if (!items.length) nodes.push(none);
    else if (!p.done) {
      more.disabled = p.paging;
      text(more, p.paging ? 'Загружаю…' : 'Показать ещё');
      nodes.push(more);
    }
    keep(list, nodes);
  };
}

/** Дети parent — nodes по порядку: лишние уходят, новые встают на место, а стоящие в том же порядке не
 * отсоединяются (фокус, нажатие и звук плеера переживают перерисовку). */
export function keep(parent, nodes) {
  const wanted = new Set(nodes);
  for (const child of [...parent.childNodes]) if (!wanted.has(child)) child.remove();
  let at = parent.firstChild;
  for (const node of nodes) {
    if (node === at) at = at.nextSibling;
    else parent.insertBefore(node, at);
  }
}

/** Блок «идёт» выбранной записи вида createPicker: кольцо, item.detail и «Отменить» (id) — POST url/<id>/cancel,
 * потом after(); отказ — fail(err), кнопка снова доступна. prev — блок прошлой перерисовки: для того же id
 * меняется только текст, узлы те же (опрос не сбивает фокус на «Отменить»). Ответ — { id, box }. */
export function busyBlock(prev, item, { id, url, fail, after }) {
  if (!prev || prev.id !== item.id) {
    const cancel = make('button', { type: 'button', class: 'btn btn-sm', id }, 'Отменить');
    cancel.addEventListener('click', async () => {
      cancel.disabled = true;
      try {
        await api('POST', `${url}/${item.id}/cancel`);
      } catch (err) {
        cancel.disabled = false;
        fail(err);
      }
      after();
    });
    const text = make('p', {});
    prev = { id: item.id, text, box: make('div', { class: 'tr-busy' },
      make('span', { class: 'ring', 'aria-hidden': 'true' }), text, cancel) };
  }
  const shown = item.detail ?? '';
  if (prev.text.textContent !== shown) prev.text.textContent = shown;
  return prev;
}

export function createFeed(el, { load, render, key, quiet = null, empty = '', head = null, onChange = () => {},
  dayTools = null }) {
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
      const title = make('h2', { class: 'feed-day-title' }, dayTools ? dayTools(k) : null, dayTitle(created));
      g = { box: make('section', { class: 'feed-day', 'data-day': k }, title, grid), grid };
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
      got = await load(items.length ? items[items.length - 1] : undefined);
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
    onChange();
    if (!done && near()) page();      // страница не заполнила экран — следующую сразу
  }

  el.addEventListener('scroll', () => { if (near()) page(); }, { passive: true });
  new ResizeObserver(() => { if (near()) page(); }).observe(el);

  function prepend(item) {
    if (!place(item, true)) return;
    items.unshift(item);
    if (el.dataset.feed !== 'loading') setState(done ? 'end' : 'ready');
    onChange();
  }

  /** Запись без полей quiet и created: по ней update решает, нужна ли новая карточка. */
  function look(item) {
    const shown = { ...item, created: undefined };
    for (const k of quiet) shown[k] = undefined;
    return JSON.stringify(shown);
  }

  function update(item) {
    const id = key(item);
    const old = nodes.get(id);
    if (!old) return;
    const i = items.findIndex((x) => key(x) === id);
    const same = quiet && i >= 0 && look(items[i]) === look(item);
    if (i >= 0) items[i] = { ...item, created: items[i].created };
    if (same) return;                 // карточка та же: фокус и нажатие на ней не теряются
    const node = render(item);
    old.replaceWith(node);
    nodes.set(id, node);
    onChange();
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
    onChange();
  }

  function redraw() {
    for (const item of items) {
      const id = key(item);
      const node = render(item);
      nodes.get(id).replaceWith(node);
      nodes.set(id, node);
    }
    onChange();
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
  return { prepend, update, remove, reload, redraw, items: () => items.slice() };
}
