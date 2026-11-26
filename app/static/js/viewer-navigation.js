import { tr } from './i18n.js';
/* Фото и видео: переходы по готовым файлам в порядке ленты, старые страницы — по требованию.
 * Снимок галереи передаётся следующему просмотру; действия и цепочка принадлежат его записи.
 * Запись вне загруженной ленты (поиск, цепочка, «Варианты») — своё окно вокруг неё: страница старше и
 * страница новее (after=), дальше — по краям окна; ленту до неё не читаем. top — окно дошло до новейшей.
 * query — отбор ленты (`&model=…`): страницы перелистывания читаются с ним же. */
import { api } from './api.js';
import { pageQuery } from './feed.js';
import { icon, iconButton } from './icons.js';
import { el, words } from './ui.js';

const ready = (item) => !!item.media_id && item.state === 'done';
const PAGE = 30;

export function viewerNavigation(dialog, { item, items, section, open, done, opener, gallery = null, query = '' }) {
  const state = gallery || { items: items.slice(), done, top: true, opener, query,
    details: !matchMedia('(max-width: 760px), (pointer: coarse)').matches };
  const top = dialog.querySelector('.viewer-top');
  const close = top.querySelector('.viewer-close');
  const details = dialog.querySelector('.viewer-bar');
  details.id = `${dialog.id}-details`;
  const toggle = iconButton('info', tr("Показать сведения"), { class: 'icon-btn viewer-details-toggle',
    'aria-controls': details.id });
  close.before(toggle);
  function paintDetails() {
    details.hidden = !state.details;
    const label = state.details ? tr("На весь экран") : tr("Показать сведения");
    toggle.setAttribute('aria-expanded', String(state.details));
    toggle.setAttribute('aria-label', label);
    toggle.title = label;
    toggle.replaceChildren(icon(state.details ? 'maximize' : 'info'));
  }
  toggle.addEventListener('click', () => { state.details = !state.details; paintDetails(); });
  paintDetails();
  const previous = iconButton('prev', tr("Предыдущий файл"), { class: 'icon-btn viewer-prev' });
  const next = iconButton('next', tr("Следующий файл"), { class: 'icon-btn viewer-next' });
  const position = el('span', { class: 'viewer-position', 'aria-live': 'polite', 'aria-atomic': 'true' });
  const error = el('p', { class: 'error small viewer-navigation-error', 'aria-live': 'polite' });
  const controls = el('div', { class: 'viewer-navigation', 'aria-label': tr("Перелистывание") }, previous, position, next, error);
  top.prepend(controls);
  dialog.dataset.item = item.id;
  let busy = false;
  const active = () => dialog.open && dialog.isConnected;
  const files = () => state.items.filter(ready);

  function paint() {
    const list = files();
    const at = list.findIndex((row) => row.id === item.id);
    const focused = document.activeElement;
    previous.disabled = busy || (at === 0 && state.top);
    next.disabled = busy || (state.done && at === list.length - 1);
    if ((focused === previous && previous.disabled) || (focused === next && next.disabled)) {
      dialog.querySelector('.viewer-close').focus({ preventScroll: true });
    }
    // в окне вне ленты номер от начала истории неизвестен
    position.textContent = busy ? tr("Загружаю…")
      : state.top ? `${at < 0 ? '…' : at + 1} / ${list.length}${state.done ? '' : '+'}` : '…';
  }

  const newerQuery = (row) => `&after=${row.created}&after_id=${encodeURIComponent(row.id)}`;
  const fresh = (rows) => {
    const ids = new Set(state.items.map((row) => row.id));
    return rows.filter((row) => !ids.has(row.id));
  };

  async function more() {
    const rows = (await api('GET', `/api/${section}?limit=${PAGE}${state.query || ''}${pageQuery(state.items.at(-1))}`)).items;
    if (!active()) return;
    const added = fresh(rows);
    state.items.push(...added);
    state.done = !added.length;
  }

  async function newer() {
    const rows = (await api('GET', `/api/${section}?limit=${PAGE}${state.query || ''}${newerQuery(state.items[0])}`)).items;
    if (!active()) return;
    state.items.unshift(...fresh(rows));
    state.top = rows.length < PAGE;
  }

  async function move(direction) {
    if (busy || !active()) return;
    busy = true;
    error.textContent = '';
    paint();
    try {
      await locate();
      if (!active()) return;
      let list = files();
      let at = list.findIndex((row) => row.id === item.id);
      while (direction > 0 && at === list.length - 1 && !state.done) {
        await more();
        if (!active()) return;
        list = files();
        at = list.findIndex((row) => row.id === item.id);
      }
      while (direction < 0 && at === 0 && !state.top) {
        await newer();
        if (!active()) return;
        list = files();
        at = list.findIndex((row) => row.id === item.id);
      }
      const target = list[at + direction];
      if (!target || !active()) return;
      // Остановить прежний ролик сразу, до асинхронного события close.
      for (const player of dialog.querySelectorAll('video')) player.pause();
      dialog.close();
      dialog.remove();
      const shown = open(target, state);
      const button = shown.querySelector(direction > 0 ? '.viewer-next' : '.viewer-prev');
      (button.disabled ? shown.querySelector('.viewer-close') : button).focus({ preventScroll: true });
    } catch (err) {
      if (active()) error.textContent = words(err);
    } finally {
      busy = false;
      if (active()) paint();
    }
  }

  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  dialog.addEventListener('close', () => {
    // У действия с результатом может быть свой переход фокуса (например, к промпту).
    if (!dialog.returnValue && !document.querySelector('dialog[open]') && state.opener?.isConnected) {
      state.opener.focus({ preventScroll: true });
    }
  });
  dialog.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
        || !['ArrowLeft', 'ArrowRight'].includes(event.key)
        || event.target.closest('input, textarea, select, video, [contenteditable="true"]')) return;
    // Сравнение и подтверждение удаления поверх просмотра получают свои клавиши.
    if ([...document.querySelectorAll('dialog[open]')].at(-1) !== dialog) return;
    event.preventDefault();
    move(event.key === 'ArrowLeft' ? -1 : 1);
  });

  const stage = dialog.querySelector('.viewer-stage');
  let touch = null;
  let suppressClickUntil = 0;
  stage.addEventListener('touchstart', (event) => {
    touch = null;
    suppressClickUntil = 0;
    if (event.touches.length !== 1 || dialog.classList.contains('full')) return;
    const point = event.touches[0];
    const player = event.target.closest('video');
    // Полоса управления плеера сохраняет перемотку и громкость.
    if (player && point.clientY > player.getBoundingClientRect().bottom - 64) return;
    touch = { x: point.clientX, y: point.clientY, id: point.identifier };
  }, { passive: true });
  stage.addEventListener('touchmove', (event) => {
    if (event.touches.length !== 1) { touch = null; return; }
    if (!touch) return;
    const point = event.touches[0];
    if (Math.abs(point.clientX - touch.x) > 15
        && Math.abs(point.clientX - touch.x) > Math.abs(point.clientY - touch.y) * 1.5) {
      if (event.cancelable) event.preventDefault();
      suppressClickUntil = performance.now() + 500;
    }
  }, { passive: false });
  stage.addEventListener('touchcancel', () => { touch = null; }, { passive: true });
  stage.addEventListener('touchend', (event) => {
    const start = touch;
    touch = null;
    if (!start || event.touches.length || dialog.classList.contains('full')) return;
    const point = [...event.changedTouches].find((p) => p.identifier === start.id);
    if (!point) return;
    const dx = point.clientX - start.x;
    if (Math.abs(dx) < 50 || Math.abs(dx) <= Math.abs(point.clientY - start.y) * 1.5) return;
    suppressClickUntil = performance.now() + 500;
    move(dx < 0 ? 1 : -1);
  }, { passive: true });
  stage.addEventListener('click', (event) => {
    if (performance.now() >= suppressClickUntil) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  // Ссылка из поиска, заданий или цепочки может вести за пределы загруженной ленты: вместо чтения
  // ленты до записи — окно вокруг неё (по странице старше и новее), «предыдущий» не перескакивает.
  async function locate() {
    if (state.items.some((row) => row.id === item.id)) return;
    if (state.done) {
      state.items.push(item);
      state.items.sort((a, b) => b.created - a.created || b.id.localeCompare(a.id));
      return;
    }
    const [above, below] = await Promise.all([newerQuery(item), pageQuery(item)].map((query) =>
      api('GET', `/api/${section}?limit=${PAGE}${state.query || ''}${query}`).then((page) => page.items)));
    if (!active()) return;
    state.items = [...above, item, ...below.filter((row) => row.id !== item.id)];
    state.top = above.length < PAGE;
    state.done = below.length < PAGE;
  }
  async function initialize() {
    busy = true;
    paint();
    try {
      await locate();
    } catch (err) {
      if (active()) error.textContent = words(err);
    } finally {
      busy = false;
      if (active()) paint();
    }
  }
  paint();
  if (!state.items.some((row) => row.id === item.id)) initialize();
}
