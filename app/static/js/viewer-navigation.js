/* Фото и видео: переходы по готовым файлам в порядке ленты, старые страницы — по требованию.
 * Снимок галереи передаётся следующему просмотру; действия и цепочка принадлежат его записи. */
import { api } from './api.js';
import { pageQuery } from './feed.js';
import { iconButton } from './icons.js';
import { el, words } from './ui.js';

const ready = (item) => !!item.media_id && item.state === 'done';

export function viewerNavigation(dialog, { item, items, section, open, done, opener, gallery = null }) {
  const state = gallery || { items: items.slice(), done, opener };
  const previous = iconButton('prev', 'Предыдущий файл', { class: 'icon-btn viewer-prev' });
  const next = iconButton('next', 'Следующий файл', { class: 'icon-btn viewer-next' });
  const position = el('span', { class: 'viewer-position', 'aria-live': 'polite', 'aria-atomic': 'true' });
  const error = el('p', { class: 'error small viewer-navigation-error', 'aria-live': 'polite' });
  const controls = el('div', { class: 'viewer-navigation', 'aria-label': 'Перелистывание' }, previous, position, next);
  dialog.querySelector('.viewer-top').prepend(controls);
  dialog.querySelector('.viewer-bar').prepend(error);
  dialog.dataset.item = item.id;
  let busy = false;
  const active = () => dialog.open && dialog.isConnected;
  const files = () => state.items.filter(ready);

  function paint() {
    const list = files();
    const at = list.findIndex((row) => row.id === item.id);
    const focused = document.activeElement;
    previous.disabled = busy || at === 0;
    next.disabled = busy || (state.done && at === list.length - 1);
    if ((focused === previous && previous.disabled) || (focused === next && next.disabled)) {
      dialog.querySelector('.viewer-close').focus({ preventScroll: true });
    }
    position.textContent = busy ? 'Загружаю…' : `${at < 0 ? '…' : at + 1} / ${list.length}${state.done ? '' : '+'}`;
  }

  async function more() {
    const rows = (await api('GET', `/api/${section}?limit=30${pageQuery(state.items.at(-1))}`)).items;
    if (!active()) return;
    const ids = new Set(state.items.map((row) => row.id));
    const added = rows.filter((row) => !ids.has(row.id));
    state.items.push(...added);
    state.done = !added.length;
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
    if (!document.querySelector('dialog[open]') && state.opener?.isConnected) state.opener.focus({ preventScroll: true });
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

  // Ссылка из заданий/цепочки может вести за пределы уже загруженной ленты.
  // Сначала достраиваем промежуток, чтобы «предыдущий» не перескакивал через него.
  async function locate() {
    while (!state.done && !state.items.some((row) => row.id === item.id)) {
      await more();
      if (!active()) return;
    }
    if (!state.items.some((row) => row.id === item.id)) {
      state.items.push(item);
      state.items.sort((a, b) => b.created - a.created || b.id.localeCompare(a.id));
    }
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
