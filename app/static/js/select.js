/* Выделение нескольких записей ленты «Фото» и «Видео» (спека 2026-10-21-studio-feed-selection):
 * режим, отметки, групповые «Скачать» и «Удалить».
 *
 * Вход в режим — «Выбрать» в шапке, долгое нажатие пальцем на карточку (она сразу отмечена) или
 * галочка, проступающая при наведении мышью. В режиме нажатие по карточке ставит и снимает отметку
 * (просмотр не открывается, ролик не играет: поверх карточки — слой CSS), Shift+щелчок отмечает
 * диапазон, кружок у дня — весь день, «Все» — всё подгруженное; Escape, «✕» и выполненное действие —
 * выход. Отметки живут только у записей ленты: лента сменилась (onChange) — sync() снимает ушедшие.
 * «Все», «Скачать» и «Удалить» — на широком экране в шапке, на телефоне — панелью на месте поля.
 *
 * createSelection({ section, nouns, selectable, mediaOf, remove }): section — 'photo' | 'video'
 * (начало id); nouns — формы слова для plural (['картинку', 'картинки', 'картинок']);
 * selectable(item) — можно ли отметить; mediaOf(item) — media id файла или null; remove(item) —
 * Promise удаления записи разделом (DELETE, «уже удалена» — успех, запись уходит из ленты).
 * Ответ — { button, tools, bar, decorate(node, item), dayTools(day), attach({ root, head, feedBox,
 * feed, error }), sync(), exit(), destroy() }: button («Выбрать») и tools (✕ и счёт) раздел ставит в
 * шапку, bar — в конец корня раздела; decorate — в конце render карточки, dayTools — опция ленты. */
import { dayKey } from './feed.js';
import { button, el, openDialog, plural, words } from './ui.js';

const PRESS_MS = 500;            // долгое нажатие
const SLOP_PX = 10;              // сдвиг пальца, после которого это уже прокрутка
const NARROW = '(max-width: 760px)';
const ICONS = {
  pick: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
  check: '<path d="M6 12.5l4 4 8-9"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
};

