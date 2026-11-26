import { tr } from './i18n.js';
import { api } from './api.js';
import { draftStore, OWUI_DRAFTS, owuiDraftKey } from './dialog-drafts.js';
import { button, el, field, openDialog, words } from './ui.js';

const path = (aid, chat) => `/api/owui/accounts/${aid}/chats/${encodeURIComponent(chat)}`;

export function chatManagement(host, { changed, removed }) {
  return {
    async pin(aid, item) {
      const out = await api('PUT', `${path(aid, item.id)}/pin`, { pinned: !item.pinned });
      changed(aid, { id: item.id, pinned: out.pinned });
    },
    rename(aid, item) {
      const input = el('input', { class: 'input', name: 'title', maxlength: '200', value: item.title || '' });
      const fail = el('p', { class: 'error', 'aria-live': 'polite' });
      const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'owui-chat-rename-save' });
      const dialog = openDialog(host, { id: 'owui-chat-rename-dialog', title: tr("Переименовать диалог"),
        body: [field(tr("Название"), input, 'title'), fail], actions: [save] });

      async function submit() {
        const title = input.value.trim();
        if (!title || save.disabled) return;
        save.disabled = true;
        try {
          const out = await api('PATCH', path(aid, item.id), { title });
          dialog.close();
          changed(aid, { id: out.id, title: out.title, updated: out.updated });
        } catch (err) {
          fail.textContent = words(err);
          save.disabled = false;
        }
      }
      save.addEventListener('click', submit);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); submit(); }
      });
      input.focus();
      input.select();
    },
    remove(aid, item) {
      const fail = el('p', { class: 'error', 'aria-live': 'polite' });
      const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'owui-chat-delete-confirm' });
      const dialog = openDialog(host, { id: 'owui-chat-delete-dialog', title: tr("Удалить диалог?"),
        body: [el('p', {}, tr("«{0}» будет удалён из Open WebUI без возможности восстановления. Ссылка Студии перестанет действовать.",
          item.title || tr("Без названия"))), fail], actions: [confirm] });
      confirm.addEventListener('click', async () => {
        confirm.disabled = true;
        try {
          await api('DELETE', path(aid, item.id));
          draftStore(OWUI_DRAFTS).save(owuiDraftKey(aid, item.id), '');
          dialog.close();
          removed(aid, item.id);
        } catch (err) {
          fail.textContent = words(err);
          confirm.disabled = false;
        }
      });
    },
  };
}
