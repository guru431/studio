import { tr } from './i18n.js';
/* Выделение нескольких записей ленты «Фото» и «Видео» (спека 2026-10-21-studio-feed-selection):
 * режим, отметки, групповые «Скачать» и «Удалить».
 *
 * Вход в режим — «Выбрать» в шапке, долгое нажатие пальцем на карточку (она сразу отмечена) или
 * галочка, проступающая при наведении мышью. В режиме нажатие по карточке ставит и снимает отметку
 * (просмотр не открывается, ролик не играет: поверх карточки — слой CSS), Shift+щелчок отмечает
 * диапазон, кружок у дня — весь день, «Все» — всё подгруженное; Escape, «✕» и выполненное действие —
 * выход. Отметки живут только у записей ленты: лента сменилась (onChange) — sync() снимает ушедшие.
 * «Все», «Скачать» и «Удалить» — на широком экране в шапке, на телефоне — панелью на месте поля.
 * «Скачать» — отмеченные с файлом (save.saveMany: ZIP, на iPhone — «Поделиться»); «Удалить» — после
 * одного подтверждения по одной, «Отмена» окна останавливает после текущей.
 *
 * createSelection({ section, nouns, selectable, mediaOf, remove, extraActions }): section — 'photo' |
 * 'video' (начало id); nouns — формы слова для plural (['картинку', 'картинки', 'картинок']);
 * selectable(item) — можно ли отметить; mediaOf(item) — media id файла или null; remove(item) —
 * Promise удаления записи разделом (DELETE, «уже удалена» — успех, запись уходит из ленты);
 * extraActions — необязательные действия раздела [{ id, label, shown(items), run(items) }]: кнопка
 * рядом с «Скачать» видна, только пока shown(отмеченные в порядке ленты); run режим не закрывает
 * («Сравнить» у фото).
 * Ответ — { button, tools, bar, decorate(node, item), dayTools(day), attach({ root, head, feedBox,
 * feed, error }), sync(), exit(), destroy() }: button («Выбрать») и tools (✕ и счёт) раздел ставит в
 * шапку, bar — в конец корня раздела; decorate — в конце render карточки, dayTools — опция ленты. */
import { dayKey } from './feed.js';
import { svg } from './icons.js';
import { saveMany } from './save.js';
import { button, el, openDialog, plural, words } from './ui.js';

const ARCHIVE_MAX = 1000;        // файлов в одном архиве (media.ARCHIVE_MAX)
const PRESS_MS = 500;            // долгое нажатие
const SLOP_PX = 10;              // сдвиг пальца, после которого это уже прокрутка
const NARROW = '(max-width: 760px)';

