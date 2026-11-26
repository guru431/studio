import { tr } from './i18n.js';
/* Поиск по тексту открытого диалога: строка над лентой, только память страницы.
 * Ищется без регистра текст сообщений человека и ответов загруженной ленты; шаги не ищутся. Подсветка — CSS Custom
 * Highlight API (`code-find`, текущее — `code-find-current`); без него — только счётчик и переход.
 *
 * findView(host, { scroller, feed, earlier, hasEarlier }) → { button, open(), close(), unmount() }:
 * - `button` (#code-find) — для ряда действий строки состояния; Ctrl+F / ⌘F, пока чат на экране и нет окна;
 * - Enter и «следующее» — вперёд, Shift+Enter и «предыдущее» — назад; Escape — закрыть и снять подсветку;
 * - «Искать в более ранней части» (видна, пока `hasEarlier()`) зовёт `earlier()` до нового совпадения или начала
 *   истории, повторное нажатие — стоп. */
import { el } from './ui.js';
import { icon, iconButton } from './icons.js';

export const FIND_MAX = 1000;
const TYPE_MS = 150;              // пересчёт после ввода и изменения ленты

/** Нижний регистр по одной кодовой единице: позиции совпадают с исходной строкой. */
function fold(text) {
  let out = '';
  for (const char of text) {
    const lower = char.toLowerCase();
    out += lower.length === char.length ? lower : char;
  }
  return out;
}

