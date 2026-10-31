/* Настройки быстрых действий учётки; тексты передаются без обрезки и переформатирования. */
import { api, me } from './api.js';
import { button, el, field, words } from './ui.js';

export function quickActionsBlock() {
  const rows = el('div', { id: 'code-quick-action-rows' });
  const note = el('p', { id: 'code-quick-actions-status', class: 'status', 'aria-live': 'polite' });
  const add = button('Добавить действие', { id: 'code-quick-actions-add', class: 'btn' }, () => {
    const row = addRow({ title: '', text: '' });
    row.querySelector('input').focus();
  });
  const save = el('button', { id: 'code-quick-actions-save', class: 'btn btn-accent', type: 'submit' }, 'Сохранить');
  const form = el('form', {}, rows, el('div', { class: 'row' }, add, save), note);
  const block = el('section', { class: 'block code-quick-actions-settings' }, el('h2', {}, 'Быстрые действия'),
    el('p', { class: 'muted small' }, 'Показываются в новом чате до первого сообщения. '
      + 'Нажатие сразу отправляет полный текст задания. Действия общие для ваших компьютеров и помощников.'), form);

  function addRow(action) {
    const title = el('input', { class: 'input', type: 'text', maxlength: '80', required: true,
      pattern: '.*\\S.*', 'aria-label': 'Заголовок действия', value: action.title });
    const text = el('textarea', { class: 'input', rows: '5', maxlength: '100000', required: true,
      'aria-label': 'Текст действия' });
    text.value = action.text;
    const remove = button('Удалить', { class: 'btn btn-sm', 'aria-label': 'Удалить действие' }, () => {
      row.remove();
      add.disabled = rows.children.length >= 20;
    });
    const row = el('div', { class: 'block code-quick-action-row' },
      field('Заголовок', title), field('Текст сообщения', text), remove);
    rows.append(row);
    add.disabled = rows.children.length >= 20;
    return row;
  }

  for (const action of me?.code_quick_actions || []) addRow(action);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code_quick_actions = [...rows.children].map((row) => ({
      title: row.querySelector('input').value, text: row.querySelector('textarea').value,
    }));
    if (code_quick_actions.some((a) => !a.title.trim() || !a.text.trim())) {
      note.className = 'status error';
      note.textContent = 'Заполните заголовок и текст каждого действия';
      return;
    }
    const controls = [...form.querySelectorAll('input, textarea, button')];
    for (const control of controls) control.disabled = true;
    try {
      const answer = await api('PATCH', '/api/me', { code_quick_actions });
      me.code_quick_actions = answer.code_quick_actions;
      note.className = 'status ok';
      note.textContent = 'Быстрые действия сохранены';
    } catch (err) {
      note.className = 'status error';
      note.textContent = words(err);
    } finally {
      for (const control of controls) control.disabled = false;
      add.disabled = rows.children.length >= 20;
    }
  });
  return block;
}