export function createSelection({ section, nouns, selectable, mediaOf, remove, extraActions = [] }) {
  const picked = new Set();      // id отмеченных (строки, как data-id)
  let selecting = false;
  let anchor = null;             // последняя отмеченная — начало диапазона Shift
  let ctx = null;                // { root, head, feedBox, feed, error } — attach()
  let alive = true;
  let busy = false;              // идёт групповое действие
  let note = '';                 // ход «Скачать» на месте счёта
  let stopDeleting = null;       // остановить идущее удаление
  let press = null;              // { id, x, y, timer } — палец на карточке
  let swallow = false;           // хвостовой click долгого нажатия
  let marked = false;            // в ленте могут быть отметки: вне режима paint() их снимает один раз

  const selectBtn = el('button', { type: 'button', class: 'btn btn-sm select-btn', id: `${section}-select`,
    title: tr("Выбрать"), 'aria-label': tr("Выбрать") });
  selectBtn.innerHTML = svg('pick');
  selectBtn.append(el('span', { class: 'select-label' }, tr("Выбрать")));
  const exitBtn = el('button', { type: 'button', class: 'icon-btn', id: `${section}-select-exit`,
    'aria-label': tr("Снять выделение"), title: tr("Снять выделение") });
  exitBtn.innerHTML = svg('x');
  const count = el('span', { class: 'select-count', id: `${section}-select-count`, 'aria-live': 'polite' });
  const tools = el('div', { class: 'select-tools' }, exitBtn, count);
  const allBtn = button(tr("Все"), { class: 'btn btn-sm', id: `${section}-select-all` });
  const downloadBtn = button(tr("Скачать"), { class: 'btn btn-sm', id: `${section}-select-download` });
  const deleteBtn = button(tr("Удалить"), { class: 'btn btn-sm btn-danger', id: `${section}-select-delete` });
  const extras = extraActions.map((a) => {
    const b = button(a.label, { class: 'btn btn-sm', id: a.id, hidden: true });
    b.addEventListener('click', () => {
      const items = pickedItems();
      if (!busy && a.shown(items)) a.run(items);
    });
    return { action: a, node: b };
  });
  const actions = el('div', { class: 'select-actions' }, allBtn, ...extras.map((x) => x.node), downloadBtn, deleteBtn);
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
    const box = el('button', { type: 'button', class: 'card-pick', role: 'checkbox', 'aria-label': tr("Отметить") });
    box.innerHTML = svg('check');
    node.append(box);
    node.dataset.pickable = '';
    mark(node, picked.has(idOf(item)));
    return node;
  }

  function dayTools(day) {
    const box = el('button', { type: 'button', class: 'day-pick', role: 'checkbox', 'aria-label': tr("Отметить день"),
      'data-day': day, 'aria-checked': 'false' });
    box.innerHTML = svg('check');
    return box;
  }

  function paint() {
    if (!ctx || (!selecting && !marked)) return;      // вне режима лента меняется без обхода карточек
    marked = selecting;
    const all = pickable();
    const full = all.length > 0 && all.every((i) => picked.has(idOf(i)));
    count.textContent = note || tr("Выбрано {0}", picked.size);
    allBtn.textContent = full ? tr("Снять") : tr("Все");
    allBtn.disabled = busy || !all.length;
    deleteBtn.disabled = busy || !picked.size;
    for (const { action, node } of extras) node.hidden = busy || !action.shown(pickedItems());
    paintDownload();
    for (const node of ctx.feedBox.querySelectorAll('.card[data-pickable]')) mark(node, picked.has(node.dataset.id));
    for (const box of ctx.feedBox.querySelectorAll('.day-pick')) {
      const day = all.filter((i) => dayKey(i.created) === box.dataset.day);
      box.setAttribute('aria-checked', String(day.length > 0 && day.every((i) => picked.has(idOf(i)))));
    }
  }

  /** Отмеченные записи в порядке ленты. */
  function pickedItems() {
    return shown().filter((i) => picked.has(idOf(i)));
  }

  /** media id отмеченных с файлом, в порядке ленты. */
  function pickedFiles() {
    return shown().filter((i) => picked.has(idOf(i))).map(mediaOf).filter(Boolean);
  }

  function paintDownload() {
    const files = pickedFiles();
    downloadBtn.textContent = files.length < picked.size ? tr("Скачать {0}", files.length) : tr("Скачать");
    downloadBtn.disabled = busy || !files.length || files.length > ARCHIVE_MAX;
    downloadBtn.title = files.length > ARCHIVE_MAX ? tr("Не больше {0} за раз", ARCHIVE_MAX) : '';
  }

  async function downloadPicked() {
    const files = pickedFiles();
    if (busy || !files.length || files.length > ARCHIVE_MAX) return;
    ctx.error.textContent = '';
    setBusy(true);
    let failed = null;
    let saved = true;
    try {
      saved = await saveMany(files, ctx.root, (i, n) => {
        note = tr("Готовлю {0} из {1}…", i, n);
        count.textContent = note;
      });
    } catch (err) {
      failed = err;
    }
    note = '';
    busy = false;
    if (!alive) return;
    if (failed) ctx.error.textContent = words(failed);
    if (failed || !saved) paint();           // «Отмена» окна «Файлы готовы» — отметки остаются
    else exit();
  }

  function enter() {
    if (selecting) return;
    selecting = true;
    ctx.root.setAttribute('data-selecting', '');
    for (const clip of ctx.feedBox.querySelectorAll('video')) clip.pause();   // под слоем его не остановить
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
    const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: `${section}-select-delete-confirm` });
    const dialog = openDialog(ctx.root, { id: `${section}-select-delete-dialog`, title: tr("Удалить {0} {1}?", n, plural(n, nouns)),
      body: [el('p', {}, tr("Удалятся из истории вместе с файлами.")), progress], actions: [confirm] });
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
        progress.textContent = tr("Удаляю {0} из {1}", done + failed + 1, n);
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
      if (failed) ctx.error.textContent = tr("Не удалось удалить {0} из {1}: {2}", failed, n, words(last));
      else if (done < n) ctx.error.textContent = tr("Удалено {0} из {1}, остальные не тронуты", done, n);
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
    cancelPress();                           // второй палец (щипок) — не долгое нажатие
    if (event.pointerType === 'mouse' || !event.isPrimary || busy) return;
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

  /* в фазе захвата — раньше разделов: открытое меню (чип, раздел — popover menu.js; аватар) Escape закрывает само,
   * без выхода */
  function onKey(event) {
    if (event.key === 'Escape' && selecting && !busy
      && !document.querySelector('dialog[open], [role="menu"]:not([hidden]):not([popover]), [popover]:popover-open')) exit();
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
    document.addEventListener('keydown', onKey, true);
    narrow.addEventListener('change', place);
    place();
    paint();
  }

  function destroy() {
    alive = false;
    cancelPress();
    if (stopDeleting) stopDeleting();
    document.removeEventListener('keydown', onKey, true);
    narrow.removeEventListener('change', place);
  }

  selectBtn.addEventListener('click', () => {
    if (ctx) enter();
  });
  exitBtn.addEventListener('click', () => { if (!busy) exit(); });
  allBtn.addEventListener('click', toggleAll);
  downloadBtn.addEventListener('click', downloadPicked);
  deleteBtn.addEventListener('click', deletePicked);

  return { button: selectBtn, tools, bar, decorate, dayTools, attach, sync, exit, destroy,
    get selecting() { return selecting; } };
}
