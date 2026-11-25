import { tr } from './i18n.js';
/* Меню сообщения ленты (спека 2026-11-25-studio-skills-status-local-models, п. 38): правая кнопка мыши, клавиша меню,
 * долгое нажатие на сенсоре — «Копировать», «Цитировать» и по пункту на каждую видимую включённую кнопку действий
 * сообщения (`.msg-actions`, `.code-msg-actions`, `.code-fork`, кроме `.msg-copy`): пункт нажимает её.
 *
 * messageMenu(scroller, { message, text, quote, said }) → { unmount() }: message — селектор узла сообщения; text(узел)
 * — его текст для «Копировать» и «Цитировать» (выделение внутри этого сообщения — важнее); quote(text) — чип цитаты
 * (null — без «Цитировать»); said(слова) — итог копирования. На ссылке, картинке, видео, звуке и в полях ввода, а
 * также с Shift — меню браузера. Где `contextmenu` долгим нажатием нет (iOS) — касание без сдвига больше MOVE_PX
 * дольше LONG_PRESS_MS; меню открывается по отпусканию пальца: иначе отпускание закрыло бы popover сразу. */
import { menuItem, popupMenu, rootZoom } from './menu.js';
import { el } from './ui.js';

export const LONG_PRESS_MS = 550;
const MOVE_PX = 10;
const NATIVE = 'a[href], img, video, audio, input, textarea';
const ACTIONS = ':is(.msg-actions, .code-msg-actions) button, .code-fork';

export function messageMenu(scroller, { message, text, quote = null, said = null }) {
  const anchor = el('button', { type: 'button', class: 'msg-menu-anchor', tabindex: '-1',
    'aria-label': tr("Действия сообщения") });
  const menu = el('div', { id: 'msg-menu', class: 'msg-menu', 'aria-label': tr("Действия сообщения") });
  let target = null;
  const picked = () => {
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return '';
    const range = sel.getRangeAt(0);
    return target?.contains(range.commonAncestorContainer) ? sel.toString().trim() : '';
  };
  const popup = popupMenu(anchor, menu, { fill: (close) => {
    const node = target;
    const words = picked() || String(text(node) ?? '').trim();
    const items = [];
    if (words) {
      items.push(menuItem(tr("Копировать"), { onSelect: async () => {
        close(false);
        try {
          await navigator.clipboard.writeText(words);
          said?.(tr("Скопировано"));
        } catch {
          said?.(tr("Не вышло"));
        }
      } }));
      if (quote) items.push(menuItem(tr("Цитировать"), { onSelect: () => { close(false); quote(words); } }));
    }
    for (const button of node.querySelectorAll(ACTIONS)) {
      if (button.matches('.msg-copy') || button.disabled || button.hidden || button.closest(message) !== node) continue;
      if (button.checkVisibility && !button.checkVisibility()) continue;
      const label = button.textContent.trim() || button.getAttribute('aria-label') || '';
      if (label) items.push(menuItem(label, { onSelect: () => { close(false); button.click(); } }));
    }
    return [items, items];
  } });
  document.body.append(anchor, menu);

  const show = (node, x, y) => {
    target = node;
    const box = node.getBoundingClientRect();
    if (!x && !y) { x = box.left + 16; y = box.top + 16; }          // клавиша меню: у начала сообщения
    const zoom = rootZoom();
    anchor.style.left = `${Math.max(0, Math.min(x, innerWidth - 1)) / zoom}px`;
    anchor.style.top = `${Math.max(0, Math.min(y, innerHeight - 1)) / zoom}px`;
    popup.close();
    popup.open();
  };
  const own = (event) => {
    const node = event.target instanceof Element ? event.target.closest(message) : null;
    if (!node || !scroller.contains(node) || event.shiftKey || event.target.closest(NATIVE)) return null;
    return node;
  };

  let press = null;                // { node, x, y, timer, long }
  const drop = () => { if (press) clearTimeout(press.timer); press = null; };
  const onContext = (event) => {
    const node = own(event);
    if (!node) return;
    event.preventDefault();
    if (press) { press.long = true; return; }      // долгое нажатие Android: меню — по отпусканию пальца
    show(node, event.clientX, event.clientY);
  };
  const onStart = (event) => {
    drop();
    if (event.touches.length !== 1) return;
    const node = own(event);
    if (!node) return;
    const { clientX: x, clientY: y } = event.touches[0];
    press = { node, x, y, long: false, timer: setTimeout(() => { if (press) press.long = true; }, LONG_PRESS_MS) };
  };
  const onMove = (event) => {
    const t = event.touches[0];
    if (press && t && Math.hypot(t.clientX - press.x, t.clientY - press.y) > MOVE_PX) drop();
  };
  const onEnd = (event) => {
    const done = press;
    drop();
    if (!done?.long) return;
    if (event.cancelable) event.preventDefault();  // без щелчка после долгого нажатия
    show(done.node, done.x, done.y);
  };
  scroller.addEventListener('contextmenu', onContext);
  scroller.addEventListener('touchstart', onStart, { passive: true });
  scroller.addEventListener('touchmove', onMove, { passive: true });
  scroller.addEventListener('touchend', onEnd);
  scroller.addEventListener('touchcancel', onEnd);   // Android мог отменить касание, отдав его contextmenu
  return {
    unmount() {
      drop();
      scroller.removeEventListener('contextmenu', onContext);
      scroller.removeEventListener('touchstart', onStart);
      scroller.removeEventListener('touchmove', onMove);
      scroller.removeEventListener('touchend', onEnd);
      scroller.removeEventListener('touchcancel', onEnd);
      popup.unmount();
      anchor.remove();
    },
  };
}
