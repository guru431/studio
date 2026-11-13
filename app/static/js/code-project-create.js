/* Пустая папка проекта создаётся мостом в одном из настроенных на компьютере корней. */
import { api } from './api.js';
import { el, words } from './ui.js';

export function projectCreate(host, agent, { created, closed, trigger }) {
  const name = el('input', { id: 'code-project-name', class: 'input', required: true, maxlength: '120',
    autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
  const folder = el('select', { id: 'code-project-root', class: 'input', required: true, disabled: true });
  const status = el('p', { class: 'muted', 'aria-live': 'polite' }, 'Загрузка папок…');
  const error = el('p', { class: 'error', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'btn btn-accent', disabled: true }, 'Создать');
  // showModal фокусирует кнопку: телефон не открывает клавиатуру до нажатия на поле.
  const cancel = el('button', { type: 'button', class: 'btn dialog-close', autofocus: true }, 'Отмена');
  const form = el('form', {},
    el('label', { class: 'field', for: name.id }, 'Название проекта', name),
    el('label', { class: 'field', for: folder.id }, 'Папка проектов', folder), status, error,
    el('div', { class: 'actions' }, cancel, submit));
  const dialog = el('dialog', { id: 'code-project-create-dialog', class: 'dialog code-project-create',
    'aria-labelledby': 'code-project-create-title' },
    el('h2', { id: 'code-project-create-title' }, 'Новый проект'),
    el('p', { class: 'muted' }, `Компьютер: ${agent.name}`), form);
  const base = `/api/code/agents/${agent.id}`;
  let busy = false;
  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') event.stopPropagation();
  });
  dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    dialog.remove();
    if (trigger.isConnected) trigger.focus({ preventScroll: true });
  });
  host.append(dialog);
  dialog.showModal();

  function refused(err) {
    if (!dialog.open) return;
    if (err.code === 'closed') { dialog.close(); closed(); }
    else error.textContent = words(err);
  }
  async function load() {
    try {
      const { items } = await api('GET', `${base}/project-roots`);
      if (!dialog.open) return;
      folder.replaceChildren(...(items.length > 1 ? [el('option', { value: '' }, 'Выберите папку')] : []),
        ...items.map((item) => el('option', { value: item.id }, item.path)));
      folder.disabled = items.length <= 1;
      submit.disabled = !items.length;
      status.textContent = items.length ? 'Будет создана новая пустая папка с указанным названием.'
        : 'На компьютере не настроены папки проектов. Добавьте папку в настройках моста.';
      if (matchMedia('(hover: hover) and (pointer: fine)').matches && document.activeElement === cancel) name.focus();
    } catch (err) { if (dialog.open) status.textContent = ''; refused(err); }
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || submit.disabled || !form.reportValidity()) return;
    const value = name.value;
    if (!value.trim() || value.startsWith('.') || /[\\/:*?"<>|\u0000-\u001f\u007f]|[. ]$/.test(value)) {
      error.textContent = 'Укажите имя папки без разделителей пути и служебных знаков.';
      name.focus();
      return;
    }
    busy = true;
    error.textContent = '';
    submit.disabled = cancel.disabled = name.disabled = folder.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const row = await api('POST', `${base}/projects`, { root: folder.value, name: value });
      if (!dialog.open) return;
      dialog.close();
      await created(row);
    } catch (err) { refused(err); }
    finally {
      busy = false;
      submit.disabled = cancel.disabled = name.disabled = false;
      folder.disabled = folder.options.length <= 1;
      form.setAttribute('aria-busy', 'false');
    }
  });
  load();
  return { close() { dialog.close(); dialog.remove(); } };
}
