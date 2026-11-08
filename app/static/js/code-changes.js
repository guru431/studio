/* Правки последнего ответа помощника. Только предоставленные CLI данные, без чтения диска. */
import { el, openDialog } from './ui.js';
import { changeView } from './code-diff.js';

export function changesView(host, { openFile }) {
  const count = el('span', { class: 'code-changed-count', 'aria-hidden': 'true' }, '0');
  const button = el('button', { type: 'button', id: 'code-changed-files', class: 'icon-btn code-changed-files',
    'aria-haspopup': 'dialog', 'aria-expanded': 'false' });
  button.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8m-8 4h5"/></svg>';
  button.append(count);
  let state = { files: [], changes: [] }, serial = '', dialog = null, selected = null;
  const body = el('div', { class: 'code-changed-body' });

  function render(focus = false) {
    const row = state.files.find((file) => file.path === selected);
    if (!row) {
      selected = null;
      const list = el('div', { class: 'code-changed-list' });
      for (const file of state.files) {
        const entry = el('button', { type: 'button', class: 'btn code-changed-file' }, file.path,
          Number.isInteger(file.added) && Number.isInteger(file.removed) ? ` · +${file.added} −${file.removed}` : '');
        entry.addEventListener('click', () => { selected = file.path; render(true); });
        list.append(entry);
      }
      body.replaceChildren(el('p', { class: 'muted small' }, 'Правки последнего ответа помощника'), list);
      if (!state.files.length) list.append(el('p', { class: 'muted' }, 'Изменённых файлов нет'));
      if (state.truncated) list.append(el('p', { class: 'muted small' }, 'Показана часть изменений'));
      if (focus) list.querySelector('button')?.focus();
      return;
    }
    const back = el('button', { type: 'button', class: 'btn btn-sm code-changed-back' }, 'К списку файлов');
    back.addEventListener('click', () => { selected = null; render(true); });
    const full = el('button', { type: 'button', class: 'btn btn-sm code-changed-open' }, 'Открыть файл');
    full.addEventListener('click', () => { dialog.close(); openFile(row.path); });
    const diff = changeView(state.changes.filter((change) => change.path === row.path));
    body.replaceChildren(el('div', { class: 'code-changed-actions' }, back, full),
      el('h3', { class: 'code-changed-path' }, row.path),
      diff || el('p', { class: 'muted' }, 'Помощник не передал изменения этого файла'));
    if (focus) back.focus();
  }

  function update(next = { files: [], changes: [] }) {
    const value = JSON.stringify(next);
    if (serial === value) return;
    serial = value;
    state = next;
    count.textContent = `${state.truncated ? '≥' : ''}${state.files.length}`;
    const label = `Изменённые файлы последнего ответа: ${count.textContent}`;
    button.setAttribute('aria-label', label);
    button.title = label;
    if (dialog?.open) render();
  }

  button.addEventListener('click', () => {
    selected = null;
    render();
    dialog = openDialog(host, { id: 'code-changed-dialog', title: 'Изменённые файлы', body: [body], cancelText: 'Закрыть' });
    dialog.classList.add('code-changed-dialog');
    button.setAttribute('aria-expanded', 'true');
    dialog.addEventListener('close', () => { dialog = null; button.setAttribute('aria-expanded', 'false'); });
  });
  update();
  return { button, update, unmount() { dialog?.close(); dialog?.remove(); body.replaceChildren(); } };
}
