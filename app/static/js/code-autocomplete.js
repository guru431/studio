import { tr } from './i18n.js';
/* Подсказки относительных путей: только память страницы, запрос всегда в POST body. */
import { api } from './api.js';
import { el } from './ui.js';

export function fileHints(text, host, { aid, pid, failed }) {
  const box = el('div', { id: 'code-file-hints', class: 'code-file-hints', hidden: true });
  const list = el('div', { role: 'listbox', id: 'code-file-options', 'aria-label': tr("Файлы проекта") });
  const status = el('div', { class: 'small muted', role: 'status' });
  box.append(list, status);
  host.append(box);
  text.setAttribute('aria-controls', list.id);
  let alive = true, timer = null, request = null, rows = [], selected = 0, token = null, generation = 0;

  function hide() {
    clearTimeout(timer);
    request?.abort();
    generation++;
    box.hidden = true;
    host.hidden = true;
    text.setAttribute('aria-expanded', 'false');
    text.removeAttribute('aria-activedescendant');
    rows = [];
  }

  function paint() {
    list.replaceChildren(...rows.map((path, index) => {
      const button = el('button', { type: 'button', role: 'option', id: `code-file-option-${index}`,
        'aria-selected': String(index === selected), class: 'code-file-option', title: path }, path);
      button.addEventListener('mousedown', (event) => event.preventDefault());
      button.addEventListener('click', () => choose(index));
      return button;
    }));
    if (rows.length) text.setAttribute('aria-activedescendant', `code-file-option-${selected}`);
  }

  function choose(index) {
    const path = rows[index];
    if (!path || !token) return;
    const value = /\s/.test(path) ? JSON.stringify(path) : path;
    text.setRangeText(`${value} `, token.start, token.end, 'end');
    hide();
    text.focus();
    text.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function input() {
    hide();
    if (text.selectionStart !== text.selectionEnd) return;
    const caret = text.selectionStart;
    const match = text.value.slice(0, caret).match(/(?:^|\s)@([^\s@]{0,200})$/);
    if (!match) return;
    token = { start: caret - match[1].length - 1, end: caret };
    const mine = generation;
    timer = setTimeout(async () => {
      const own = new AbortController();
      request = own;
      try {
        const data = await api('POST', `/api/code/agents/${aid}/projects/${pid}/files/suggest`,
          { query: match[1] }, { signal: own.signal });
        if (!alive || own.signal.aborted || mine !== generation) return;
        rows = Array.isArray(data.items) ? data.items.filter((path) => typeof path === 'string') : [];
        selected = 0;
        box.hidden = false;
        host.hidden = false;
        text.setAttribute('aria-expanded', 'true');
        status.textContent = !rows.length ? tr("Файлы не найдены") : data.complete === false
          ? tr("Обход ограничен — показана часть файлов") : data.more ? tr("Уточните имя для остальных файлов") : tr("Enter — выбрать · Esc — закрыть");
        paint();
      } catch (err) {
        if (alive && !own.signal.aborted && mine === generation) {
          hide();
          failed(err);
        }
      }
    }, 180);
  }

  function keydown(event) {
    if (box.hidden || event.isComposing) return;
    if (event.key === 'Escape' || (rows.length && ['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key))) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === 'Escape') hide();
      else if (event.key === 'Enter' || event.key === 'Tab') choose(selected);
      else {
        selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
        paint();
        list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
      }
    }
  }
  function click() { input(); }
  text.addEventListener('input', input);
  text.addEventListener('click', click);
  text.addEventListener('keydown', keydown);
  return { unmount() {
    alive = false;
    hide();
    text.removeEventListener('input', input);
    text.removeEventListener('click', click);
    text.removeEventListener('keydown', keydown);
    box.remove();
  } };
}