function svg(name) {
  return `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
}

export function createSelection({ section, nouns, selectable, mediaOf, remove }) {
  const picked = new Set();      // id отмеченных (строки, как data-id)
  let selecting = false;
  let anchor = null;             // последняя отмеченная — начало диапазона Shift
  let ctx = null;                // { root, head, feedBox, feed, error } — attach()
  let alive = true;
  let busy = false;              // идёт групповое действие
  let stopDeleting = null;       // остановить идущее удаление
  let press = null;              // { id, x, y, timer } — палец на карточке
  let swallow = false;           // хвостовой click долгого нажатия

  const selectBtn = el('button', { type: 'button', class: 'btn btn-sm select-btn', id: `${section}-select`,
    title: 'Выбрать', 'aria-label': 'Выбрать' });
  selectBtn.innerHTML = `${svg('pick')}<span class="select-label">Выбрать</span>`;   // константы выше
  const exitBtn = el('button', { type: 'button', class: 'icon-btn', id: `${section}-select-exit`,
    'aria-label': 'Снять выделение', title: 'Снять выделение' });
  exitBtn.innerHTML = svg('x');
  const count = el('span', { class: 'select-count', id: `${section}-select-count`, 'aria-live': 'polite' });
  const tools = el('div', { class: 'select-tools' }, exitBtn, count);
  const allBtn = button('Все', { class: 'btn btn-sm', id: `${section}-select-all` });
  const downloadBtn = button('Скачать', { class: 'btn btn-sm', id: `${section}-select-download` });
  const deleteBtn = button('Удалить', { class: 'btn btn-sm btn-danger', id: `${section}-select-delete` });
  const actions = el('div', { class: 'select-actions' }, allBtn, downloadBtn, deleteBtn);
  const bar = el('div', { class: 'select-bar' });
  const narrow = matchMedia(NARROW);

  const idOf = (item) => String(item.id);
  const shown = () => (ctx ? ctx.feed.items() : []);
  const pickable = () => shown().filter(selectable);
  const cardOf = (target) => {
    const card = target.closest('.card[data-id]');
    return card && ctx.feedBox.contains(card) ? card : null;
  };

  function mark(node, on) {
    node.dataset.selected = String(on);
    const box = node.querySelector(':scope > .card-pick');
    if (box) box.setAttribute('aria-checked', String(on));
  }

  function decorate(node, item) {
    if (!selectable(item)) return node;
    const box = el('button', { type: 'button', class: 'card-pick', role: 'checkbox', 'aria-label': 'Отметить' });
    box.innerHTML = svg('check');
    node.append(box);
    node.dataset.pickable = '';
    mark(node, picked.has(idOf(item)));
    return node;
  }

  function dayTools(day) {
    const box = el('button', { type: 'button', class: 'day-pick', role: 'checkbox', 'aria-label': 'Отметить день',
      'data-day': day, 'aria-checked': 'false' });
    box.innerHTML = svg('check');
    return box;
  }

  function paint() {
    if (!ctx) return;
    const all = pickable();
    const full = all.length > 0 && all.every((i) => picked.has(idOf(i)));
    count.textContent = `Выбрано ${picked.size}`;
    allBtn.textContent = full ? 'Снять' : 'Все';
    allBtn.disabled = busy || !all.length;
    deleteBtn.disabled = busy || !picked.size;
    paintDownload();
    for (const node of ctx.feedBox.querySelectorAll('.card[data-pickable]')) mark(node, picked.has(node.dataset.id));
    for (const box of ctx.feedBox.querySelectorAll('.day-pick')) {
      const day = all.filter((i) => dayKey(i.created) === box.dataset.day);
      box.setAttribute('aria-checked', String(day.length > 0 && day.every((i) => picked.has(idOf(i)))));
    }
  }

  function paintDownload() {
    downloadBtn.disabled = busy || !picked.size;
  }

  function enter() {
    if (selecting) return;
    selecting = true;
    ctx.root.setAttribute('data-selecting', '');
    paint();
  }

  function exit() {
    selecting = false;
    picked.clear();
    anchor = null;
    if (ctx) ctx.root.removeAttribute('data-selecting');
    paint();
  }

  function setMany(list, on) {
    for (const item of list) {
      if (on) picked.add(idOf(item));
      else picked.delete(idOf(item));
    }
  }

  function toggle(id, shift) {
    const items = pickable();
    const at = items.findIndex((i) => idOf(i) === id);
    if (at < 0) return;
    const from = anchor === null ? -1 : items.findIndex((i) => idOf(i) === anchor);
    if (shift && from >= 0 && from !== at) {
      setMany(items.slice(Math.min(from, at), Math.max(from, at) + 1), true);
    } else if (picked.has(id)) {
      picked.delete(id);
    } else {
      picked.add(id);
    }
    if (picked.has(id)) anchor = id;
    paint();
  }

  function toggleDay(day) {
    const list = pickable().filter((i) => dayKey(i.created) === day);
    setMany(list, !(list.length && list.every((i) => picked.has(idOf(i)))));
    paint();
  }

  function toggleAll() {
    const all = pickable();
    setMany(all, !(all.length && all.every((i) => picked.has(idOf(i)))));
    paint();
  }

  /** Отметки только у записей ленты, которые можно отметить; затем — вид. */
  function sync() {
    if (!ctx) return;
    const live = new Set(pickable().map(idOf));
    for (const id of [...picked]) if (!live.has(id)) picked.delete(id);
    if (anchor !== null && !live.has(anchor)) anchor = null;
    paint();
  }

  function setBusy(on) {
    busy = on;
    paint();
  }

  function deletePicked() {
    const list = shown().filter((i) => picked.has(idOf(i)));
    const n = list.length;
    if (!n) return;
    const progress = el('p', { class: 'muted small', 'aria-live': 'polite' });
    const confirm = button('Удалить', { class: 'btn btn-danger solid', id: `${section}-select-delete-confirm` });
    const dialog = openDialog(ctx.root, { id: `${section}-select-delete-dialog`, title: `Удалить ${n} ${plural(n, nouns)}?`,
      body: [el('p', {}, 'Удалятся из истории вместе с файлами.'), progress], actions: [confirm] });
    let running = false;
    let stopped = false;
    dialog.addEventListener('close', () => { if (running) stopped = true; });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      running = true;
      stopDeleting = () => { stopped = true; };
      ctx.error.textContent = '';
      setBusy(true);
      let done = 0;
      let failed = 0;
      let last = null;
      for (const item of list) {
        if (stopped) break;
        progress.textContent = `Удаляю ${done + failed + 1} из ${n}`;
        try {
          await remove(item);
          picked.delete(idOf(item));
          done += 1;
        } catch (err) {
          failed += 1;
          last = err;
        }
      }
      running = false;
      stopDeleting = null;
      if (dialog.open) dialog.close();
      if (!alive) return;
      busy = false;
      if (failed) ctx.error.textContent = `Не удалось удалить ${failed} из ${n}: ${words(last)}`;
      else if (done < n) ctx.error.textContent = `Удалено ${done} из ${n}, остальные не тронуты`;
      if (done === n) exit();
      else sync();
    });
  }

  /* нажатия в ленте: одним обработчиком в фазе захвата — до кнопок карточек */
  function onClick(event) {
    if (swallow) {
      swallow = false;
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const dayBox = event.target.closest('.day-pick');
    if (dayBox) {
      event.preventDefault();
      if (selecting && !busy) toggleDay(dayBox.dataset.day);
      return;
    }
    const card = cardOf(event.target);
    if (!card || !card.hasAttribute('data-pickable')) return;
    const box = event.target.closest('.card-pick');
    if (!selecting && !box) return;
    event.preventDefault();
    event.stopPropagation();
    if (busy) return;
    enter();
    toggle(card.dataset.id, event.shiftKey);
  }

  function onPointerDown(event) {
    swallow = false;
    if (event.pointerType === 'mouse' || busy) return;
    const card = cardOf(event.target);
    if (!card || !card.hasAttribute('data-pickable')) return;
    const id = card.dataset.id;
    press = { id, x: event.clientX, y: event.clientY, timer: setTimeout(() => {
      press = null;
      swallow = true;
      enter();
      if (!picked.has(id)) toggle(id, false);
    }, PRESS_MS) };
  }

  function onPointerMove(event) {
    if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) > SLOP_PX) cancelPress();
  }

  function cancelPress() {
    if (press) clearTimeout(press.timer);
    press = null;
  }

  function onContextMenu(event) {
    const card = cardOf(event.target);
    if (card && card.hasAttribute('data-pickable')) event.preventDefault();      // Android: долгое нажатие — наше
  }

  function onKey(event) {
    if (event.key === 'Escape' && selecting && !busy && !document.querySelector('dialog[open]')) exit();
  }

  function place() {
    (narrow.matches ? bar : ctx.head).append(actions);
  }

  function attach(where) {
    ctx = where;
    const box = ctx.feedBox;
    box.addEventListener('click', onClick, true);
    box.addEventListener('pointerdown', onPointerDown);
    box.addEventListener('pointermove', onPointerMove, { passive: true });
    for (const type of ['pointerup', 'pointercancel', 'scroll']) box.addEventListener(type, cancelPress, { passive: true });
    box.addEventListener('contextmenu', onContextMenu);
    document.addEventListener('keydown', onKey);
    narrow.addEventListener('change', place);
    place();
    paint();
  }

  function destroy() {
    alive = false;
    cancelPress();
    if (stopDeleting) stopDeleting();
    document.removeEventListener('keydown', onKey);
    narrow.removeEventListener('change', place);
  }

  selectBtn.addEventListener('click', () => {
    if (ctx) enter();
  });
  exitBtn.addEventListener('click', () => { if (!busy) exit(); });
  allBtn.addEventListener('click', toggleAll);
  deleteBtn.addEventListener('click', deletePicked);

  return { button: selectBtn, tools, bar, actions, downloadBtn, decorate, dayTools, attach, sync, exit, destroy,
    get selecting() { return selecting; } };
}
