import { tr } from './i18n.js';
/* Название диалога в шапке «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 17, 41): вместо слова
 * «Чат» — название открытого диалога, рядом «Действия с диалогом»: «Переименовать» и «Удалить» — те же окна, что в
 * меню списка (`rename`, `remove` получают строку диалога), «Скопировать адрес» — адрес чата в буфер: успех —
 * видимая отметка `note` рядом с кнопкой, отказ — слова через `said` (видимая строка ошибки раздела). */
import { el } from './ui.js';
import { iconButton } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { NAMES } from './sections-meta.js';

const COPIED_MS = 1500;          // как у «Копировать» ответа (chat.js)

export function chatTitle({ rename, remove, said }) {
  const node = el('h1', { id: 'chat-title', class: 'chat-title' }, NAMES.chat);
  const actions = iconButton('more', tr("Действия с диалогом"), { id: 'chat-title-actions', class: 'icon-btn chat-title-actions',
    hidden: true });
  const menu = el('div', { id: 'chat-title-menu', class: 'chat-menu', role: 'menu', 'aria-label': tr("Действия с диалогом") });
  // успех копирования — короткой видимой отметкой рядом с кнопкой, как «Копировать» → «Скопировано»
  const note = el('span', { id: 'chat-title-note', class: 'chat-title-note muted small', role: 'status', hidden: true });
  let noteTimer = null;
  let current = null;
  const popup = popupMenu(actions, menu, { align: 'end', fill: (close) => {
    const item = current;
    const items = [
      menuItem(tr("Переименовать"), { attrs: { id: 'chat-title-rename' }, onSelect: () => { close(); rename(item); } }),
      menuItem(tr("Удалить"), { attrs: { id: 'chat-title-delete', class: 'danger' }, onSelect: () => { close(); remove(item); } }),
      menuItem(tr("Скопировать адрес"), { attrs: { id: 'chat-title-copy' }, onSelect: async () => {
        close();
        try {
          await navigator.clipboard.writeText(`${location.origin}/#/chat/${item.id}`);
        } catch {
          said(tr("Не удалось скопировать адрес"));      // отказ — видимой строкой раздела (#chat-error)
          return;
        }
        clearTimeout(noteTimer);
        note.textContent = tr("Адрес скопирован");
        note.hidden = false;
        noteTimer = setTimeout(() => { note.hidden = true; note.textContent = ''; }, COPIED_MS);
      } })];
    return [items, items];
  } });
  return {
    node, actions, menu, note,
    /** Строка открытого диалога (`chat.chat`) или null — новый диалог. */
    set(chat) {
      current = chat;
      const name = chat ? chat.title || tr("Новый диалог") : NAMES.chat;
      if (node.textContent !== name) node.textContent = name;
      if (chat) node.title = name;
      else node.removeAttribute('title');
      actions.hidden = !chat;
      if (!chat) popup.close();
    },
  };
}
