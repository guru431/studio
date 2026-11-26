import { locale, message, tr } from './i18n.js';
/* Части ответа (инструмент `run_part`, спека 2026-11-25-studio-skills-status-local-models, п. 7): шаг под ответом и
 * кнопка `#chat-parts` в строке состояния «Чата».
 *
 * Шаг части с сервера — `{kind: "part", n, title, state: running|done|error, ms, text}` (у ошибки — `error`); отказ до
 * запуска — без `n` и `ms`; шаги инструментов внутри части — обычные шаги с ключом `part`. Идущий ответ этой вкладки
 * знает части по событиям SSE `part` (`noteParts`), до перечитывания — без текста итога.
 * partNode(step, { ended }) — `li.web-step.web-part`: «Часть N: название · идёт / готово / ошибка · м:сс» и раскрываемый
 * итог; шаг `running` у кончившегося ответа — «прервана».
 * partsButton({ root }) → { node, paint(message) } — «Части: готово из запущенных» последнего ответа, скрыта без частей;
 * нажатие — окно со списком частей. */
import { icon } from './icons.js';
import { el, openDialog } from './ui.js';

const STATES = { running: () => tr("идёт"), done: () => tr("Готово").toLocaleLowerCase(locale()), error: () => tr("ошибка") };

function clock(ms) {
  const s = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function partNode(step, { ended = false } = {}) {
  // вызов из события `tool` идущего ответа (`{kind, label}`, до события `part`) — идущая часть с названием из `label`
  const going = step.state || 'running';
  const state = going === 'running' && ended ? 'cut' : going;
  const word = state === 'cut' ? tr("прервана") : (STATES[state] || STATES.error)();
  const head = [
    el('span', { class: 'part-n' }, `${Number.isInteger(step.n) ? tr("Часть {0}", step.n) : tr("Часть")}: `),
    el('span', { class: 'part-title' }, step.title || step.label || ''),
    ' · ', el('span', { class: 'part-state' }, word),
    Number.isFinite(step.ms) ? ` · ${clock(step.ms)}` : null,
  ];
  const error = step.error ? el('span', { class: 'error' }, ` — ${message(step.error)}`) : null;
  const li = el('li', { class: 'web-step web-part', 'data-state': state, 'data-part': step.n ?? null });
  if (step.text) li.append(el('details', { class: 'part-result' }, el('summary', {}, ...head, error),
    el('div', { class: 'part-text' }, step.text)));
  else li.append(...head, error);
  return li;
}

/** Событие SSE `part` идущего ответа: `{n, title, state}` при запуске, `{n, title, state, ms}` при итоге. */
export function noteParts(m, data) {
  if (!m || !data || !Number.isInteger(data.n)) return;
  m.liveParts ||= new Map();
  m.liveParts.set(data.n, { ...(m.liveParts.get(data.n) || {}), kind: 'part', ...data });
  // вызов этой части в `calls` (окно «Шаги текущего ответа»): последний `part` без номера или с тем же номером
  const call = (m.calls || []).findLast((c) => c.kind === 'part' && (c.n === data.n || (c.n == null && c.label === data.title)));
  if (call) Object.assign(call, { n: data.n, state: data.state, ...(Number.isFinite(data.ms) ? { ms: data.ms } : {}) });
}

/** Запущенные части ответа: шаги с сервера, а до перечитывания — события этой вкладки. */
export function partsOf(m) {
  const saved = (Array.isArray(m?.tools) ? m.tools : [])
    .filter((s) => s?.kind === 'part' && Number.isInteger(s.n) && !('part' in s));
  return saved.length ? saved : [...(m?.liveParts?.values() || [])];
}

export function partsButton({ root }) {
  const count = el('span', { class: 'chat-parts-count', 'aria-hidden': 'true' });
  const node = el('button', { type: 'button', id: 'chat-parts', class: 'icon-btn', 'aria-haspopup': 'dialog', hidden: true },
    icon('subagents'), count);
  let shown = null;            // ответ, чьи части считает кнопка
  let dialog = null;
  let drawn = '';
  const body = el('ul', { class: 'web-list chat-parts-list' });

  function paintDialog(parts, ended) {
    const value = JSON.stringify([parts, ended]);
    if (drawn === value) return;
    drawn = value;
    const expanded = new Set([...body.querySelectorAll('li:has(details[open])')].map((li) => Number(li.dataset.part)));
    body.replaceChildren(...parts.map((s) => {
      const item = partNode(s, { ended });
      const detail = item.querySelector('details');
      if (detail && expanded.has(s.n)) detail.open = true;
      return item;
    }));
  }

  node.addEventListener('click', () => {
    if (!shown || dialog?.open) return;
    drawn = '';
    body.replaceChildren();
    paintDialog(partsOf(shown), shown.status !== 'streaming');
    dialog = openDialog(root, { id: 'chat-parts-dialog', title: tr("Части ответа"), cancelText: tr("Закрыть"), body: [body] });
  });

  return {
    node,
    paint(m) {
      const list = partsOf(m);
      if (dialog?.open && (!list.length || shown?.id !== m?.id)) dialog.close();
      if (dialog?.open) paintDialog(list, m.status !== 'streaming');
      shown = list.length ? m : null;
      node.hidden = !list.length;
      if (!list.length) return;
      const done = list.filter((s) => s.state === 'done').length;
      const words = tr("{0} из {1}", done, list.length);
      if (count.textContent !== words) count.textContent = words;
      const label = tr("Части: {0} из {1}", done, list.length);
      if (node.getAttribute('aria-label') !== label) {
        node.setAttribute('aria-label', label);
        node.title = label;
      }
    },
  };
}
