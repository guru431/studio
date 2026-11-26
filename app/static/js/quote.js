import { tr } from './i18n.js';
/* «Цитировать» (спека 2026-11-25-studio-skills-status-local-models, п. 38): цитата из ленты — чипом над полем ввода
 * «Чата», «Агентов» и Open WebUI; при отправке раздел зовёт fold() первой строкой, и цитата встаёт в начало самого
 * поля строками «> …» — дальше код видит обычное поле, сверки «поле равно отправленному» целы.
 *
 * quoted(text) — текст без краевых пробелов, каждая строка с «> »; отправляемая цитата целиком (с «> » и «…» обрезки)
 *   — не длиннее QUOTE_MAX знаков.
 * quoteChip(host, field, { id, changed }) → { node, set(text), clear(), value(), composed(), fold() }: чип `div.quote-chip#id`
 *   (значок, начало цитаты, «Убрать цитату») — в host перед рамкой поля; changed() — после смены цитаты.
 * selectionQuote(scroller, { texts, quote }) → { unmount() }: выделение целиком внутри одного узла `texts` ленты —
 *   кнопка `#quote-float` «Цитировать» у конца выделения; нажатие — quote(выделенный текст). */
import { icon, iconButton } from './icons.js';
import { rootZoom } from './menu.js';
import { el } from './ui.js';

export const QUOTE_MAX = 4000;
const SHOWN = 80;
const TITLE = 300;
const EDGE = 8;

export function quoted(text) {
  const out = String(text ?? '').trim().split('\n').map((line) => `> ${line}`).join('\n');
  return out.length > QUOTE_MAX ? `${out.slice(0, QUOTE_MAX - 1)}…` : out;
}

const cut = (text, n) => (text.length > n ? `${text.slice(0, n)}…` : text);

export function quoteChip(host, field, { id = 'quote-chip', changed = null } = {}) {
  let current = '';
  const label = el('span', { class: 'quote-chip-text' });
  const remove = iconButton('x', tr("Убрать цитату"), { class: 'icon-btn quote-chip-remove' });
  const node = el('div', { class: 'quote-chip', id, hidden: true }, icon('quote'), label, remove);
  const paint = () => {
    node.hidden = !current;
    const flat = current.replace(/\s+/g, ' ');
    label.textContent = current ? tr("Цитата: {0}", cut(flat, SHOWN)) : '';
    node.title = current ? cut(flat, TITLE) : '';
  };
  const set = (text) => {
    current = String(text ?? '').trim();
    paint();
    changed?.();
    if (current) field.focus({ preventScroll: true });
  };
  const clear = () => {
    if (!current) return;
    current = '';
    paint();
    changed?.();
  };
  remove.addEventListener('click', () => { clear(); field.focus({ preventScroll: true }); });
  if (host) host.insertBefore(node, field.closest('.field-box') && host.contains(field.closest('.field-box'))
    ? field.closest('.field-box') : null);
  const composed = () => !current ? field.value : [quoted(current), field.value].filter(Boolean).join('\n\n');
  return {
    node, set, clear, composed,
    value: () => current,
    fold() {
      if (!current) return false;
      field.value = composed();
      field.dispatchEvent(new Event('input', { bubbles: true }));
      clear();
      return true;
    },
  };
}

export function selectionQuote(scroller, { texts, quote }) {
  const button = el('button', { type: 'button', class: 'btn btn-sm quote-float', id: 'quote-float', hidden: true },
    icon('quote'), tr("Цитировать"));
  document.body.append(button);
  let picked = '';
  const hide = () => { button.hidden = true; picked = ''; };
  const chosen = () => {
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    const node = (n) => (n?.nodeType === Node.ELEMENT_NODE ? n : n?.parentElement)?.closest(texts);
    const start = node(sel.anchorNode);
    if (!start || start !== node(sel.focusNode) || !scroller.contains(start)) return null;
    return { text, range: sel.getRangeAt(sel.rangeCount - 1) };
  };
  const paint = () => {
    const found = chosen();
    if (!found) { hide(); return; }
    picked = found.text;
    const rects = found.range.getClientRects();
    const rect = rects[rects.length - 1] || found.range.getBoundingClientRect();
    button.hidden = false;
    const { width, height } = button.getBoundingClientRect();
    const zoom = rootZoom();
    button.style.left = `${Math.max(EDGE, Math.min(rect.right - width / 2, innerWidth - width - EDGE)) / zoom}px`;
    const below = rect.bottom + EDGE;
    button.style.top = `${(below + height + EDGE <= innerHeight ? below : Math.max(EDGE, rect.top - height - EDGE)) / zoom}px`;
  };
  const onKey = (event) => { if (event.key === 'Escape' && !button.hidden) hide(); };
  button.addEventListener('mousedown', (event) => event.preventDefault());   // выделение остаётся
  button.addEventListener('click', () => {
    const text = picked;
    hide();
    document.getSelection()?.removeAllRanges();
    if (text) quote(text);
  });
  document.addEventListener('selectionchange', paint);
  document.addEventListener('keydown', onKey);
  scroller.addEventListener('scroll', hide, { passive: true });
  return {
    unmount() {
      document.removeEventListener('selectionchange', paint);
      document.removeEventListener('keydown', onKey);
      scroller.removeEventListener('scroll', hide);
      button.remove();
    },
  };
}