export function findView(host, { scroller, feed, earlier, hasEarlier = () => false,
  prefix = 'code', texts = '.code-user-text, .code-timeline-text' }) {
  const id = (part = '') => `${prefix}-find${part}`;
  const input = el('input', { type: 'text', id: id('-input'), class: 'input code-find-input', autocomplete: 'off',
    spellcheck: 'false', enterkeyhint: 'search', 'aria-label': tr("Найти в чате"), placeholder: tr("Найти в чате") });
  const count = el('span', { id: id('-count'), class: 'code-find-count', 'aria-live': 'polite' });
  const prev = iconButton('upward', tr("Предыдущее совпадение"), { id: id('-prev') });
  const next = iconButton('downward', tr("Следующее совпадение"), { id: id('-next') });
  const earlierButton = el('button', { type: 'button', id: id('-earlier'), class: 'btn btn-sm code-find-earlier',
    hidden: true }, tr("Искать в более ранней части"));
  const closeButton = iconButton('x', tr("Закрыть поиск"), { id: id('-close') });
  const bar = el('div', { id: id('-bar'), class: 'code-find-bar', role: 'search', hidden: true,
    'aria-label': tr("Поиск в чате") }, input, count, prev, next, earlierButton, closeButton);
  const button = el('button', { type: 'button', id: id(), class: 'icon-btn', 'aria-label': tr("Найти в чате"),
    title: tr("Найти в чате · Ctrl+F"), 'aria-controls': bar.id, 'aria-expanded': 'false' }, icon('search'));
  host.prepend(bar);

  const highlights = typeof Highlight === 'function' && globalThis.CSS?.highlights ? CSS.highlights : null;
  let matches = [];
  let more = false;                // совпадений больше FIND_MAX
  let current = -1;
  let typing = null;               // пересчёт после ввода: новый запрос, первое совпадение
  let changed = null;              // пересчёт после изменения ленты: текущее совпадение остаётся
  let searching = false;           // идёт «Искать в более ранней части»
  let stop = false;
  let alive = true;

  function collect(query) {
    const found = [];
    let total = 0;
    for (const box of feed.querySelectorAll(texts)) {
      // Кнопки разметки («Копировать» у кода) и скрытое — не текст сообщения.
      const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, { acceptNode: (node) =>
        node.parentElement?.closest('button, [hidden], [aria-hidden="true"], details:not([open])')
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
      const nodes = [];
      let text = '';
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        nodes.push({ node, at: text.length });
        text += node.data;
      }
      const hay = fold(text);
      for (let at = hay.indexOf(query); at !== -1; at = hay.indexOf(query, at + query.length)) {
        total += 1;
        if (total > FIND_MAX) return { found, more: true };
        found.push(rangeOf(nodes, at, at + query.length));
      }
    }
    return { found, more: false };
  }

  function rangeOf(nodes, start, end) {
    const place = (offset, last) => {
      const item = nodes.findLast((n) => last ? n.at < offset : n.at <= offset && offset < n.at + n.node.data.length)
        || nodes.at(-1);
      return [item.node, offset - item.at];
    };
    const range = document.createRange();
    range.setStart(...place(start, false));
    range.setEnd(...place(end, true));
    return range;
  }

  function paint() {
    const query = input.value.trim();
    count.textContent = query ? tr("{0} из {1}", matches.length ? current + 1 : 0, more ? `${FIND_MAX}+` : matches.length) : '';
    prev.disabled = next.disabled = !matches.length;
    earlierButton.hidden = !query || (!hasEarlier() && !searching);
    earlierButton.textContent = searching ? tr("Остановить поиск") : tr("Искать в более ранней части");
    if (!highlights) return;
    if (matches.length) {
      highlights.set('code-find', new Highlight(...matches));
      highlights.set('code-find-current', new Highlight(matches[current]));
    } else {
      clearHighlights();
    }
  }

  function clearHighlights() {
    highlights?.delete('code-find');
    highlights?.delete('code-find-current');
  }

  function reveal() {
    const range = matches[current];
    if (!range) return;
    const rect = range.getBoundingClientRect();
    const box = scroller.getBoundingClientRect();
    if (rect.top < box.top || rect.bottom > box.bottom) {
      scroller.scrollTop += rect.top - box.top - scroller.clientHeight / 3;
    }
  }

  /** Пересчёт совпадений; `fresh` — новый запрос: первое совпадение и переход к нему. */
  function recount(fresh) {
    clearTimeout(typing);
    clearTimeout(changed);
    typing = changed = null;
    const query = fold(input.value.trim());
    ({ found: matches, more } = query ? collect(query) : { found: [], more: false });
    current = !matches.length ? -1 : fresh || current < 0 ? 0 : Math.min(current, matches.length - 1);
    paint();
    if (fresh) reveal();
  }

  function step(delta) {
    if (typing !== null) { recount(true); return; }     // запрос ещё не пересчитан: первое совпадение нового
    if (changed !== null) recount(false);                 // лента изменилась (идёт ход): позиция та же, шаг обычный
    if (!matches.length) return;
    current = (current + delta + matches.length) % matches.length;
    paint();
    reveal();
  }

  async function searchEarlier() {
    if (searching) { stop = true; return; }
    searching = true;
    stop = false;
    paint();
    const had = matches.length;
    try {
      while (alive && !stop && !bar.hidden && hasEarlier()) {
        const loaded = await earlier();
        if (!alive || bar.hidden) return;
        recount(false);
        if (matches.length > had) {      // новые совпадения — сверху ленты: ближайшее к прежнему тексту
          current = matches.length - had - 1;
          paint();
          reveal();
          break;
        }
        if (!loaded) break;
      }
    } finally {
      searching = false;
      if (alive) paint();
    }
  }

  const observer = new MutationObserver(() => {
    // пересчёт после ввода и так впереди; поток хода меняет ленту чаще TYPE_MS — таймер не перевзводится,
    // иначе пересчёт не наступил бы до конца хода
    if (bar.hidden || typing !== null || changed !== null) return;
    changed = setTimeout(() => recount(false), TYPE_MS);
  });

  function open() {
    if (bar.hidden) {
      bar.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      observer.observe(feed, { childList: true, subtree: true, characterData: true });
      if (input.value.trim()) recount(false);
      else paint();
    }
    input.focus();
    input.select();
  }

  function close() {
    if (bar.hidden) return;
    const inside = bar.contains(document.activeElement);
    bar.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    stop = true;
    observer.disconnect();
    clearTimeout(typing);
    clearTimeout(changed);
    typing = changed = null;
    matches = [];
    current = -1;
    clearHighlights();
    if (inside) scroller.focus({ preventScroll: true });
  }

  function onKey(event) {
    if (event.code !== 'KeyF' || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
    if (document.activeElement === input) return;              // второе нажатие — поиск браузера
    if (button.hidden || !host.isConnected || !(host.checkVisibility?.() ?? host.offsetParent !== null)) return;
    if (document.querySelector('dialog[open], :popover-open')) return;
    event.preventDefault();
    open();
  }

  input.addEventListener('input', () => {
    clearTimeout(typing);
    clearTimeout(changed);
    changed = null;
    typing = setTimeout(() => recount(true), TYPE_MS);
  });
  bar.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === 'Enter' && event.target === input && !event.isComposing) {
      event.preventDefault();
      step(event.shiftKey ? -1 : 1);
    }
  });
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  earlierButton.addEventListener('click', searchEarlier);
  closeButton.addEventListener('click', close);
  button.addEventListener('click', open);
  document.addEventListener('keydown', onKey);

  return {
    button, open, close,
    reset() {
      close();
      input.value = '';
    },
    unmount() {
      alive = false;
      close();
      document.removeEventListener('keydown', onKey);
      bar.remove();
    },
  };
}
