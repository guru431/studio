import { tr } from './i18n.js';
/* Отображение предоставленных CLI правок. Старое содержимое с диска не восстанавливаем. */
import { el } from './ui.js';

const MAX_LINES = 240;
const LANGUAGES = { py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript',
  json: 'json', css: 'css', html: 'xml', xml: 'xml', sh: 'bash', ps1: 'powershell', yaml: 'yaml', yml: 'yaml', md: 'markdown' };

export function patchFiles(patch, truncated = false) {
  const files = [];
  let current = null, old = null, inHunk = false;
  for (const line of linesOf(patch)) {
    if (line.startsWith('diff --git ')) { current = null; old = null; inHunk = false; }
    if (line.startsWith('@@ ')) inHunk = true;
    if (line.startsWith('--- ') && !inHunk) old = line.slice(4).replace(/^a\//, '');
    if (line.startsWith('+++ ') && !inHunk) {
      const path = line.slice(4).replace(/^b\//, '');
      current = { path: path === '/dev/null' ? old || tr("Удалённый файл") : path, patch: '', truncated };
      files.push(current);
    } else if (current) current.patch += line + '\n';
  }
  return files.length ? files : [{ path: tr("Изменения за ход"), patch, truncated }];
}

function codeCell(text, path) {
  const cell = el('span', { class: 'code-change-code' }, text);
  const language = LANGUAGES[path.split('.').at(-1)?.toLowerCase()];
  if (language && text.length <= 1000 && window.hljs?.getLanguage(language)) {
    cell.classList.add(`language-${language}`);
    window.hljs.highlightElement(cell);
  }
  return cell;
}

function numbered(rows) {
  let left = null, right = null;
  return rows.map(([kind, text]) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) { left = Number(hunk[1]); right = Number(hunk[2]); return { kind: 'header', text }; }
    if (/^(diff --git |index |\\ No newline)/.test(text)
      || (left === null && /^(--- |\+\+\+ )/.test(text))) return { kind: 'header', text };
    if (kind === 'context' && !text.startsWith(' ')) return { kind: 'header', text };
    const row = { kind, text, left: kind !== 'added' ? left : null, right: kind !== 'removed' ? right : null };
    if (left !== null && kind !== 'added') left++;
    if (right !== null && kind !== 'removed') right++;
    return row;
  });
}

function paired(rows) {
  const pairs = [];
  for (let i = 0; i < rows.length;) {
    const row = rows[i];
    if (row.kind === 'removed' || row.kind === 'added') {
      const left = [], right = [];
      while (rows[i]?.kind === 'removed') left.push(rows[i++]);
      while (rows[i]?.kind === 'added') right.push(rows[i++]);
      for (let j = 0; j < Math.max(left.length, right.length); j++) pairs.push([left[j], right[j]]);
    } else { pairs.push([row, row]); i++; }
  }
  return pairs;
}

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
      let inHunk = false;
      rows = linesOf(change.patch).flatMap((text) => {
        if (text.startsWith('@@ ')) inHunk = true;
        if (text.startsWith('diff --git ')) inHunk = false;
        if (!inHunk && /^(diff --git |index |--- |\+\+\+ )/.test(text)) return [];
        return [[inHunk && text.startsWith('+') ? 'added' : inHunk && text.startsWith('-') ? 'removed' : 'context', text]];
      });
      caption = tr("Изменения");
    } else if (typeof change.before === 'string' && typeof change.after === 'string') {
      rows = comparison(change.before, change.after);
      caption = tr("Изменённый фрагмент");
    } else if (typeof change.after === 'string') {
      rows = linesOf(change.after).map((text) => ['context', text]);
      caption = tr("Новое содержимое");
    } else continue;
    const added = rows.filter(([kind]) => kind === 'added').length;
    const removed = rows.filter(([kind]) => kind === 'removed').length;
    const counts = added || removed ? ` · +${added} −${removed}` : '';
    const partial = change.truncated || rows.length > MAX_LINES;
    const shown = numbered(rows.slice(0, MAX_LINES));
    const patch = typeof change.patch === 'string';
    const content = (row) => row ? row.kind === 'header' ? el('span', { class: 'code-change-code' }, row.text)
      : codeCell(patch ? row.text.slice(1) : row.text, change.path) : el('span', {});
    blocks.push(el('div', { class: 'code-change' },
      el('div', { class: 'code-change-title' }, `${change.path} · ${caption}${counts}${partial ? tr(" · показана часть") : ''}`),
      el('div', { class: 'code-change-lines', tabindex: '0', role: 'region', 'aria-label': `${caption}: ${change.path}` },
        el('div', { class: 'code-diff-unified' }, ...shown.map((row) => el('div', { class: `code-change-line ${row.kind}` },
          el('span', { class: 'code-change-number', 'aria-hidden': 'true' }, row.left ?? row.right ?? ''),
          el('span', { class: 'code-change-marker', 'aria-hidden': 'true' }, row.kind === 'added' ? '+' : row.kind === 'removed' ? '−' : ' '), content(row)))),
        el('div', { class: 'code-diff-split', 'aria-label': tr("Слева до изменения, справа после") }, ...paired(shown).map(([left, right]) =>
          el('div', { class: 'code-diff-pair' }, ...[left, right].map((row, index) => el('div', {
            class: `code-change-line ${row?.kind || 'empty'}` },
          el('span', { class: 'code-change-number', 'aria-hidden': 'true' }, (index ? row?.right : row?.left) ?? ''), content(row)))))),
        ...(partial ? [el('span', { class: 'muted' }, tr("… показана часть изменения"))] : []))));
  }
  if (changes.length > 8) blocks.push(el('p', { class: 'muted small code-change-title' }, tr("Показаны первые 8 из {0} файлов", changes.length)));
  return blocks.length ? el('div', { class: 'code-changes' }, ...blocks) : null;
}
