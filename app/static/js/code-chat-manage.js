import { tr } from './i18n.js';
/* «Переименовать» и «Удалить сессию» «Агентов» (мост 0.47.0, `hello.chat_manage`; только владельцу компьютера).
 * Название хранит помощник: Claude Code и Codex; OpenCode и DeepSeek Harness своё название и удаление не дают.
 * Удалить можно только сессию, начатую в Студии (`origin: studio`), и «Новый чат» без хода (`empty`) любого помощника.
 *
 * renameChat(host, { url, title, request, done(title) }) — окно с полем: PUT <url>/title, отказ — словами в окне;
 * deleteChat(host, { url, engine, title, request, done() }) — подтверждение: DELETE <url>, отказ — словами в окне.
 * url — адрес чата без `?engine=`, engine — помощник (его добавляет сюда). */
import { api } from './api.js';
import { ENGINE_NAMES } from './code-options.js';
import { button, el, field, openDialog, words } from './ui.js';

const NAMED = new Set(['claude', 'codex']);
const TITLE_MAX = 200;           // claude_code.TITLE_MAX

export const canRename = (chat) => NAMED.has(chat.engine || 'claude') && !chat.empty;
export const canDelete = (chat) => chat.origin === 'studio' && (NAMED.has(chat.engine || 'claude') || !!chat.empty);

const query = (engine) => (engine && engine !== 'claude' ? `?engine=${engine}` : '');

export function renameChat(host, { url, engine = 'claude', title = '', request = api, done }) {
  const input = el('input', { class: 'input', name: 'title', maxlength: String(TITLE_MAX), autocomplete: 'off',
    value: title || '' });
  const fail = el('p', { class: 'error', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'code-rename-save' });
  const dialog = openDialog(host, { id: 'code-rename-dialog', title: tr("Переименовать сессию"),
    body: [field(tr("Название"), input, 'title'),
      el('p', { class: 'small muted' }, tr("Название сохранится в {0} и будет видно и в VS Code или терминале.",
        ENGINE_NAMES[engine])), fail], actions: [save] });
  const submit = async () => {
    const value = input.value.trim();
    if (save.disabled || !value) return;
    save.disabled = true;
    try {
      await request('PUT', `${url}/title${query(engine)}`, { title: value });
      dialog.close();
      done?.(value);
    } catch (err) {
      if (!dialog.isConnected) return;
      fail.textContent = words(err);
      save.disabled = false;
    }
  };
  save.addEventListener('click', submit);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); submit(); }
  });
  input.focus();
  input.select();
  return dialog;
}

export function deleteChat(host, { url, engine = 'claude', title = '', request = api, done }) {
  const yes = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'code-delete-confirm' });
  const fail = el('p', { class: 'error', 'aria-live': 'polite' });
  const dialog = openDialog(host, { id: 'code-delete-dialog', title: tr("Удалить сессию?"),
    body: [el('p', {}, engine === 'codex'
      ? tr("«{0}» исчезнет из списка, а Codex перенесёт её в свой архив.", title || tr("Новый чат"))
      : tr("«{0}» будет удалена с компьютера. Восстановить её нельзя.", title || tr("Новый чат"))), fail],
    actions: [yes] });
  yes.addEventListener('click', async () => {
    yes.disabled = true;
    try {
      await request('DELETE', `${url}${query(engine)}`);
      dialog.close();
      done?.();
    } catch (err) {
      if (!dialog.isConnected) return;
      fail.textContent = words(err);
      yes.disabled = false;
    }
  });
  return dialog;
}
