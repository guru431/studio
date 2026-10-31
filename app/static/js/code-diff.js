/* Отображение предоставленных CLI правок. Старое содержимое с диска не восстанавливаем. */
import { el } from './ui.js';

const MAX_LINES = 240;

function linesOf(value) {
  const lines = String(value).split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines;
}

function comparison(before, after) {
  const left = linesOf(before), right = linesOf(after);
  let head = 0, tail = 0;
  while (head < left.length && head < right.length && left[head] === right[head]) head++;
  while (tail < left.length - head && tail < right.length - head
    && left[left.length - tail - 1] === right[right.length - tail - 1]) tail++;
  const old = left.slice(head, left.length - tail), next = right.slice(head, right.length - tail);
  let middle;
  if (old.length * next.length <= 60000) {
    const table = Array.from({ length: old.length + 1 }, () => new Uint16Array(next.length + 1));
    for (let i = old.length - 1; i >= 0; i--) for (let j = next.length - 1; j >= 0; j--) {
      table[i][j] = old[i] === next[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
    middle = [];
    let i = 0, j = 0;
    while (i < old.length || j < next.length) {
      if (i < old.length && j < next.length && old[i] === next[j]) middle.push(['context', old[i++]]), j++;
      else if (i < old.length && (j === next.length || table[i + 1][j] >= table[i][j + 1])) middle.push(['removed', old[i++]]);
      else middle.push(['added', next[j++]]);
    }
  } else {
    // Большие фрагменты показываем целиком заменёнными; квадратичную таблицу не строим.
    middle = [...old.map((text) => ['removed', text]), ...next.map((text) => ['added', text])];
  }
  return [
    ...left.slice(Math.max(0, head - 3), head).map((text) => ['context', text]),
    ...middle,
    ...right.slice(right.length - tail, right.length - tail + 3).map((text) => ['context', text]),
  ];
}

export function changeView(changes) {
  if (!Array.isArray(changes) || !changes.length) return null;
  const blocks = [];
  for (const change of changes.slice(0, 8)) {
    if (!change || typeof change.path !== 'string') continue;
    let rows, caption;
    if (typeof change.patch === 'string') {
      rows = linesOf(change.patch).map((text) => [text.startsWith('+') && !text.startsWith('+++') ? 'added'
        : text.startsWith('-') && !text.startsWith('---') ? 'removed' : 'context', text]);
      caption = 'Изменения';
    } else if (typeof change.before === 'string' && typeof change.after === 'string') {
      rows = comparison(change.before, change.after);
      caption = 'Изменённый фрагмент';
    } else if (typeof change.after === 'string') {
      rows = linesOf(change.after).map((text) => ['context', text]);
      caption = 'Новое содержимое';
    } else continue;
    const added = rows.filter(([kind]) => kind === 'added').length;
    const removed = rows.filter(([kind]) => kind === 'removed').length;
    const counts = added || removed ? ` · +${added} −${removed}` : '';
    const partial = change.truncated || rows.length > MAX_LINES;
    blocks.push(el('div', { class: 'code-change' },
      el('div', { class: 'code-change-title' }, `${change.path} · ${caption}${counts}${partial ? ' · показана часть' : ''}`),
      el('pre', { class: 'code-change-lines', tabindex: '0', 'aria-label': `${caption}: ${change.path}` },
        ...rows.slice(0, MAX_LINES).map(([kind, text]) => el('span', { class: `code-change-line ${kind}` },
          el('span', { class: 'code-change-marker', 'aria-hidden': 'true' },
            typeof change.patch === 'string' ? '' : kind === 'added' ? '+' : kind === 'removed' ? '−' : ' '), text)),
        ...(partial ? [el('span', { class: 'muted' }, '… показана часть изменения')] : []))));
  }
  return blocks.length ? el('div', { class: 'code-changes' }, ...blocks) : null;
}
