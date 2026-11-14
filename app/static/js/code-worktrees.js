import { tr } from './i18n.js';
/* Git-worktree создаётся из HEAD и открывается как отдельный проект разрешённого корня. */
import { api } from './api.js';
import { el, words } from './ui.js';

export function worktreeView(host, { aid, pid, opened }) {
  const base = `/api/code/agents/${aid}/projects/${pid}/worktrees`;
  const button = el('button', { type: 'button', id: 'code-worktrees', class: 'btn btn-sm' }, tr("Рабочие копии"));
  const name = el('input', { id: 'code-worktree-name', class: 'input', required: true, maxlength: '31',
    pattern: '[a-z0-9](?:[a-z0-9]|-){0,30}', placeholder: 'task-name', 'aria-label': tr("Имя рабочей копии"), autocomplete: 'off' });
  const create = el('button', { type: 'submit', class: 'btn btn-accent' }, tr("Создать"));
  const form = el('form', { class: 'code-worktree-form' }, name, create);
  const list = el('div', { class: 'code-worktree-list' });
  const status = el('p', { class: 'muted small', role: 'status' });
  const close = el('button', { type: 'button', class: 'btn' }, tr("Закрыть"));
  const dialog = el('dialog', { id: 'code-worktrees-dialog', class: 'dialog', 'aria-labelledby': 'code-worktrees-title' },
    el('h2', { id: 'code-worktrees-title' }, tr("Рабочие копии Git")),
    el('p', { class: 'muted small' }, tr("Новая копия создаётся из последнего коммита в отдельной ветке. Текущие незакоммиченные правки остаются в исходном проекте. Настройки и зависимости новой копии могут требовать подготовки.")),
    list, form, status, el('div', { class: 'dialog-actions' }, close));
  host.append(button, dialog);
  let alive = true, request = null;

  async function open(row) {
    if (!/^[0-9a-f]{16}$/.test(row.pid)) return;
    await opened?.(row);
    if (!alive) return;
    dialog.close();
    location.replace(`#/code/${aid}/${row.pid}?tab=new`);
  }

  async function load() {
    request?.abort();
    const own = request = new AbortController();
    status.textContent = tr("Читаю рабочие копии…");
    try {
      const data = await api('GET', base, undefined, { signal: own.signal });
      if (!alive || own.signal.aborted) return;
      status.textContent = '';
      list.replaceChildren(...(data.items || []).map((row) => {
        const entry = el('button', { type: 'button', class: 'btn code-worktree-row', disabled: !!row.current },
          `${row.name} · ${row.branch || tr("без ветки")}${row.current ? tr(" · текущая") : ''}`);
        entry.addEventListener('click', () => open(row));
        return entry;
      }));
    } catch (err) {
      if (alive && !own.signal.aborted) status.textContent = words(err);
    }
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity() || create.disabled) return;
    create.disabled = true;
    status.textContent = tr("Создаю рабочую копию…");
    try {
      const data = await api('POST', base, { name: name.value });
      if (alive) await open(data);
    } catch (err) {
      if (alive) status.textContent = words(err);
    } finally {
      create.disabled = false;
    }
  });
  button.addEventListener('click', () => { dialog.showModal(); load(); });
  close.addEventListener('click', () => dialog.close());
  return { unmount() { alive = false; request?.abort(); button.remove(); dialog.remove(); } };
}
